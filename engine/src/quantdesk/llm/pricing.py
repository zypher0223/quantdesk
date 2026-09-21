"""What a model call costs, and how a run's spend is accounted.

A budget feature that does not know the unit price is theatre, so the prices here
are the ones the provider publishes, with the time-of-day rule they publish it
under. DeepSeek bills input twice over - a cache hit costs a fraction of a miss -
which is exactly the split a multi-agent run lives or dies by, so the three token
classes are priced separately rather than averaged.

Rates are per one million tokens. Rates also live in the operator's config, so a
price change is a config edit rather than a code change; each ledger entry records
the rates it was charged at, so an old entry stays explainable after a change.

Source: https://api-docs.deepseek.com/quick_start/pricing (read 2026-09-15).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any

# DeepSeek's peak window: 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday. Every
# other hour is off-peak and costs half as much.
PEAK_WINDOWS_UTC = ((1, 4), (6, 10))
USD_PER_MILLION = 1_000_000.0


@dataclass(frozen=True)
class ModelRates:
    """USD per million tokens, split by the class the provider bills on."""

    cache_hit: float
    cache_miss: float
    output: float

    def peak(self) -> "ModelRates":
        return self

    def off_peak(self) -> "ModelRates":
        return ModelRates(
            cache_hit=self.cache_hit / 2,
            cache_miss=self.cache_miss / 2,
            output=self.output / 2,
        )

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


# Published peak rates. Off-peak is half, applied by `rates_for`.
PUBLISHED_RATES: dict[tuple[str, str], ModelRates] = {
    ("deepseek", "deepseek-v4-pro"): ModelRates(cache_hit=0.044, cache_miss=1.32, output=3.96),
    ("deepseek", "deepseek-flash"): ModelRates(cache_hit=0.006, cache_miss=0.30, output=1.20),
    # Legacy names still served by the same models at the same price.
    ("deepseek", "deepseek-v4-flash"): ModelRates(cache_hit=0.006, cache_miss=0.30, output=1.20),
    ("deepseek", "deepseek-chat"): ModelRates(cache_hit=0.044, cache_miss=1.32, output=3.96),
    ("deepseek", "deepseek-reasoner"): ModelRates(cache_hit=0.044, cache_miss=1.32, output=3.96),
}
# Rates the provider does not publish stay unpriced, and unpriced means "unknown",
# never "free". A model that is not in this table and not in config reports its
# cost as null and says so.
UNPRICED = None


def is_peak(moment: dt.datetime | None = None) -> bool:
    """Is this instant inside the provider's peak billing window?"""
    now = moment or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    else:
        now = now.astimezone(dt.timezone.utc)
    if now.weekday() >= 5:
        return False
    for start, end in PEAK_WINDOWS_UTC:
        if start <= now.hour < end:
            return True
    return False


def rates_for(
    provider: str,
    model: str,
    *,
    at: dt.datetime | None = None,
    overrides: dict[str, dict[str, float]] | None = None,
) -> ModelRates | None:
    """The rates to charge one call at, or None when the model is unpriced.

    `overrides` is the operator's config: `{"deepseek:deepseek-v4-pro": {...}}`.
    """
    key = f"{provider.lower()}:{model}"
    if overrides and key in overrides:
        entry = overrides[key]
        try:
            custom = ModelRates(
                cache_hit=float(entry.get("cache_hit", 0.0)),
                cache_miss=float(entry.get("cache_miss", 0.0)),
                output=float(entry.get("output", 0.0)),
            )
        except (TypeError, ValueError):
            custom = None
        if custom is not None:
            return custom
    base = PUBLISHED_RATES.get((provider.lower(), model))
    if base is None:
        return UNPRICED
    return base if is_peak(at) else base.off_peak()


@dataclass
class TokenUsage:
    """Tokens one call (or one whole run) consumed, in the classes billed."""

    cache_hit: int = 0
    cache_miss: int = 0
    output: int = 0
    calls: int = 0

    @property
    def input_total(self) -> int:
        return self.cache_hit + self.cache_miss

    @property
    def total(self) -> int:
        return self.input_total + self.output

    def add(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            cache_hit=self.cache_hit + other.cache_hit,
            cache_miss=self.cache_miss + other.cache_miss,
            output=self.output + other.output,
            calls=self.calls + other.calls,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "cacheHit": self.cache_hit,
            "cacheMiss": self.cache_miss,
            "inputTotal": self.input_total,
            "output": self.output,
            "total": self.total,
            "calls": self.calls,
        }


@dataclass
class CostBreakdown:
    """What a usage cost, per model, with the rates that produced it."""

    usd: float | None = None
    models: list[dict[str, Any]] = field(default_factory=list)
    unpriced: list[str] = field(default_factory=list)
    peak: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def price_usage(
    usage_by_model: dict[str, TokenUsage],
    *,
    provider: str,
    at: dt.datetime | None = None,
    overrides: dict[str, dict[str, float]] | None = None,
) -> CostBreakdown:
    """Price a run's tokens, model by model.

    A run may use a reasoning model for the deep passes and a cheap one for the
    quick ones, and the two are billed differently, so the breakdown keeps them
    apart instead of applying one rate to the total.
    """
    breakdown = CostBreakdown(peak=is_peak(at))
    if not usage_by_model:
        # A run that never reached a model spent exactly nothing, and zero is a
        # known cost. Reporting it as unknown would make an unrelated failure -
        # a missing import, a refused credential - look like an unpriced model
        # and would spend the per-run budget verdict on money nobody spent.
        breakdown.usd = 0.0
        return breakdown
    total = 0.0
    priced_any = False
    for model, usage in sorted(usage_by_model.items()):
        rates = rates_for(provider, model, at=at, overrides=overrides)
        entry = {"model": model, "usage": usage.as_dict()}
        if rates is None:
            entry["usd"] = None
            entry["rates"] = None
            breakdown.unpriced.append(model)
        else:
            cost = (
                usage.cache_hit * rates.cache_hit
                + usage.cache_miss * rates.cache_miss
                + usage.output * rates.output
            ) / USD_PER_MILLION
            total += cost
            priced_any = True
            entry["usd"] = round(cost, 6)
            entry["rates"] = rates.as_dict()
        breakdown.models.append(entry)
    breakdown.usd = round(total, 6) if priced_any else None
    return breakdown


def budget_state(
    *,
    spent_today_usd: float | None,
    spent_run_usd: float | None,
    daily_limit_usd: float | None,
    per_run_limit_usd: float | None,
) -> dict[str, Any]:
    """Where a run stands against its limits, and whether it may proceed.

    An unpriced run is refused when a limit is configured, because a limit that
    cannot be enforced is worse than no limit: it reads as protection.
    """
    problems: list[str] = []
    if daily_limit_usd is not None:
        if spent_today_usd is None:
            problems.append("已配置每日上限，但当日开销不可知（模型未定价）")
        elif spent_today_usd >= daily_limit_usd:
            problems.append(f"当日开销 ${spent_today_usd:.4f} 已达上限 ${daily_limit_usd:.4f}")
    if per_run_limit_usd is not None:
        if spent_run_usd is None:
            problems.append("已配置单次上限，但本次开销不可知（模型未定价）")
        elif spent_run_usd > per_run_limit_usd:
            problems.append(f"本次开销 ${spent_run_usd:.4f} 超过单次上限 ${per_run_limit_usd:.4f}")
    return {
        "allowed": not problems,
        "problems": problems,
        "dailyLimitUsd": daily_limit_usd,
        "perRunLimitUsd": per_run_limit_usd,
        "spentTodayUsd": None if spent_today_usd is None else round(spent_today_usd, 6),
        "spentRunUsd": None if spent_run_usd is None else round(spent_run_usd, 6),
        "remainingTodayUsd": (
            None if daily_limit_usd is None or spent_today_usd is None
            else round(max(0.0, daily_limit_usd - spent_today_usd), 6)
        ),
    }


def usage_from_payload(payload: dict[str, Any]) -> TokenUsage:
    """Read one provider usage block into the three billed classes.

    Providers spell the cache counters differently (`prompt_cache_hit_tokens` on
    DeepSeek, `prompt_tokens_details.cached_tokens` on OpenAI), and a missing
    counter means the tokens were billed as misses rather than as free.
    """
    if not isinstance(payload, dict):
        return TokenUsage()
    prompt = _int(payload.get("prompt_tokens") or payload.get("input_tokens"))
    completion = _int(payload.get("completion_tokens") or payload.get("output_tokens"))
    hit = _int(payload.get("prompt_cache_hit_tokens"))
    if hit is None:
        for key in ("prompt_tokens_details", "input_token_details"):
            details = payload.get(key)
            if not isinstance(details, dict):
                continue
            for nested in ("cached_tokens", "cache_read"):
                candidate = _int(details.get(nested))
                if candidate is not None:
                    hit = candidate
                    break
            if hit is not None:
                break
    hit = hit or 0
    miss = _int(payload.get("prompt_cache_miss_tokens"))
    if miss is None:
        miss = max(0, (prompt or 0) - hit)
    return TokenUsage(cache_hit=hit, cache_miss=miss, output=completion or 0, calls=1)


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
