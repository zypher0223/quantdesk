"""The cycle state machine: sequence, repetition, skips and reversal.

The detector says what one bar *looks like*. This module decides whether that reading
is allowed to become the cycle's current phase, which is where the discipline lives:

* an upside cycle starts with a `wedge_pop`, and `ema_crossback` / `base_n_break` may
  only repeat *inside* an upside cycle - they cannot bootstrap one, so "price touched
  the EMA" alone never opens a position;
* a `reversal_extension` is recorded but never becomes the cycle's phase: it is the
  observation that precedes a wedge pop;
* `exhaustion_extension` marks the trend late and blocks new entries without closing
  the cycle by itself;
* a cycle only ends through a confirmed `wedge_drop` (upside) or a confirmed bullish
  `wedge_pop` (downside), which is what stops a single EMA cross from flipping sides;
* skips are allowed and recorded: a cycle may jump from `wedge_pop` straight to
  `base_n_break`, and the record says so rather than inventing the missing phase.

The machine keeps no state beyond what it returns, so a phase series is a pure
function of the bars and the parameters - the property the truncation test relies on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from . import indicators as ind
from .defaults import PARAMETER_VERSION
from .detector import evaluate
from .models import PhaseRecord

# Which confirmed phases may start or continue a cycle. Anything not listed cannot.
UPSIDE_CYCLE_PHASES = ("wedge_pop", "ema_crossback", "base_n_break")
DOWNSIDE_CYCLE_PHASES = ("downside_ema_crossback", "downside_base_n_break")


@dataclass
class CycleState:
    cycle: str = "none"  # none | upside | downside
    phase: str = "none"  # last confirmed phase
    entries: int = 0  # confirmed bullish entries seen in this cycle
    late: bool = False  # an exhaustion observation is in force
    upside_anchor_low: float | None = None
    downside_anchor_high: float | None = None
    history: list[str] = field(default_factory=list)


def run(
    bars: Sequence[dict],
    parameters: dict[str, Any],
    *,
    parameter_version: str = PARAMETER_VERSION,
) -> list[PhaseRecord]:
    """The full phase series for one interval. Deterministic and replayable."""
    # Indicators take price series, not bars: the window arithmetic stays in one place.
    closes = [float(bar["close"]) for bar in bars]
    ema_fast = ind.ema(closes, int(parameters["emaFast"]))
    ema_slow = ind.ema(closes, int(parameters["emaSlow"]))
    long_sma = ind.sma(closes, int(parameters["longSma"]))
    atr_values = ind.atr(bars, int(parameters.get("atrPeriod") or 14))
    pivot_lookback = int(parameters["pivotLookback"])

    state = CycleState()
    records: list[PhaseRecord] = []
    for index in range(len(bars)):
        record = evaluate(
            bars, index, parameters,
            cycle=state.cycle,
            upside_anchor_low=state.upside_anchor_low,
            downside_anchor_high=state.downside_anchor_high,
            ema_fast=ema_fast, ema_slow=ema_slow, long_sma=long_sma,
            atr_values=atr_values, parameter_version=parameter_version,
        )
        # The cycle's structure levels come from the bars before this one, so a level
        # used at bar `i` can never be the level bar `i` created.
        low = ind.prior_low(bars, index, pivot_lookback)
        high = ind.prior_high(bars, index, pivot_lookback)
        # The cycle follows what the *detector* recognised. Whether an entry is
        # allowed is a signal-layer decision, and letting it drive the cycle meant a
        # campaign configured for `ema_crossback` never recognised the wedge pop that
        # starts the cycle - so the crossback it was waiting for could never confirm.
        detected = record.status == "confirmed"
        advances_cycle = _apply(record, state, entry_stages=_entry_stages(parameters))
        if detected and advances_cycle:
            if record.phase in UPSIDE_CYCLE_PHASES:
                if state.cycle != "upside":
                    state.cycle = "upside"
                    state.upside_anchor_low = low
                elif low is not None and (
                    state.upside_anchor_low is None or low > state.upside_anchor_low
                ):
                    # The structure low trails up with the cycle: a crossback is only
                    # valid while the most recent swing low holds.
                    state.upside_anchor_low = low
                state.entries += 1
                state.late = False
            elif record.phase in DOWNSIDE_CYCLE_PHASES:
                state.cycle = "downside"
                if high is not None:
                    state.downside_anchor_high = high
                state.late = False
            elif record.phase == "wedge_drop":
                state.cycle = "downside"
                state.phase = "wedge_drop"
                state.upside_anchor_low = None
                state.late = False
            state.phase = record.phase
            state.history.append(record.phase)
        elif record.phase == "exhaustion_extension":
            state.late = True
        record.cycle = state.cycle
        records.append(record)
    return records


def _entry_stages(parameters: dict[str, Any]) -> tuple[str, ...]:
    raw = parameters.get("entryStages") or ["wedge_pop", "ema_crossback", "base_n_break"]
    if isinstance(raw, str):
        raw = [item.strip() for item in raw.split(",") if item.strip()]
    return tuple(raw)


def _apply(record: PhaseRecord, state: CycleState, *, entry_stages: tuple[str, ...]) -> bool:
    """Downgrade a reading and return whether it may advance cycle state.

    A phase excluded only by ``entryStages`` still describes market structure and may
    start the cycle. Invalid sequence and late-cycle add-ons must not advance state.
    """
    if record.status != "confirmed":
        return False
    allowed: tuple[str, ...] = UPSIDE_CYCLE_PHASES + DOWNSIDE_CYCLE_PHASES + ("wedge_drop",)
    if record.phase not in allowed:
        return False
    if record.phase in ("ema_crossback", "base_n_break") and state.cycle != "upside":
        record.status = "candidate"
        record.warnings.append(
            f"{record.phase} 只能出现在已确认的上行周期内；当前周期为 {state.cycle}，"
            "因此仅作为候选记录，不产生入场"
        )
        return False
    if record.phase in DOWNSIDE_CYCLE_PHASES and state.cycle != "downside":
        record.status = "candidate"
        record.warnings.append(
            f"{record.phase} 需要已确认的下行周期；当前周期为 {state.cycle}，仅作为候选记录"
        )
        return False
    if (
        record.phase == "wedge_drop"
        and state.cycle != "upside"
        and not record.checks.get("priorUpsideContext")
    ):
        # The antecedent is "an upside cycle, or price structure that was above the
        # slow EMA recently". The detector establishes the second one on the bar
        # itself; the state machine only has to accept it, or a market that tops out
        # without a confirmed wedge pop could never start a downside cycle.
        record.status = "candidate"
        record.warnings.append(
            "wedge_drop 需要此前处于上行周期或均线上方的顶部结构，或延伸衰竭；当前不满足"
        )
        return False
    if record.phase in ("ema_crossback", "base_n_break") and state.late:
        record.status = "candidate"
        record.warnings.append("已出现延伸衰竭观察：该阶段只记为加仓候选，不新开仓")
        return False
    if (
        record.phase in UPSIDE_CYCLE_PHASES
        and record.phase not in entry_stages
        and state.entries == 0
    ):
        # `entryStages` selects which *bullish entries* may open a position. It must
        # not touch an exit (a wedge drop still has to close a long) and it must not
        # touch the downside phases, which `sideMode` governs instead. Applying it to
        # everything silently turned every exit into an observation.
        record.status = "candidate"
        record.warnings.append(
            f"入场阶段配置未包含 {record.phase}，因此不产生首仓入场（不影响离场与下行阶段）"
        )
    return True
