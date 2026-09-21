"""Single-interval phase detection: the rule set, evaluated bar by bar.

`evaluate` looks at exactly one bar with the state the machine had *before* it, and
returns what that bar showed. It never decides what to do about it - the state machine
handles sequence, repetition and reversal, and the signal layer maps confirmed phases
to events. Splitting it that way is what makes each rule testable on its own.

Rules implemented, with the thresholds taken from `defaults` (never hard-coded here):

* **reversal extension** (observation): price below both EMAs by more than
  `extensionAtr` ATR, with a volume or a wick/reversal bar, while the cycle is down.
* **wedge pop** (confirmation): a contraction in range and EMA distance, then a close
  back above both EMAs and above the *contraction window's* pivot, on `volumeConfirm`
  volume. The breakout level is the wedge's own high, not the `pivotLookback` high:
  see the note where the two are computed.
* **ema crossback** (confirmation): an upside cycle already in force, fast EMA above
  slow, price returning to the EMA zone within `crossbackToleranceAtr`, structure low
  intact, and a supportive close.
* **base n' break** (confirmation): an upside cycle, a contraction, and a close above
  the prior pivot (optionally on expanding volume).
* **exhaustion extension** (observation): price stretched by `exhaustionAtr`, with an
  expansion or a rejection wick.
* **wedge drop** (confirmation): an upside cycle, a break of structure with the fast
  EMA turning down, on volume - never from a single EMA cross.
* **downside ema crossback / downside base n' break**: the bearish mirror, with their
  own volume threshold because downside volume structure is not symmetric.
"""

from __future__ import annotations

from typing import Any, Sequence

from . import indicators as ind
from .models import HigherTimeframeView, Pivot, PhaseRecord


def _bar(bars: Sequence[dict], index: int) -> dict:
    return bars[index]


def _wedge_below_slow_ema(bars: Sequence[dict], index: int, window: int,
                          ema_slow: Sequence[float | None]) -> bool:
    """Did the wedge window sit below the slow EMA?

    The mean close of the contraction window against the mean slow EMA over the same
    window. Both windows end before `index`, so the reading cannot see the pop bar.
    """
    start = max(0, index - window)
    if start >= index:
        return False
    closes = [float(bars[position]["close"]) for position in range(start, index)]
    slows = [ema_slow[position] for position in range(start, index)]
    if not closes or any(value is None for value in slows):
        return False
    return sum(closes) / len(closes) < sum(slows) / len(slows)  # type: ignore[arg-type]


def _close(bars: Sequence[dict], index: int) -> float:
    return float(bars[index]["close"])


def evaluate(
    bars: Sequence[dict],
    index: int,
    parameters: dict[str, Any],
    *,
    cycle: str,
    upside_anchor_low: float | None,
    downside_anchor_high: float | None,
    ema_fast: Sequence[float | None],
    ema_slow: Sequence[float | None],
    long_sma: Sequence[float | None],
    atr_values: Sequence[float | None],
    parameter_version: str,
) -> PhaseRecord:
    """The phase reading for one bar. Pure: same inputs, same record.

    Rule order is part of the semantics: **structural confirmations are evaluated
    before observations**. A sharp breakdown is both "price stretched below the EMAs"
    and "a wedge dropped"; whichever is checked first wins the bar, and the structure
    is the stronger evidence. The first version checked reversal extension first and
    every downside break was swallowed by an observation that places no order.
    """
    bar = _bar(bars, index)
    close = _close(bars, index)
    fast, slow = ema_fast[index], ema_slow[index]
    atr_value = atr_values[index]
    record = PhaseRecord(
        time=int(bar["ts"]),
        ema_fast=None if fast is None else round(fast, 6),
        ema_slow=None if slow is None else round(slow, 6),
        atr=None if atr_value is None else round(atr_value, 6),
        cycle=cycle,
        parameter_version=parameter_version,
    )
    if fast is None or slow is None or atr_value in (None, 0):
        record.warnings.append("指标尚未预热完成")
        return record

    window = int(parameters["contractionWindow"])
    lookback = int(parameters["pivotLookback"])
    volume_window = int(parameters["volumeWindow"])
    volume_confirm = float(parameters["volumeConfirm"])
    downside_volume = float(parameters["downsideVolumeConfirm"])
    extension_atr = float(parameters["extensionAtr"])
    exhaustion_atr = float(parameters["exhaustionAtr"])
    tolerance = float(parameters["crossbackToleranceAtr"])
    contraction_threshold = float(parameters["contractionThreshold"])

    ratio = ind.volume_ratio(bars, index, volume_window)
    contraction = ind.contraction_score(bars, index, window)
    gap = ind.ema_gap_ratio(ema_fast, ema_slow, index, window)
    pivot_high = ind.prior_high(bars, index, lookback)
    pivot_low = ind.prior_low(bars, index, lookback)
    # The wedge's own levels: the high/low of the contraction window. A pop breaks out
    # of the contraction it formed in, not of the pre-contraction expansion - the
    # `pivotLookback` high spans both windows, so on real bars it sits above the whole
    # decline and "close above it" stopped describing a pop at all. Measured on BTC 1d
    # over 2000 bars: 35 contraction readings, of which 1 closed above the 20-bar pivot
    # and 0 satisfied every pop condition, so the bullish cycle could never start and
    # the three bullish entry phases were unreachable. Scoped to the pop on purpose:
    # `base_n_break` still uses the wider pivot, where a new 20-bar high is the point.
    wedge_pivot_high = ind.prior_high(bars, index, window)
    wedge_pivot_low = ind.prior_low(bars, index, window)
    distance = ind.distance_in_atr(close, fast, atr_value)
    upper_wick = ind.upper_wick_ratio(bar)
    lower_wick = ind.lower_wick_ratio(bar)
    previous_close = float(bars[index - 1]["close"]) if index else close
    prior_slow = ema_slow[index - 1] if index else None

    record.volume_ratio = ratio
    record.contraction_score = contraction
    record.distance_atr = distance

    # ---- shared facts, recorded so the UI and the tests read the same numbers
    record.checks.update(
        {
            "belowBothEma": close < fast and close < slow,
            "aboveBothEma": close > fast and close > slow,
            "fastAboveSlow": fast > slow,
            "fastRising": index > 0 and ema_fast[index - 1] is not None and fast > ema_fast[index - 1],
            "fastFalling": index > 0 and ema_fast[index - 1] is not None and fast < ema_fast[index - 1],
            "contracted": bool(contraction is not None and contraction >= contraction_threshold),
            "volumeExpanded": bool(ratio is not None and ratio >= volume_confirm),
            "volumeExpandedDownside": bool(ratio is not None and ratio >= downside_volume),
            "abovePivot": bool(pivot_high is not None and close > pivot_high),
            "aboveWedgePivot": bool(
                wedge_pivot_high is not None and close > wedge_pivot_high
            ),
            # A wedge forms *below* the averages and the pop reclaims them; a platform
            # that breaks out inside an uptrend is a base n' break. Without this the
            # scoped pivot let the pop branch claim every contraction breakout in an
            # existing uptrend - the sequence test caught it as a `wedge_pop` appearing
            # where `base_n_break` belongs. Measured over the wedge window rather than
            # on the single bar before this one: a pop's first reclaim bar often is the
            # bar before the confirmation, and "the last bar closed below the average"
            # threw those away (BTC 1d: 4 pops became 1).
            "wedgeBelowSlowEma": _wedge_below_slow_ema(bars, index, window, ema_slow),
            "belowPivot": bool(pivot_low is not None and close < pivot_low),
            "reversalBar": close > previous_close,
            "downBar": close < previous_close,
            "upperRejection": bool(upper_wick is not None and upper_wick >= 0.4),
            "lowerRejection": bool(lower_wick is not None and lower_wick >= 0.4),
            "emaGapNarrow": bool(gap is not None and gap >= contraction_threshold),
        }
    )

    if pivot_high is not None:
        record.pivot_price = round(pivot_high, 6)
    record.setup_low = None if pivot_low is None else round(pivot_low, 6)
    record.invalidation_price = (
        round(pivot_low, 6) if pivot_low is not None else None
    )

    # ---- 2. wedge pop (confirmation)
    if (
        record.checks["contracted"]
        and record.checks["emaGapNarrow"]
        and close > fast
        and close > slow
        and record.checks["aboveWedgePivot"]
        and record.checks["volumeExpanded"]
        and record.checks["wedgeBelowSlowEma"]
        # A wedge pop *starts* an upside cycle - it is the reversal that reclaims the
        # averages. The same shape inside an existing upside cycle is a continuation
        # platform, and belongs to `base_n_break` (which is checked after this rule, so
        # an over-broad pop condition silently steals its bars). The state machine's
        # contract is the same: "an upside cycle starts with a wedge_pop".
        and cycle in ("downside", "none")
    ):
        record.phase = "wedge_pop"
        record.status = "confirmed"
        record.direction = "bullish"
        # The pop's own levels are the contraction's, and they are set here rather than
        # by the shared block above: the level a pop breaks is the contraction high, and
        # the level that invalidates it is the contraction low (the wedge's low).
        record.pivot_price = round(wedge_pivot_high, 6)
        record.invalidation_price = (
            None if wedge_pivot_low is None else round(wedge_pivot_low, 6)
        )
        record.setup_low = record.invalidation_price
        record.confidence = _confidence(
            [record.checks["contracted"], record.checks["emaGapNarrow"],
             record.checks["aboveWedgePivot"], record.checks["volumeExpanded"]]
        )
        record.reasons = _reasons(
            "价格重新站上 EMA10/EMA20",
            f"突破收缩区枢轴 {wedge_pivot_high:.4f}（{window} 根收缩窗口的高点）",
            f"成交量达到过去 {volume_window} 根均量的 {ratio:.2f} 倍（阈值 {volume_confirm}）",
            f"振幅收缩评分 {contraction:.2f}（阈值 {contraction_threshold}）",
        )
        return record

    # ---- 4. ema crossback (confirmation; first entry or an add-on candidate)
    if (
        cycle == "upside"
        and record.checks["fastAboveSlow"]
        and (record.checks["fastRising"] or (long_sma[index] is not None and close > long_sma[index]))
        and distance is not None
        and abs(distance) <= tolerance
        and (upside_anchor_low is None or close > upside_anchor_low)
        and (record.checks["reversalBar"] or close >= fast)
    ):
        record.phase = "ema_crossback"
        record.status = "confirmed"
        record.direction = "bullish"
        record.confidence = _confidence(
            [record.checks["fastAboveSlow"], abs(distance) <= tolerance,
             upside_anchor_low is None or close > upside_anchor_low, record.checks["reversalBar"]]
        )
        record.reasons = _reasons(
            "上行周期已确认，价格回踩 EMA 区域",
            f"距快线 {abs(distance):.2f} 个 ATR（容差 {tolerance}）",
            "结构低点未被跌破" if upside_anchor_low is None or close > upside_anchor_low
            else "结构低点已跌破",
            "收盘重新站稳" if record.checks["reversalBar"] else "收盘仍在均线上方",
        )
        return record

    # ---- 5. base n' break (confirmation)
    if (
        cycle == "upside"
        and record.checks["contracted"]
        and record.checks["abovePivot"]
        and close > slow
        and (record.checks["volumeExpanded"] or ratio is None)
    ):
        record.phase = "base_n_break"
        record.status = "confirmed"
        record.direction = "bullish"
        record.confidence = _confidence(
            [record.checks["contracted"], record.checks["abovePivot"], close > slow]
        )
        record.reasons = _reasons(
            "上行周期中的收缩平台",
            f"收盘突破此前枢轴 {pivot_high:.4f}",
            "成交量扩张" if record.checks["volumeExpanded"] else "成交量数据不足，仅按价格确认",
        )
        return record

    # ---- 6. wedge drop (confirmation; exit)
    #
    # The antecedent is "an upside cycle or an exhaustion reading". A confirmed upside
    # phase is one way to know that; price having traded above the slow EMA inside the
    # pivot window is the other, and without it a market that tops out before ever
    # printing a wedge pop could never start a downside cycle at all.
    # "Was there really an uptrend before this?" - price must have been *stretched*
    # above the slow EMA, not merely above it. The loose version (close above the slow
    # EMA at some point) confirmed 50 wedge drops in a flat market, because an
    # oscillating price sits above its average half the time. A trend antecedent has to
    # mean the market actually extended.
    prior_upside_context = any(
        (ind.distance_in_atr(float(bars[position]["close"]), ema_slow[position],
                             atr_values[position]) or 0.0) >= extension_atr
        for position in range(max(0, index - lookback * 2), index)
    )
    record.checks["priorUpsideContext"] = bool(prior_upside_context)
    if (
        (cycle == "upside" or (cycle == "none" and prior_upside_context))
        and (record.checks["belowPivot"] or close < slow)
        and (record.checks["fastFalling"] or not record.checks["fastAboveSlow"])
        and (record.checks["volumeExpandedDownside"] or record.checks["downBar"])
        and (downside_anchor_high is None or close < downside_anchor_high)
    ):
        record.phase = "wedge_drop"
        record.status = "confirmed"
        record.direction = "bearish"
        # Bearish structure breaks the prior low and is invalidated above the prior
        # high. Keeping those semantics direction-aware prevents a short stop from
        # being placed below its entry.
        record.pivot_price = None if pivot_low is None else round(pivot_low, 6)
        record.invalidation_price = None if pivot_high is None else round(pivot_high, 6)
        record.setup_low = None
        record.confidence = _confidence(
            [record.checks["belowPivot"], record.checks["fastFalling"],
             record.checks["volumeExpandedDownside"]]
        )
        record.reasons = _reasons(
            "上行周期后价格跌破均线附近结构" if cycle == "upside"
            else "此前价格结构处于均线上方（未形成已确认的上行阶段，但有顶部结构）",
            f"跌破局部结构低点 {pivot_low:.4f}" if pivot_low is not None else "收盘跌破慢线",
            "快线转弱",
            f"成交量 {ratio:.2f} 倍"
            if ratio is not None and record.checks["volumeExpandedDownside"]
            else "反弹失败（收盘走低）",
        )
        return record

    # ---- 7. downside phases (only meaningful when the caller allows shorting)
    if cycle == "downside" and record.checks["belowBothEma"]:
        if (
            distance is not None
            and abs(distance) <= tolerance
            and not record.checks["fastAboveSlow"]
            and (record.checks["downBar"] or close <= slow)
        ):
            record.phase = "downside_ema_crossback"
            record.status = "confirmed"
            record.direction = "bearish"
            record.pivot_price = None if pivot_low is None else round(pivot_low, 6)
            record.invalidation_price = None if pivot_high is None else round(pivot_high, 6)
            record.setup_low = None
            record.confidence = _confidence(
                [not record.checks["fastAboveSlow"], abs(distance) <= tolerance,
                 record.checks["downBar"]]
            )
            record.reasons = _reasons(
                "下行周期中价格反抽 EMA 区域",
                f"距快线 {abs(distance):.2f} 个 ATR（容差 {tolerance}）",
                "快线仍在慢线下方",
            )
            return record
        if (
            record.checks["contracted"]
            and pivot_low is not None
            and close < pivot_low
            and (record.checks["volumeExpandedDownside"] or ratio is None)
        ):
            record.phase = "downside_base_n_break"
            record.status = "confirmed"
            record.direction = "bearish"
            record.pivot_price = round(pivot_low, 6)
            record.invalidation_price = None if pivot_high is None else round(pivot_high, 6)
            record.setup_low = None
            record.confidence = _confidence(
                [record.checks["contracted"], close < pivot_low,
                 record.checks["volumeExpandedDownside"]]
            )
            record.reasons = _reasons(
                "下行周期中的收缩平台",
                f"收盘跌破此前枢轴 {pivot_low:.4f}",
                f"下行放量阈值 {downside_volume}（与上行不对称）",
            )
            return record

    # ---- 7. reversal extension (observation only; evaluated last on purpose)
    if (
        close < fast
        and close < slow
        and distance is not None
        and distance <= -extension_atr
        and (record.checks["volumeExpanded"] or record.checks["lowerRejection"]
             or record.checks["reversalBar"])
        and cycle in ("downside", "none")
    ):
        record.phase = "reversal_extension"
        record.status = "candidate"
        record.direction = "bullish"
        record.confidence = _confidence(
            [distance <= -extension_atr, record.checks["volumeExpanded"],
             record.checks["lowerRejection"], cycle == "downside"]
        )
        record.reasons = _reasons(
            f"收盘低于两条均线 {abs(distance):.2f} 个 ATR（阈值 {extension_atr}）",
            "出现放量或长下影或收盘反转" if record.checks["volumeExpanded"]
            else ("出现长下影" if record.checks["lowerRejection"] else "收盘高于前一根"),
            f"当前周期状态：{_cycle_label(cycle)}",
        )
        record.warnings.append("观察阶段：不直接入场，等待确认阶段")
        return record

    # ---- 8. exhaustion extension (observation; blocks new entries)
    #
    # It requires an upside cycle that already exists: "exhaustion" is a late-trend
    # reading, and without this the rule stole the breakout bar itself - the first bar
    # of a wedge pop is, by construction, also far from the fast EMA.
    if (
        cycle == "upside"
        and distance is not None
        and distance >= exhaustion_atr
        and (record.checks["upperRejection"] or not record.checks["reversalBar"]
             or (record.checks["volumeExpanded"] and contraction is not None
                 and contraction < contraction_threshold))
    ):
        record.phase = "exhaustion_extension"
        record.status = "candidate"
        record.direction = "bullish"
        record.confidence = _confidence(
            [distance >= exhaustion_atr, record.checks["upperRejection"],
             not record.checks["reversalBar"], record.checks["volumeExpanded"]]
        )
        record.reasons = _reasons(
            f"收盘高于快线 {distance:.2f} 个 ATR（阈值 {exhaustion_atr}）",
            "出现长上影或放量滞涨" if record.checks["upperRejection"] else "上涨动能减弱",
            "趋势晚期提示：不再追加入场",
        )
        record.warnings.append("延伸衰竭：禁止新开仓；是否平仓由 exitOnExhaustion 决定")
        return record

    return record


def _confidence(checks: list[bool]) -> float:
    """Share of the rule's conditions that hold - evidence completeness, not a win rate.

    This is deliberately *not* a probability: it says how much of the rule was
    satisfied, which is what a reader can verify from the reasons list.
    """
    if not checks:
        return 0.0
    return sum(1 for item in checks if item) / len(checks)


def _reasons(*items: str) -> list[str]:
    return [item for item in items if item]


def _cycle_label(cycle: str) -> str:
    return {"upside": "上行周期", "downside": "下行周期"}.get(cycle, "无明确周期")


def trend_view(bars: Sequence[dict], interval: str, *, ema_fast: Sequence[float | None],
               ema_slow: Sequence[float | None], long_sma: Sequence[float | None],
               phase: str = "none") -> HigherTimeframeView:
    """The backdrop summary of one higher interval, from its own closed bars."""
    closes = [float(bar["close"]) for bar in bars]
    if not closes:
        return HigherTimeframeView(interval=interval, available=False, reason="没有已收盘K线")
    trend = ind.trend_of(closes, ema_fast[-1], ema_slow[-1], long_sma[-1])
    return HigherTimeframeView(
        interval=interval,
        phase=phase,
        trend=trend,
        closed_at=int(bars[-1]["ts"]),
        available=True,
        reason="",
    )
