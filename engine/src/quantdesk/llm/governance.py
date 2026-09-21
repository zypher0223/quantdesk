"""Cost governance for paid research runs: budgets, reuse and a cost ledger.

A multi-agent research run is the most expensive thing this project does, and the
one place where a mistake is billed rather than displayed. Three rules follow from
that, and this module enforces all three:

* **Ask first.** A single-run and a daily budget are checked before the money is
  spent. A run that cannot be priced is refused when a budget is configured,
  because an unenforceable limit reads as protection without being any.
* **Do not pay twice.** Two runs are the same run when the instrument, the trade
  date, the model configuration, the prompt version and the *data version* all
  match. The data version is part of the key on purpose: reusing a report written
  against bars that have since been repaired would hide the repair.
* **Write it down.** Every attempt - including refused and failed ones - lands in
  the ledger with what it read, what it cost, how long it took and why it stopped.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..datahub.db import Database
from ..config.settings import quantdesk_home
from .pricing import CostBreakdown, TokenUsage, budget_state, price_usage, usage_from_payload

# Bumped whenever the prompt scaffolding or the analyst contract changes, so a
# cached report can be told apart from one produced under different instructions.
PROMPT_VERSION = "ta-prompt/1"
# How long a reused result stays acceptable. A research verdict is a statement
# about a moment; a week-old one is a different statement.
DEFAULT_REUSE_WINDOW_HOURS = 24
# A graph can come back with an analyst report missing instead of failing. One
# missing dimension is a thinner answer; most of the panel missing is no answer,
# and a rating produced by one analyst must not be published as a normal rating.
DEFAULT_MAX_MISSING_ANALYSTS = 1
DEFAULT_MAX_ANALYST_RETRIES = 1

# The analysts this project asks for, and what each one's absence means.
ANALYST_REPORTS = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}


@dataclass
class RunBudget:
    """The limits one run is allowed to consume."""

    per_run_usd: float | None = None
    daily_usd: float | None = None
    reuse_window_hours: int = DEFAULT_REUSE_WINDOW_HOURS
    max_retries: int = 1
    allow_unpriced: bool = False
    # Ceiling on the tokens a single attempt may generate, as a second, cruder
    # guard for a run whose model is not priced.
    max_tokens_per_run: int | None = None
    # How many analysts may be missing before a conclusion stops counting as a
    # rating, and how many whole passes may be repeated to fill a missing report.
    max_missing_analysts: int = DEFAULT_MAX_MISSING_ANALYSTS
    max_analyst_retries: int = DEFAULT_MAX_ANALYST_RETRIES

    def as_dict(self) -> dict[str, Any]:
        return {
            "perRunUsd": self.per_run_usd,
            "dailyUsd": self.daily_usd,
            "reuseWindowHours": self.reuse_window_hours,
            "maxRetries": self.max_retries,
            "allowUnpriced": self.allow_unpriced,
            "maxTokensPerRun": self.max_tokens_per_run,
            "maxMissingAnalysts": self.max_missing_analysts,
            "maxAnalystRetries": self.max_analyst_retries,
        }

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "RunBudget":
        """Read the operator's settings, ignoring anything unusable."""
        raw = config or {}
        research = raw.get("research") if isinstance(raw.get("research"), dict) else raw

        def number(key: str) -> float | None:
            value = research.get(key)
            if value in (None, ""):
                return None
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed > 0 else None

        reuse = research.get("reuse_window_hours")
        retries = research.get("max_retries")
        analyst_retries = research.get("max_analyst_retries")
        missing = research.get("max_missing_analysts")
        return cls(
            per_run_usd=number("agent_run_budget_usd"),
            daily_usd=number("agent_daily_budget_usd"),
            reuse_window_hours=int(reuse) if isinstance(reuse, (int, float)) and reuse >= 0 else DEFAULT_REUSE_WINDOW_HOURS,
            max_retries=int(retries) if isinstance(retries, (int, float)) and retries >= 0 else 1,
            allow_unpriced=bool(research.get("allow_unpriced_agents", False)),
            max_analyst_retries=(
                int(analyst_retries)
                if isinstance(analyst_retries, (int, float)) and analyst_retries >= 0
                else DEFAULT_MAX_ANALYST_RETRIES
            ),
            max_missing_analysts=(
                int(missing)
                if isinstance(missing, (int, float)) and missing >= 0
                else DEFAULT_MAX_MISSING_ANALYSTS
            ),
        )


@dataclass
class RunAttempt:
    """What one attempt at a research run produced, before it is stored."""

    ok: bool
    usage: dict[str, TokenUsage] = field(default_factory=dict)
    cost: CostBreakdown | None = None
    duration_s: float = 0.0
    retries: int = 0
    analyst_retries: int = 0
    failure: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    reused_from: str | None = None

    def total_tokens(self) -> int:
        return sum(item.total for item in self.usage.values())


class LedgerCallback:
    """Collect token usage per model from a LangChain run.

    Deliberately a plain object with the one callback method the runtime calls,
    so the TradingAgents subprocess does not need this package importable and the
    engine does not need a LangChain dependency of its own.
    """

    def __init__(self) -> None:
        self.usage: dict[str, TokenUsage] = {}
        self.calls = 0

    # -- LangChain callback surface -------------------------------------
    def on_llm_end(self, response: Any, **kwargs: Any) -> None:  # noqa: D401 - callback name
        self.calls += 1
        model = self._model_name(response, kwargs)
        payload = self._usage_payload(response)
        if payload is None:
            return
        usage = usage_from_payload(payload)
        current = self.usage.get(model, TokenUsage())
        self.usage[model] = current.add(usage)

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:  # noqa: D401 - callback name
        self.calls += 1

    def absorb(self, model: str, totals: dict[str, Any]) -> None:
        """Take usage reported by a subprocess worker, in the same units.

        The worker counts its own tokens; this is how those counts reach the
        ledger without the parent having to guess or re-price from scratch.
        """
        usage = TokenUsage(
            cache_hit=int(totals.get("cacheHit") or 0),
            cache_miss=int(totals.get("cacheMiss") or 0),
            output=int(totals.get("output") or 0),
            calls=int(totals.get("calls") or 0),
        )
        self.usage[model] = self.usage.get(model, TokenUsage()).add(usage)

    # -- readers ---------------------------------------------------------
    def snapshot(self) -> dict[str, dict[str, int]]:
        return {model: usage.as_dict() for model, usage in sorted(self.usage.items())}

    def totals(self) -> dict[str, int]:
        combined = TokenUsage()
        for usage in self.usage.values():
            combined = combined.add(usage)
        combined.calls = self.calls
        return combined.as_dict()

    # -- internals -------------------------------------------------------
    @staticmethod
    def _model_name(response: Any, kwargs: dict[str, Any]) -> str:
        for candidate in (
            kwargs.get("invocation_params", {}).get("model") if isinstance(kwargs.get("invocation_params"), dict) else None,
            getattr(response, "llm_output", {}).get("model_name") if isinstance(getattr(response, "llm_output", None), dict) else None,
        ):
            if candidate:
                return str(candidate)
        return "unknown"

    @staticmethod
    def _usage_payload(response: Any) -> dict[str, Any] | None:
        llm_output = getattr(response, "llm_output", None)
        if isinstance(llm_output, dict):
            for key in ("token_usage", "usage", "usage_metadata"):
                payload = llm_output.get(key)
                if isinstance(payload, dict):
                    return payload
        generations = getattr(response, "generations", None) or []
        for batch in generations:
            for generation in batch or []:
                message = getattr(generation, "message", None)
                metadata = getattr(message, "usage_metadata", None)
                if isinstance(metadata, dict):
                    return metadata
                response_metadata = getattr(message, "response_metadata", None)
                if isinstance(response_metadata, dict):
                    payload = response_metadata.get("token_usage")
                    if isinstance(payload, dict):
                        return payload
        return None


def _json_list(raw: Any) -> list[str]:
    """Read a JSON list column that a caller may have written as a plain list."""
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str) and raw:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in decoded] if isinstance(decoded, list) else []
    return []


def config_fingerprint(profile: Any, analysts: list[str], runtime_commit: str | None) -> str:
    """A short id for everything that decides what the models are asked.

    Two runs with the same fingerprint asked the same questions of the same models
    with the same runtime; a change to any of it must produce a new report.
    """
    payload = json.dumps(
        {
            "provider": getattr(profile, "provider", None),
            "deep": getattr(profile, "deep_model", None),
            "quick": getattr(profile, "quick_model", None),
            "baseUrl": getattr(profile, "base_url", None),
            "maxTokens": getattr(profile, "max_tokens", None),
            "temperature": getattr(profile, "temperature", None),
            "analysts": sorted(analysts),
            "runtime": runtime_commit,
            "prompt": PROMPT_VERSION,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def reuse_key(
    *,
    venue_symbol: str,
    trade_date: str,
    config_fingerprint_value: str,
    data_version: str | None,
) -> str:
    """The identity of a run whose result may be reused verbatim."""
    payload = "|".join(
        [
            venue_symbol,
            trade_date,
            config_fingerprint_value,
            data_version or "nodata",
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class AgentCostGovernor:
    """Budgets, reuse and the ledger, around one research run."""

    def __init__(self, home=None, budget: RunBudget | None = None):
        self.home = home or quantdesk_home()
        self.db = Database(self.home / "quantdesk.db")
        self.budget = budget or RunBudget()

    # -- budget ----------------------------------------------------------
    def spent_today(self, *, now: dt.datetime | None = None) -> dict[str, Any]:
        """What has been spent since 00:00 UTC, and how much of it is unpriced."""
        moment = now or dt.datetime.now(dt.timezone.utc)
        start = moment.astimezone(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.db.agent_cost_totals(start_ts=int(start.timestamp() * 1000))

    def check_budget(self, *, now: dt.datetime | None = None) -> dict[str, Any]:
        """May a new run start, and what is left to spend today?"""
        totals = self.spent_today(now=now)
        spent = totals["usd"]
        if totals["unpricedRuns"]:
            # An unpriced run in the window makes the day's total a lower bound,
            # not a total, and a budget cannot be enforced against a lower bound.
            spent = None
        state = budget_state(
            spent_today_usd=spent,
            spent_run_usd=None,
            daily_limit_usd=self.budget.daily_usd,
            per_run_limit_usd=None,
        )
        state["today"] = totals
        if self.budget.daily_usd is not None and totals["unpricedRuns"]:
            state["allowed"] = False
            state["problems"].append(
                f"今日有 {totals['unpricedRuns']} 次运行未能定价，无法确认是否超出每日上限"
            )
        # A limit that is only checked after the fact is not a limit. With a
        # per-run cap configured, the day must still have room for one more run of
        # that size before another is started.
        if (
            self.budget.daily_usd is not None
            and self.budget.per_run_usd is not None
            and state["allowed"]
        ):
            remaining = self.budget.daily_usd - totals["usd"]
            if remaining < self.budget.per_run_usd:
                state["allowed"] = False
                state["problems"].append(
                    f"今日剩余额度 ${remaining:.4f} 不足一次运行的预留上限 ${self.budget.per_run_usd:.4f}，"
                    "已停止新的付费运行"
                )
                state["remainingTodayUsd"] = round(max(0.0, remaining), 6)
        return state

    def check_attempt(self, attempt: RunAttempt, *, now: dt.datetime | None = None) -> dict[str, Any]:
        """Did this attempt stay inside the single-run limit?"""
        state = budget_state(
            spent_today_usd=None,
            spent_run_usd=attempt.cost.usd if attempt.cost else None,
            daily_limit_usd=None,
            per_run_limit_usd=self.budget.per_run_usd,
        )
        if self.budget.max_tokens_per_run:
            total = attempt.total_tokens()
            if total > self.budget.max_tokens_per_run:
                state["allowed"] = False
                state["problems"].append(
                    f"本次消耗 {total} tokens 超过上限 {self.budget.max_tokens_per_run}"
                )
        state["tokens"] = attempt.total_tokens()
        return state

    def price(self, usage: dict[str, TokenUsage], provider: str, *, at: dt.datetime | None = None) -> CostBreakdown:
        return price_usage(usage, provider=provider, at=at)

    # -- reuse -----------------------------------------------------------
    def find_reuse(self, key: str, *, now: dt.datetime | None = None) -> dict[str, Any] | None:
        """A previous successful run whose result still stands, if there is one."""
        since = None
        if self.budget.reuse_window_hours:
            moment = now or dt.datetime.now(dt.timezone.utc)
            since = int((moment - dt.timedelta(hours=self.budget.reuse_window_hours)).timestamp() * 1000)
        row = self.db.find_reusable_run(key, since_ts=since)
        if row is None:
            return None
        created = int(row.get("created_ts") or 0)
        return {
            "runId": row["run_id"],
            "createdTs": created,
            "ageHours": round((time.time() * 1000 - created) / 3_600_000, 3) if created else None,
            "rating": row.get("rating"),
            "costUsd": row.get("cost_usd"),
            "dataVersion": row.get("data_version"),
            "dataAsOf": row.get("data_as_of"),
            "configFingerprint": row.get("config_fingerprint"),
            # Carried over so the reused receipt reports the coverage the original
            # run actually had, instead of an empty panel.
            "missingAnalysts": _json_list(row.get("missing_analysts")),
        }

    def note_reuse(self, **entry: Any) -> None:
        """Record that a run was served from an earlier result instead of paid for."""
        self.db.record_agent_cost(entry)

    # -- ledger ----------------------------------------------------------
    def record(
        self,
        *,
        run_id: str,
        venue_symbol: str,
        trade_date: str,
        profile: Any,
        analysts: list[str],
        missing_analysts: list[str],
        fingerprint: str,
        data_reference: dict[str, Any],
        attempt: RunAttempt,
        ok: bool,
        rating: str | None,
        staleness: dict[str, Any],
        reuse_key_value: str,
        created_ts: int | None = None,
    ) -> dict[str, Any]:
        """Write one attempt to the ledger and return the stored entry."""
        entry = {
            "run_id": run_id,
            "venue_symbol": venue_symbol,
            "trade_date": trade_date,
            "profile": getattr(profile, "name", "unknown"),
            "provider": getattr(profile, "provider", "unknown"),
            "deep_model": getattr(profile, "deep_model", None),
            "quick_model": getattr(profile, "quick_model", None),
            "config_fingerprint": fingerprint,
            "prompt_version": PROMPT_VERSION,
            "data_version": data_reference.get("version"),
            "data_as_of": data_reference.get("asOf"),
            "analysts": analysts,
            "missing_analysts": missing_analysts,
            "reuse_key": reuse_key_value,
            "reused_from": attempt.reused_from,
            "ok": ok,
            "rating": rating,
            "staleness": staleness,
            "usage": {model: usage.as_dict() for model, usage in attempt.usage.items()},
            # Token data exists, so unpriced models in this run make the day's
            # total a lower bound. A run that never reached a model is left at
            # False: its cost is known to be zero and it must not poison the day.
            "usage_known": bool(attempt.usage),
            "cost_usd": attempt.cost.usd if attempt.cost else None,
            "cost_detail": attempt.cost.as_dict() if attempt.cost else {},
            "duration_s": round(attempt.duration_s, 3),
            "retries": attempt.retries,
            "analyst_retries": attempt.analyst_retries,
            "failure": attempt.failure,
            "created_ts": int(created_ts if created_ts is not None else time.time() * 1000),
        }
        self.db.record_agent_cost(entry)
        return entry

    def ledger(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recent runs, with the JSON columns decoded for reporting."""
        out: list[dict[str, Any]] = []
        for row in self.db.list_agent_costs(limit):
            item = dict(row)
            for column in ("analysts", "missing_analysts", "usage", "cost_detail", "staleness", "failure"):
                raw = item.get(column)
                if isinstance(raw, str) and raw:
                    try:
                        item[column] = json.loads(raw)
                    except json.JSONDecodeError:
                        item[column] = raw
                elif raw in (None, ""):
                    item[column] = [] if column in ("analysts", "missing_analysts") else None
            item["ok"] = bool(item.get("ok"))
            out.append(item)
        return out

    def summary(self, *, days: int = 7, now: dt.datetime | None = None) -> dict[str, Any]:
        """Spend and outcomes over the recent past, for the settings page."""
        moment = now or dt.datetime.now(dt.timezone.utc)
        start = moment - dt.timedelta(days=days)
        totals = self.db.agent_cost_totals(start_ts=int(start.timestamp() * 1000))
        rows = self.ledger(limit=200)
        by_model: dict[str, dict[str, Any]] = {}
        for row in rows:
            for model, usage in (row.get("usage") or {}).items():
                bucket = by_model.setdefault(model, {"tokens": 0, "calls": 0, "usd": 0.0, "priced": False})
                bucket["tokens"] += int(usage.get("total") or 0)
                bucket["calls"] += int(usage.get("calls") or 0)
            for model in (row.get("cost_detail") or {}).get("models", []):
                bucket = by_model.setdefault(model.get("model") or "unknown", {"tokens": 0, "calls": 0, "usd": 0.0, "priced": False})
                if model.get("usd") is not None:
                    bucket["usd"] += float(model["usd"])
                    bucket["priced"] = True
        return {
            "windowDays": days,
            "budget": self.budget.as_dict(),
            "totals": totals,
            "today": self.spent_today(now=moment),
            "byModel": {
                model: {**data, "usd": round(data["usd"], 6) if data["priced"] else None}
                for model, data in by_model.items()
            },
            "recent": [
                {
                    "runId": row.get("run_id"),
                    "symbol": row.get("venue_symbol"),
                    "tradeDate": row.get("trade_date"),
                    "rating": row.get("rating"),
                    "ok": row.get("ok"),
                    "costUsd": row.get("cost_usd"),
                    "durationS": row.get("duration_s"),
                    "retries": row.get("retries"),
                    "analystRetries": row.get("analyst_retries") or 0,
                    # Degraded means "ran, and part of the panel reported". A run
                    # that never reached a model is broken, not degraded, and the
                    # two must not share a word in the ledger.
                    "degraded": bool(row.get("missing_analysts")) and len(row.get("missing_analysts") or []) < len(row.get("analysts") or []),
                    "reusedFrom": row.get("reused_from"),
                    "missingAnalysts": row.get("missing_analysts"),
                    "createdTs": row.get("created_ts"),
                }
                for row in rows[:20]
            ],
        }


def analyst_coverage(reports: dict[str, Any] | None, requested: list[str]) -> dict[str, Any]:
    """Which requested analysts actually produced a report.

    A partial run is a partial answer: the missing analysts are named so a reader
    can tell "no news drove this" from "the news analyst never ran".
    """
    present = reports or {}
    missing: list[str] = []
    covered: list[str] = []
    for analyst in requested:
        key = ANALYST_REPORTS.get(analyst)
        if key and present.get(key):
            covered.append(analyst)
        else:
            missing.append(analyst)
    return {
        "requested": list(requested),
        "covered": covered,
        "missing": missing,
        "complete": not missing,
        "degraded": bool(missing) and bool(covered),
        "failed": bool(missing) and not covered,
    }


def run_governed_symbol(
    *,
    spec: Any,
    profile: Any,
    trade_date: str,
    analysts: list[str] | None = None,
    home: Any = None,
    runtime_commit: str | None = None,
    timeout_seconds: int | None = None,
    run_id: str | None = None,
    force: bool = False,
    now: dt.datetime | None = None,
    external_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The one way a paid research pass is started, budget and all.

    Every entry point - the synchronous route and the job queue behind the
    research button - goes through here, because the two drifting apart is how a
    queue ends up spending money that nothing accounts for.
    """
    from ..config.settings import load_app_config, quantdesk_home
    from ..tradingagents_runner import run_tradingagents, target_for

    home = home or quantdesk_home()
    research = load_app_config(home).research
    # External evidence is gathered before the paid pass so the models see it, and
    # gathered best-effort: a research run must not fail because OpenBB is down.
    if external_evidence is None:
        from ..research.external_bridge import (
            collect_best_effort,
            evidence_records,
            portfolio_risk_context,
            prompt_block,
            summary_meta,
        )

        bundle = collect_best_effort(symbol=spec.venue_symbol, trade_date=trade_date, home=home)
        # The research brief also carries the portfolio's own risk picture: the
        # position being judged, its share of the book, and how the whole book
        # behaves. Computed from QuantDesk positions and returns, by the external
        # risk service, and skipped entirely when that service is off.
        risk_block, risk_meta = portfolio_risk_context(home, symbol=spec.venue_symbol)
        prompt = prompt_block(bundle)
        if risk_block:
            prompt = f"{prompt}\n\n{risk_block}" if prompt else risk_block
        meta = summary_meta(bundle)
        meta["portfolioRisk"] = risk_meta
        external_evidence = {
            "prompt": prompt,
            "appendix": None,
            "records": evidence_records(bundle),
            "meta": meta,
            "degraded": bundle.degraded,
            "enabled": bundle.external_enabled,
        }
    budget = RunBudget.from_config(research)
    governor = AgentCostGovernor(home, budget)
    selected = list(analysts or target_for(spec).analysts)
    configured = int(timeout_seconds or research.get("timeout_seconds", 1800))
    timeout = max(60, min(configured, 3600))

    def execute(*, analysts: list[str], timeout_seconds: int, callbacks: list):
        return run_tradingagents(
            spec,
            profile,
            trade_date,
            analysts=analysts,
            timeout_seconds=timeout_seconds,
            callbacks=callbacks,
            external_evidence=external_evidence,
        )

    return governed_run(
        governor=governor,
        profile=profile,
        spec=spec,
        trade_date=trade_date,
        analysts=selected,
        runner=execute,
        runtime_commit=runtime_commit,
        timeout_seconds=timeout,
        run_id=run_id,
        force=force,
        now=now,
        external_evidence=external_evidence,
    )


def report_meta(outcome: dict[str, Any]) -> dict[str, Any]:
    """The provenance a stored report must carry to still be checkable later."""
    result = outcome.get("result") or {}
    return {
        **(result.get("meta") or {}),
        "promptVersion": PROMPT_VERSION,
        # Which panel was asked, and which of it answered: an archived report that
        # does not say this cannot be re-checked against a later run.
        "analysts": (outcome.get("analystCoverage") or {}).get("requested"),
        "costUsd": outcome.get("costUsd"),
        "usage": outcome.get("usage"),
        "costDetail": outcome.get("costDetail"),
        "dataVersion": (outcome.get("data") or {}).get("version"),
        "dataAsOf": (outcome.get("data") or {}).get("asOf"),
        "configFingerprint": outcome.get("configFingerprint"),
        "analystCoverage": outcome.get("analystCoverage"),
        "analystRetries": outcome.get("analystRetries"),
        "degraded": outcome.get("degraded"),
        "staleness": outcome.get("staleness"),
        "durationS": outcome.get("durationS"),
        "retries": outcome.get("retries"),
        "failure": outcome.get("failure"),
        "runOk": outcome.get("ok"),
        "externalEvidence": outcome.get("externalEvidence"),
    }


def run_payload(outcome: dict[str, Any], run_id: str) -> dict[str, Any]:
    """The API shape of a governed run: the report plus its receipt.

    A reused answer is a real answer, so it is returned - labelled, with its
    cost at zero, because that is what it cost.
    """
    if outcome.get("reused"):
        result: dict[str, Any] = {
            "rating": outcome.get("rating"),
            "reused": True,
            "reusedFrom": outcome.get("reusedFrom"),
            "warnings": [outcome.get("reason") or "命中复用窗口内的同配置结果"],
        }
    else:
        result = dict(outcome.get("result") or {})
    warnings = list(result.get("warnings") or [])
    if not outcome.get("ok") and not outcome.get("reused"):
        warnings.append(str((outcome.get("failure") or {}).get("message") or "运行失败"))
    return {
        **result,
        "runId": run_id,
        "cost": {
            "usd": outcome.get("costUsd"),
            "usage": outcome.get("usage"),
            "detail": outcome.get("costDetail"),
            "budget": outcome.get("budgetState"),
        },
        "data": outcome.get("data"),
        "analystCoverage": outcome.get("analystCoverage"),
        "staleness": outcome.get("staleness"),
        "durationS": outcome.get("durationS"),
        "retries": outcome.get("retries"),
        "analystRetries": outcome.get("analystRetries"),
        "degraded": outcome.get("degraded"),
        # The external side travels with the report so both research paths render
        # the same panel from the same shape.
        "externalEvidence": outcome.get("externalEvidence"),
        "reused": bool(outcome.get("reused")),
        "ok": outcome.get("ok"),
        "failure": outcome.get("failure"),
        "warnings": warnings,
    }


def governed_run(
    *,
    governor: "AgentCostGovernor",
    profile: Any,
    spec: Any,
    trade_date: str,
    analysts: list[str],
    runner: Any,
    runtime_commit: str | None = None,
    timeout_seconds: int = 1800,
    now: dt.datetime | None = None,
    run_id: str | None = None,
    force: bool = False,
    external_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a paid research pass inside its budget, or explain why it did not.

    The order matters: the data version is read first because it is part of the
    reuse key, the budget is checked before anything is spent, and the ledger is
    written on every exit path - including the two that spend nothing.
    """
    from ..tradingagents_runner import TradingAgentsError, data_reference

    budget = governor.budget
    reference = data_reference(spec.venue_symbol, trade_date)
    fingerprint = config_fingerprint(profile, analysts, runtime_commit)
    key = reuse_key(
        venue_symbol=spec.venue_symbol,
        trade_date=trade_date,
        config_fingerprint_value=fingerprint,
        data_version=reference.get("version"),
    )
    base = {
        "symbol": spec.venue_symbol,
        "displaySymbol": spec.display_symbol,
        "tradeDate": trade_date,
        "budget": budget.as_dict(),
        "data": reference,
        "configFingerprint": fingerprint,
        "reuseKey": key,
    }

    if not force:
        cached = governor.find_reuse(key, now=now)
        if cached is not None:
            # A reused answer must describe itself as completely as a fresh one:
            # a reader comparing two receipts should not have to guess why one of
            # them has no coverage and no staleness.
            reused_missing = list(cached.get("missingAnalysts") or [])
            coverage = {
                "requested": list(analysts),
                "covered": [name for name in analysts if name not in reused_missing],
                "missing": reused_missing,
                "complete": not reused_missing,
                "degraded": bool(reused_missing) and len(reused_missing) < len(analysts),
                "failed": bool(reused_missing) and len(reused_missing) == len(analysts),
            }
            staleness = {
                "stale": bool(reference.get("stale")),
                "reason": reference.get("staleReason") or "",
                "asOf": reference.get("asOf"),
                "tradeDate": trade_date,
            }
            attempt = RunAttempt(ok=True, reused_from=cached["runId"])
            entry = governor.record(
                run_id=run_id or f"reuse-{key}",
                venue_symbol=spec.venue_symbol,
                trade_date=trade_date,
                profile=profile,
                analysts=analysts,
                missing_analysts=reused_missing,
                fingerprint=fingerprint,
                data_reference=reference,
                attempt=attempt,
                ok=True,
                rating=cached.get("rating"),
                staleness=staleness,
                reuse_key_value=key,
                created_ts=int((now or dt.datetime.now(dt.timezone.utc)).timestamp() * 1000),
            )
            return {
                **base,
                "reused": True,
                "ok": True,
                "refused": False,
                "reusedFrom": cached["runId"],
                "costUsd": 0.0,
                "usage": {},
                "rating": cached.get("rating"),
                "modelRating": cached.get("rating"),
                "analystCoverage": coverage,
                "degraded": coverage["degraded"],
                # Nothing ran here, so there is no duration, no retry and no
                # budget verdict for this response to report.
                "durationS": 0.0,
                "retries": 0,
                "analystRetries": 0,
                "staleness": staleness,
                "ledger": entry,
                "reason": f"命中 {budget.reuse_window_hours} 小时内的同配置结果，未产生费用",
            }

    state = governor.check_budget(now=now)
    if not state["allowed"]:
        attempt = RunAttempt(ok=False, failure={"type": "BudgetRefused", "message": "; ".join(state["problems"])})
        governor.record(
            run_id=run_id or f"refused-{key}",
            venue_symbol=spec.venue_symbol,
            trade_date=trade_date,
            profile=profile,
            analysts=analysts,
            missing_analysts=list(analysts),
            fingerprint=fingerprint,
            data_reference=reference,
            attempt=attempt,
            ok=False,
            rating=None,
            staleness={"stale": bool(reference.get("stale"))},
            reuse_key_value=key,
        )
        return {
            **base,
            "reused": False,
            "ok": False,
            "refused": True,
            "costUsd": 0.0,
            "usage": {},
            "budgetState": state,
            "reason": "; ".join(state["problems"]),
            "ledger": None,
        }

    callback = LedgerCallback()
    started = time.monotonic()
    provider = getattr(profile, "provider", "unknown")
    retries = 0
    analyst_retries = 0
    attempt = RunAttempt(ok=False)

    def one_pass() -> tuple[bool, dict[str, Any]]:
        """Run the graph once; return whether it produced a result and its coverage."""
        try:
            attempt.result = runner(analysts=analysts, timeout_seconds=timeout_seconds, callbacks=[callback])
            attempt.ok = True
            attempt.failure = None
        except TradingAgentsError as exc:
            attempt.ok = False
            attempt.failure = {"type": type(exc).__name__, "message": str(exc), "retryable": exc.retryable}
            return False, analyst_coverage(None, analysts)
        return True, analyst_coverage((attempt.result or {}).get("reports") or {}, analysts)

    def repeat_is_affordable() -> bool:
        """Would another pass still fit inside the single-run allowance?"""
        if budget.per_run_usd is None:
            return True
        return (governor.price(callback.usage, provider, at=now).usd or 0.0) < budget.per_run_usd

    while True:
        produced, coverage = one_pass()
        if produced:
            # A graph that returns without one analyst's report has degraded
            # rather than failed. One bounded repeat is worth it, because the
            # missing dimension is usually a single flaky call, but the loop
            # stops there: the panel is the product, not a lever to keep pulling.
            if coverage["missing"] and analyst_retries < budget.max_analyst_retries and repeat_is_affordable():
                analyst_retries += 1
                continue
            break
        failure = attempt.failure or {}
        if failure.get("retryable") and retries < budget.max_retries:
            retries += 1
            continue
        break
    attempt.duration_s = time.monotonic() - started
    attempt.retries = retries
    attempt.analyst_retries = analyst_retries
    attempt.usage = callback.usage

    priced_at = now or dt.datetime.now(dt.timezone.utc)
    attempt.cost = governor.price(attempt.usage, provider, at=priced_at)
    if attempt.cost.unpriced and not budget.allow_unpriced:
        attempt.ok = False
        attempt.failure = {
            "type": "UnpricedModel",
            "message": f"以下模型没有单价，已按未定价处理：{', '.join(attempt.cost.unpriced)}",
        }
    post = governor.check_attempt(attempt, now=priced_at)
    if attempt.ok and not post["allowed"]:
        # The money is already spent; the run is kept but marked as over budget so
        # the ledger does not quietly absorb it.
        attempt.failure = {"type": "OverBudget", "message": "; ".join(post["problems"])}

    reports = (attempt.result or {}).get("reports") or {}
    coverage = analyst_coverage(reports, analysts)
    rating = (attempt.result or {}).get("rating")
    staleness = {
        "stale": bool(reference.get("stale")),
        "reason": reference.get("staleReason") or "",
        "asOf": reference.get("asOf"),
        "tradeDate": trade_date,
    }
    if staleness["stale"]:
        # An expired input cannot support a normal rating; the models' answer is
        # still recorded, but it is labelled as unusable rather than published.
        rating = None
    if coverage["missing"]:
        (attempt.result or {}).setdefault("warnings", []).append(
            f"缺失分析师报告：{', '.join(coverage['missing'])}（这些维度没有参与结论）"
        )
        if len(coverage["missing"]) > budget.max_missing_analysts:
            # Past the allowance this is not a thin answer, it is a different
            # question: the conclusion was reached without most of the panel, so
            # it is archived as a degraded run instead of published as a rating.
            attempt.ok = False
            rating = None
            attempt.failure = {
                "type": "AnalystDegraded",
                "message": (
                    f"缺失 {len(coverage['missing'])} 名分析师"
                    f"（{', '.join(coverage['missing'])}），超过允许的 {budget.max_missing_analysts} 名，"
                    "本次结论不作为评级发布"
                ),
            }

    entry = governor.record(
        run_id=run_id or f"run-{key}",
        venue_symbol=spec.venue_symbol,
        trade_date=trade_date,
        profile=profile,
        analysts=analysts,
        missing_analysts=coverage["missing"],
        fingerprint=fingerprint,
        data_reference=reference,
        attempt=attempt,
        ok=bool(attempt.ok),
        rating=rating,
        staleness=staleness,
        reuse_key_value=key,
        created_ts=int(priced_at.timestamp() * 1000),
    )
    return {
        **base,
        "reused": False,
        "refused": False,
        "ok": bool(attempt.ok),
        "rating": rating,
        "modelRating": (attempt.result or {}).get("rating"),
        "analystCoverage": coverage,
        "usage": {model: usage.as_dict() for model, usage in attempt.usage.items()},
        "costUsd": attempt.cost.usd,
        "costDetail": attempt.cost.as_dict(),
        "durationS": round(attempt.duration_s, 3),
        "retries": retries,
        "analystRetries": analyst_retries,
        # Part of the panel reported and part did not: the answer is thinner than
        # it claims. A run that produced nothing at all is reported as failed.
        "degraded": bool(coverage["degraded"]),
        "failure": attempt.failure,
        "budgetState": post,
        "staleness": staleness,
        "externalEvidence": external_evidence.get("meta") if external_evidence else None,
        "result": attempt.result,
        "ledger": entry,
        "reason": (attempt.failure or {}).get("message") if attempt.failure else None,
    }
