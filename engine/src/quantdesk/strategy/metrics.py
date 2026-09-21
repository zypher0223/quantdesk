"""Performance statistics for a backtest, computed one way for every caller.

A rule backtest produces an equity curve and a trade list; whether a strategy is
worth anything is decided by the numbers derived from them. This module owns that
arithmetic so the API, the CLI and the walk-forward report cannot disagree, and so
each figure states the period it was annualised over.

Everything here is computed from the equity curve the engine already produced:
no look-ahead, no re-simulation, and no metric that needs data the run did not
have. Ratios are annualised from the bar interval, which the caller must supply,
because "Sharpe" without a period is not a number.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

# A trading year is taken as 365 days for crypto (24/7) and 252 sessions for
# tokenised equities. The caller says which; the default is the harsher 365.
CRYPTO_SESSIONS_PER_YEAR = 365.0
EQUITY_SESSIONS_PER_YEAR = 252.0

# Below this much history an annualised figure is an extrapolation, not a
# measurement: a good week compounds into an absurd CAGAR. The number is still
# reported, with the window length next to it and a warning attached.
MIN_YEARS_FOR_ANNUALISATION = 0.08  # ~29 days

TRADING_DAYS_MS = 86_400_000.0


@dataclass
class PerformanceMetrics:
    """Everything the validation report quotes, plus how it was annualised."""

    bars: int = 0
    period_ms: int = 0
    window_days: float = 0.0
    years: float = 0.0
    periods_per_year: float = 0.0
    calendar_days_per_year: float = CRYPTO_SESSIONS_PER_YEAR

    initial_equity: float = 0.0
    final_equity: float = 0.0
    total_return_pct: float = 0.0
    annualised_return_pct: float | None = None
    benchmark_return_pct: float | None = None
    excess_return_pct: float | None = None

    max_drawdown_pct: float = 0.0
    max_drawdown_duration_bars: int = 0
    volatility_pct: float | None = None
    downside_deviation_pct: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    calmar: float | None = None

    trades: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float | None = None
    payoff_ratio: float | None = None
    expectancy: float | None = None
    average_win: float | None = None
    average_loss: float | None = None
    largest_win: float | None = None
    largest_loss: float | None = None
    max_consecutive_losses: int = 0
    exposure_pct: float = 0.0
    liquidations: int = 0
    total_fees: float = 0.0
    total_funding: float = 0.0
    total_return_after_costs_pct: float = 0.0

    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _returns(equity: list[float]) -> list[float]:
    """Simple returns between consecutive marks, skipping non-positive equity."""
    out: list[float] = []
    for previous, current in zip(equity, equity[1:]):
        if previous <= 0:
            continue
        out.append(current / previous - 1.0)
    return out


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def _annualisation(interval: str | None, period_ms: int, calendar_days: float) -> tuple[float, float]:
    """Periods per year and years covered, from the bar interval."""
    if period_ms <= 0:
        return 0.0, 0.0
    periods_per_day = TRADING_DAYS_MS / period_ms
    periods_per_year = periods_per_day * calendar_days
    return periods_per_year, periods_per_day


def compute_metrics(
    equity_curve: list[dict],
    trades: list[dict] | None = None,
    *,
    interval_ms: int | None = None,
    initial_capital: float | None = None,
    calendar_days_per_year: float = CRYPTO_SESSIONS_PER_YEAR,
    total_fees: float = 0.0,
    total_funding: float = 0.0,
    risk_free_rate: float = 0.0,
) -> PerformanceMetrics:
    """Derive every reported statistic from one equity curve.

    `equity_curve` is the engine's `[{time, equity, ...}]`. `interval_ms` is the
    bar length: without it the annualised figures are withheld rather than
    guessed, because annualising by the wrong period is worse than not reporting.
    """
    warnings: list[str] = []
    points = [point for point in equity_curve if point.get("equity") is not None]
    if not points:
        return PerformanceMetrics(warnings=["没有净值曲线，无法计算绩效指标"])

    equity = [float(point["equity"]) for point in points]
    times = [int(point.get("time") or 0) for point in points]
    start = float(initial_capital if initial_capital is not None else equity[0])
    metrics = PerformanceMetrics(
        bars=len(equity),
        initial_equity=start,
        final_equity=equity[-1],
        calendar_days_per_year=calendar_days_per_year,
        total_fees=round(float(total_fees), 6),
        total_funding=round(float(total_funding), 6),
    )
    metrics.total_return_pct = round((equity[-1] / start - 1) * 100, 6) if start > 0 else 0.0
    metrics.total_return_after_costs_pct = metrics.total_return_pct

    if interval_ms and interval_ms > 0:
        metrics.period_ms = int(interval_ms)
        metrics.periods_per_year, periods_per_day = _annualisation(interval_ms, interval_ms, calendar_days_per_year)
        span_ms = max(0, times[-1] - times[0]) + interval_ms
        metrics.years = span_ms / (TRADING_DAYS_MS * calendar_days_per_year)
        metrics.window_days = round(span_ms / TRADING_DAYS_MS, 4)
        if metrics.years > 0 and equity[-1] > 0 and start > 0:
            growth = equity[-1] / start
            metrics.annualised_return_pct = round((growth ** (1 / metrics.years) - 1) * 100, 6)
        if 0 < metrics.years < MIN_YEARS_FOR_ANNUALISATION:
            warnings.append(
                f"样本只有 {metrics.window_days:.1f} 天，年化收益与 Calmar 是外推值，不代表可持续水平"
            )
        _ = periods_per_day
    else:
        warnings.append("未提供K线周期，年化收益、Sharpe、Sortino 与 Calmar 不予计算")

    # -- drawdown ---------------------------------------------------------
    peak = start
    trough = start
    peak_index = 0
    max_dd = 0.0
    max_dd_bars = 0
    for index, value in enumerate(equity):
        if value > peak:
            peak = value
            peak_index = index
            trough = value
        elif value < trough:
            trough = value
            depth = (peak - trough) / peak * 100 if peak > 0 else 0.0
            if depth > max_dd:
                max_dd = depth
                max_dd_bars = index - peak_index
    metrics.max_drawdown_pct = round(max_dd, 6)
    metrics.max_drawdown_duration_bars = max_dd_bars

    # -- ratios -----------------------------------------------------------
    returns = _returns(equity)
    if len(returns) >= 2 and metrics.periods_per_year > 0:
        deviation = _std(returns)
        metrics.volatility_pct = round(deviation * math.sqrt(metrics.periods_per_year) * 100, 6)
        downside = [value for value in returns if value < 0]
        if len(downside) >= 2:
            downside_dev = math.sqrt(sum(value**2 for value in downside) / len(downside))
            metrics.downside_deviation_pct = round(downside_dev * math.sqrt(metrics.periods_per_year) * 100, 6)
        per_period_risk_free = risk_free_rate / metrics.periods_per_year
        if deviation > 0:
            metrics.sharpe = round(
                (sum(returns) / len(returns) - per_period_risk_free) / deviation * math.sqrt(metrics.periods_per_year), 6
            )
        if metrics.downside_deviation_pct:
            downside_dev = metrics.downside_deviation_pct / 100 / math.sqrt(metrics.periods_per_year)
            if downside_dev > 0:
                metrics.sortino = round(
                    (sum(returns) / len(returns) - per_period_risk_free) / downside_dev * math.sqrt(metrics.periods_per_year),
                    6,
                )
    if metrics.max_drawdown_pct > 0 and metrics.annualised_return_pct is not None:
        metrics.calmar = round(metrics.annualised_return_pct / metrics.max_drawdown_pct, 6)

    # -- trades -----------------------------------------------------------
    rows = trades or []
    metrics.trades = len(rows)
    liquidation_count = sum(1 for row in rows if row.get("liquidated"))
    metrics.liquidations = liquidation_count
    if rows:
        pnls = [float(row.get("net_pnl") or 0.0) for row in rows]
        wins = [value for value in pnls if value > 0]
        losses = [value for value in pnls if value < 0]
        metrics.win_rate_pct = round(len(wins) / len(rows) * 100, 4)
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        metrics.profit_factor = round(gross_win / gross_loss, 6) if gross_loss else (None if gross_win else 0.0)
        metrics.average_win = round(sum(wins) / len(wins), 6) if wins else None
        metrics.average_loss = round(sum(losses) / len(losses), 6) if losses else None
        if metrics.average_win is not None and metrics.average_loss:
            metrics.payoff_ratio = round(metrics.average_win / abs(metrics.average_loss), 6)
        metrics.expectancy = round(sum(pnls) / len(rows), 6)
        metrics.largest_win = round(max(pnls), 6)
        metrics.largest_loss = round(min(pnls), 6)
        streak = 0
        for value in pnls:
            if value < 0:
                streak += 1
                metrics.max_consecutive_losses = max(metrics.max_consecutive_losses, streak)
            else:
                streak = 0
        bars_held = sum(int(row.get("bars_held") or 0) for row in rows)
        metrics.exposure_pct = round(bars_held / len(equity) * 100, 4) if equity else 0.0

    metrics.warnings = warnings
    return metrics


def benchmark_buy_and_hold(
    candles: list[dict],
    *,
    initial_capital: float,
    interval_ms: int | None = None,
    calendar_days_per_year: float = CRYPTO_SESSIONS_PER_YEAR,
    fee_bps: float = 0.0,
) -> PerformanceMetrics:
    """Buy the first close, hold to the last: the bar every strategy must clear.

    Costs are charged once on entry and once on exit, so the benchmark is not
    handed a free round trip the strategy has to pay for.
    """
    if not candles:
        return PerformanceMetrics(warnings=["没有K线，无法计算基准"])
    ordered = sorted(candles, key=lambda row: int(row["ts"]))
    entry = float(ordered[0]["close"])
    exit_price = float(ordered[-1]["close"])
    cost = (fee_bps / 10_000) * 2
    final = initial_capital * (exit_price / entry) * (1 - cost) if entry > 0 else initial_capital
    curve = [
        {"time": int(row["ts"]), "equity": initial_capital * (float(row["close"]) / entry) * (1 - cost / 2)}
        for row in ordered
    ]
    curve[-1] = {"time": int(ordered[-1]["ts"]), "equity": final}
    metrics = compute_metrics(
        curve,
        [],
        interval_ms=interval_ms,
        initial_capital=initial_capital,
        calendar_days_per_year=calendar_days_per_year,
        total_fees=initial_capital * cost,
    )
    metrics.benchmark_return_pct = metrics.total_return_pct
    return metrics


def compare_to_benchmark(metrics: PerformanceMetrics, benchmark: PerformanceMetrics) -> PerformanceMetrics:
    """Attach the benchmark and the excess return to a strategy's metrics."""
    metrics.benchmark_return_pct = benchmark.total_return_pct
    metrics.excess_return_pct = round(metrics.total_return_pct - benchmark.total_return_pct, 6)
    return metrics
