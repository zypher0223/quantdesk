"""External research and analytics endpoints.

Three surfaces, all read-only with respect to trading:

* **status** - is OpenBB / Fincept configured, which provider answers what, how the
  calls have gone, and what the licence position is. Nothing here prints a key.
* **evidence** - the standardised external readings behind a research run, with
  their providers, publish times, freshness and the reasons anything is missing.
* **portfolio risk / scenario** - Fincept's calculations over QuantDesk's own
  positions and returns. The result is advice for the risk panel; it cannot place
  an order or change a position, because no such path exists here.
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ..analytics import PortfolioAnalyticsService, paper_snapshot
from ..config.settings import load_app_config, quantdesk_home
from ..config.symbol_map import SCENARIOS, mapping_payload
from ..datahub.db import Database
from ..datahub.view import read_history
from ..paper.engine import PaperEngine
from ..plugins import PluginError, PluginManager, PluginRegistry
from ..research.external import STATUS_OK, ExternalEvidenceService
from ..research.external_bridge import enabled_plugin_id
from ..tradingagents_runner import target_for

router = APIRouter(prefix="/api/external", tags=["external"])

# The report's licence position, stated where an operator will read it.
LICENCES = {
    "openbb": {
        "name": "OpenBB",
        "licence": "AGPL-3.0（另有商业许可）",
        "mode": "独立运行环境或 REST，不并入 QuantDesk 安装包",
        "note": "对外分发或提供网络服务前需重新确认许可条件",
    },
    "fincept": {
        "name": "Fincept",
        "licence": "Terminal 仓库为 AGPL-3.0-or-later",
        "mode": "仅官方 REST API，不使用其仓库代码、名称与界面样式",
        "note": "不克隆、不安装、不复制 Fincept Terminal 代码",
    },
}


class PortfolioRiskRequest(BaseModel):
    confidence: float = Field(0.95, gt=0, lt=1)
    optimize: bool = False
    returnsPoints: int = Field(200, ge=30, le=1000)
    force: bool = False


class ExternalSettingsRequest(BaseModel):
    openbbEnabled: bool | None = None
    finceptEnabled: bool | None = None
    timeoutSeconds: int | None = Field(None, ge=1, le=300)
    maxRetries: int | None = Field(None, ge=0, le=10)
    requestsPerMinute: int | None = Field(None, ge=1, le=6000)
    cacheTtlMinutes: dict[str, int] | None = None
    retentionDays: dict[str, int] | None = None
    providers: dict[str, str] | None = None
    fallbacks: dict[str, list[str]] | None = None


class ProviderTestRequest(BaseModel):
    capability: Literal["research_tool", "analytics"] = "research_tool"
    topic: str = Field("company_profile", max_length=40)
    symbol: str = Field("NVDAUSDT", min_length=2, max_length=32)
    # A paid probe spends money, so it is opt-in per request and never the default.
    allowPaid: bool = False


class ScenarioRequest(BaseModel):
    scenario: str = Field(min_length=1, max_length=80)
    confidence: float = Field(0.95, gt=0, lt=1)
    shocks: dict[str, float] | None = None
    force: bool = False


def _home():
    return quantdesk_home()


def _db() -> Database:
    return Database(_home() / "quantdesk.db")


def _external_config() -> tuple[dict, dict]:
    config = load_app_config(_home())
    return config.external or {}, config.openbb or {}


def _plugin_status(manager: PluginManager, capability: str, enabled_flag: bool) -> dict:
    records, invalid = manager.discover()
    matches = [record for record in records if capability in record.manifest.capabilities]
    plugin_id = enabled_plugin_id(manager, capability)
    detail: dict[str, Any] = {
        "capability": capability,
        "configured": enabled_flag,
        "pluginId": plugin_id,
        "installed": [record.manifest.id for record in matches],
        "enabled": [record.manifest.id for record in matches if record.enabled],
        "invalid": invalid,
        "healthy": None,
        "runtime": None,
        "error": "",
    }
    if plugin_id:
        try:
            health = manager.health(plugin_id)["result"]
            detail["healthy"] = bool(health.get("ok"))
            detail["runtime"] = {
                key: health.get(key)
                for key in ("transport", "runtimeReady", "credentialPresent", "baseUrl", "note", "topics", "endpoints")
                if key in health
            }
        except PluginError as exc:
            detail["healthy"] = False
            detail["error"] = str(exc)
    return detail


def _rate_limit_evidence(db: Database) -> dict:
    """How the provider calls have actually gone, from the rows we kept."""
    rows = db.external_analytics_stats()
    totals = {"ok": 0, "error": 0, "averageMs": None, "samples": 0, "lastError": None}
    durations: list[float] = []
    for row in rows:
        status = str(row.get("status") or "")
        count = int(row.get("total") or 0)
        if status == STATUS_OK:
            totals["ok"] += count
        else:
            totals["error"] += count
        if row.get("average_ms") is not None:
            durations.append(float(row["average_ms"]))
            totals["samples"] += count
    if durations:
        totals["averageMs"] = round(sum(durations) / len(durations), 2)
    failures = db.list_external_analytics(limit=20)
    for row in failures:
        if row.get("status") != STATUS_OK:
            totals["lastError"] = {"at": row.get("updated_ts"), "error": row.get("error")}
            break
    return totals


@router.get("/status")
def external_status():
    """Everything the settings page needs, and nothing secret."""
    home = _home()
    external, openbb = _external_config()
    manager = PluginManager(home)
    db = _db()
    evidence_stats = db.external_evidence_stats()
    total = sum(int(row.get("total") or 0) for row in evidence_stats)

    calls = db.list_external_analytics(limit=200)
    hits = db.list_external_evidence(limit=200)
    # A cache hit rate the operator can act on: cached readings versus calls made.
    cached = sum(1 for row in hits if row.get("status") == STATUS_OK)
    cache_hit_rate = round(cached / total, 4) if total else None

    return {
        "external": {
            "openbbEnabled": bool(external.get("openbb_enabled")),
            "finceptEnabled": bool(external.get("fincept_enabled")),
            "timeoutSeconds": external.get("default_timeout_seconds"),
            "maxRetries": external.get("max_retries"),
            "backoffBaseSeconds": external.get("backoff_base_seconds"),
            "maxBackoffSeconds": external.get("max_backoff_seconds"),
            "requestsPerMinute": external.get("requests_per_minute"),
            "cacheTtlMinutes": external.get("cache_ttl_minutes") or {},
            "retentionDays": external.get("retention_days") or {},
        },
        "providers": {
            "openbb": (openbb.get("providers") or {}),
            "fallbacks": (openbb.get("fallbacks") or {}),
            "pointInTime": (openbb.get("point_in_time") or {}),
        },
        "plugins": {
            "research": _plugin_status(manager, "research_tool", bool(external.get("openbb_enabled"))),
            "analytics": _plugin_status(manager, "analytics", bool(external.get("fincept_enabled"))),
        },
        "evidence": {
            "rows": total,
            "cacheHitRate": cache_hit_rate,
            "byProvider": evidence_stats,
            "recent": [
                {
                    "symbol": row.get("symbol"),
                    "topic": row.get("topic"),
                    "provider": row.get("provider"),
                    "status": row.get("status"),
                    "asOf": row.get("as_of"),
                    "publishedAt": row.get("published_at"),
                    "observedAt": row.get("observed_at"),
                    "warning": row.get("warning"),
                    "updatedTs": row.get("updated_ts"),
                }
                for row in hits[:20]
            ],
        },
        "analytics": {
            "calls": len(calls),
            "rate": _rate_limit_evidence(db),
            "recent": [
                {
                    "kind": row.get("kind"),
                    "provider": row.get("provider"),
                    "status": row.get("status"),
                    "marketVersion": row.get("market_snapshot_version"),
                    "durationMs": row.get("duration_ms"),
                    "error": row.get("error"),
                    "updatedTs": row.get("updated_ts"),
                }
                for row in calls[:20]
            ],
        },
        "licences": LICENCES,
        "checkedAt": int(time.time() * 1000),
    }


@router.post("/settings")
def save_external_settings(request: ExternalSettingsRequest):
    """Persist the external panel. Secrets are not accepted here.

    Keys go through the credentials endpoint, which writes them to keys.env and
    never echoes them back; this endpoint only carries configuration.
    """
    from ..config.settings import KNOWN_OPENBB_PROVIDERS, write_external_settings

    external: dict[str, Any] = {}
    if request.openbbEnabled is not None:
        external["openbb_enabled"] = bool(request.openbbEnabled)
    if request.finceptEnabled is not None:
        external["fincept_enabled"] = bool(request.finceptEnabled)
    if request.timeoutSeconds is not None:
        external["default_timeout_seconds"] = int(request.timeoutSeconds)
    if request.maxRetries is not None:
        external["max_retries"] = int(request.maxRetries)
    if request.requestsPerMinute is not None:
        external["requests_per_minute"] = int(request.requestsPerMinute)

    providers = request.providers or {}
    unknown = sorted({name for name in providers.values() if name and name not in KNOWN_OPENBB_PROVIDERS})
    if unknown:
        raise HTTPException(422, f"未知的 OpenBB Provider：{', '.join(unknown)}；可选：{', '.join(KNOWN_OPENBB_PROVIDERS)}")
    for topic, chain in (request.fallbacks or {}).items():
        bad = sorted({name for name in chain if name and name not in KNOWN_OPENBB_PROVIDERS})
        if bad:
            raise HTTPException(422, f"{topic} 的备用 Provider 非法：{', '.join(bad)}")
    for table, values in (("cache_ttl_minutes", request.cacheTtlMinutes), ("retention_days", request.retentionDays)):
        for key, value in (values or {}).items():
            if value < 0 or value > 5_256_000:
                raise HTTPException(422, f"{table}.{key} 超出合理范围：{value}")

    payload: dict[str, Any] = {"external": external}
    if request.cacheTtlMinutes:
        payload["cache_ttl_minutes"] = {key: int(value) for key, value in request.cacheTtlMinutes.items()}
    if request.retentionDays:
        payload["retention_days"] = {key: int(value) for key, value in request.retentionDays.items()}
    if providers or request.fallbacks:
        payload["openbb"] = {"providers": providers, "fallbacks": request.fallbacks}
    if not payload.get("external") and len(payload) == 1:
        raise HTTPException(422, "没有需要保存的设置")
    try:
        path = write_external_settings(payload, _home())
    except OSError as exc:
        raise HTTPException(409, f"无法写入 {path}：{exc.strerror or exc}") from exc
    return {"saved": True, "configFile": str(path), "status": external_status()}


@router.post("/test")
def test_provider(request: ProviderTestRequest):
    """Connectivity check that costs nothing by default.

    Without `allowPaid` this reports the runtime, credential and plugin state; a
    probe that spends money requires the caller to ask for it explicitly, because
    the report forbids unrequested paid tests.
    """
    home = _home()
    external, openbb = _external_config()
    manager = PluginManager(home)
    enabled = (
        bool(external.get("openbb_enabled"))
        if request.capability == "research_tool"
        else bool(external.get("fincept_enabled"))
    )
    status = _plugin_status(manager, request.capability, enabled)
    result: dict[str, Any] = {
        "capability": request.capability,
        "enabled": enabled,
        "plugin": status,
        "paid": False,
        "called": False,
        "outcome": "仅完成本地检查（未调用付费接口）",
    }
    if not enabled:
        result["outcome"] = "该能力未启用"
        return result
    if status["pluginId"] is None:
        result["outcome"] = "没有启用对应插件"
        return result
    if not request.allowPaid:
        result["note"] = "付费探测未获授权：如需真实调用一次接口，请显式允许"
        return result

    result["paid"] = True
    try:
        if request.capability == "research_tool":
            registry = PluginRegistry(manager)
            payload = mapping_payload(request.symbol)
            outcome = registry.collect_research(
                status["pluginId"],
                __import__("quantdesk.plugins.protocol", fromlist=["ResearchCollectRequest"]).ResearchCollectRequest(
                    symbol=request.symbol, tradeDate=time.strftime("%Y-%m-%d"), topics=[request.topic],
                    mapping=payload["mapping"], providers=_provider_chain(request.topic, openbb),
                ),
            )
            result["called"] = True
            result["outcome"] = (
                f"Provider {outcome.evidence[0].provider} 返回 {len(outcome.evidence)} 条证据"
                if outcome.evidence
                else "；".join(
                    f"{item.get('provider') or '未知'}：{item.get('reason') or '无原因'}"
                    for item in outcome.unavailable
                ) or "Provider 未返回数据"
            )
        else:
            result["outcome"] = "Fincept 付费探测请在模拟盘「组合风险」页发起，以便同时带上真实持仓"
    except PluginError as exc:
        result["outcome"] = f"插件调用失败：{exc}"
    return result


def _provider_chain(topic: str, openbb: dict) -> list[str]:
    """The providers to try for a topic, using the configured spelling of it."""
    from ..research.external import provider_config_key

    key = provider_config_key(topic)
    providers = (openbb.get("providers") or {}).get(key)
    chain = [providers] if providers else []
    chain += list((openbb.get("fallbacks") or {}).get(key) or [])
    return [item for item in chain if item]


@router.get("/evidence")
def external_evidence(
    symbol: str = Query(..., min_length=2, max_length=32),
    limit: int = Query(50, ge=1, le=200),
):
    """The standardised readings stored for one contract."""
    home = _home()
    try:
        mapping = mapping_payload(symbol)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    rows = _db().list_external_evidence(symbol=symbol, limit=limit)
    external, openbb = _external_config()
    return {
        "symbol": symbol,
        "mapping": mapping,
        "providers": openbb.get("providers") or {},
        "pointInTime": openbb.get("point_in_time") or {},
        "evidence": [
            {
                "topic": row.get("topic"),
                "provider": row.get("provider"),
                "endpoint": row.get("endpoint"),
                "status": row.get("status"),
                "asOf": row.get("as_of"),
                "publishedAt": row.get("published_at"),
                "observedAt": row.get("observed_at"),
                "expiresAt": row.get("expires_at"),
                "source": row.get("source_url"),
                "contentHash": row.get("content_hash"),
                "pointInTime": bool(row.get("point_in_time")),
                "warning": row.get("warning"),
                "stale": _is_stale(row, external),
            }
            for row in rows
        ],
    }


def _is_stale(row: dict, external: dict) -> bool:
    ttl = (external.get("cache_ttl_minutes") or {}).get(row.get("topic"))
    if not isinstance(ttl, (int, float)) or ttl <= 0:
        return False
    age_minutes = (time.time() * 1000 - int(row.get("updated_ts") or 0)) / 60_000
    return age_minutes > ttl


def _register_analytics() -> tuple[Any, str]:
    """The enabled analytics adapter, or the reason there is none."""
    manager = PluginManager(_home())
    plugin_id = enabled_plugin_id(manager, "analytics")
    if plugin_id is None:
        return None, "没有启用任何 analytics 插件（设置页可开启 Fincept 组合分析）"
    try:
        return PluginRegistry(manager), ""
    except PluginError as exc:  # pragma: no cover - registry construction is trivial
        return None, str(exc)


def _paper_snapshot(*, points: int):
    """The book, priced from QuantDesk's own data (shared with the research prompt).

    Stored marks, not a live quote: the snapshot carries a market version built
    from the candle series, and an analytics answer is cached against that
    version. A live mark would let two calls share one version while carrying
    different prices, which is how a cached answer gets reused for a different
    question.
    """
    return paper_snapshot(_home(), points=points, live_marks=False)


def _analytics_service(registry, plugin_id: str, external: dict) -> PortfolioAnalyticsService:
    def provider(kind: str, params: dict) -> dict:
        if kind == "portfolio":
            from ..plugins.protocol import AnalyticsPortfolioRequest

            result = registry.portfolio_analytics(
                plugin_id,
                AnalyticsPortfolioRequest(
                    asOf=params["asOf"], baseCurrency=params["baseCurrency"],
                    confidence=params["confidence"], positions=params["positions"],
                    returns=params["returns"], marketSnapshotVersion=params["marketSnapshotVersion"],
                ),
            )
        else:
            from ..plugins.protocol import AnalyticsScenarioRequest

            result = registry.scenario_analytics(
                plugin_id,
                AnalyticsScenarioRequest(
                    asOf=params["asOf"], baseCurrency=params["baseCurrency"],
                    confidence=params["confidence"], positions=params["positions"],
                    scenario=params["scenario"], shocks=params["shocks"],
                    marketSnapshotVersion=params["marketSnapshotVersion"],
                ),
            )
        payload = result.model_dump()
        # The protocol's `returns` were only an input; echoing them back would
        # bloat the stored result without adding information.
        payload.pop("returns", None)
        payload.pop("marketSnapshotVersion", None)
        return payload

    return PortfolioAnalyticsService(_db(), provider, config=external)


def _require_enabled(external: dict) -> None:
    if not external.get("fincept_enabled"):
        raise HTTPException(409, "Fincept 组合分析未启用（设置页可开启）")


@router.get("/scenarios")
def scenarios():
    return {
        "scenarios": [
            {"id": item["id"], "label": item["label"], "description": item["description"]}
            for item in SCENARIOS
        ]
    }


@router.post("/portfolio-risk")
async def portfolio_risk(request: PortfolioRiskRequest):
    external, _ = _external_config()
    _require_enabled(external)
    registry, problem = _register_analytics()
    if registry is None:
        return {"ok": False, "unavailable": problem, "snapshot": None, "result": None}
    snapshot = await run_in_threadpool(_paper_snapshot, points=request.returnsPoints)
    service = _analytics_service(registry, enabled_plugin_id(PluginManager(_home()), "analytics"), external)
    outcome = await run_in_threadpool(
        service.portfolio, snapshot,
        confidence=request.confidence, optimize=request.optimize, force=request.force,
    )
    return {**outcome.as_dict(), "snapshot": snapshot.as_dict()}


@router.post("/scenario")
async def scenario(request: ScenarioRequest):
    external, _ = _external_config()
    _require_enabled(external)
    registry, problem = _register_analytics()
    if registry is None:
        return {"ok": False, "unavailable": problem, "snapshot": None, "result": None}
    snapshot = await run_in_threadpool(_paper_snapshot, points=200)
    service = _analytics_service(registry, enabled_plugin_id(PluginManager(_home()), "analytics"), external)
    try:
        outcome = await run_in_threadpool(
            service.scenario, snapshot, scenario_id=request.scenario,
            shocks=request.shocks, confidence=request.confidence, force=request.force,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    payload = {**outcome.as_dict(), "snapshot": snapshot.as_dict()}
    if outcome.ok and isinstance(outcome.result, dict):
        # A scenario result is a warning, never a permission: the local limits
        # decide what may be held, and this adds no path around them.
        payload["advisory"] = "情景结果仅用于风险提示，不改变 QuantDesk 本地开仓与风控限制"
    return payload
