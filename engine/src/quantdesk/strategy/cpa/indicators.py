"""Indicators for the CPA detector.

Everything here is a pure function of a list of closed bars, and every window ends at
the bar being evaluated - never after it. That is the whole anti-leakage story at this
layer: a pivot for bar `i` is the extreme of `[i - lookback, i)`, a volume ratio
compares bar `i` against the mean of `[i - window, i)`, and no function takes an index
it could read forward from.

Indicators return `None` while the window is not yet full, so a caller can say
"not enough data" instead of computing on a partial window and pretending.
"""

from __future__ import annotations

from typing import Sequence


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """Exponential moving average, seeded with the first value.

    Seeded rather than SMA-seeded on purpose: it is defined from bar 0, so an early
    bar's value never changes when more history arrives. A phase series must not move
    because the caller fetched more bars.
    """
    if period < 1:
        raise ValueError("EMA 周期必须大于 0")
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out: list[float | None] = []
    current = float(values[0])
    for index, value in enumerate(values):
        current = float(value) if index == 0 else alpha * float(value) + (1 - alpha) * current
        out.append(current if index >= period - 1 else None)
    return out


def sma(values: Sequence[float], period: int) -> list[float | None]:
    if period < 1:
        raise ValueError("SMA 周期必须大于 0")
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for index, value in enumerate(values):
        total += float(value)
        if index >= period:
            total -= float(values[index - period])
        if index >= period - 1:
            out[index] = total / period
    return out


def true_ranges(bars: Sequence[dict]) -> list[float]:
    """True range per bar; bar 0 uses its own high-low."""
    out: list[float] = []
    for index, bar in enumerate(bars):
        high, low = float(bar["high"]), float(bar["low"])
        if index == 0:
            out.append(high - low)
            continue
        previous_close = float(bars[index - 1]["close"])
        out.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return out


def atr(bars: Sequence[dict], period: int) -> list[float | None]:
    """Simple average of true range over `period` bars (documented, not Wilder-smoothed).

    Simple-average ATR keeps every value a function of a fixed window, which makes the
    truncation test - "cut the history anywhere, the earlier readings must not change" -
    true by construction rather than by luck.
    """
    if period < 1:
        raise ValueError("ATR 周期必须大于 0")
    ranges = true_ranges(bars)
    out: list[float | None] = [None] * len(ranges)
    total = 0.0
    for index, value in enumerate(ranges):
        total += value
        if index >= period:
            total -= ranges[index - period]
        if index >= period - 1:
            out[index] = total / period
    return out


def volume_ratio(bars: Sequence[dict], index: int, window: int) -> float | None:
    """Bar `index`'s volume against the mean of the `window` bars before it."""
    if window < 1 or index < window:
        return None
    history = [float(bar.get("volume") or 0.0) for bar in bars[index - window:index]]
    if not history:
        return None
    mean = sum(history) / len(history)
    if mean <= 0:
        return None
    return float(bars[index].get("volume") or 0.0) / mean


def distance_in_atr(price: float, reference: float | None, atr_value: float | None) -> float | None:
    """Signed distance from a reference, in ATR units. Positive means above."""
    if reference is None or atr_value in (None, 0):
        return None
    return (float(price) - float(reference)) / float(atr_value)


def prior_high(bars: Sequence[dict], index: int, lookback: int) -> float | None:
    """Highest high of `[index - lookback, index)` - strictly before this bar."""
    if lookback < 1 or index < lookback:
        return None
    return max(float(bar["high"]) for bar in bars[index - lookback:index])


def prior_low(bars: Sequence[dict], index: int, lookback: int) -> float | None:
    """Lowest low of `[index - lookback, index)` - strictly before this bar."""
    if lookback < 1 or index < lookback:
        return None
    return min(float(bar["low"]) for bar in bars[index - lookback:index])


def contraction_score(bars: Sequence[dict], index: int, window: int) -> float | None:
    """How much quieter the bars before this one are than the bars before *those*.

    Recent mean range against the immediately preceding window of the same size, so
    the reading is "the market got quieter here", not "this is a quiet market". A
    wedge that forms after a decline therefore scores high; a quiet market that was
    always quiet scores near zero, which is the honest answer - there is no wedge in a
    market that never expanded.

    Both windows end before `index`: the evaluated bar is the breakout, and letting it
    in would let a violent move destroy the contraction it broke out of.
    """
    if window < 2 or index < window * 2:
        return None
    recent = [float(bar["high"]) - float(bar["low"]) for bar in bars[index - window:index]]
    baseline = [float(bar["high"]) - float(bar["low"]) for bar in bars[index - window * 2:index - window]]
    if len(recent) < window or len(baseline) < window:
        return None
    recent_mean = sum(recent) / len(recent)
    baseline_mean = sum(baseline) / len(baseline)
    if baseline_mean <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - recent_mean / baseline_mean))


def ema_gap_ratio(fast: Sequence[float | None], slow: Sequence[float | None], index: int,
                  window: int) -> float | None:
    """How narrow the fast/slow EMA gap was just before this bar.

    Same prior-only convention as `contraction_score`: the gap that matters for a
    breakout is the one the wedge had, not the one the breakout created.
    """
    if window < 2 or index < window:
        return None
    gaps: list[float] = []
    for position in range(index - window, index):
        f, s = fast[position], slow[position]
        if f is None or s is None or s == 0:
            return None
        gaps.append(abs(f - s) / abs(s))
    if not gaps:
        return None
    widest = max(gaps)
    if widest <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - gaps[-1] / widest))


def upper_wick_ratio(bar: dict) -> float | None:
    """Upper wick as a share of the bar's range; a rejection reads high."""
    high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
    span = high - low
    if span <= 0:
        return None
    return (high - max(close, float(bar["open"]))) / span


def lower_wick_ratio(bar: dict) -> float | None:
    high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
    span = high - low
    if span <= 0:
        return None
    return (min(close, float(bar["open"])) - low) / span


def trend_of(closes: Sequence[float], fast: float | None, slow: float | None,
             long_value: float | None) -> str:
    """A coarse backdrop read used for higher-timeframe context, not for entries."""
    if fast is None or slow is None:
        return "unknown"
    last = float(closes[-1])
    above_long = long_value is None or last >= long_value
    if fast > slow and above_long:
        return "bullish"
    if fast < slow and not above_long:
        return "bearish"
    return "sideways"
