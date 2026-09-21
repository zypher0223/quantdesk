"""Data structures for a CPA phase series.

One closed bar produces at most one *primary* phase, plus the candidate states that
were true at that bar. `status` distinguishes an observation from a confirmation:
`reversal_extension` and `exhaustion_extension` are observations and never place an
order on their own, which is why the field exists at all.

Every record carries the parameter version and the reasons that produced it. A phase
without its reasons would be an opinion; with them it is a checkable claim.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class Pivot:
    """A structure level computed strictly from bars *before* the bar it is used on."""

    kind: str  # high | low
    price: float
    window: int
    index: int  # bar index the level came from

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HigherTimeframeView:
    """What one higher interval had *closed* at the moment of the base bar."""

    interval: str = ""
    phase: str = "none"
    trend: str = "unknown"  # bullish | bearish | sideways | unknown
    closed_at: int = 0
    available: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "interval": self.interval,
            "phase": self.phase,
            "trend": self.trend,
            "closedAt": self.closed_at,
            "available": self.available,
            "reason": self.reason,
        }


@dataclass
class PhaseRecord:
    """One bar's phase reading, in the shape the report specified."""

    time: int
    phase: str = "none"
    status: str = "none"  # none | candidate | confirmed
    direction: str = "neutral"
    confidence: float = 0.0
    # Levels a trade would be managed against. `None` means "not established yet".
    pivot_price: float | None = None
    invalidation_price: float | None = None
    setup_low: float | None = None
    ema_fast: float | None = None
    ema_slow: float | None = None
    distance_atr: float | None = None
    volume_ratio: float | None = None
    contraction_score: float | None = None
    atr: float | None = None
    higher: HigherTimeframeView | None = None
    background: HigherTimeframeView | None = None
    cycle: str = "none"  # upside | downside | none
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    parameter_version: str = ""
    # Which of the report's rules this bar satisfied, as data rather than prose.
    checks: dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "time": int(self.time),
            "phase": self.phase,
            "status": self.status,
            "direction": self.direction,
            "confidence": round(float(self.confidence), 4),
            "pivotPrice": self.pivot_price,
            "invalidationPrice": self.invalidation_price,
            "setupLow": self.setup_low,
            "ema10": self.ema_fast,
            "ema20": self.ema_slow,
            "distanceAtr": None if self.distance_atr is None else round(self.distance_atr, 4),
            "volumeRatio": None if self.volume_ratio is None else round(self.volume_ratio, 4),
            "contractionScore": (
                None if self.contraction_score is None else round(self.contraction_score, 4)
            ),
            "atr": None if self.atr is None else round(self.atr, 6),
            "cycle": self.cycle,
            "higherTimeframe": (self.higher or HigherTimeframeView()).as_dict(),
            "backgroundTimeframe": (self.background or HigherTimeframeView()).as_dict(),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "checks": dict(self.checks),
            "parameterVersion": self.parameter_version,
        }


@dataclass
class PhaseSeries:
    """The whole reading for one symbol and interval."""

    symbol: str
    display_symbol: str
    interval: str
    product_type: str
    records: list[PhaseRecord] = field(default_factory=list)
    snapshot_hash: str = ""
    data_version: str = ""
    parameter_version: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    higher_intervals: tuple[str, str] = ("", "")
    warnings: list[str] = field(default_factory=list)
    insufficient: bool = False
    insufficient_reason: str = ""

    def current(self) -> PhaseRecord | None:
        return self.records[-1] if self.records else None

    def runs(self) -> dict[str, int]:
        """How many *episodes* each phase occurred in, not how many bars it lasted.

        A trending market re-confirms the same phase on every bar it remains true -
        measured: 132 bars of `wedge_drop` in one 800-bar window, which is one
        episode, not 132 events. `counts` keeps the per-bar figure (the chart draws
        from those bars), and this gives the figure a reader would quote.
        """
        out: dict[str, int] = {}
        previous: str | None = None
        for record in self.records:
            active = record.status != "none" and record.phase != "none"
            if active and record.phase != previous:
                key = record.phase if record.status == "confirmed" else f"{record.phase}（观察）"
                out[key] = out.get(key, 0) + 1
            previous = record.phase if active else None
        return dict(sorted(out.items(), key=lambda item: -item[1]))

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for record in self.records:
            if record.status == "none":
                continue
            key = record.phase if record.status == "confirmed" else f"{record.phase}（观察）"
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items(), key=lambda item: -item[1]))

    def as_dict(self, *, limit: int | None = None) -> dict[str, Any]:
        records: Iterable[PhaseRecord] = self.records
        if limit is not None:
            records = list(self.records)[-int(limit):]
        return {
            "symbol": self.symbol,
            "displaySymbol": self.display_symbol,
            "interval": self.interval,
            "productType": self.product_type,
            "snapshotHash": self.snapshot_hash,
            "dataVersion": self.data_version,
            "parameterVersion": self.parameter_version,
            "parameters": dict(self.parameters),
            "higherIntervals": {
                "management": self.higher_intervals[0],
                "background": self.higher_intervals[1],
            },
            "bars": len(self.records),
            "insufficient": self.insufficient,
            "insufficientReason": self.insufficient_reason,
            "counts": self.counts(),
            "phaseRuns": self.runs(),
            "current": (self.current().as_dict() if self.current() else None),
            "records": [record.as_dict() for record in records],
            "warnings": list(self.warnings),
            "attribution": (
                "概念来源：Oliver Kell 公开描述的 Cycle of Price Action；本实现为 "
                "QuantDesk 规则化适配版，阈值均为本仓库研究参数，非作者原始规则。"
            ),
        }
