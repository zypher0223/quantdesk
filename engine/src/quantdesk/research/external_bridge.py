"""Bridge from the OpenBB adapter to the places research actually happens.

The pipeline the execution report specifies:

    OpenBB 采集 → 时点过滤 → 去重与缓存 → 证据标准化 → QuantDesk 证据包 → 研判

Everything on the far side of "标准化" is here: turning a bundle of gated readings
into (a) a block a model reads, (b) an appendix a human reads, and (c) metadata the
archive keeps.

The rule that shapes all three outputs: if the external side is missing, slow,
misconfigured or refused, the research still runs and the output says it was
degraded. Every consumer of this module gets an answer; the answer may be "no
external evidence, and here is why".
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from ..config.settings import load_app_config, quantdesk_home
from ..config.symbol_map import mapping_for, mapping_payload
from ..datahub.db import Database
from .external import (
    TOPICS_WITHOUT_PUBLICATION_TIME,
    EvidenceBundle,
    EvidenceRecord,
    ExternalEvidenceService,
    provider_plan,
    ttl_minutes,
)

logger = logging.getLogger(__name__)

# The topics a research run asks for, grouped the way the report groups them.
DEFAULT_TOPICS: tuple[str, ...] = (
    "company_profile",
    "fundamentals",
    "financial_growth",
    "earnings",
    "news",
    "macro_calendar",
)

# How much of a reading's value to put in a prompt. Enough to reason about,
# bounded so one document cannot crowd out the rest of the evidence pack.
MAX_VALUE_CHARS = 1200
MAX_READINGS_IN_PROMPT = 12


def _truncate(value: Any, limit: int = MAX_VALUE_CHARS) -> str:
    text = value if isinstance(value, str) else _json(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "…（已截断）"


def _json(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)


def plugin_fetcher(registry, plugin_id: str, *, mapping: dict, trade_date: str):
    """A fetcher the evidence service can call: one topic, one provider plan.

    The engine decides the mapping and the provider order and hands both to the
    adapter, so the adapter never guesses a ticker or a provider.
    """

    def fetch(provider: str, topic: str, symbol: str) -> dict:
        from ..plugins.protocol import ResearchCollectRequest

        request = ResearchCollectRequest(
            symbol=symbol,
            tradeDate=trade_date,
            topics=[topic],
            mapping=mapping,
            providers=[provider],
        )
        result = registry.collect_research(plugin_id, request)
        payload = {
            "evidence": [item.model_dump() for item in result.evidence],
            "observedAt": result.evidence[0].observedAt if result.evidence else "",
        }
        if result.unavailable:
            reasons = [
                f"{entry.get('provider') or '未知 Provider'}：{entry.get('reason') or '无原因'}"
                for entry in result.unavailable
                if isinstance(entry, dict)
            ]
            payload["unavailable"] = "；".join(reasons) or "Provider 未返回数据"
        if result.warnings:
            payload["warnings"] = list(result.warnings)
        return payload

    return fetch


def enabled_plugin_id(manager, capability: str) -> str | None:
    """The single enabled adapter for a capability, or None.

    More than one is a configuration error the operator must resolve, not
    something to pick from silently: which research provider a report cites is a
    decision, not a coin flip.
    """
    matches = [record.manifest.id for record in manager.discover()[0]
               if record.enabled and capability in record.manifest.capabilities]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning("启用了多个 %s 插件：%s；将只使用第一个", capability, ", ".join(matches))
    return matches[0]


def collect_evidence(
    *,
    symbol: str,
    trade_date: str,
    topics: Iterable[str] | None = None,
    home=None,
    db=None,
    registry=None,
    manager=None,
) -> EvidenceBundle:
    """Fetch, gate and standardise external evidence for one contract.

    Returns an empty bundle with an explanatory `unavailable` entry when the
    external side is off or unusable - never an exception, because research must
    still run without it.
    """
    home = home or quantdesk_home()
    config = load_app_config(home)
    external = config.external or {}
    if not external.get("openbb_enabled"):
        bundle = EvidenceBundle(symbol=symbol, trade_date=trade_date, external_enabled=False)
        bundle.unavailable.append({"topic": "*", "provider": "", "reason": "OpenBB 研究未启用"})
        return bundle

    try:
        mapping = mapping_for(symbol)
    except KeyError as exc:
        bundle = EvidenceBundle(symbol=symbol, trade_date=trade_date)
        bundle.unavailable.append({"topic": "*", "provider": "", "reason": str(exc)})
        return bundle

    from ..plugins import PluginManager, PluginRegistry

    home = home or quantdesk_home()
    manager = manager or PluginManager(home)
    plugin_id = enabled_plugin_id(manager, "research_tool")
    if plugin_id is None:
        bundle = EvidenceBundle(symbol=symbol, trade_date=trade_date)
        bundle.unavailable.append(
            {"topic": "*", "provider": "", "reason": "没有启用任何 research_tool 插件"}
        )
        return bundle

    db = db or Database(home / "quantdesk.db")
    service = ExternalEvidenceService(db, external, config.openbb)
    registry = registry or PluginRegistry(manager)
    selected = [item for item in (topics or DEFAULT_TOPICS)]
    configured = config.openbb or {}
    # A topic nobody configured a provider for is not asked for: an unconfigured
    # interface is a policy decision, not a fetch to attempt and fail.
    wanted = [topic for topic in selected if provider_plan(topic, configured)]
    skipped = [topic for topic in selected if topic not in wanted]
    bundle = service.collect(
        symbol=symbol,
        trade_date=trade_date,
        topics=wanted,
        fetcher=plugin_fetcher(registry, plugin_id, mapping=mapping_payload(symbol)["mapping"], trade_date=trade_date),
    )
    for topic in skipped:
        bundle.unavailable.append({"topic": topic, "provider": "", "reason": "该接口未配置 Provider"})
    return bundle


def collect_best_effort(*, symbol: str, trade_date: str, skip_reason: str = "", **kwargs) -> EvidenceBundle:
    """`collect_evidence` that cannot raise, for callers on a critical path.

    `skip_reason` records why a caller deliberately did not ask (a demo chart, for
    instance) so the run reports a switched-off feature rather than a failure.
    """
    if skip_reason:
        bundle = EvidenceBundle(symbol=symbol, trade_date=trade_date, external_enabled=False)
        bundle.unavailable.append({"topic": "*", "provider": "", "reason": skip_reason})
        return bundle
    try:
        return collect_evidence(symbol=symbol, trade_date=trade_date, **kwargs)
    except Exception as exc:  # noqa: BLE001 - external research is never load-bearing
        logger.warning("外部证据采集失败：%s: %s", type(exc).__name__, exc)
        bundle = EvidenceBundle(symbol=symbol, trade_date=trade_date)
        bundle.errors.append(
            {"topic": "*", "provider": "", "reason": f"{type(exc).__name__}: {exc}"}
        )
        return bundle


def prompt_block(bundle: EvidenceBundle) -> str:
    """The evidence as the model sees it: readings, dates, and what is missing."""
    if not bundle.external_enabled:
        # Nothing was asked for, so there is nothing to tell the model about -
        # and no reason to make its report explain a feature nobody enabled.
        return ""
    if not bundle.usable and not bundle.degraded:
        return ""
    lines = [
        "## 外部证据（OpenBB，非行情来源）",
        "",
        "以下为外部研究数据，只用于基本面、新闻与宏观判断。",
        "价格、成交量、资金费率与强平一律以 Bybit 为准，不要用这里的任何数字改写点位。",
        "",
    ]
    for record in bundle.usable[:MAX_READINGS_IN_PROMPT]:
        published = record.published_at or "未提供发布时间"
        lines.append(f"- [{record.topic}] {record.label}")
        lines.append(f"  provider={record.provider} endpoint={record.endpoint} asOf={record.as_of or '—'} publishedAt={published}")
        lines.append(f"  value={_truncate(record.value)}")
        if record.source:
            lines.append(f"  source={record.source}")
        for warning in record.warnings:
            lines.append(f"  warning={warning}")
    if len(bundle.usable) > MAX_READINGS_IN_PROMPT:
        lines.append(f"- （其余 {len(bundle.usable) - MAX_READINGS_IN_PROMPT} 条已省略）")
    missing = bundle.unavailable + [
        {"topic": item.get("topic"), "reason": item.get("reason")} for item in bundle.rejected
    ]
    if missing:
        lines += ["", "### 本次不可用的外部证据（必须在报告中说明）", ""]
        for item in missing:
            lines.append(f"- {item.get('topic') or '未知主题'}：{item.get('reason') or '无原因'}")
    if bundle.degraded:
        lines += [
            "",
            "**证据降级**：部分外部证据缺失或被时点校验拒绝，请在报告中明确说明，并降低结论置信度。",
        ]
    return "\n".join(lines)


def appendix(bundle: EvidenceBundle, *, title: str = "外部证据") -> str:
    """The evidence as a reader sees it: sources, publish times, freshness, gaps."""
    lines = [f"## {title}", ""]
    if not bundle.external_enabled:
        lines.append("- 外部研究未启用（设置页可开启 OpenBB 研究）")
        return "\n".join(lines)
    if not bundle.usable and not bundle.degraded:
        lines.append("- 本次没有外部证据")
        return "\n".join(lines)
    lines += [
        f"- 可用证据：{len(bundle.usable)} 条；主题：{', '.join(bundle.topics_present()) or '无'}",
        f"- 缓存命中/外部调用：{bundle.cache_hits}/{bundle.calls}",
        f"- 数据状态：{'证据降级（有缺失）' if bundle.degraded else '完整'}",
        "",
    ]
    if bundle.usable:
        lines += ["| 主题 | 读数 | Provider | 数据时间 | 发布时间 | 来源 |", "| --- | --- | --- | --- | --- | --- |"]
        for record in bundle.usable:
            source = f"[链接]({record.source})" if record.source.startswith("http") else (record.source or "—")
            lines.append(
                f"| {record.topic} | {record.label} | {record.provider} | {record.as_of or '—'} "
                f"| {record.published_at or '—'} | {source} |"
            )
    for label, items in (("不可用", bundle.unavailable), ("被时点校验拒绝", bundle.rejected), ("采集错误", bundle.errors)):
        if not items:
            continue
        lines += ["", f"### {label}", ""]
        for item in items:
            detail = item.get("reason") or "无原因"
            basis = f"（判据：{item['basis']}）" if item.get("basis") else ""
            lines.append(f"- {item.get('topic') or '未知主题'}：{detail}{basis}")
    if bundle.degraded:
        lines += ["", "> 证据降级：外部数据不完整，本报告的结论置信度应相应下调。"]
    return "\n".join(lines)


def summary_meta(bundle: EvidenceBundle) -> dict:
    """What the archive keeps about the external side of a run."""
    providers = sorted({record.provider for record in bundle.usable})
    topics = bundle.topics_present()
    return {
        "enabled": bundle.external_enabled,
        "usable": len(bundle.usable),
        "topics": topics,
        "providers": providers,
        "cacheHits": bundle.cache_hits,
        "calls": bundle.calls,
        "degraded": bundle.degraded,
        "unavailable": bundle.unavailable,
        "rejected": bundle.rejected,
        "errors": bundle.errors,
        "publishedAt": {record.key: record.published_at for record in bundle.usable},
        "sources": {record.key: record.source for record in bundle.usable},
        "asOf": {record.key: record.as_of for record in bundle.usable},
    }


def evidence_records(bundle: EvidenceBundle) -> list[dict]:
    return [record.as_dict() for record in bundle.usable]


def topic_expiry_map(external_config: dict | None = None) -> dict[str, int]:
    """The cache window per topic, for the settings page to display."""
    config = external_config or {}
    configured = (config.get("cache_ttl_minutes") or {})
    topics = set(DEFAULT_TOPICS) | set(configured) | set(TOPICS_WITHOUT_PUBLICATION_TIME)
    return {topic: ttl_minutes(topic, config) for topic in sorted(topics)}


def portfolio_risk_context(home, *, symbol: str, points: int = 200, provider=None,
                           live_marks: bool = False) -> tuple[str, dict]:
    """A Fincept portfolio-risk summary for a research prompt, and its provenance.

    Returns `(block, meta)`. The block is empty when Fincept is off or could not
    answer, and the meta always says which of those it was - a report that lists
    "组合风险：未启用" is more useful than one that silently omits the section.
    """
    from ..analytics import PortfolioAnalyticsService, paper_snapshot
    from ..config.settings import load_app_config
    from ..datahub.db import Database

    config = load_app_config(home)
    external = config.external or {}
    if not external.get("fincept_enabled"):
        return "", {"enabled": False, "reason": "Fincept 组合分析未启用"}

    db = Database(home / "quantdesk.db")
    try:
        # A research brief is reproducible: it reads stored marks, never a price
        # fetched on the spot.
        snapshot = paper_snapshot(home, points=points, db=db, live_marks=live_marks)
    except Exception as exc:  # noqa: BLE001 - research continues without the summary
        return "", {"enabled": True, "ok": False, "reason": f"{type(exc).__name__}: {exc}"}

    if not snapshot.positions:
        return "", {"enabled": True, "ok": False, "reason": "模拟盘当前没有持仓，无组合风险可算"}

    if provider is None:
        from ..plugins import PluginManager, PluginRegistry
        from ..plugins.protocol import AnalyticsPortfolioRequest

        manager = PluginManager(home)
        plugin_id = enabled_plugin_id(manager, "analytics")
        if plugin_id is None:
            return "", {"enabled": True, "ok": False, "reason": "没有启用 analytics 插件"}
        registry = PluginRegistry(manager)

        def provider(kind: str, params: dict) -> dict:
            result = registry.portfolio_analytics(
                plugin_id,
                AnalyticsPortfolioRequest(
                    asOf=params["asOf"], baseCurrency=params["baseCurrency"],
                    confidence=params["confidence"], positions=params["positions"],
                    returns=params["returns"], marketSnapshotVersion=params["marketSnapshotVersion"],
                ),
            )
            payload = result.model_dump()
            payload.pop("returns", None)
            payload.pop("marketSnapshotVersion", None)
            return payload

    service = PortfolioAnalyticsService(db, provider, config=external)
    outcome = service.portfolio(snapshot)
    meta = {
        "enabled": True,
        "ok": outcome.ok,
        "provider": outcome.provider,
        "cached": outcome.cached,
        "marketVersion": outcome.market_version,
        "inputHash": outcome.input_hash,
        "requestId": outcome.request_id,
        "durationMs": outcome.duration_ms,
        "positions": len(snapshot.positions),
        "grossExposure": snapshot.gross_exposure,
        "netExposure": snapshot.net_exposure,
        "marginUsed": snapshot.margin_used,
        "reason": outcome.unavailable or outcome.error,
        "warnings": list(outcome.warnings),
    }
    if not outcome.ok or not isinstance(outcome.result, dict):
        return "", meta

    result = outcome.result
    metrics = result.get("metrics") or {}
    contributions = result.get("riskContributions") or []
    lines = [
        "## Fincept 组合风险摘要（外部计算，用于风险提示）",
        "",
        f"- 市场快照版本：{outcome.market_version}（结果只在该快照下成立）",
        f"- 持仓数：{len(snapshot.positions)} · 总名义敞口 {snapshot.gross_exposure:.2f} · "
        f"净敞口 {snapshot.net_exposure:.2f} · 保证金占用 {snapshot.margin_used:.2f}",
        f"- 组合波动率：{_fmt(metrics.get('volatility'))} · VaR {_fmt(metrics.get('var'))} · "
        f"CVaR {_fmt(metrics.get('cvar'))} · 最大回撤 {_fmt(metrics.get('maxDrawdown'))}",
    ]
    own = next((item for item in contributions if str(item.get("symbol")) == symbol), None)
    if own is not None:
        lines.append(
            f"- 当前标的 {symbol} 的风险贡献：{_fmt(own.get('value'))}"
            f"（占组合 {_fmt(own.get('percentage'))}%）"
        )
    else:
        lines.append(f"- {symbol} 当前不在持仓中，因此没有该标的的风险贡献")
    if snapshot.warnings:
        lines.append("- 数据提醒：" + "；".join(snapshot.warnings[:4]))
    lines += [
        "",
        "这些数字来自外部风险服务，只用于提示风险。仓位与杠杆仍由 QuantDesk 本地规则决定，",
        "不要用它们改写 Bybit 行情给出的点位。",
    ]
    return "\n".join(lines), meta


def _fmt(value: object) -> str:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    return f"{number:.6g}"
