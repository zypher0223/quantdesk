"""Vibe time-series factors and the statistical validator behind them.

This is a QuantDesk v3 plugin: a JSON-RPC subprocess that answers three messages
over stdin/stdout, one JSON line each.

* `health`               - is the process alive
* `factor.catalog`       - the whitelisted factor library this provider offers
* `factor.compute`       - the factor values for one contract's closed bars
* `validation.analyze`   - statistical diagnostics for a finished backtest

Two boundaries are deliberate and worth stating:

* It never touches the network, the filesystem or the database. It receives bars
  and a finished backtest, and returns numbers.
* It never recomputes the P&L. The engine's equity curve and trade list are the
  truth; the validator only describes how fragile they are.

The factor library is written from the definitions of the usual time-series
factors (momentum, volatility, liquidity, carry, positioning) rather than copied
from another project, so every formula is inspectable here and pinned by
`implementationVersion`. All factors are pure functions of the bars, funding and
open interest they are handed, which is what makes a factor run reproducible.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
from typing import Any, Callable

PROVIDER = "vibe-factors"
PROVIDER_VERSION = "0.1.0"
IMPLEMENTATION_VERSION = "vibe/1"
# A factor value is only published from the first bar where its window is full.
MIN_BARS = 3
# Statistical work is bounded so one request cannot occupy the process forever.
MAX_RESAMPLES = 2_000
MAX_PERMUTATIONS = 2_000
MAX_COMBINATIONS = 400


# --------------------------------------------------------------------- maths
#
# Small, dependency-free helpers. Everything here is deterministic given a seed.


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(max(0.0, variance))


def _skew(values: list[float]) -> float:
    if len(values) < 3:
        return 0.0
    mean, deviation = _mean(values), _stdev(values)
    if deviation == 0:
        return 0.0
    return sum(((value - mean) / deviation) ** 3 for value in values) * len(values) / (
        (len(values) - 1) * (len(values) - 2)
    )


def _kurtosis(values: list[float]) -> float:
    """Excess kurtosis (0 for a normal sample)."""
    if len(values) < 4:
        return 0.0
    mean, deviation = _mean(values), _stdev(values)
    if deviation == 0:
        return 0.0
    total = sum(((value - mean) / deviation) ** 4 for value in values)
    n = len(values)
    return (n * (n + 1) * total) / ((n - 1) * (n - 2) * (n - 3)) - 3 * (n - 1) ** 2 / (
        (n - 2) * (n - 3)
    )


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _interval(values: list[float], confidence: float = 0.95) -> dict[str, float | None]:
    tail = (1.0 - confidence) / 2.0
    return {"low": _percentile(values, tail), "high": _percentile(values, 1 - tail),
            "confidence": confidence}


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _normal_ppf(probability: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation)."""
    if probability <= 0.0:
        return float("-inf")
    if probability >= 1.0:
        return float("inf")
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00)
    low, high = 0.02425, 1 - 0.02425
    if probability < low:
        q = math.sqrt(-2 * math.log(probability))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if probability > high:
        q = math.sqrt(-2 * math.log(1 - probability))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = probability - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def _safe(value: float | None, digits: int = 10) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


# ------------------------------------------------------------- factor helpers


class Bars:
    """The candles a factor run was handed, plus its funding and open interest."""

    def __init__(self, payload: dict[str, Any]):
        rows = payload.get("candles") or []
        self.interval = str(payload.get("timeframe") or "1h")
        self.time = [int(row["time"]) for row in rows]
        self.open = [float(row["open"]) for row in rows]
        self.high = [float(row["high"]) for row in rows]
        self.low = [float(row["low"]) for row in rows]
        self.close = [float(row["close"]) for row in rows]
        self.volume = [float(row.get("volume") or 0.0) for row in rows]
        self.turnover = [
            float(row["turnover"]) if row.get("turnover") not in (None, "") else None for row in rows
        ]
        self.funding = sorted(
            ((int(item["ts"]), float(item.get("rate") or 0.0)) for item in payload.get("funding") or []),
            key=lambda item: item[0],
        )
        self.open_interest = sorted(
            ((int(item["ts"]), float(item.get("oi") or 0.0)) for item in payload.get("openInterest") or []),
            key=lambda item: item[0],
        )

    def __len__(self) -> int:
        return len(self.close)

    def returns(self, lookback: int = 1) -> list[float | None]:
        out: list[float | None] = [None] * len(self.close)
        for index in range(lookback, len(self.close)):
            previous = self.close[index - lookback]
            out[index] = (self.close[index] / previous - 1.0) if previous else None
        return out

    def log_returns(self) -> list[float | None]:
        out: list[float | None] = [None] * len(self.close)
        for index in range(1, len(self.close)):
            previous = self.close[index - 1]
            out[index] = math.log(self.close[index] / previous) if previous > 0 and self.close[index] > 0 else None
        return out

    def typical(self) -> list[float]:
        return [(self.high[i] + self.low[i] + self.close[i]) / 3.0 for i in range(len(self.close))]

    def trailing(self, value: Callable[[int], float | None], window: int, *, skip: int = 1) -> list[float | None]:
        """Rolling mean of a per-bar series, ending `skip` bars behind the current one.

        The skip is what keeps a factor usable at bar i: a window that included bar
        i itself would leak the value being predicted.
        """
        out: list[float | None] = [None] * len(self.close)
        for index in range(len(self.close)):
            end = index - skip + 1
            start = end - window
            if start < 0 or end <= 0:
                continue
            window_values = [value(position) for position in range(start, end)]
            if any(item is None for item in window_values):
                continue
            out[index] = sum(window_values) / window  # type: ignore[arg-type]
        return out

    def funding_by_bar(self, window: int = 1) -> list[float | None]:
        """The funding rate in force at each bar: the last settlement up to that bar."""
        out: list[float | None] = []
        index = 0
        latest: float | None = None
        for stamp in self.time:
            while index < len(self.funding) and self.funding[index][0] <= stamp:
                latest = self.funding[index][1]
                index += 1
            out.append(latest)
        if window <= 1:
            return out
        # A sum over the last `window` settlements, so a carry factor can look back.
        summed: list[float | None] = []
        for position in range(len(out)):
            if position + 1 < window:
                summed.append(None)
                continue
            chunk = out[position - window + 1: position + 1]
            summed.append(sum(chunk) if all(item is not None for item in chunk) else None)
        return summed

    def oi_change(self, lookback: int) -> list[float | None]:
        out: list[float | None] = [None] * len(self.close)
        stamps = [item[0] for item in self.open_interest]
        values = [item[1] for item in self.open_interest]
        if len(values) <= lookback:
            return out
        position = 0
        latest_index: int | None = None
        for index, stamp in enumerate(self.time):
            while position < len(stamps) and stamps[position] <= stamp:
                latest_index = position
                position += 1
            if latest_index is None or latest_index - lookback < 0:
                continue
            previous = values[latest_index - lookback]
            current = values[latest_index]
            out[index] = (current / previous - 1.0) if previous else None
        return out


# ------------------------------------------------------------- the factor set
#
# Each entry is (id, name, family, warmup, required fields, callable). The callable
# returns one value per bar, None while its window is not full, so a caller can see
# exactly which bars a factor could speak about.


def _sma(bars: Bars, window: int) -> list[float | None]:
    return bars.trailing(lambda i: bars.close[i], window)


def _factor_momentum(bars: Bars, window: int = 24) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        base = bars.close[index - window]
        out[index] = (bars.close[index] / base - 1.0) if base else None
    return out


def _factor_volatility(bars: Bars, window: int = 24) -> list[float | None]:
    returns = bars.log_returns()
    periods = _periods_per_year(bars.interval)
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        window_values = [value for value in returns[index - window + 1: index + 1] if value is not None]
        if len(window_values) < max(2, window // 2):
            continue
        out[index] = _stdev(window_values) * math.sqrt(periods)
    return out


def _factor_rsi(bars: Bars, window: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, len(bars)):
        change = bars.close[index] - bars.close[index - 1]
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
        if len(gains) < window:
            continue
        avg_gain = _mean(gains[-window:])
        avg_loss = _mean(losses[-window:])
        if avg_gain + avg_loss == 0:
            out[index] = 50.0
        else:
            out[index] = 100.0 * avg_gain / (avg_gain + avg_loss)
    return out


def _ema(values: list[float], span: int) -> list[float]:
    alpha = 2.0 / (span + 1.0)
    out: list[float] = []
    current = values[0] if values else 0.0
    for value in values:
        current = value if not out else current + alpha * (value - current)
        out.append(current)
    return out


def _factor_macd(bars: Bars, fast: int = 12, slow: int = 26, signal: int = 9) -> list[float | None]:
    if len(bars) < slow + signal:
        return [None] * len(bars)
    fast_line = _ema(bars.close, fast)
    slow_line = _ema(bars.close, slow)
    macd = [fast_line[i] - slow_line[i] for i in range(len(bars))]
    signal_line = _ema(macd[slow:], signal)
    out: list[float | None] = [None] * len(bars)
    for index in range(slow + signal, len(bars)):
        out[index] = macd[index] - signal_line[index - slow]
    return out


def _factor_bollinger_z(bars: Bars, window: int = 20) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        chunk = bars.close[index - window: index]
        deviation = _stdev(chunk)
        out[index] = (bars.close[index] - _mean(chunk)) / deviation if deviation else None
    return out


def _factor_atr(bars: Bars, window: int = 14) -> list[float | None]:
    true_range: list[float] = [bars.high[0] - bars.low[0]] if len(bars) else []
    for index in range(1, len(bars)):
        true_range.append(max(
            bars.high[index] - bars.low[index],
            abs(bars.high[index] - bars.close[index - 1]),
            abs(bars.low[index] - bars.close[index - 1]),
        ))
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        average = _mean(true_range[index - window: index])
        out[index] = (average / bars.close[index]) if bars.close[index] else None
    return out


def _factor_range_expansion(bars: Bars, window: int = 12) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        current = bars.high[index] - bars.low[index]
        history = [bars.high[i] - bars.low[i] for i in range(index - window, index)]
        average = _mean(history)
        out[index] = (current / average) if average else None
    return out


def _factor_volume_z(bars: Bars, window: int = 24) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        chunk = bars.volume[index - window: index]
        deviation = _stdev(chunk)
        out[index] = (bars.volume[index] - _mean(chunk)) / deviation if deviation else None
    return out


def _factor_volume_trend(bars: Bars, window: int = 24) -> list[float | None]:
    """Correlation between volume and the sign of the bar's move."""
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        signs = [1.0 if bars.close[i] >= bars.open[i] else -1.0 for i in range(index - window, index)]
        volumes = bars.volume[index - window: index]
        mean_sign, mean_volume = _mean(signs), _mean(volumes)
        numerator = sum((signs[i] - mean_sign) * (volumes[i] - mean_volume) for i in range(window))
        denominator = math.sqrt(
            sum((sign - mean_sign) ** 2 for sign in signs)
            * sum((volume - mean_volume) ** 2 for volume in volumes)
        )
        out[index] = numerator / denominator if denominator else None
    return out


def _factor_amihud(bars: Bars, window: int = 24) -> list[float | None]:
    """Illiquidity: |return| per unit of traded notional, averaged."""
    returns = bars.returns()
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        values: list[float] = []
        for position in range(index - window + 1, index + 1):
            change = returns[position]
            notional = bars.turnover[position]
            if notional is None or not notional:
                notional = bars.volume[position] * bars.close[position]
            if change is None or not notional:
                continue
            values.append(abs(change) / notional)
        if values:
            out[index] = _mean(values) * 1e6
    return out


def _factor_vwap_gap(bars: Bars, window: int = 24) -> list[float | None]:
    typical = bars.typical()
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        volumes = bars.volume[index - window: index]
        total = sum(volumes)
        if not total:
            continue
        vwap = sum(typical[i] * volumes[i - (index - window)] for i in range(index - window, index)) / total
        out[index] = (bars.close[index] / vwap - 1.0) if vwap else None
    return out


def _factor_efficiency_ratio(bars: Bars, window: int = 24) -> list[float | None]:
    """Kaufman efficiency: net move over the path actually travelled."""
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        path = sum(abs(bars.close[i] - bars.close[i - 1]) for i in range(index - window + 1, index + 1))
        net = abs(bars.close[index] - bars.close[index - window])
        out[index] = (net / path) if path else None
    return out


def _factor_autocorrelation(bars: Bars, window: int = 24, lag: int = 1) -> list[float | None]:
    returns = bars.log_returns()
    out: list[float | None] = [None] * len(bars)
    for index in range(window + lag, len(bars)):
        series = [value for value in returns[index - window + 1: index + 1]]
        shifted = [value for value in returns[index - window + 1 - lag: index + 1 - lag]]
        if any(item is None for item in series) or any(item is None for item in shifted):
            continue
        left, right = series, shifted  # type: ignore[assignment]
        mean_left, mean_right = _mean(left), _mean(right)  # type: ignore[arg-type]
        numerator = sum((left[i] - mean_left) * (right[i] - mean_right) for i in range(len(left)))  # type: ignore[arg-type]
        denominator = math.sqrt(
            sum((value - mean_left) ** 2 for value in left)  # type: ignore[union-attr]
            * sum((value - mean_right) ** 2 for value in right)  # type: ignore[union-attr]
        )
        out[index] = numerator / denominator if denominator else None
    return out


def _factor_hurst(bars: Bars, window: int = 48) -> list[float | None]:
    """Rescaled-range exponent over the window, a coarse trend-persistence read."""
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        chunk = [bars.close[i] for i in range(index - window, index + 1)]
        returns = [math.log(chunk[i] / chunk[i - 1]) for i in range(1, len(chunk)) if chunk[i - 1] > 0]
        if len(returns) < 8:
            continue
        mean = _mean(returns)
        cumulative, running, minimum, maximum = 0.0, 0.0, 0.0, 0.0
        for value in returns:
            running += value - mean
            cumulative += (value - mean) ** 2
            minimum = min(minimum, running)
            maximum = max(maximum, running)
        deviation = math.sqrt(cumulative / len(returns))
        if deviation == 0 or maximum == minimum:
            continue
        rescaled = (maximum - minimum) / deviation
        out[index] = math.log(rescaled) / math.log(len(returns)) if rescaled > 0 else None
    return out


def _factor_downside_deviation(bars: Bars, window: int = 24) -> list[float | None]:
    returns = bars.returns()
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        negatives = [value for value in returns[index - window + 1: index + 1]
                     if value is not None and value < 0]
        if not negatives:
            out[index] = 0.0
            continue
        out[index] = math.sqrt(sum(value ** 2 for value in negatives) / len(negatives))
    return out


def _factor_skew(bars: Bars, window: int = 48) -> list[float | None]:
    returns = bars.returns()
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        chunk = [value for value in returns[index - window + 1: index + 1] if value is not None]
        if len(chunk) >= 8:
            out[index] = _skew(chunk)
    return out


def _factor_kurtosis(bars: Bars, window: int = 48) -> list[float | None]:
    returns = bars.returns()
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        chunk = [value for value in returns[index - window + 1: index + 1] if value is not None]
        if len(chunk) >= 8:
            out[index] = _kurtosis(chunk)
    return out


def _factor_max_drawdown(bars: Bars, window: int = 48) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        peak = bars.close[index - window]
        worst = 0.0
        for position in range(index - window, index + 1):
            peak = max(peak, bars.close[position])
            if peak:
                worst = min(worst, bars.close[position] / peak - 1.0)
        out[index] = worst
    return out


def _factor_stochastic(bars: Bars, window: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        highest = max(bars.high[index - window + 1: index + 1])
        lowest = min(bars.low[index - window + 1: index + 1])
        span = highest - lowest
        out[index] = ((bars.close[index] - lowest) / span * 100.0) if span else None
    return out


def _factor_williams_r(bars: Bars, window: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        highest = max(bars.high[index - window + 1: index + 1])
        lowest = min(bars.low[index - window + 1: index + 1])
        span = highest - lowest
        out[index] = ((highest - bars.close[index]) / span * -100.0) if span else None
    return out


def _factor_cci(bars: Bars, window: int = 20) -> list[float | None]:
    typical = bars.typical()
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        chunk = typical[index - window + 1: index + 1]
        average = _mean(chunk)
        deviation = _mean([abs(value - average) for value in chunk])
        out[index] = ((typical[index] - average) / (0.015 * deviation)) if deviation else None
    return out


def _factor_ema_slope(bars: Bars, window: int = 24) -> list[float | None]:
    line = _ema(bars.close, window)
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        base = line[index - window]
        out[index] = (line[index] / base - 1.0) if base else None
    return out


def _factor_trend_strength(bars: Bars, window: int = 24) -> list[float | None]:
    """Share of bars in the window that closed in the same direction as the window."""
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        move = bars.close[index] - bars.close[index - window]
        if move == 0:
            out[index] = 0.0
            continue
        aligned = sum(
            1 for position in range(index - window + 1, index + 1)
            if (bars.close[position] - bars.close[position - 1]) * move > 0
        )
        out[index] = aligned / window
    return out


def _factor_carry(bars: Bars, window: int = 3) -> list[float | None]:
    """Funding paid to hold the position: summed over the last settlements."""
    return bars.funding_by_bar(window)


def _factor_carry_momentum(bars: Bars, window: int = 6) -> list[float | None]:
    funding = bars.funding_by_bar(1)
    out: list[float | None] = [None] * len(bars)
    for index in range(window, len(bars)):
        current, previous = funding[index], funding[index - window]
        if current is None or previous is None:
            continue
        out[index] = current - previous
    return out


def _factor_oi_change(bars: Bars, window: int = 6) -> list[float | None]:
    return bars.oi_change(window)


def _factor_oi_price_divergence(bars: Bars, window: int = 6) -> list[float | None]:
    """Price up while open interest falls (or the reverse): positioning failing to confirm."""
    price = _factor_momentum(bars, window)
    interest = bars.oi_change(window)
    out: list[float | None] = [None] * len(bars)
    for index in range(len(bars)):
        if price[index] is None or interest[index] is None:
            continue
        out[index] = price[index] - interest[index]
    return out


FACTORS: list[dict[str, Any]] = [
    {"id": "vibe.momentum.24", "name": "动量（24 根）", "family": "momentum", "warmup": 24,
     "fields": ["close"], "fn": lambda bars: _factor_momentum(bars, 24),
     "description": "收盘价相对 24 根前的变化率。"},
    {"id": "vibe.momentum.96", "name": "动量（96 根）", "family": "momentum", "warmup": 96,
     "fields": ["close"], "fn": lambda bars: _factor_momentum(bars, 96),
     "description": "更长周期的动量，用来区分趋势与短期噪声。"},
    {"id": "vibe.ema_slope.24", "name": "EMA 斜率（24）", "family": "trend", "warmup": 48,
     "fields": ["close"], "fn": lambda bars: _factor_ema_slope(bars, 24),
     "description": "24 根 EMA 相对自身 24 根前的变化率。"},
    {"id": "vibe.trend_strength.24", "name": "趋势一致性（24）", "family": "trend", "warmup": 25,
     "fields": ["close"], "fn": lambda bars: _factor_trend_strength(bars, 24),
     "description": "窗口内与整体方向一致的 K 线占比。"},
    {"id": "vibe.efficiency_ratio.24", "name": "效率系数（24）", "family": "trend", "warmup": 25,
     "fields": ["close"], "fn": lambda bars: _factor_efficiency_ratio(bars, 24),
     "description": "净位移除以路径长度（Kaufman）。"},
    {"id": "vibe.hurst.48", "name": "Hurst 指数（48）", "family": "trend", "warmup": 49,
     "fields": ["close"], "fn": lambda bars: _factor_hurst(bars, 48),
     "description": "重标极差指数：>0.5 趋势延续，<0.5 均值回复。"},
    {"id": "vibe.volatility.24", "name": "年化波动率（24）", "family": "volatility", "warmup": 25,
     "fields": ["close"], "fn": lambda factor_bars: _factor_volatility(factor_bars, 24),
     "description": "24 根对数收益的标准差，按 15 分钟 K 线年化。"},
    {"id": "vibe.atr.14", "name": "ATR 占比（14）", "family": "volatility", "warmup": 15,
     "fields": ["high", "low", "close"], "fn": lambda bars: _factor_atr(bars, 14),
     "description": "真实波幅均值占收盘价的比例。"},
    {"id": "vibe.range_expansion.12", "name": "振幅扩张（12）", "family": "volatility", "warmup": 13,
     "fields": ["high", "low"], "fn": lambda bars: _factor_range_expansion(bars, 12),
     "description": "当根振幅相对前 12 根均值的倍数。"},
    {"id": "vibe.downside_dev.24", "name": "下行偏差（24）", "family": "volatility", "warmup": 25,
     "fields": ["close"], "fn": lambda bars: _factor_downside_deviation(bars, 24),
     "description": "只统计负收益的平方根均值。"},
    {"id": "vibe.skew.48", "name": "收益偏度（48）", "family": "distribution", "warmup": 49,
     "fields": ["close"], "fn": lambda bars: _factor_skew(bars, 48),
     "description": "窗口内收益的偏度。"},
    {"id": "vibe.kurtosis.48", "name": "收益峰度（48）", "family": "distribution", "warmup": 49,
     "fields": ["close"], "fn": lambda bars: _factor_kurtosis(bars, 48),
     "description": "窗口内收益的超额峰度（厚尾程度）。"},
    {"id": "vibe.max_drawdown.48", "name": "窗口最大回撤（48）", "family": "distribution", "warmup": 49,
     "fields": ["close"], "fn": lambda bars: _factor_max_drawdown(bars, 48),
     "description": "窗口内买入持有的最大回撤。"},
    {"id": "vibe.rsi.14", "name": "RSI（14）", "family": "oscillator", "warmup": 15,
     "fields": ["close"], "fn": lambda bars: _factor_rsi(bars, 14),
     "description": "经典相对强弱指标。"},
    {"id": "vibe.macd.hist", "name": "MACD 柱（12/26/9）", "family": "oscillator", "warmup": 35,
     "fields": ["close"], "fn": _factor_macd,
     "description": "MACD 与其信号线之差。"},
    {"id": "vibe.bollinger_z.20", "name": "布林带 Z 值（20）", "family": "oscillator", "warmup": 21,
     "fields": ["close"], "fn": lambda bars: _factor_bollinger_z(bars, 20),
     "description": "收盘价相对 20 根均值的标准分。"},
    {"id": "vibe.stochastic.14", "name": "随机指标 %K（14）", "family": "oscillator", "warmup": 15,
     "fields": ["high", "low", "close"], "fn": lambda bars: _factor_stochastic(bars, 14),
     "description": "收盘价在窗口高低区间中的位置。"},
    {"id": "vibe.williams_r.14", "name": "威廉指标 %R（14）", "family": "oscillator", "warmup": 15,
     "fields": ["high", "low", "close"], "fn": lambda bars: _factor_williams_r(bars, 14),
     "description": "窗口高点到收盘价的距离（负值）。"},
    {"id": "vibe.cci.20", "name": "CCI（20）", "family": "oscillator", "warmup": 21,
     "fields": ["high", "low", "close"], "fn": lambda bars: _factor_cci(bars, 20),
     "description": "典型价格偏离其均值的程度。"},
    {"id": "vibe.volume_z.24", "name": "成交量 Z 值（24）", "family": "liquidity", "warmup": 25,
     "fields": ["volume"], "fn": lambda bars: _factor_volume_z(bars, 24),
     "description": "当根成交量相对前 24 根的标准分。"},
    {"id": "vibe.volume_trend.24", "name": "量价相关（24）", "family": "liquidity", "warmup": 25,
     "fields": ["volume", "close", "open"], "fn": lambda bars: _factor_volume_trend(bars, 24),
     "description": "成交量与 K 线方向的相关系数。"},
    {"id": "vibe.amihud.24", "name": "Amihud 非流动性（24）", "family": "liquidity", "warmup": 25,
     "fields": ["close", "volume", "turnover"], "fn": lambda bars: _factor_amihud(bars, 24),
     "description": "单位成交额推动的价格变动（放大约 1e6 倍）。"},
    {"id": "vibe.vwap_gap.24", "name": "VWAP 偏离（24）", "family": "liquidity", "warmup": 25,
     "fields": ["close", "volume"], "fn": lambda bars: _factor_vwap_gap(bars, 24),
     "description": "收盘价相对 24 根成交量加权均价的比例差。"},
    {"id": "vibe.autocorr.24", "name": "收益自相关（24，lag1）", "family": "structure", "warmup": 26,
     "fields": ["close"], "fn": lambda bars: _factor_autocorrelation(bars, 24, 1),
     "description": "一阶自相关：正值表示延续，负值表示反转。"},
    {"id": "vibe.carry.3", "name": "资金费率累积（3 次结算）", "family": "carry", "warmup": 3,
     "fields": ["funding"], "fn": lambda bars: _factor_carry(bars, 3),
     "sources": ["bybit"], "description": "最近 3 次资金费结算之和。"},
    {"id": "vibe.carry_momentum.6", "name": "资金费率变化（6）", "family": "carry", "warmup": 7,
     "fields": ["funding"], "fn": lambda bars: _factor_carry_momentum(bars, 6),
     "sources": ["bybit"], "description": "当前资金费率相对 6 根前的差。"},
    {"id": "vibe.oi_change.6", "name": "持仓量变化（6）", "family": "positioning", "warmup": 7,
     "fields": ["open_interest"], "fn": lambda bars: _factor_oi_change(bars, 6),
     "sources": ["bybit"], "description": "持仓量相对 6 个采样点的变化率。"},
    {"id": "vibe.oi_price_divergence.6", "name": "量价背离（6）", "family": "positioning", "warmup": 7,
     "fields": ["open_interest", "close"], "fn": lambda bars: _factor_oi_price_divergence(bars, 6),
     "sources": ["bybit"], "description": "价格动量减持仓量变化：持仓没有确认价格。"},
]

FACTOR_INDEX = {item["id"]: item for item in FACTORS}
SUPPORTED_TIMEFRAMES = ["15m", "1h", "4h", "1d"]


def _formula_hash(entry: dict[str, Any]) -> str:
    material = json.dumps(
        {"id": entry["id"], "family": entry["family"], "warmup": entry["warmup"],
         "fields": entry["fields"], "implementation": IMPLEMENTATION_VERSION},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def catalog() -> dict[str, Any]:
    return {
        "factors": [
            {
                "id": entry["id"],
                "name": entry["name"],
                "family": entry["family"],
                "mode": "time_series",
                "requiredFields": entry["fields"],
                "warmupBars": entry["warmup"],
                "supportedTimeframes": SUPPORTED_TIMEFRAMES,
                "implementationVersion": IMPLEMENTATION_VERSION,
                "sources": entry.get("sources", ["derived"]),
                "formulaHash": _formula_hash(entry),
                "description": entry["description"],
            }
            for entry in FACTORS
        ],
        "providerVersion": f"{PROVIDER}/{PROVIDER_VERSION}",
        "warnings": [],
    }


def compute(request: dict[str, Any]) -> dict[str, Any]:
    bars = Bars(request)
    requested = list(request.get("factorIds") or [])
    parameters = request.get("parameters") or {}
    window_scale = int(parameters.get("windowScale") or 1)
    series: list[dict[str, Any]] = []
    warnings: list[str] = []
    if len(bars) < MIN_BARS:
        return {"snapshotHash": request.get("snapshotHash", ""), "series": [],
                "warnings": ["K 线不足，未计算任何因子"]}
    for factor_id in requested:
        entry = FACTOR_INDEX.get(factor_id)
        if entry is None:
            warnings.append(f"未收录的因子：{factor_id}")
            continue
        try:
            values = entry["fn"](bars)
        except Exception as exc:  # noqa: BLE001 - one bad factor must not fail the batch
            warnings.append(f"{factor_id} 计算失败：{type(exc).__name__}: {exc}")
            continue
        if window_scale != 1:
            values = _scale_windows(entry["id"], bars, window_scale)
        series.append({
            "factorId": factor_id,
            "values": [
                {"time": bars.time[index], "value": _safe(values[index])}
                for index in range(len(bars))
            ],
            "implementationVersion": IMPLEMENTATION_VERSION,
        })
    return {"snapshotHash": request.get("snapshotHash", ""), "series": series, "warnings": warnings}


def _scale_windows(factor_id: str, bars: Bars, scale: int) -> list[float | None]:
    """Re-run a windowed factor with its window multiplied, for a sensitivity read.

    Only the factors whose window is a plain lookback can be re-scaled; the rest
    keep their definition unchanged, because a silently different formula would be
    worse than no sensitivity reading at all.
    """
    base = FACTOR_INDEX[factor_id]
    window = max(2, int(base["warmup"] * max(1, scale)))
    builders = {
        "vibe.momentum.24": lambda: _factor_momentum(bars, window),
        "vibe.momentum.96": lambda: _factor_momentum(bars, window),
        "vibe.rsi.14": lambda: _factor_rsi(bars, window),
        "vibe.bollinger_z.20": lambda: _factor_bollinger_z(bars, window),
        "vibe.atr.14": lambda: _factor_atr(bars, window),
        "vibe.volume_z.24": lambda: _factor_volume_z(bars, window),
    }
    builder = builders.get(factor_id)
    return builder() if builder else base["fn"](bars)


# ------------------------------------------------------------ factor analysis
#
# What the factors are for: asking whether a result is more than a lucky draw.
# Every test below states its null hypothesis and what it randomises.


def _returns_from_equity(points: list[dict[str, Any]]) -> tuple[list[int], list[float]]:
    stamps = [int(point["time"]) for point in points]
    equity = [float(point["equity"]) for point in points]
    returns: list[float] = []
    for index in range(1, len(equity)):
        previous = equity[index - 1]
        returns.append((equity[index] / previous - 1.0) if previous else 0.0)
    return stamps[1:], returns


def _sharpe(returns: list[float], periods_per_year: float) -> float:
    deviation = _stdev(returns)
    if not returns or deviation == 0:
        return 0.0
    return _mean(returns) / deviation * math.sqrt(periods_per_year)


def _max_drawdown_from_returns(returns: list[float], initial: float = 1.0) -> float:
    equity, peak, worst = initial, initial, 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


def _periods_per_year(interval: str) -> float:
    return {"15m": 365 * 24 * 4, "1h": 365 * 24, "4h": 365 * 6, "1d": 365}.get(interval, 365 * 24)


def _moving_block_resample(returns: list[float], block: int, rng: random.Random) -> list[float]:
    size = len(returns)
    if block <= 1 or size <= block:
        return [returns[rng.randrange(size)] for _ in range(size)]
    blocks = int(math.ceil(size / block))
    out: list[float] = []
    for _ in range(blocks):
        start = rng.randrange(size - block + 1)
        out.extend(returns[start:start + block])
    return out[:size]


def bootstrap(returns: list[float], *, confidence: float, resamples: int, rng: random.Random,
              periods_per_year: float) -> dict[str, Any]:
    """Moving-block bootstrap of the return series.

    Blocks (rather than single bars) keep the autocorrelation that volatility
    clustering creates, so the interval is not narrower than the data deserves.
    """
    size = len(returns)
    if size < 20:
        return {"method": "moving_block", "unavailable": "样本不足（少于 20 根），无法做分块 bootstrap"}
    block = max(2, int(round(size ** (1.0 / 3.0))))
    resamples = max(50, min(int(resamples), MAX_RESAMPLES))
    sharpes: list[float] = []
    returns_pct: list[float] = []
    drawdowns: list[float] = []
    finals: list[float] = []
    for _ in range(resamples):
        sample = _moving_block_resample(returns, block, rng)
        sharpes.append(_sharpe(sample, periods_per_year))
        total = 1.0
        for value in sample:
            total *= 1.0 + value
        returns_pct.append((total - 1.0) * 100.0)
        drawdowns.append(_max_drawdown_from_returns(sample) * 100.0)
        finals.append(total * 100.0)
    positive = sum(1 for value in sharpes if value > 0) / len(sharpes)
    return {
        "method": "moving_block",
        "blockSize": block,
        "resamples": resamples,
        "sharpe": _interval(sharpes, confidence),
        "returnPct": _interval(returns_pct, confidence),
        "maxDrawdown": _interval(drawdowns, confidence),
        "positiveSharpeProbability": round(positive, 6),
        "finalEquityPercentiles": {
            "p05": _percentile(finals, 0.05), "p25": _percentile(finals, 0.25),
            "p50": _percentile(finals, 0.50), "p75": _percentile(finals, 0.75),
            "p95": _percentile(finals, 0.95),
        },
    }


def path_risk(trades: list[dict[str, Any]], *, simulations: int, rng: random.Random,
              initial_equity: float) -> dict[str, Any]:
    """Reorder the trades that already happened.

    This is not a significance test and says so in the result: it answers "how much
    of the drawdown was the order I happened to get?".
    """
    if len(trades) < 3:
        return {"simulations": 0, "unavailable": "交易太少（少于 3 笔），路径风险没有意义"}
    simulations = max(20, min(int(simulations), MAX_RESAMPLES))
    pnls = [float(trade.get("netPnl") or 0.0) for trade in trades]
    drawdowns: list[float] = []
    streaks: list[int] = []
    finals: list[float] = []
    for _ in range(simulations):
        order = pnls[:]
        rng.shuffle(order)
        equity, peak, worst, streak, worst_streak = initial_equity, initial_equity, 0.0, 0, 0
        for value in order:
            equity += value
            peak = max(peak, equity)
            worst = min(worst, equity / peak - 1.0 if peak else 0.0)
            if value < 0:
                streak += 1
                worst_streak = max(worst_streak, streak)
            else:
                streak = 0
        drawdowns.append(worst * 100.0)
        streaks.append(worst_streak)
        finals.append(equity)
    return {
        "simulations": simulations,
        "drawdown": _interval(drawdowns),
        "maxConsecutiveLosses": _interval([float(value) for value in streaks]),
        "finalEquity": _interval(finals),
    }


def randomization(returns: list[float], trades: list[dict[str, Any]], benchmark: dict[str, Any], *,
                  permutations: int, rng: random.Random, periods_per_year: float) -> dict[str, Any]:
    """The null hypothesis that matters: the signals carry no information.

    With a benchmark curve the test is a signal-shift test: the strategy's exposure
    over time is reconstructed from its trades, shifted against the market's own
    returns, and the question is how often a randomly timed version of the same
    exposure would have done as well. Without a benchmark the test degrades to a
    block permutation of the strategy's own returns, which only asks whether the
    *order* of the returns carried information - and the result says which of the
    two was used.
    """
    observed = _sharpe(returns, periods_per_year)
    permutations = max(50, min(int(permutations), MAX_PERMUTATIONS))
    market = _benchmark_returns(benchmark)
    method = "block_permutation"
    null: list[float] = []
    if market and trades:
        exposure = _exposure_from_trades(trades)
        aligned_market, aligned_exposure = _align(market, exposure)
        if len(aligned_market) >= 30 and any(value != 0 for value in aligned_exposure):
            method = "signal_shift"
            observed = _sharpe([a * b for a, b in zip(aligned_exposure, aligned_market)], periods_per_year)
            for _ in range(permutations):
                shift = rng.randrange(1, len(aligned_exposure))
                shifted = aligned_exposure[-shift:] + aligned_exposure[:-shift]
                null.append(_sharpe([a * b for a, b in zip(shifted, aligned_market)], periods_per_year))
    if not null:
        size = len(returns)
        if size < 20:
            return {"method": method, "permutations": 0, "observedSharpe": _safe(observed),
                    "unavailable": "样本不足，无法做信号随机化"}
        block = max(2, int(round(size ** (1.0 / 3.0))))
        for _ in range(permutations):
            sample = _moving_block_resample(returns, block, rng)
            null.append(_sharpe(sample, periods_per_year))
    extreme = sum(1 for value in null if value >= observed)
    p_value = (extreme + 1) / (len(null) + 1)  # never exactly zero
    return {
        "method": method,
        "permutations": len(null),
        "observedSharpe": _safe(observed),
        "pValue": _safe(p_value),
        "nullSharpe": _interval(null),
    }


def _benchmark_returns(benchmark: dict[str, Any]) -> list[tuple[int, float]]:
    curve = benchmark.get("equityCurve") or benchmark.get("curve") or []
    stamps: list[int] = []
    returns: list[float] = []
    previous: float | None = None
    previous_stamp = 0
    for point in curve:
        if not isinstance(point, dict):
            continue
        value = point.get("equity")
        if value in (None, ""):
            continue
        value = float(value)
        stamp = int(point.get("time") or 0)
        if previous:
            stamps.append(previous_stamp)
            returns.append(value / previous - 1.0)
        previous, previous_stamp = value, stamp
    return list(zip(stamps, returns))


def _exposure_from_trades(trades: list[dict[str, Any]]) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for trade in trades:
        entry = int(trade.get("entryTime") or 0)
        exit_ = int(trade.get("exitTime") or entry)
        direction = 1.0 if trade.get("direction") == "long" else -1.0
        out.append((entry, direction))
        out.append((exit_, 0.0))
    out.sort(key=lambda item: item[0])
    return out


def _align(market: list[tuple[int, float]], exposure: list[tuple[int, float]]) -> tuple[list[float], list[float]]:
    """Put both series on the market's clock: exposure is held until it changes."""
    aligned_market: list[float] = []
    aligned_exposure: list[float] = []
    position = 0
    current = 0.0
    for stamp, value in market:
        while position < len(exposure) and exposure[position][0] <= stamp:
            current = exposure[position][1]
            position += 1
        aligned_market.append(value)
        aligned_exposure.append(current)
    return aligned_market, aligned_exposure


def deflated_sharpe(returns: list[float], *, trials: int, trial_sharpes: list[float],
                    periods_per_year: float) -> dict[str, Any]:
    """Bailey & López de Prado's deflated Sharpe ratio.

    A Sharpe chosen as the best of N tries is not the Sharpe of one hypothesis.
    The correction needs the number of trials and how spread out those trials'
    Sharpes were; without the spread it is not applied, and the result says so
    rather than reporting a number it cannot defend.
    """
    size = len(returns)
    if size < 30:
        return {"applied": False, "note": "样本不足（少于 30 根），未做 Deflated Sharpe"}
    observed = _sharpe(returns, periods_per_year)
    spread = _stdev(trial_sharpes) if len(trial_sharpes) >= 2 else 0.0
    if trials < 2 or spread <= 0:
        return {"applied": False, "trials": trials,
                "note": "缺少多次尝试的 Sharpe 离散度，无法估计“最好一次”的期望值，未做 Deflated Sharpe"}
    gamma = 0.5772156649
    expected_max = spread * (
        (1 - gamma) * _normal_ppf(1 - 1.0 / trials) + gamma * _normal_ppf(1 - 1.0 / (trials * math.e))
    )
    skew, kurt = _skew(returns), _kurtosis(returns)
    denominator = math.sqrt(max(1e-12, 1 - skew * observed + (kurt - 1) / 4.0 * observed ** 2))
    statistic = (observed - expected_max) * math.sqrt(size - 1) / denominator
    return {
        "applied": True,
        "trials": trials,
        "deflatedSharpe": _safe(_normal_cdf(statistic)),
        "observedSharpe": _safe(observed),
        "expectedMaxSharpe": _safe(expected_max),
        "sharpeSpread": _safe(spread),
        "note": f"以 {trials} 次尝试、Sharpe 离散度 {spread:.3f} 校正后的显著度",
    }


def probability_of_overfitting(matrix: list[list[float]], *, max_combinations: int,
                               rng: random.Random) -> dict[str, Any]:
    """PBO by combinatorially symmetric cross-validation.

    The matrix is candidates x blocks of out-of-sample performance (one column per
    walk-forward window). For every split of the blocks into two halves, the
    candidate that looks best in one half is checked in the other; PBO is how often
    it lands in the bottom half there. A high value means the selection procedure,
    not the strategy, is what the result measures.
    """
    if not matrix or len(matrix) < 2:
        return {"applied": False, "note": "只有一个候选参数，无法估计回测过拟合概率"}
    blocks = len(matrix[0])
    if any(len(row) != blocks for row in matrix):
        return {"applied": False, "note": "候选矩阵形状不一致"}
    if blocks < 4 or blocks % 2:
        return {"applied": False, "note": f"需要至少 4 个且为偶数的分块，当前 {blocks} 个"}
    combinations = []
    for mask in range(1 << blocks):
        if bin(mask).count("1") * 2 != blocks:
            continue
        combinations.append([index for index in range(blocks) if mask >> index & 1])
    if len(combinations) > max_combinations:
        combinations = rng.sample(combinations, max_combinations)
    below = 0
    lambdas: list[float] = []
    for chosen in combinations:
        rest = [index for index in range(blocks) if index not in chosen]
        in_sample = [_mean([row[index] for index in chosen]) for row in matrix]
        out_sample = [_mean([row[index] for index in rest]) for row in matrix]
        best = max(range(len(in_sample)), key=lambda position: in_sample[position])
        better = sum(1 for value in out_sample if value < out_sample[best])
        tied = sum(1 for value in out_sample if value == out_sample[best])
        rank = (better + 0.5 * tied) / len(out_sample)
        lambdas.append(rank)
        if rank <= 0.5:
            below += 1
    return {
        "applied": True,
        "probability": round(below / len(combinations), 6),
        "combinations": len(combinations),
        "blocks": blocks,
        "candidates": len(matrix),
        "medianOutOfSampleRank": _safe(_percentile(lambdas, 0.5)),
        "note": "CSCV：把滚动窗口分成两半，看样本内最优的候选在另一半的排名",
    }


def analyze(request: dict[str, Any]) -> dict[str, Any]:
    seed = int(request.get("seed") or 42)
    rng = random.Random(seed)
    interval = request.get("interval") or "1h"
    periods = _periods_per_year(interval)
    tests = request.get("tests") or {}
    enabled = set(tests.get("enabled") or ["pathRisk", "bootstrap", "randomization", "multipleTesting"])
    points = request.get("equityCurve") or []
    trades = request.get("trades") or []
    warnings: list[str] = []
    stamps, returns = _returns_from_equity(points)
    initial_equity = float(points[0]["equity"]) if points else 0.0
    del stamps
    result: dict[str, Any] = {
        "provider": PROVIDER,
        "runId": str(request.get("runId") or ""),
        "algorithmVersion": IMPLEMENTATION_VERSION,
        "seed": seed,
        "samples": len(returns),
        "warnings": warnings,
    }
    if len(returns) < 20:
        result["unavailable"] = f"净值曲线只有 {len(returns)} 个收益点，统计验证需要至少 20 个"
        return result

    confidence = float(tests.get("confidence") or 0.95)
    if "pathRisk" in enabled:
        path = path_risk(trades, simulations=int(tests.get("pathRisk", {}).get("simulations") or 0) or 400,
                         rng=rng, initial_equity=initial_equity)
        result["pathRisk"] = path
        if path.get("unavailable"):
            warnings.append(str(path["unavailable"]))
    if "bootstrap" in enabled:
        block = bootstrap(returns, confidence=confidence,
                          resamples=int(tests.get("bootstrap", {}).get("resamples") or 0) or 400,
                          rng=rng, periods_per_year=periods)
        if block.get("unavailable"):
            warnings.append(str(block["unavailable"]))
        result["bootstrap"] = block
        percentiles = block.get("finalEquityPercentiles")
        if percentiles:
            result["equityPercentiles"] = {
                key: [value] for key, value in percentiles.items() if value is not None
            }
        result["tailLoss"] = _interval(
            [value for value in returns if value <= (_percentile(returns, 0.05) or 0.0)],
            confidence=confidence,
        )
    if "randomization" in enabled:
        result["randomization"] = randomization(
            returns, trades, request.get("benchmark") or {},
            permutations=int(tests.get("randomization", {}).get("permutations") or 0) or 400,
            rng=rng, periods_per_year=periods,
        )
    if "multipleTesting" in enabled:
        settings = tests.get("multipleTesting") or {}
        matrix = [[float(value) for value in row] for row in (settings.get("candidateMatrix") or [])]
        trial_sharpes = [float(value) for value in (settings.get("candidateSharpes") or [])]
        trials = int(settings.get("trials") or len(matrix) or 1)
        deflated = deflated_sharpe(returns, trials=trials, trial_sharpes=trial_sharpes,
                                   periods_per_year=periods)
        overfitting = probability_of_overfitting(
            matrix, max_combinations=min(int(settings.get("maxCombinations") or 252), MAX_COMBINATIONS),
            rng=rng,
        )
        result["multipleTesting"] = {
            "trials": trials,
            "factorCount": int(settings.get("factorCount") or 0),
            "parameterCombinations": int(settings.get("parameterCombinations") or len(matrix) or 0),
            "deflatedSharpe": deflated.get("deflatedSharpe"),
            "probabilityOfBacktestOverfitting": overfitting.get("probability"),
            "method": "deflated_sharpe + cscv_pbo",
            "applied": bool(deflated.get("applied") or overfitting.get("applied")),
            "note": "；".join(filter(None, [deflated.get("note"), overfitting.get("note")]))[:400],
        }
    return result


# ------------------------------------------------------------------- dispatch

METHODS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "health": lambda request: {
        "ok": True, "provider": PROVIDER, "version": PROVIDER_VERSION,
        "factors": len(FACTORS), "implementation": IMPLEMENTATION_VERSION,
    },
    "factor.catalog": lambda request: catalog(),
    "factor.compute": compute,
    "validation.analyze": analyze,
}


def respond(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
    sys.stdout.flush()


def main() -> int:
    line = sys.stdin.readline()
    if not line:
        return 0
    try:
        request = json.loads(line)
    except json.JSONDecodeError as exc:
        respond({"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": f"无法解析请求：{exc}"}})
        return 1
    identifier = request.get("id")
    method = str(request.get("method") or "")
    handler = METHODS.get(method)
    if handler is None:
        respond({"jsonrpc": "2.0", "id": identifier,
                 "error": {"code": -32601, "message": f"不支持的方法：{method}"}})
        return 1
    try:
        result = handler(request.get("params") or {})
    except Exception as exc:  # noqa: BLE001 - reported to the engine as a plugin error
        respond({"jsonrpc": "2.0", "id": identifier,
                 "error": {"code": -32000, "message": f"{type(exc).__name__}: {exc}"}})
        return 1
    respond({"jsonrpc": "2.0", "id": identifier, "result": result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
