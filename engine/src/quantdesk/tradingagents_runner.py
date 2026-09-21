"""Portable subprocess boundary for the upstream TradingAgents graph."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config.instruments import InstrumentSpec
from .config.settings import LLMProfile, quantdesk_home
from .llm import credential_issue, resolve_api_key

UPSTREAM_COMMIT = "be952b8eccb49720509af544c6675233bc1f10d0"
PROVIDER_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "glm-cn": "ZHIPU_CN_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
}


class TradingAgentsError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, detail: str = ""):
        self.retryable = retryable
        self.detail = detail
        super().__init__(message)


@dataclass(frozen=True)
class AnalysisTarget:
    symbol: str
    asset_type: str
    analysts: tuple[str, ...]
    fundamental_symbol: str | None = None


def target_for(spec: InstrumentSpec) -> AnalysisTarget:
    if spec.is_crypto:
        return AnalysisTarget(f"{spec.display_symbol}-USD", "crypto", ("market",))
    # The graph analyzes the traded contract itself. Public-equity symbols are
    # carried separately and are used only by news/fundamental vendor adapters.
    return AnalysisTarget(
        spec.venue_symbol,
        "stock",
        ("market", "social", "news", "fundamentals"),
        spec.underlying_symbol or spec.display_symbol,
    )


def runtime_python() -> str:
    """Which interpreter runs the upstream graph.

    Order: the environment variable, then the configured path, then this
    interpreter. The configured path matters because the graph lives in its own
    virtualenv with its own pinned dependencies, and a CLI invocation does not
    inherit the service's environment.
    """
    explicit = os.environ.get("TRADINGAGENTS_PYTHON")
    if explicit:
        return explicit
    try:
        from .config.settings import load_app_config

        configured = str(load_app_config().research.get("tradingagents_python") or "").strip()
        if configured:
            return os.path.expanduser(configured)
    except Exception:  # noqa: BLE001 - fall back to this interpreter
        pass
    return sys.executable


def _child_env(profile: LLMProfile, key: str) -> dict[str, str]:
    env = dict(os.environ)
    provider = profile.provider.lower()
    expected_key = PROVIDER_KEY_ENV.get(provider)
    if expected_key:
        env[expected_key] = key
    env.update(
        {
            "TRADINGAGENTS_LLM_PROVIDER": provider,
            "TRADINGAGENTS_DEEP_THINK_LLM": profile.deep_model or profile.quick_model,
            "TRADINGAGENTS_QUICK_THINK_LLM": profile.quick_model or profile.deep_model,
            "TRADINGAGENTS_OUTPUT_LANGUAGE": "Chinese",
            "TRADINGAGENTS_MAX_DEBATE_ROUNDS": "1",
            "TRADINGAGENTS_MAX_RISK_ROUNDS": "1",
            "TRADINGAGENTS_CHECKPOINT_ENABLED": "true",
            "TRADINGAGENTS_LLM_MAX_RETRIES": "3",
        }
    )
    if profile.base_url:
        env["TRADINGAGENTS_LLM_BACKEND_URL"] = profile.base_url
    if profile.max_tokens:
        env["TRADINGAGENTS_MAX_TOKENS"] = str(profile.max_tokens)
    if profile.proxy:
        env["HTTP_PROXY"] = profile.proxy
        env["HTTPS_PROXY"] = profile.proxy

    home = quantdesk_home() / "tradingagents"
    env["TRADINGAGENTS_RESULTS_DIR"] = str(home / "logs")
    env["TRADINGAGENTS_CACHE_DIR"] = str(home / "cache")
    env["TRADINGAGENTS_MEMORY_LOG_PATH"] = str(home / "memory" / "trading_memory.md")

    # A separately installed interpreter still needs to find QuantDesk's worker.
    package_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def runtime_status(timeout: float = 15.0) -> dict[str, Any]:
    command = [
        runtime_python(), "-c",
        "from tradingagents.graph.trading_graph import TradingAgentsGraph; "
        "from tradingagents.default_config import DEFAULT_CONFIG; print('0.4.0')",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ready": False, "python": runtime_python(), "reason": str(exc), "commit": UPSTREAM_COMMIT}
    return {
        "ready": completed.returncode == 0,
        "python": runtime_python(),
        "version": completed.stdout.strip() if completed.returncode == 0 else None,
        "reason": completed.stderr.strip()[-1000:] if completed.returncode else None,
        "commit": UPSTREAM_COMMIT,
    }


from .datahub.snapshot import history_version  # noqa: E402 - kept local to the helper


def data_reference(symbol: str, trade_date: str, *, interval: str = "1d") -> dict[str, Any]:
    """Pin the version and the as-of time of the bars a research run will read.

    A research report is only checkable if it says which data it read: the venue
    symbol, the newest bar it could see, the version of that history, and whether
    the data is older than the trade date it was run for. A run whose data ends
    before its trade date is reported as stale rather than as a normal rating.
    """
    from .config.instruments import BY_VENUE_SYMBOL
    from .config.settings import quantdesk_home
    from .datahub.db import Database
    from .datahub.snapshot import load_snapshot
    from .datahub.venue import INTERVAL_MS, last_closed_open_ts

    spec = BY_VENUE_SYMBOL.get(symbol)
    if spec is None:
        return {"symbol": symbol, "available": False, "reason": "不在固定合约池内"}

    step = INTERVAL_MS[interval]
    try:
        db = Database(quantdesk_home() / "quantdesk.db")
        stored = db.last_open_ts("bybit", spec.venue_symbol, interval)
        if stored is None:
            return {"symbol": spec.venue_symbol, "available": False, "reason": "本地没有日线数据"}
        # Provenance names the last *closed* bar: the still-forming one is not
        # something the run could have read as history.
        newest = last_closed_open_ts(stored, step)
        window = 400
        snapshot = load_snapshot(
            db,
            symbols=[spec.venue_symbol],
            interval=interval,
            from_ts=newest - (window - 1) * step,
            to_ts=newest,
            product_types={spec.venue_symbol: spec.product_type},
            with_funding=False,
            with_marks=False,
        )
        coverage = snapshot.coverage[spec.venue_symbol]
        rows = snapshot.candles(spec.venue_symbol)
        if not rows:
            return {"symbol": spec.venue_symbol, "available": False, "reason": "本地没有该周期的历史数据"}
        # "As of" names the newest bar the run could actually read, not the edge of
        # the window it asked for: those differ whenever the store is missing days,
        # and a provenance line that rounds up is a provenance line that lies.
        newest_bar = max(int(row["ts"]) for row in rows)
        canonical = history_version(
            "bybit",
            spec.venue_symbol,
            interval,
            newest - (window - 1) * step,
            newest,
            rows,
        )
        try:
            requested = dt.date.fromisoformat(str(trade_date))
        except ValueError:
            requested = None
        as_of = dt.datetime.fromtimestamp(newest_bar / 1000, dt.timezone.utc).date()
        today = dt.datetime.now(dt.timezone.utc).date()
        # Two different failures, both of which invalidate a normal rating: the
        # history does not reach the date being judged, or the history itself has
        # gone cold while real trading days have passed since the last bar.
        earliest = dt.datetime.fromtimestamp(
            (newest - (window - 1) * step) / 1000, dt.timezone.utc
        ).date()
        lag_days = (requested - as_of).days if requested else 0
        cold_days = (today - as_of).days
        if requested and requested < earliest:
            # The trade date predates everything stored: there is no history for it.
            stale, stale_days, stale_reason = True, (earliest - requested).days, "本地没有该日期的历史数据"
        elif requested and lag_days > 1:
            stale, stale_days, stale_reason = True, lag_days, "数据未覆盖研判日期"
        elif requested and today > requested and cold_days > 1:
            stale, stale_days, stale_reason = True, cold_days, "研判日期已过去，但本地数据没有跟上"
        else:
            stale, stale_days, stale_reason = False, 0, ""
        provenance = {
            "symbol": spec.venue_symbol,
            "displaySymbol": spec.display_symbol,
            "available": True,
            "interval": interval,
            "asOf": as_of.isoformat(),
            "tradeDate": requested.isoformat() if requested else str(trade_date),
            "bars": len(rows),
            "complete": coverage.complete,
            "missingInSession": coverage.missing_in_session,
            "sources": coverage.sources,
            "stale": stale,
            "staleByDays": stale_days,
            "staleReason": stale_reason,
            "version": canonical,
        }
        return provenance
    except Exception as exc:  # noqa: BLE001 - a research run must not fail on provenance
        return {"symbol": symbol, "available": False, "reason": f"{type(exc).__name__}: {exc}"}


def run_tradingagents(
    spec: InstrumentSpec,
    profile: LLMProfile,
    trade_date: str,
    *,
    analysts: list[str] | None = None,
    timeout_seconds: int = 1800,
    callbacks: list[Any] | None = None,
    external_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = target_for(spec)
    selected = tuple(analysts or target.analysts)
    allowed = {"market", "social", "news", "fundamentals"}
    if not selected or set(selected) - allowed:
        raise TradingAgentsError("分析师列表无效")
    if target.asset_type == "crypto" and "fundamentals" in selected:
        raise TradingAgentsError("加密资产流程不支持 fundamentals 分析师")

    key, _ = resolve_api_key(profile.provider, profile.api_key_env)
    issue = credential_issue(key)
    if not key or issue:
        raise TradingAgentsError(f"profile「{profile.name}」缺少可用 API Key" + (f"：{issue}" if issue else ""))
    if not (profile.deep_model or profile.quick_model):
        raise TradingAgentsError(f"profile「{profile.name}」没有配置文本模型")

    reference = data_reference(target.symbol, trade_date)
    payload = {
        "symbol": target.symbol,
        "asset_type": target.asset_type,
        "trade_date": trade_date,
        "data_as_of": reference.get("asOf"),
        "data_version": reference.get("version"),
        # A callback object cannot cross a process boundary, so the worker is told
        # to install its own usage collector instead.
        "collect_usage": bool(callbacks),
        "analysts": list(selected),
        "profile": profile.name,
        "fundamental_symbol": target.fundamental_symbol,
        "company_name": spec.name,
    }
    # External evidence is optional enrichment. When it is present the worker
    # appends it to the fundamentals and news the analysts already read, and the
    # archive keeps which readings were used and when they were published.
    if external_evidence:
        payload["external_evidence"] = str(external_evidence.get("prompt") or "")
        payload["external_evidence_records"] = list(external_evidence.get("records") or [])
        payload["external_evidence_meta"] = external_evidence.get("meta") or {}
        payload["evidence_degraded"] = bool(external_evidence.get("degraded"))
    command = [runtime_python(), "-m", "quantdesk.tradingagents_worker"]
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=_child_env(profile, key),
        )
    except subprocess.TimeoutExpired as exc:
        raise TradingAgentsError(
            f"TradingAgents 超过 {timeout_seconds} 秒仍未完成", retryable=True,
            detail=(exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
        ) from exc
    except OSError as exc:
        raise TradingAgentsError(f"无法启动 TradingAgents 运行时：{exc}") from exc

    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise TradingAgentsError(
            "TradingAgents 没有返回有效 JSON",
            detail=(completed.stderr or completed.stdout)[-4000:],
        ) from exc
    if completed.returncode or not result.get("ok"):
        error = result.get("error") or {}
        raise TradingAgentsError(
            str(error.get("message") or f"TradingAgents 退出码 {completed.returncode}"),
            retryable=bool(error.get("retryable")),
            detail=(completed.stderr or "")[-4000:],
        )
    result["venue_symbol"] = spec.venue_symbol
    result["display_symbol"] = spec.display_symbol
    result["profile"] = profile.name
    result["data"] = reference
    warnings: list[str] = []
    if callbacks:
        # Replay the worker's per-model usage into the caller's collector, so the
        # parent accounts for the same tokens the child reported.
        usage = ((result.get("meta") or {}).get("usage") or {})
        for model, totals in usage.items():
            if not isinstance(totals, dict):
                continue
            for callback in callbacks:
                collector = getattr(callback, "absorb", None)
                if callable(collector):
                    collector(model, totals)
    if reference.get("stale"):
        warnings.append(
            f"数据截止 {reference.get('asOf')}，{reference.get('staleReason') or '数据已过期'}"
            f"（差 {reference.get('staleByDays')} 天），该结论不得作为正常评级使用"
        )
    if reference.get("available") and not reference.get("complete"):
        warnings.append(f"输入数据在交易时段内缺 {reference.get('missingInSession')} 根K线")
    evidence_meta = (result.get("meta") or {}).get("external_evidence") or {}
    if evidence_meta.get("degraded"):
        missing = evidence_meta.get("unavailable") or []
        detail = "；".join(
            f"{item.get('topic') or '未知主题'}：{item.get('reason') or '无原因'}"
            for item in missing[:4]
            if isinstance(item, dict)
        )
        rejected = evidence_meta.get("rejected") or []
        if rejected:
            detail += ("；" if detail else "") + "；".join(
                f"{item.get('topic') or '未知主题'}被时点校验拒绝：{item.get('reason') or '无原因'}"
                for item in rejected[:4]
                if isinstance(item, dict)
            )
        warnings.append(
            "证据降级：外部证据不完整" + (f"（{detail}）" if detail else "") + "，本报告结论置信度应相应下调"
        )
    # Did the stored history change while the models were thinking?
    after = data_reference(target.symbol, trade_date)
    if reference.get("version") and after.get("version") != reference.get("version"):
        warnings.append("运行期间本地历史发生变化，报告与当前存储的数据版本不一致")
        result["data"]["versionChanged"] = True
    result["warnings"] = warnings
    return result
