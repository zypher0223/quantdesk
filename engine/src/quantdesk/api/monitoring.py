"""Operations and market-data-quality dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool

from ..alerts import AlertEngine
from ..config.settings import quantdesk_home
from ..datahub.market_service import get_market_service
from ..monitoring import DataQualityMonitor
from ..scheduler import get_scheduler
from ..tradingagents_queue import get_tradingagents_queue
from .data import get_backfill_worker

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


@router.get("")
async def monitoring_overview(request: Request):
    home = quantdesk_home()
    payload = await run_in_threadpool(DataQualityMonitor(home).overview)
    alert_engine = AlertEngine(home)
    rules = alert_engine.list_rules()
    market = get_market_service(home)
    scheduler = get_scheduler(home).status()
    payload["components"] = {
        "gateway": {"status": "running", "detail": "HTTP API 已响应"},
        # The whole scheduler status, not only its `market` slice: the config
        # decides how the rotation is described, and the market block alone did
        # not carry it.
        "marketScheduler": {**scheduler["market"], "config": scheduler.get("config", {})},
        "marketData": {
            # The live link, not the fallback collector: state, reconnects and
            # the age of the most recent real message from the venue.
            "status": "running" if market.running else "stopped",
            **market.health(),
        },
        "alertEngine": {
            "status": "running",
            "enabledRules": sum(1 for rule in rules if rule["enabled"]),
            "last24hEvents": len([
                event for event in alert_engine.list_events(200)
                if int(event["triggeredAt"]) >= payload["checkedAt"] - 86_400_000
            ]),
        },
        "tradingAgents": get_tradingagents_queue(home).status(),
        "backfillQueue": get_backfill_worker().status(),
        # Optional external providers: switched off is a normal state, and the
        # block says which of the two it is instead of leaving a blank panel.
        "external": await run_in_threadpool(external_health, home),
        "paperMonitor": getattr(request.app.state, "paper_monitor", None),
    }
    return payload


def external_health(home) -> dict:
    """OpenBB and Fincept as the monitoring page needs them.

    Reads stored rows and plugin health only: a monitoring poll must never spend
    an external call or a cent.
    """
    from ..api.external import _plugin_status
    from ..config.settings import load_app_config
    from ..datahub.db import Database
    from ..plugins import PluginManager

    config = load_app_config(home)
    external = config.external or {}
    db = Database(home / "quantdesk.db")
    manager = PluginManager(home)
    evidence = db.external_evidence_stats()
    analytics = db.external_analytics_stats()
    analytics_rows = db.list_external_analytics(limit=50)
    failures = [row for row in analytics_rows if row.get("status") != "ok"]
    durations = [float(row["duration_ms"]) for row in analytics_rows if row.get("duration_ms") is not None]
    evidence_total = sum(int(row.get("total") or 0) for row in evidence)
    return {
        "openbb": {
            **_plugin_status(manager, "research_tool", bool(external.get("openbb_enabled"))),
            "topics": evidence,
            "cachedRows": evidence_total,
            "lastSuccess": max((int(row.get("newest") or 0) for row in evidence), default=0) or None,
            "lastError": next(
                (
                    {"at": row.get("updated_ts"), "error": row.get("warning")}
                    for row in db.list_external_evidence(limit=50)
                    if row.get("status") in ("error", "rejected")
                ),
                None,
            ),
            "rejectedRows": sum(int(row.get("total") or 0) for row in evidence if row.get("status") == "rejected"),
            "unavailableRows": sum(int(row.get("total") or 0) for row in evidence if row.get("status") == "unavailable"),
        },
        "fincept": {
            **_plugin_status(manager, "analytics", bool(external.get("fincept_enabled"))),
            "calls": len(analytics_rows),
            "failures": len(failures),
            "successRate": round((len(analytics_rows) - len(failures)) / len(analytics_rows), 4) if analytics_rows else None,
            "averageMs": round(sum(durations) / len(durations), 2) if durations else None,
            "rateLimited": sum(1 for row in failures if "429" in str(row.get("error") or "")),
            "cacheHits": sum(1 for row in analytics_rows if row.get("status") == "ok"),
            "byKind": analytics,
            "lastSuccess": max(
                (int(row.get("updated_ts") or 0) for row in analytics_rows if row.get("status") == "ok"),
                default=0,
            ) or None,
            "lastError": next(
                (
                    {"at": row.get("updated_ts"), "error": row.get("error")}
                    for row in failures
                ),
                None,
            ),
        },
        "licences": __import__("quantdesk.api.external", fromlist=["LICENCES"]).LICENCES,
    }


@router.get("/symbols/{symbol}")
async def symbol_quality(symbol: str):
    return await run_in_threadpool(DataQualityMonitor(quantdesk_home()).check_symbol, symbol)
