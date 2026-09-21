"""Machine-only entry point that executes a real TradingAgentsGraph."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

REPORT_KEYS = (
    "market_report", "sentiment_report", "news_report", "fundamentals_report",
    "investment_plan", "trader_investment_plan", "final_trade_decision",
)


try:  # The graph validates its callbacks, so this must be a real handler.
    from langchain_core.callbacks import BaseCallbackHandler as _CallbackBase
except Exception:  # noqa: BLE001 - accounting still works as a plain object

    class _CallbackBase:  # type: ignore[no-redef]
        pass


class UsageCollector(_CallbackBase):
    """Minimal LangChain callback that records tokens per model.

    It inherits the runtime's own handler base because the model constructor
    validates its `callbacks` argument by type: a duck-typed object is rejected
    before a single token is spent. The logic is still self-contained, so the
    engine package does not need to be installed in the runtime's environment.
    """

    def __init__(self) -> None:
        try:
            super().__init__()
        except Exception:  # noqa: BLE001 - a base without a usable constructor
            pass
        self.usage: dict[str, dict[str, int]] = {}
        self.calls = 0

    def on_llm_end(self, response, **kwargs) -> None:
        self.calls += 1
        payload = self._payload(response)
        if not isinstance(payload, dict):
            return
        model = self._model(response, kwargs)
        prompt = self._int(payload.get("prompt_tokens") or payload.get("input_tokens")) or 0
        completion = self._int(payload.get("completion_tokens") or payload.get("output_tokens")) or 0
        hit = self._int(payload.get("prompt_cache_hit_tokens"))
        if hit is None:
            # Providers name the same counter differently: DeepSeek reports
            # `prompt_cache_hit_tokens`, OpenAI `prompt_tokens_details.cached_tokens`,
            # and LangChain's own metadata `input_token_details.cache_read`.
            for key in ("prompt_tokens_details", "input_token_details"):
                details = payload.get(key)
                if isinstance(details, dict):
                    for nested in ("cached_tokens", "cache_read"):
                        hit = self._int(details.get(nested))
                        if hit is not None:
                            break
                    if hit is not None:
                        break
        hit = hit or 0
        miss = self._int(payload.get("prompt_cache_miss_tokens"))
        if miss is None:
            miss = max(0, prompt - hit)
        bucket = self.usage.setdefault(model, {"cacheHit": 0, "cacheMiss": 0, "output": 0, "calls": 0})
        bucket["cacheHit"] += hit
        bucket["cacheMiss"] += miss
        bucket["output"] += completion
        bucket["calls"] += 1

    def on_llm_error(self, error, **kwargs) -> None:
        self.calls += 1

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {model: dict(values) for model, values in sorted(self.usage.items())}

    def totals(self) -> dict[str, int]:
        combined = {"cacheHit": 0, "cacheMiss": 0, "output": 0, "calls": self.calls}
        for values in self.usage.values():
            for key in ("cacheHit", "cacheMiss", "output"):
                combined[key] += values[key]
        combined["inputTotal"] = combined["cacheHit"] + combined["cacheMiss"]
        combined["total"] = combined["inputTotal"] + combined["output"]
        return combined

    @staticmethod
    def _int(value):
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _model(response, kwargs) -> str:
        params = kwargs.get("invocation_params") if isinstance(kwargs, dict) else None
        if isinstance(params, dict) and params.get("model"):
            return str(params["model"])
        output = getattr(response, "llm_output", None)
        if isinstance(output, dict) and output.get("model_name"):
            return str(output["model_name"])
        return "unknown"

    @staticmethod
    def _payload(response):
        output = getattr(response, "llm_output", None)
        if isinstance(output, dict):
            for key in ("token_usage", "usage", "usage_metadata"):
                if isinstance(output.get(key), dict):
                    return output[key]
        for batch in getattr(response, "generations", None) or []:
            for generation in batch or []:
                message = getattr(generation, "message", None)
                metadata = getattr(message, "usage_metadata", None)
                if isinstance(metadata, dict):
                    return metadata
                response_metadata = getattr(message, "response_metadata", None)
                if isinstance(response_metadata, dict) and isinstance(response_metadata.get("token_usage"), dict):
                    return response_metadata["token_usage"]
        return None



def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def main() -> int:
    started = time.monotonic()
    try:
        request = json.load(sys.stdin)
        trade_date = date.fromisoformat(str(request["trade_date"])).isoformat()
        if date.fromisoformat(trade_date) > date.today():
            raise ValueError("analysis date cannot be in the future")
        asset_type = str(request["asset_type"])
        analysts = tuple(request["analysts"])

        # Token accounting: the graph passes these callbacks into every LLM it
        # builds, so one collector sees the whole run's usage per model. It is a
        # plain object rather than a package import, so the runtime needs no extra
        # dependency for it.
        ledger = UsageCollector()

        with contextlib.redirect_stdout(sys.stderr):
            from tradingagents.default_config import DEFAULT_CONFIG
            from tradingagents.graph.trading_graph import TradingAgentsGraph

            config = DEFAULT_CONFIG.copy()
            home = Path(os.environ.get("TRADINGAGENTS_RESULTS_DIR", Path.home() / ".tradingagents" / "logs")).parent
            config["results_dir"] = os.environ.get("TRADINGAGENTS_RESULTS_DIR", str(home / "logs"))
            config["data_cache_dir"] = os.environ.get("TRADINGAGENTS_CACHE_DIR", str(home / "cache"))
            config["memory_log_path"] = os.environ.get("TRADINGAGENTS_MEMORY_LOG_PATH", str(home / "memory" / "trading_memory.md"))
            Path(config["memory_log_path"]).parent.mkdir(parents=True, exist_ok=True)

            market_source = "yfinance"
            if asset_type == "crypto":
                from quantdesk.tradingagents_hyperliquid import install_hyperliquid_bridge
                install_hyperliquid_bridge(config)
                market_source = "hyperliquid"
            else:
                from quantdesk.tradingagents_bybit import install_bybit_bridge
                install_bybit_bridge(
                    config,
                    venue_symbol=request["symbol"],
                    fundamental_symbol=request.get("fundamental_symbol"),
                    company_name=request.get("company_name"),
                    external_evidence=str(request.get("external_evidence") or ""),
                )
                market_source = "bybit"

            graph = TradingAgentsGraph(
                selected_analysts=analysts,
                debug=False,
                config=config,
                callbacks=[ledger],
            )
            # Outcome resolution in upstream uses yfinance with the graph ticker.
            # Our graph ticker is the exact exchange contract, so disable that
            # unrelated lookup rather than accidentally evaluating another asset.
            graph._resolve_pending_entries = lambda _ticker: None
            final_state, rating = graph.propagate(request["symbol"], trade_date, asset_type=asset_type)

        reports = {key: final_state.get(key) for key in REPORT_KEYS if final_state.get(key) not in (None, "")}
        debates = {
            "investment": final_state.get("investment_debate_state"),
            "risk": final_state.get("risk_debate_state"),
        }
        _emit({
            "ok": True,
            "symbol": request["symbol"],
            "trade_date": trade_date,
            "asset_type": asset_type,
            "analysts": list(analysts),
            "rating": str(rating),
            "requires_review": str(rating) == "REVIEW",
            "reports": reports,
            "debates": debates,
            "external_evidence": request.get("external_evidence_records") or [],
            "meta": {
                "provider": config.get("llm_provider"),
                "deep_model": config.get("deep_think_llm"),
                "quick_model": config.get("quick_think_llm"),
                "debate_rounds": config.get("max_debate_rounds"),
                "risk_rounds": config.get("max_risk_discuss_rounds"),
                "market_data_source": market_source,
                "market_symbol": request["symbol"],
                "fundamental_symbol": request.get("fundamental_symbol"),
                "duration_seconds": round(time.monotonic() - started, 3),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "usage": ledger.snapshot(),
                "usage_totals": ledger.totals(),
                # The external side of the run, recorded whether or not it
                # contributed: which provider answered, when each reading was
                # published, and whether the evidence was degraded.
                "external_evidence": request.get("external_evidence_meta") or None,
                "evidence_degraded": bool(request.get("evidence_degraded")),
            },
        })
        return 0
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        message = str(exc)
        _emit({
            "ok": False,
            "error": {
                "type": type(exc).__name__,
                "message": message,
                "retryable": any(word in message.lower() for word in ("timeout", "429", "rate limit")),
            },
            "meta": {
                "duration_seconds": round(time.monotonic() - started, 3),
                "usage": ledger.snapshot() if "ledger" in dir() else {},
                "usage_totals": ledger.totals() if "ledger" in dir() else {},
            },
        })
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
