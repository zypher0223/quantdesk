"""The seven gates a factor has to pass before a campaign may propose it.

Why a gate file at all: "the agent may only use allowlisted factors" is a promise
until something checks it against the data. This module is that something. It turns
a factor from a name into evidence — how much of it is actually computable, whether
it moves, whether it is associated with what happens next, whether that association
survives a bootstrap over time, whether it survives the fees its own turnover
implies, and whether it is just a copy of a factor already selected.

Every number here is computed from **QuantDesk's own stored bars**, strictly as-of:
a factor value at bar `t` is only ever paired with the return from `t` forward, and
the forward window is part of the report. Nothing is fetched, nothing is simulated
with the execution model (that is a later, separate gate): this file answers "is
there anything here at all", not "would this have made money".

The gates, in the order they are applied:

1. `coverage`     - enough computable points, per symbol and pooled;
2. `finiteness`   - no infinities, no exploding magnitudes, a real distribution;
3. `dispersion`   - enough distinct values to rank anything;
4. `predictive`   - association with forward returns, with a moving-block bootstrap
                    p-value and a sign-consistency requirement across sub-samples;
5. `persistence`  - the signal is not redrawn from scratch every bar;
6. `cost`         - a sign(factor) strategy nets positive after fees and slippage
                    charged on its own turnover;
7. `redundancy`   - not a near-copy of a factor already in the library.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

GATE_NAMES = (
    "coverage",
    "finiteness",
    "dispersion",
    "predictive",
    "persistence",
    "cost",
    "redundancy",
)


@dataclass(frozen=True)
class GateThresholds:
    """Every knob a factor has to clear, in one place, with its default."""

    min_points_per_symbol: int = 120
    min_symbol_coverage: float = 0.6
    min_symbols: int = 2
    max_abs_value: float = 1e9
    min_distinct_values: int = 50
    max_tie_fraction: float = 0.5
    min_abs_ic: float = 0.02
    # Measured on this machine: with one symbol the predictive gate lets about one
    # noise factor in five through, with two or thirteen symbols none of twenty
    # seeds do, because the sign-consistency rule has more cells to disagree in.
    # `min_symbols = 2` below is therefore part of this gate's calibration, not just
    # a coverage choice.
    max_ic_p_value: float = 0.10
    bootstrap_resamples: int = 400
    bootstrap_block: int = 0  # 0 -> ceil(T ** (1/3)), the usual moving-block rule
    min_sign_consistency: float = 0.6
    min_autocorrelation: float = 0.20
    cost_bps: float = 11.0  # taker fee + slippage, one side, as the engine charges
    min_net_edge_bps: float = 0.0
    max_redundancy: float = 0.70


@dataclass
class GateOutcome:
    name: str
    passed: bool
    metric: dict[str, Any] = field(default_factory=dict)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FactorEvidence:
    factor_id: str
    family: str
    group: str
    interval: str
    horizon_bars: int
    gates: list[GateOutcome]
    symbols: list[str]

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates)

    def gate(self, name: str) -> GateOutcome | None:
        return next((gate for gate in self.gates if gate.name == name), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "factorId": self.factor_id,
            "family": self.family,
            "group": self.group,
            "interval": self.interval,
            "horizonBars": self.horizon_bars,
            "symbols": list(self.symbols),
            "passed": self.passed,
            "gates": [gate.as_dict() for gate in self.gates],
        }


# --------------------------------------------------------------------------
# statistics, all pure python so a scan needs no plugin and no pandas
# --------------------------------------------------------------------------

def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for offset in range(position, end + 1):
            ranks[order[offset]] = average
        position = end + 1
    return ranks


def spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Rank correlation, ties averaged. None when either side is constant."""
    if len(left) != len(right) or len(left) < 3:
        return None
    rank_left, rank_right = _ranks(left), _ranks(right)
    mean_left = sum(rank_left) / len(rank_left)
    mean_right = sum(rank_right) / len(rank_right)
    covariance = sum(
        (a - mean_left) * (b - mean_right) for a, b in zip(rank_left, rank_right)
    )
    var_left = math.sqrt(sum((a - mean_left) ** 2 for a in rank_left))
    var_right = math.sqrt(sum((b - mean_right) ** 2 for b in rank_right))
    if var_left == 0 or var_right == 0:
        return None
    return covariance / (var_left * var_right)


def _block_size(bars: int, configured: int) -> int:
    if configured > 0:
        return max(1, min(configured, bars))
    return max(1, min(bars, int(round(bars ** (1.0 / 3.0)))))


def block_bootstrap_p_value(
    pairs: Sequence[tuple[float, float]],
    *,
    resamples: int,
    block: int,
    seed: int = 20260916,
) -> tuple[float | None, float | None]:
    """A p-value for "mean IC is zero", resampling blocks of consecutive bars.

    Bars are not independent: a daily factor is autocorrelated and so is its
    forward return, so an i.i.d. permutation would manufacture significance. The
    moving-block bootstrap keeps runs of consecutive observations together and
    recentres the resampled statistic on the observed one, which is the standard
    way to ask whether an association this size could come from noise.
    """
    if len(pairs) < 30:
        return None, None
    observed = spearman([pair[0] for pair in pairs], [pair[1] for pair in pairs])
    if observed is None:
        return None, None
    size = _block_size(len(pairs), block)
    rng = random.Random(seed)
    count = len(pairs)
    starts = count - size + 1
    null: list[float] = []
    for _ in range(resamples):
        sample: list[tuple[float, float]] = []
        while len(sample) < count:
            start = rng.randrange(starts)
            sample.extend(pairs[start : start + size])
        sample = sample[:count]
        value = spearman([pair[0] for pair in sample], [pair[1] for pair in sample])
        if value is not None:
            null.append(value)
    if len(null) < 20:
        return observed, None
    # Two-sided: how often does a recentred null reach the observed magnitude.
    recentred = [abs(value - observed) for value in null]
    extreme = sum(1 for value in recentred if value >= abs(observed))
    return observed, (extreme + 1) / (len(recentred) + 1)


def lag_one_autocorrelation(values: Sequence[float]) -> float | None:
    """Persistence of the signal itself, pooled over the series."""
    if len(values) < 10:
        return None
    return spearman(values[:-1], values[1:])


# --------------------------------------------------------------------------
# the gates
# --------------------------------------------------------------------------

def forward_returns(closes: dict[str, list[float]], horizon: int) -> dict[str, list[float | None]]:
    """Per-bar forward return, then subtract the group's own mean at that bar.

    The subtraction is what makes the gates about the *factor* rather than about the
    market: crypto rose over most of this sample, so any factor that is usually long
    would otherwise look skilful in the cost gate and would pick up spurious IC in
    the predictive gate. What is left is a relative bet inside the group, which is
    the only thing a factor is entitled to claim credit for.
    """
    raw: dict[str, list[float | None]] = {}
    length = 0
    for symbol, values in closes.items():
        length = max(length, len(values))
    for symbol, values in closes.items():
        series: list[float | None] = []
        for index in range(len(values)):
            end = index + horizon
            start_price = values[index]
            if end < len(values) and start_price and start_price > 0:
                end_price = values[end]
                series.append(end_price / start_price - 1.0 if end_price else None)
            else:
                series.append(None)
        raw[symbol] = series
    for index in range(length):
        present = [
            series[index]
            for series in raw.values()
            if index < len(series) and series[index] is not None
        ]
        if len(present) < 2:
            continue
        mean = sum(present) / len(present)
        for series in raw.values():
            if index < len(series) and series[index] is not None:
                series[index] = series[index] - mean
    return raw


def _pairs_for(
    values: Sequence[float | None],
    forwards: Sequence[float | None],
    horizon: int,
) -> list[tuple[float, float]]:
    """(factor at t, excess forward return t -> t+h), only where both exist."""
    pairs: list[tuple[float, float]] = []
    for index in range(len(values) - horizon):
        value = values[index]
        if value is None or index >= len(forwards):
            continue
        forward = forwards[index]
        if forward is None:
            continue
        pairs.append((float(value), float(forward)))
    return pairs


def gate_coverage(
    series: dict[str, list[float | None]], thresholds: GateThresholds
) -> GateOutcome:
    per_symbol = {}
    for symbol, values in series.items():
        present = sum(1 for value in values if value is not None)
        per_symbol[symbol] = {
            "points": present,
            "bars": len(values),
            "coverage": round(present / len(values), 4) if values else 0.0,
        }
    qualifying = [
        symbol
        for symbol, item in per_symbol.items()
        if item["points"] >= thresholds.min_points_per_symbol
        and item["coverage"] >= thresholds.min_symbol_coverage
    ]
    passed = len(qualifying) >= thresholds.min_symbols
    detail = (
        f"{len(qualifying)} 个标的达到 {thresholds.min_points_per_symbol} 点且覆盖率 "
        f"{thresholds.min_symbol_coverage:.0%}：{', '.join(qualifying) if qualifying else '无'}"
    )
    return GateOutcome("coverage", passed, {"perSymbol": per_symbol, "qualifying": qualifying}, detail)


def gate_finiteness(
    series: dict[str, list[float | None]], thresholds: GateThresholds
) -> GateOutcome:
    values = [value for series_values in series.values() for value in series_values if value is not None]
    if not values:
        return GateOutcome("finiteness", False, {"values": 0}, "没有任何非空取值")
    infinite = sum(1 for value in values if not math.isfinite(value))
    largest = max(abs(value) for value in values if math.isfinite(value)) if infinite < len(values) else math.inf
    passed = infinite == 0 and largest <= thresholds.max_abs_value
    detail = (
        f"{len(values)} 个取值，非有限 {infinite} 个，最大绝对值 {largest:.3g}"
        f"（上限 {thresholds.max_abs_value:.0g}）"
    )
    return GateOutcome(
        "finiteness",
        passed,
        {"values": len(values), "nonFinite": infinite, "maxAbs": largest, "mean": statistics.fmean(values)},
        detail,
    )


def gate_dispersion(
    series: dict[str, list[float | None]], thresholds: GateThresholds
) -> GateOutcome:
    values = [value for series_values in series.values() for value in series_values if value is not None]
    if len(values) < thresholds.min_distinct_values:
        return GateOutcome(
            "dispersion", False, {"values": len(values)}, f"取值太少（{len(values)} 个），无法评估区分度"
        )
    ordered = sorted(values)
    quartile = len(ordered) // 4
    lower = ordered[quartile]
    upper = ordered[-quartile - 1]
    distinct = len(set(round(value, 10) for value in values))
    counts: dict[float, int] = {}
    for value in values:
        key = round(value, 10)
        counts[key] = counts.get(key, 0) + 1
    tie_fraction = max(counts.values()) / len(values)
    passed = (
        distinct >= thresholds.min_distinct_values
        and upper > lower
        and tie_fraction <= thresholds.max_tie_fraction
    )
    detail = (
        f"{distinct} 个不同取值，四分位距 {upper - lower:.6g}，"
        f"最常见取值占比 {tie_fraction:.1%}（上限 {thresholds.max_tie_fraction:.0%}）"
    )
    return GateOutcome(
        "dispersion",
        passed,
        {
            "distinct": distinct,
            "iqr": upper - lower,
            "tieFraction": tie_fraction,
            "min": ordered[0],
            "max": ordered[-1],
        },
        detail,
    )


def gate_predictive(
    series: dict[str, list[float | None]],
    forwards: dict[str, list[float | None]],
    *,
    horizon: int,
    thresholds: GateThresholds,
) -> GateOutcome:
    """Association with excess forward returns, tested on blocks of consecutive bars."""
    per_symbol: dict[str, Any] = {}
    pooled: list[tuple[float, float]] = []
    half_ics: list[float] = []
    for symbol, values in series.items():
        pairs = _pairs_for(values, forwards.get(symbol, []), horizon)
        if len(pairs) < 60:
            per_symbol[symbol] = {"pairs": len(pairs), "ic": None}
            continue
        ic = spearman([pair[0] for pair in pairs], [pair[1] for pair in pairs])
        per_symbol[symbol] = {"pairs": len(pairs), "ic": None if ic is None else round(ic, 4)}
        pooled.extend(pairs)
        middle = len(pairs) // 2
        for half in (pairs[:middle], pairs[middle:]):
            if len(half) >= 30:
                value = spearman([pair[0] for pair in half], [pair[1] for pair in half])
                if value is not None:
                    half_ics.append(value)
    if len(pooled) < 120:
        return GateOutcome(
            "predictive", False, {"pairs": len(pooled)}, f"可用配对只有 {len(pooled)} 个，样本不足以判断"
        )
    ic, p_value = block_bootstrap_p_value(
        pooled, resamples=thresholds.bootstrap_resamples, block=thresholds.bootstrap_block
    )
    if ic is None:
        return GateOutcome("predictive", False, {"pairs": len(pooled)}, "因子或收益在样本内没有变化")
    agreeing = sum(1 for value in half_ics if value * ic > 0)
    consistency = agreeing / len(half_ics) if half_ics else 0.0
    passed = (
        abs(ic) >= thresholds.min_abs_ic
        and p_value is not None
        and p_value <= thresholds.max_ic_p_value
        and consistency >= thresholds.min_sign_consistency
    )
    detail = (
        f"IC {ic:+.4f}（|IC| 下限 {thresholds.min_abs_ic}），分块 bootstrap p={p_value:.3f}"
        f"（上限 {thresholds.max_ic_p_value}），半样本同号率 {consistency:.0%}"
        f"（下限 {thresholds.min_sign_consistency:.0%}），配对 {len(pooled)} 个"
    )
    return GateOutcome(
        "predictive",
        passed,
        {
            "ic": ic,
            "pValue": p_value,
            "pairs": len(pooled),
            "signConsistency": consistency,
            "block": _block_size(len(pooled), thresholds.bootstrap_block),
            "resamples": thresholds.bootstrap_resamples,
            "perSymbol": per_symbol,
        },
        detail,
    )


def gate_persistence(
    series: dict[str, list[float | None]], thresholds: GateThresholds
) -> GateOutcome:
    per_symbol: dict[str, float | None] = {}
    for symbol, values in series.items():
        clean = [value if value is not None else math.nan for value in values]
        # Autocorrelation on the contiguous non-null stretch: splitting a series at
        # a gap would compare two unrelated points.
        best: list[float] = []
        current: list[float] = []
        for value in clean:
            if math.isnan(value):
                if len(current) > len(best):
                    best = current
                current = []
            else:
                current.append(value)
        if len(current) > len(best):
            best = current
        per_symbol[symbol] = lag_one_autocorrelation(best) if len(best) >= 30 else None
    usable = [value for value in per_symbol.values() if value is not None]
    if not usable:
        return GateOutcome("persistence", False, {"perSymbol": per_symbol}, "没有足够长的连续序列")
    pooled = statistics.fmean(usable)
    passed = pooled >= thresholds.min_autocorrelation
    detail = (
        f"一阶自相关（均值）{pooled:+.3f}，下限 {thresholds.min_autocorrelation}"
        f"（{len(usable)} 个标的可算）"
    )
    return GateOutcome(
        "persistence",
        passed,
        {"autocorrelation": pooled, "perSymbol": per_symbol},
        detail,
    )


def gate_cost(
    series: dict[str, list[float | None]],
    forwards: dict[str, list[float | None]],
    *,
    horizon: int,
    thresholds: GateThresholds,
) -> GateOutcome:
    """A market-neutral sign(factor) book, charged the fees its turnover implies.

    Conservative on purpose: each bar the group is made dollar-neutral
    (`weight_i = sign(factor_i) - mean_j sign(factor_j)`), held for exactly
    `horizon` bars, and every change of weight pays the full one-side cost. Being
    long everything is not a factor view, so it earns nothing here; what remains is
    the part of the return that the factor actually separates.
    """
    cost = thresholds.cost_bps / 10_000.0
    symbols = sorted(series)
    length = min(len(series[symbol]) for symbol in symbols) if symbols else 0
    gross_total = cost_total = 0.0
    observations = 0
    previous: dict[str, float] = {symbol: 0.0 for symbol in symbols}
    per_symbol_gross: dict[str, float] = {symbol: 0.0 for symbol in symbols}
    per_symbol_cost: dict[str, float] = {symbol: 0.0 for symbol in symbols}
    for index in range(length):
        weights: dict[str, float] = {}
        for symbol in symbols:
            value = series[symbol][index]
            weights[symbol] = 0.0 if value is None else (1.0 if value > 0 else (-1.0 if value < 0 else 0.0))
        active = [symbol for symbol in symbols if series[symbol][index] is not None]
        if len(active) < 2:
            previous = {symbol: 0.0 for symbol in symbols}
            continue
        mean_side = sum(weights[symbol] for symbol in active) / len(active)
        bar_gross = bar_cost = 0.0
        for symbol in symbols:
            weight = weights[symbol] - (mean_side if symbol in active else 0.0)
            forward = forwards.get(symbol, [])
            if index < len(forward) and forward[index] is not None:
                bar_gross += weight * forward[index]
                per_symbol_gross[symbol] += weight * forward[index]
            bar_cost += abs(weight - previous[symbol]) * cost
            per_symbol_cost[symbol] += abs(weight - previous[symbol]) * cost
        previous = weights
        gross_total += bar_gross / len(active)
        cost_total += bar_cost / len(active)
        observations += 1
    if observations < 120:
        return GateOutcome("cost", False, {"bars": observations}, "可评估的持仓区间不足")
    gross_bps = gross_total / observations * 10_000
    cost_bps = cost_total / observations * 10_000
    net_bps = gross_bps - cost_bps
    passed = net_bps > thresholds.min_net_edge_bps
    detail = (
        f"每根市场中性毛收益 {gross_bps:+.2f} bps，换手成本 {cost_bps:.2f} bps，净 {net_bps:+.2f} bps"
        f"（单边成本假设 {thresholds.cost_bps} bps，需大于 {thresholds.min_net_edge_bps}）"
    )
    return GateOutcome(
        "cost",
        passed,
        {
            "grossBps": gross_bps,
            "costBps": cost_bps,
            "netBps": net_bps,
            "bars": observations,
            "perSymbolGrossBps": {
                symbol: round(value / observations * 10_000, 3)
                for symbol, value in per_symbol_gross.items()
            },
        },
        detail,
    )


def gate_redundancy(
    series: dict[str, list[float | None]],
    selected: dict[str, dict[str, list[float | None]]],
    thresholds: GateThresholds,
) -> GateOutcome:
    """Is this factor a near-copy of one already in the library?"""
    if not selected:
        return GateOutcome("redundancy", True, {"compared": []}, "库里还没有其它因子，无需比较")
    worst = 0.0
    worst_id = ""
    compared: list[dict[str, Any]] = []
    for factor_id, other in selected.items():
        correlations: list[float] = []
        for symbol, values in series.items():
            other_values = other.get(symbol)
            if other_values is None:
                continue
            pairs = [
                (a, b)
                for a, b in zip(values, other_values)
                if a is not None and b is not None
            ]
            if len(pairs) < 60:
                continue
            value = spearman([pair[0] for pair in pairs], [pair[1] for pair in pairs])
            if value is not None:
                correlations.append(abs(value))
        if not correlations:
            continue
        pooled = statistics.fmean(correlations)
        compared.append({"factorId": factor_id, "absCorrelation": round(pooled, 4)})
        if pooled > worst:
            worst, worst_id = pooled, factor_id
    passed = worst <= thresholds.max_redundancy
    detail = (
        f"与库内因子最高相关 {worst:.3f}"
        + (f"（{worst_id}）" if worst_id else "")
        + f"，上限 {thresholds.max_redundancy}"
    )
    return GateOutcome(
        "redundancy", passed, {"maxAbsCorrelation": worst, "worst": worst_id, "compared": compared}, detail
    )


def evaluate(
    factor_id: str,
    *,
    family: str,
    group: str,
    interval: str,
    horizon: int,
    series: dict[str, list[float | None]],
    forwards: dict[str, list[float | None]],
    selected: dict[str, dict[str, list[float | None]]] | None = None,
    thresholds: GateThresholds | None = None,
) -> FactorEvidence:
    """Run all seven gates for one factor on one group."""
    limits = thresholds or GateThresholds()
    usable = {symbol: values for symbol, values in series.items() if symbol in forwards}
    gates = [
        gate_coverage(usable, limits),
        gate_finiteness(usable, limits),
        gate_dispersion(usable, limits),
    ]
    # The remaining gates are only meaningful on data that got this far; running
    # them anyway would report numbers computed on a column of nulls as if they
    # meant something.
    if all(gate.passed for gate in gates):
        gates.append(gate_predictive(usable, forwards, horizon=horizon, thresholds=limits))
    else:
        gates.append(GateOutcome("predictive", False, {}, "前置闸门未通过，未做预测性检验"))
    if gates[0].passed:
        gates.append(gate_persistence(usable, limits))
        gates.append(gate_cost(usable, forwards, horizon=horizon, thresholds=limits))
    else:
        gates.append(GateOutcome("persistence", False, {}, "覆盖不足，未评估持续性"))
        gates.append(GateOutcome("cost", False, {}, "覆盖不足，未评估成本"))
    gates.append(gate_redundancy(usable, selected or {}, limits))
    return FactorEvidence(
        factor_id=factor_id,
        family=family,
        group=group,
        interval=interval,
        horizon_bars=horizon,
        gates=gates,
        symbols=sorted(usable),
    )


# --------------------------------------------------------------------------
# the scan
# --------------------------------------------------------------------------

@dataclass
class ScanRequest:
    group: str
    interval: str
    horizon_bars: int
    symbols: list[str]
    factor_ids: list[str]
    bars: int = 2_000
    library_target: int = 30
    library_minimum: int = 15
    thresholds: GateThresholds = field(default_factory=GateThresholds)


def group_symbols(group: str) -> list[str]:
    """The venue symbols of a validation group.

    SOXL/SOXS are leveraged ETFs and are deliberately not in the stock group's
    cross-section (D5); BTC/ETH are their own group for the same reason a
    two-name cross-section is not a cross-section.
    """
    from .config.instruments import INSTRUMENTS

    if group == "stock":
        return [item.venue_symbol for item in INSTRUMENTS if item.product_type == "stock"]
    if group == "leveraged_etf":
        return [item.venue_symbol for item in INSTRUMENTS if item.product_type == "etf"]
    if group == "crypto":
        return [item.venue_symbol for item in INSTRUMENTS if item.is_crypto]
    raise ValueError(f"未知分组：{group}")


def _read_group_bars(home: Path, request: ScanRequest) -> tuple[dict[str, list[float]], dict[str, list[int]]]:
    from .datahub.db import Database
    from .datahub.view import read_history

    database = Database(home / "quantdesk.db")
    closes: dict[str, list[float]] = {}
    times: dict[str, list[int]] = {}
    for symbol in request.symbols:
        snapshot = read_history(
            database, symbol=symbol, interval=request.interval, bars=request.bars,
            display_symbol=symbol, product_type="crypto",
        )
        closes[symbol] = [float(bar["close"]) for bar in snapshot.bars]
        times[symbol] = [int(bar["ts"]) for bar in snapshot.bars]
    return closes, times


def _candles_for_plugin(closes: dict[str, list[float]], times: dict[str, list[int]],
                        highs: dict, lows: dict, opens: dict, volumes: dict, symbol: str) -> list[dict]:
    return [
        {
            "time": times[symbol][index],
            "open": opens[symbol][index],
            "high": highs[symbol][index],
            "low": lows[symbol][index],
            "close": closes[symbol][index],
            "volume": volumes[symbol][index],
            "turnover": closes[symbol][index] * volumes[symbol][index] or None,
        }
        for index in range(len(closes[symbol]))
    ]


def scan(request: ScanRequest, *, home: Path | None = None) -> dict[str, Any]:
    """Compute every factor on the group's stored bars and run the gates."""
    from .config.settings import quantdesk_home
    from .plugins import PluginManager, PluginRegistry
    from .plugins.protocol import FactorComputeRequest

    home = home or quantdesk_home()
    manager = PluginManager(home)
    provider = next(
        (
            record.manifest.id
            for record in manager.discover()[0]
            if record.enabled and "factor_provider" in record.manifest.capabilities
        ),
        None,
    )
    if provider is None:
        raise RuntimeError("没有启用的因子提供者，无法评估闸门")
    registry = PluginRegistry(manager)

    from .datahub.db import Database
    from .datahub.view import read_history
    from .factors import _carry_inputs, _oi_interval

    database = Database(home / "quantdesk.db")
    bars: dict[str, list[dict]] = {}
    closes: dict[str, list[float]] = {}
    carry: dict[str, tuple[list[dict], list[dict]]] = {}
    for symbol in request.symbols:
        snapshot = read_history(
            database, symbol=symbol, interval=request.interval, bars=request.bars,
            display_symbol=symbol, product_type="crypto", with_funding=True,
        )
        rows = []
        for bar in snapshot.bars:
            rows.append({
                "time": int(bar["ts"]), "open": float(bar["open"]), "high": float(bar["high"]),
                "low": float(bar["low"]), "close": float(bar["close"]),
                "volume": float(bar.get("volume") or 0.0),
                "turnover": float(bar["close"]) * float(bar.get("volume") or 0.0) or None,
            })
        bars[symbol] = rows
        closes[symbol] = [row["close"] for row in rows]
        # Funding and open interest are part of what a factor is allowed to read, so
        # a scan that omitted them would judge the carry/positioning families on
        # missing inputs and call the result "no edge".
        carry[symbol] = _carry_inputs(database, symbol, request.interval, rows,
                                      oi_interval=_oi_interval(request.interval))[:2]
    if not bars:
        raise RuntimeError(f"分组 {request.group} 没有任何K线")

    series: dict[str, dict[str, list[float | None]]] = {}
    warnings: list[str] = []
    from .factors import _batches

    for symbol, candles in bars.items():
        # The plugin's stdout is capped at 1 MB by the runtime, so a wide request
        # goes out in batches that fit - the same rule the engine's own factor runs
        # follow, for the same reason.
        for batch in _batches(len(candles), request.factor_ids):
            response = registry.compute_factors(
                provider,
                FactorComputeRequest(
                    symbol=symbol, timeframe=request.interval, snapshotHash="gate-scan",
                    factorIds=batch, candles=candles,
                    funding=carry[symbol][0], openInterest=carry[symbol][1],
                ),
            )
            warnings.extend(f"{symbol}: {item}" for item in response.warnings)
            for entry in response.series:
                values = [point.value for point in entry.values]
                series.setdefault(entry.factorId, {})[symbol] = values

    forwards = forward_returns(closes, request.horizon_bars)
    evidence: list[FactorEvidence] = []
    selected: dict[str, dict[str, list[float | None]]] = {}
    ordered = sorted(
        request.factor_ids,
        key=lambda factor_id: 0 if factor_id.startswith("vibe.") else 1,
    )
    for factor_id in ordered:
        per_symbol = series.get(factor_id, {})
        family = "quantdesk" if factor_id.startswith("vibe.") else "vibe-trading"
        outcome = evaluate(
            factor_id,
            family=family,
            group=request.group,
            interval=request.interval,
            horizon=request.horizon_bars,
            series=per_symbol,
            forwards=forwards,
            selected=selected,
            thresholds=request.thresholds,
        )
        evidence.append(outcome)
        if outcome.passed and len(selected) < request.library_target:
            selected[factor_id] = per_symbol

    # A library smaller than the floor is reported as such: 15 was the plan's floor
    # because a search space narrower than that cannot express a real hypothesis.
    clusters = _duplicate_clusters(series)
    return {
        "group": request.group,
        "interval": request.interval,
        "horizonBars": request.horizon_bars,
        "provider": provider,
        "symbols": sorted(bars),
        "barsPerSymbol": {symbol: len(rows) for symbol, rows in bars.items()},
        "thresholds": asdict(request.thresholds),
        "libraryTarget": request.library_target,
        "libraryMinimum": request.library_minimum,
        "libraryComplete": len(selected) >= request.library_minimum,
        "library": [
            {
                "factorId": factor_id,
                "family": "quantdesk" if factor_id.startswith("vibe.") else "vibe-trading",
                "evidence": next(item.as_dict() for item in evidence if item.factor_id == factor_id),
            }
            for factor_id in selected
        ],
        "evidence": [item.as_dict() for item in evidence],
        "duplicateClusters": clusters,
        "distinctBehaviours": len(clusters),
        "warnings": warnings,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _duplicate_clusters(series: dict[str, dict[str, list[float | None]]]) -> list[dict[str, Any]]:
    """Factors that are the same factor twice, judged by identical rank order.

    Upstream metadata can call two things different names while the code computes a
    monotone transform of one of them - `qlib158_beta60` is `(c - c[-60]) / c / 60`,
    i.e. `roc60` rescaled, not a beta. Any test that ranks the values cannot tell
    them apart, so the library must not pretend it has two independent factors.
    """
    buckets: dict[tuple, list[str]] = {}
    for factor_id, per_symbol in sorted(series.items()):
        signatures = []
        usable = True
        for symbol in sorted(per_symbol):
            values = per_symbol[symbol]
            present = [(index, value) for index, value in enumerate(values) if value is not None]
            if len(present) < 60:
                # Too short to compare. Keying these together would report "four
                # factors that returned nothing" as "four copies of one factor".
                usable = False
                break
            ranks = _ranks([value for _, value in present])
            signatures.append((symbol, tuple(round(rank, 6) for rank in ranks), len(present)))
        if not usable or not signatures:
            continue
        buckets.setdefault(tuple(signatures), []).append(factor_id)
    clusters = [
        {"size": len(members), "factors": members}
        for members in buckets.values()
        if len(members) > 1
    ]
    return sorted(clusters, key=lambda item: (-item["size"], item["factors"][0]))


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对一组因子跑七道闸门，产出受控因子库")
    parser.add_argument("--group", required=True, choices=("stock", "leveraged_etf", "crypto"))
    parser.add_argument("--interval", default="1h", choices=("15m", "1h", "4h", "1d", "1w"))
    parser.add_argument("--horizon", type=int, default=0, help="前向收益的 bar 数，默认 24（1h）/1（1d）")
    parser.add_argument("--bars", type=int, default=2_000)
    parser.add_argument("--factors", default="all", help="all 或逗号分隔的因子 ID")
    parser.add_argument("--out", default="", help="把报告写到这个 JSON 文件")
    args = parser.parse_args(list(argv) if argv is not None else None)

    from .config.settings import quantdesk_home
    from .plugins import PluginManager
    from .plugins.protocol import FactorCatalogResult

    home = quantdesk_home()
    manager = PluginManager(home)
    provider = next(
        (record.manifest.id for record in manager.discover()[0]
         if record.enabled and "factor_provider" in record.manifest.capabilities),
        None,
    )
    if provider is None:
        print("没有启用的因子提供者", file=sys.stderr)
        return 2
    from .plugins import PluginRegistry

    catalog: FactorCatalogResult = PluginRegistry(manager).factor_catalog(provider)
    available = [item for item in catalog.factors if args.interval in item.supportedTimeframes]
    if args.factors == "all":
        wanted = [item.id for item in available]
    else:
        wanted = [part.strip() for part in args.factors.split(",") if part.strip()]
    horizon = args.horizon or (24 if args.interval in {"15m", "1h", "4h"} else 1)
    request = ScanRequest(
        group=args.group,
        interval=args.interval,
        horizon_bars=horizon,
        symbols=group_symbols(args.group),
        factor_ids=wanted,
        bars=args.bars,
    )
    report = scan(request, home=home)
    payload = json.dumps(report, ensure_ascii=False, indent=1)
    from .factor_library import save_report

    stored = save_report(report, home)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    passed = [item for item in report["evidence"] if item["passed"]]
    print(
        f"{args.group}/{args.interval}：{len(report['evidence'])} 个因子，{len(passed)} 个通过全部闸门，"
        f"受控库 {len(report['library'])} 个（下限 {report['libraryMinimum']}，"
        f"{'达标' if report['libraryComplete'] else '未达标'}）"
    )
    for entry in report["library"]:
        predictive = next(g for g in entry["evidence"]["gates"] if g["name"] == "predictive")
        cost = next(g for g in entry["evidence"]["gates"] if g["name"] == "cost")
        print(
            f"  {entry['factorId']:26s} IC {predictive['metric'].get('ic', 0):+.4f} "
            f"p={predictive['metric'].get('pValue', 1):.3f} 净 {cost['metric'].get('netBps', 0):+.2f} bps"
        )
    print(f"报告 -> {stored}")
    if args.out:
        print(f"另存 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
