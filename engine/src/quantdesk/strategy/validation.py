"""Strategy validation: segments, walk-forward, parameter search, leakage checks.

A backtest answers "what would this rule have returned". It does not answer "was
this rule fitted to the sample", which is the question that decides whether a
result means anything. This module adds the second half:

* train / validation / test segments that are cut once, by time, and never
  shuffled, so a parameter chosen on the validation segment is scored on bars the
  choice never saw;
* walk-forward windows that re-select parameters on each training window and
  score them on the window that follows, which is the closest thing to how the
  rule would actually have been traded;
* a parameter grid search whose overfitting warnings compare in-sample against
  out-of-sample, and say so when the winner is only good on the sample it was
  chosen from;
* multi-contract portfolio runs with explicit capital allocation;
* provenance: data range and hash, strategy and parameter version, cost model and
  run timestamp, so a result can be reproduced rather than believed;
* leakage checks that look for the two mistakes that invalidate everything else:
  signals that change when future bars are removed, and bars that were not closed
  when the signal was taken.

Nothing here re-implements the engine. Every run goes through `run_backtest`, so
the costs, the risk ladder and the mark-price rules stay in one place.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from ..backtest import BacktestConfig, BacktestResult, run_backtest
from ..risk import RiskProfile
from .metrics import (
    CRYPTO_SESSIONS_PER_YEAR,
    EQUITY_SESSIONS_PER_YEAR,
    PerformanceMetrics,
    benchmark_buy_and_hold,
    compare_to_benchmark,
    compute_metrics,
)

# A parameter set is only interesting if it traded. Below this many trades the
# statistics are noise regardless of how good the return looks.
MIN_TRADES_FOR_CONFIDENCE = 10
# Out-of-sample degradation beyond this fraction of the in-sample result is
# reported as overfitting rather than as a result.
OVERFIT_DEGRADATION_LIMIT = 0.5

StrategyRunner = Callable[[list[dict], BacktestConfig], list[int | None]]
SignalSource = Callable[[list[dict], dict[str, Any]], list[int | None]]


@dataclass
class Segment:
    """One contiguous, chronologically ordered slice of the sample."""

    name: str
    from_ts: int
    to_ts: int
    bars: int
    # Bars carried in front of the segment so indicators are warm at its start.
    # They are read by the strategy but never scored: both the metric window and
    # the trade list start at `from_ts`.
    warmup: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StrategyProvenance:
    """Everything needed to reproduce one run."""

    strategy_id: str
    parameters: dict[str, Any]
    symbol: str | None
    universe: list[str]
    interval: str | None
    bars: int
    from_ts: int | None
    to_ts: int | None
    data_hash: str
    cost_model: dict[str, Any]
    risk_source: str | None
    engine: str = "quantdesk.backtest.run_backtest"
    engine_version: str = "2"
    run_at: int = field(default_factory=lambda: int(time.time() * 1000))
    metrics_version: str = "1"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def data_fingerprint(candles: list[dict]) -> str:
    """A stable hash of the bars a run used.

    Two runs over the same bars produce the same fingerprint, so a stored result
    can be checked against the data rather than trusted.
    """
    digest = hashlib.sha256()
    for row in sorted(candles, key=lambda item: int(item["ts"])):
        digest.update(
            f"{int(row['ts'])}|{float(row['open']):.10g}|{float(row['high']):.10g}|"
            f"{float(row['low']):.10g}|{float(row['close']):.10g}|{float(row.get('volume') or 0):.10g}\n".encode()
        )
    return digest.hexdigest()[:16]


def sessions_per_year(product_type: str | None) -> float:
    """Tokenised equities do not trade every calendar day; crypto does."""
    return EQUITY_SESSIONS_PER_YEAR if product_type and product_type != "crypto" else CRYPTO_SESSIONS_PER_YEAR


# -- segmentation ------------------------------------------------------------


def split_segments(
    candles: list[dict],
    *,
    train: float = 0.6,
    validation: float = 0.2,
    warmup: int = 0,
    names: tuple[str, str, str] = ("train", "validation", "test"),
) -> list[Segment]:
    """Cut the sample into three consecutive segments, oldest first.

    The cut is by time and never shuffled: shuffling bars is how a strategy ends
    up trained on the future it is then scored against.
    """
    ordered = sorted(candles, key=lambda row: int(row["ts"]))
    if not ordered:
        raise ValueError("没有K线可以分段")
    if train <= 0 or validation <= 0 or train + validation >= 1:
        raise ValueError("训练/验证比例必须在 (0,1) 内且留出测试段")
    total = len(ordered)
    train_end = int(total * train)
    validation_end = train_end + int(total * validation)
    # Each segment needs a start and a next bar to trade from, so each needs two.
    if train_end < 2 or validation_end - train_end < 2 or total - validation_end < 2:
        raise ValueError(
            f"样本 {total} 根不足以切成三段（训练 {train_end}、验证 {validation_end - train_end}、"
            f"测试 {total - validation_end}），请放宽区间或提供更多历史"
        )
    bounds = [(0, train_end), (train_end, validation_end), (validation_end, total)]
    segments: list[Segment] = []
    for index, (name, (start, end)) in enumerate(zip(names, bounds)):
        rows = ordered[start:end]
        # Every segment after the first carries warmup bars in front of it, so its
        # own start is not mistaken for the beginning of history. Those bars are
        # read, never scored.
        lead = min(int(warmup), start) if index > 0 else 0
        segments.append(
            Segment(
                name=name,
                from_ts=int(rows[0]["ts"]),
                to_ts=int(rows[-1]["ts"]),
                bars=len(rows),
                warmup=lead,
            )
        )
    return segments


def segment_slice(candles: list[dict], segment: Segment) -> list[dict]:
    """The bars of one segment, including the warmup bars in front of it."""
    ordered = sorted(candles, key=lambda row: int(row["ts"]))
    first = next((index for index, row in enumerate(ordered) if int(row["ts"]) >= segment.from_ts), None)
    if first is None:
        return []
    last = first
    while last < len(ordered) and int(ordered[last]["ts"]) <= segment.to_ts:
        last += 1
    start = max(0, first - int(segment.warmup))
    return ordered[start:last]


def _score_offset(segment: Segment) -> int:
    """How many warmup bars at the front of a slice are not scored."""
    return int(segment.warmup)


# -- one run -----------------------------------------------------------------


@dataclass
class SegmentResult:
    segment: Segment
    metrics: PerformanceMetrics
    warnings: list[str] = field(default_factory=list)
    trades: int = 0
    # The scored part of the equity curve: warmup bars are excluded, so the curve
    # starts where the segment starts.
    metrics_curve: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"segment": self.segment.as_dict(), **self.metrics.as_dict(), "warnings": self.warnings}


def evaluate_segment(
    candles: list[dict],
    config: BacktestConfig,
    segment: Segment,
    *,
    signals: list[int | None] | None = None,
    funding: list[dict] | None = None,
    marks: list[dict] | None = None,
    risk_profile: RiskProfile | None = None,
    interval: str | None = None,
    interval_ms: int | None = None,
    product_type: str | None = None,
    fee_bps: float | None = None,
    initial_capital: float | None = None,
    risk_free_rate: float = 0.0,
) -> tuple[SegmentResult, BacktestResult]:
    """Score one segment, with the benchmark computed over the same bars."""
    window = segment_slice(candles, segment)
    if not window:
        raise ValueError(f"分段 {segment.name} 没有数据")
    trim = _score_offset(segment)
    funding_rows = _within(funding or [], int(window[0]["ts"]), int(window[-1]["ts"]))
    mark_rows = _within(marks or [], int(window[0]["ts"]), int(window[-1]["ts"]))
    result = run_backtest(
        window,
        config,
        funding=funding_rows,
        marks=mark_rows,
        risk_profile=risk_profile,
        interval=interval,
        signal_events=signals,
    )
    capital = float(initial_capital if initial_capital is not None else config.initial_capital)
    curve = [point for point in result.equity_curve if trim == 0 or int(point["time"]) >= segment.from_ts]
    trades = [row for row in result.as_dict()["trades"] if trim == 0 or int(row["entry_time"]) >= segment.from_ts]
    metrics = compute_metrics(
        curve,
        trades,
        interval_ms=interval_ms,
        initial_capital=capital,
        calendar_days_per_year=sessions_per_year(product_type),
        total_fees=result.total_fees,
        total_funding=result.total_funding,
        risk_free_rate=risk_free_rate,
    )
    benchmark = benchmark_buy_and_hold(
        [row for row in window if int(row["ts"]) >= segment.from_ts],
        initial_capital=capital,
        interval_ms=interval_ms,
        calendar_days_per_year=sessions_per_year(product_type),
        fee_bps=float(fee_bps if fee_bps is not None else config.fee_bps),
    )
    compare_to_benchmark(metrics, benchmark)
    segment_result = SegmentResult(
        segment=segment,
        metrics=metrics,
        warnings=list(result.warnings),
        trades=len(trades),
        metrics_curve=curve,
    )
    return segment_result, result


def _within(rows: Iterable[dict], start: int, end: int) -> list[dict]:
    return [row for row in rows if start <= int(row["ts"]) <= end]


# -- parameter search --------------------------------------------------------


@dataclass
class ParameterCandidate:
    parameters: dict[str, Any]
    in_sample: PerformanceMetrics
    out_of_sample: PerformanceMetrics | None = None
    degraded: bool = False
    selected: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "parameters": self.parameters,
            "inSample": self.in_sample.as_dict(),
            "outOfSample": self.out_of_sample.as_dict() if self.out_of_sample else None,
            "degraded": self.degraded,
            "selected": self.selected,
        }


def parameter_grid(grid: dict[str, list[Any]] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand a grid into parameter dictionaries, largest product first.

    Accepts either a cross-product grid (`{parameter: [values]}`) or an already
    expanded list of parameter sets. Two callers legitimately hold the second shape -
    the CPA path builds its grid through the generic `parameterGrid`, and a caller that
    filtered out incoherent combinations before searching has already done the
    expansion - so both are accepted rather than forcing one of them to re-expand.
    """
    if isinstance(grid, list):
        return [dict(item) for item in grid] or [{}]
    if not grid:
        return [{}]
    keys = sorted(grid)
    combinations: list[dict[str, Any]] = [{}]
    for key in keys:
        values = list(grid[key])
        if not values:
            raise ValueError(f"参数 {key} 的候选值为空")
        combinations = [{**combination, key: value} for combination in combinations for value in values]
    return combinations


def search_parameters(
    candles: list[dict],
    config: BacktestConfig,
    grid: dict[str, list[Any]],
    *,
    signal_source: SignalSource,
    train: Segment,
    validation: Segment,
    test: Segment | None = None,
    funding: list[dict] | None = None,
    marks: list[dict] | None = None,
    risk_profile: RiskProfile | None = None,
    interval: str | None = None,
    interval_ms: int | None = None,
    product_type: str | None = None,
    risk_free_rate: float = 0.0,
    on_candidate: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Score every candidate on train and validation, and rank by validation.

    The selection rule is fixed and stated: the parameter set is chosen on the
    validation segment, never on the test segment. Scoring on the test segment
    happens once, for the single selected set, and is reported separately.
    """
    candidates: list[ParameterCandidate] = []
    skipped: list[dict[str, Any]] = []
    plan = parameter_grid(grid)
    for index, parameters in enumerate(plan, start=1):
        if on_candidate is not None:
            # A long search says where it is; the run queue turns this into a
            # progress figure instead of a spinner that reports nothing.
            on_candidate(index, len(plan))
        merged = {**config.strategy_params, **parameters}
        if not _coherent(config.strategy_id, merged):
            # A grid is written as a cross product, so it always contains pairs the
            # strategy itself rejects. They are recorded as skipped rather than
            # allowed to abort the whole search.
            skipped.append({"parameters": merged, "reason": "参数组合不被策略接受"})
            continue
        train_signals = signal_source(segment_slice(candles, train), merged)
        run_config = _with_params(config, merged)
        train_result, _ = evaluate_segment(
            candles, run_config, train,
            signals=train_signals, funding=funding, marks=marks, risk_profile=risk_profile,
            interval=interval, interval_ms=interval_ms, product_type=product_type,
            risk_free_rate=risk_free_rate,
        )
        candidate = ParameterCandidate(parameters=merged, in_sample=train_result.metrics)
        validation_signals = signal_source(segment_slice(candles, validation), merged)
        validation_result, _ = evaluate_segment(
            candles, run_config, validation,
            signals=validation_signals, funding=funding, marks=marks, risk_profile=risk_profile,
            interval=interval, interval_ms=interval_ms, product_type=product_type,
            risk_free_rate=risk_free_rate,
        )
        candidate.out_of_sample = validation_result.metrics
        candidate.degraded = _degraded(candidate.in_sample, candidate.out_of_sample)
        candidates.append(candidate)

    def rank(candidate: ParameterCandidate) -> float:
        metrics = candidate.out_of_sample or candidate.in_sample
        # Return alone is not a ranking: a rule that only wins by taking
        # outsized drawdown is not better than a steadier one.
        return _objective(metrics)

    candidates.sort(key=rank, reverse=True)
    if candidates:
        candidates[0].selected = True
    best = candidates[0] if candidates else None
    test_result: SegmentResult | None = None
    if best is not None and test is not None:
        merged = best.parameters
        test_signals = signal_source(segment_slice(candles, test), merged)
        test_result, _ = evaluate_segment(
            candles, _with_params(config, merged), test,
            signals=test_signals, funding=funding, marks=marks, risk_profile=risk_profile,
            interval=interval, interval_ms=interval_ms, product_type=product_type,
            risk_free_rate=risk_free_rate,
        )
    warnings = _overfit_warnings(best, candidates, test_result)
    if skipped and not candidates:
        warnings.append(f"网格中的 {len(skipped)} 组参数都被策略拒绝，没有可评估的候选")
    elif skipped:
        warnings.append(f"网格中有 {len(skipped)} 组参数被策略拒绝，已跳过")
    return {
        # The grid as the caller expressed it: a cross product, or the expanded list a
        # caller that pre-filtered its combinations already holds.
        "grid": (
            dict(grid) if isinstance(grid, dict)
            else {"combinations": [dict(item) for item in grid]}
        ),
        "candidates": len(candidates),
        # A compact ranking of every candidate, not just the winner: a
        # multiple-testing correction needs to know how wide the field was, and a
        # CSCV reading needs the losers' numbers too.
        "ranking": [_compact_candidate(candidate) for candidate in candidates],
        "skipped": skipped,
        "best": best.as_dict() if best else None,
        "test": test_result.as_dict() if test_result else None,
        "warnings": warnings,
    }


def _compact_candidate(candidate: ParameterCandidate) -> dict[str, Any]:
    """One candidate's headline numbers, small enough to keep in a result."""
    metrics = candidate.out_of_sample or candidate.in_sample
    payload = {
        "parameters": candidate.parameters,
        "selected": bool(candidate.selected),
        "degraded": bool(candidate.degraded),
    }
    for key in ("sharpe", "total_return_pct", "max_drawdown_pct", "trades"):
        value = getattr(metrics, key, None) if metrics is not None else None
        payload[key] = value
    return payload


def _coherent(strategy_id: str, parameters: dict[str, Any]) -> bool:
    """Can the strategy run with these parameters at all?"""
    if strategy_id == "ma_cross":
        fast = parameters.get("fastPeriod")
        slow = parameters.get("slowPeriod")
        if fast is None or slow is None:
            return True
        return int(slow) > int(fast) >= 2
    return True


def _with_params(config: BacktestConfig, parameters: dict[str, Any]) -> BacktestConfig:
    from dataclasses import replace

    updated = replace(config, strategy_params=dict(parameters))
    if "fastPeriod" in parameters:
        updated.fast_period = int(parameters["fastPeriod"])
    if "slowPeriod" in parameters:
        updated.slow_period = int(parameters["slowPeriod"])
    return updated


def ranking_return(metrics: PerformanceMetrics) -> float:
    """The return a candidate is ranked on.

    Annualising a ten-day window multiplies it by thirty-six, which turns noise
    into a winner. A window shorter than the annualisation threshold is therefore
    ranked on its actual return, and the report says which figure was used.
    """
    from .metrics import MIN_YEARS_FOR_ANNUALISATION

    if metrics.years >= MIN_YEARS_FOR_ANNUALISATION and metrics.annualised_return_pct is not None:
        return metrics.annualised_return_pct
    return metrics.total_return_pct


def _objective(metrics: PerformanceMetrics) -> float:
    """Rank candidates by risk-adjusted return, not by raw return.

    A parameter set that barely traded is not a winner, but it is also not proof
    that the others are better, so a thin out-of-sample segment is ranked below a
    traded one and the report says the sample was too thin to decide.
    """
    base = ranking_return(metrics)
    drawdown = max(metrics.max_drawdown_pct, 1.0)
    sharpe = metrics.sharpe if metrics.sharpe is not None else 0.0
    confidence = 0.0 if metrics.trades < 3 else min(1.0, metrics.trades / MIN_TRADES_FOR_CONFIDENCE)
    return base / drawdown + sharpe + confidence


def _degraded(in_sample: PerformanceMetrics, out_of_sample: PerformanceMetrics) -> bool:
    """Did the parameter set fall apart out of sample?

    Compares the same figure the ranking used. Comparing an annualised in-sample
    number against a short out-of-sample one would flag almost everything, since
    annualising a two-month window multiplies it by six.
    """
    inside = ranking_return(in_sample)
    outside = ranking_return(out_of_sample)
    if inside <= 0:
        return outside < inside
    return outside < inside * (1 - OVERFIT_DEGRADATION_LIMIT)


def _overfit_warnings(
    best: ParameterCandidate | None,
    candidates: list[ParameterCandidate],
    test: SegmentResult | None,
) -> list[str]:
    warnings: list[str] = []
    if best is None:
        return ["参数网格为空，未做任何搜索"]

    def value(metrics: PerformanceMetrics) -> float:
        return ranking_return(metrics)

    inside, outside = value(best.in_sample), value(best.out_of_sample) if best.out_of_sample else None
    if outside is not None and inside > 0 and outside < inside * (1 - OVERFIT_DEGRADATION_LIMIT):
        warnings.append(
            "过拟合迹象：训练段 {inside:.2f}% 到验证段 {outside:.2f}%，衰减超过 {limit:.0%}"
            "（区间收益 {inside_raw:.2f}% -> {outside_raw:.2f}%）".format(
                inside=inside,
                outside=outside,
                limit=OVERFIT_DEGRADATION_LIMIT,
                inside_raw=best.in_sample.total_return_pct,
                outside_raw=best.out_of_sample.total_return_pct if best.out_of_sample else 0.0,
            )
        )
    if outside is not None and outside <= 0 < inside:
        warnings.append("训练段为正、验证段为负：该参数只在样本内有效")
    for label, metrics in (("训练", best.in_sample), ("验证", best.out_of_sample)):
        if metrics is None:
            continue
        if (metrics.annualised_return_pct or 0) > 10_000 or (metrics.annualised_return_pct or 0) < -99:
            warnings.append(
                f"{label}段年化 {metrics.annualised_return_pct:.0f}% 是把 {metrics.window_days:.0f} 天外推到一年的结果，"
                "不代表可持续水平"
            )

    spread = [value(candidate.out_of_sample or candidate.in_sample) for candidate in candidates]
    if len(spread) >= 4:
        spread.sort()
        best_value = value(best.out_of_sample or best.in_sample)
        median = spread[len(spread) // 2]
        if median != 0 and best_value > median * 3:
            warnings.append(
                f"参数敏感：验证段最优 {best_value:.2f}% 远高于中位数 {median:.2f}%，结果对参数取值高度敏感"
            )
    if best.in_sample.trades < MIN_TRADES_FOR_CONFIDENCE:
        warnings.append(f"训练段只有 {best.in_sample.trades} 笔成交，统计意义不足")
    if best.out_of_sample and best.out_of_sample.trades < MIN_TRADES_FOR_CONFIDENCE:
        warnings.append(f"验证段只有 {best.out_of_sample.trades} 笔成交，统计意义不足")
    short_windows = [
        segment
        for segment in (best.in_sample, best.out_of_sample, test.metrics if test else None)
        if segment is not None and 0 < segment.years < 0.08
    ]
    if short_windows:
        warnings.append("有分段短于 29 天，排序与告警使用区间总收益而非年化收益，避免外推放大噪声")
    if test is not None:
        test_value = value(test.metrics)
        if outside is not None and outside > 0 and test_value < outside * (1 - OVERFIT_DEGRADATION_LIMIT):
            warnings.append(f"测试段 {test_value:.2f}% 明显弱于验证段 {outside:.2f}%，需重新评估")
        if test.metrics.trades < MIN_TRADES_FOR_CONFIDENCE:
            warnings.append(f"测试段只有 {test.metrics.trades} 笔成交，不足以支撑结论")
    return warnings


# -- walk-forward ------------------------------------------------------------


@dataclass
class WalkForwardWindow:
    index: int
    train: Segment
    validation: Segment

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "train": self.train.as_dict(), "validation": self.validation.as_dict()}


def walk_forward_windows(
    candles: list[dict],
    *,
    windows: int = 4,
    train_fraction: float = 0.5,
    warmup: int = 0,
) -> list[WalkForwardWindow]:
    """Anchored walk-forward windows: each trains on more history than the last.

    Anchored rather than rolling, so the training window starts at the same bar
    every time and a change in the chosen parameters cannot come from the sample
    simply moving forward.
    """
    ordered = sorted(candles, key=lambda row: int(row["ts"]))
    total = len(ordered)
    if windows < 1:
        raise ValueError("walk-forward 至少需要 1 个窗口")
    if not 0 < train_fraction < 1:
        raise ValueError("训练比例必须在 (0,1) 内")
    anchor = int(total * train_fraction)
    remaining = total - anchor
    if remaining < windows * 2:
        raise ValueError(f"样本 {total} 根不足以切成 {windows} 个 walk-forward 窗口，请减少窗口或补充历史")
    step = remaining // windows
    out: list[WalkForwardWindow] = []
    for index in range(windows):
        train_end = anchor + index * step
        validation_end = total if index == windows - 1 else train_end + step
        train_rows = ordered[:train_end]
        validation_rows = ordered[train_end:validation_end]
        if len(train_rows) < 2 or len(validation_rows) < 2:
            continue
        out.append(
            WalkForwardWindow(
                index=index + 1,
                train=Segment("train", int(train_rows[0]["ts"]), int(train_rows[-1]["ts"]), len(train_rows)),
                validation=Segment(
                    "validation",
                    int(validation_rows[0]["ts"]),
                    int(validation_rows[-1]["ts"]),
                    len(validation_rows),
                    warmup=warmup,
                ),
            )
        )
    if not out:
        raise ValueError("walk-forward 未能生成任何窗口")
    return out


def run_walk_forward(
    candles: list[dict],
    config: BacktestConfig,
    *,
    signal_source: SignalSource,
    grid: dict[str, list[Any]],
    windows: int = 4,
    train_fraction: float = 0.5,
    warmup: int = 0,
    funding: list[dict] | None = None,
    marks: list[dict] | None = None,
    risk_profile: RiskProfile | None = None,
    interval: str | None = None,
    interval_ms: int | None = None,
    product_type: str | None = None,
    risk_free_rate: float = 0.0,
    on_window: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Select parameters on each training window, score them on the next one."""
    plan = walk_forward_windows(candles, windows=windows, train_fraction=train_fraction, warmup=warmup)
    results: list[dict[str, Any]] = []
    for index, window in enumerate(plan, start=1):
        if on_window is not None:
            on_window(index, len(plan))
        search = search_parameters(
            candles,
            config,
            grid,
            signal_source=signal_source,
            train=window.train,
            validation=window.validation,
            funding=funding,
            marks=marks,
            risk_profile=risk_profile,
            interval=interval,
            interval_ms=interval_ms,
            product_type=product_type,
            risk_free_rate=risk_free_rate,
        )
        best = search["best"] or {}
        results.append(
            {
                "window": window.as_dict(),
                "parameters": (best.get("parameters") or {}),
                "validation": search["test"] or best.get("outOfSample"),
                "warnings": search["warnings"],
                # Every candidate's validation result for this window, not just the
                # winner's. A PBO/CSCV reading needs to know how the candidates that
                # did *not* win behaved out of sample, and the search has already
                # computed exactly that.
                "candidates": [
                    {
                        "parameters": item.get("parameters"),
                        "validation": {
                            "total_return_pct": item.get("total_return_pct"),
                            "sharpe": item.get("sharpe"),
                            "max_drawdown_pct": item.get("max_drawdown_pct"),
                        },
                    }
                    for item in search.get("ranking") or []
                ],
            }
        )
    stable = len({json.dumps(item["parameters"], sort_keys=True) for item in results}) == 1 if results else False
    positive = sum(
        1
        for item in results
        if item["validation"] and (item["validation"].get("total_return_pct") or 0) > 0
    )
    warnings: list[str] = []
    if results and not stable:
        warnings.append(f"参数在 {len(results)} 个窗口中不稳定：{len({json.dumps(i['parameters'], sort_keys=True) for i in results})} 种取值")
    if results and positive < len(results):
        warnings.append(f"{len(results) - positive}/{len(results)} 个窗口的验证段为负收益")
    return {
        "windows": results,
        "stableParameters": stable,
        "positiveWindows": positive,
        "warnings": warnings,
    }


# -- leakage checks ----------------------------------------------------------


def check_signal_leakage(
    candles: list[dict],
    *,
    signal_source: SignalSource,
    parameters: dict[str, Any],
    probes: int = 5,
) -> dict[str, Any]:
    """Do signals change when the future is removed?

    A signal at bar i must be a function of bars 0..i only. Truncating the series
    after bar i and regenerating must produce the same value at i; if it does not,
    the rule is reading bars it could not have had.
    """
    ordered = sorted(candles, key=lambda row: int(row["ts"]))
    if len(ordered) < 4:
        return {"checked": 0, "leaks": [], "clean": True}
    full = signal_source(ordered, parameters)
    step = max(1, len(ordered) // (probes + 1))
    leaks: list[dict[str, Any]] = []
    checked = 0
    for index in range(step, len(ordered) - 1, step):
        truncated = signal_source(ordered[: index + 1], parameters)
        checked += 1
        if truncated[index] != full[index]:
            leaks.append(
                {
                    "bar": int(ordered[index]["ts"]),
                    "full": full[index],
                    "truncated": truncated[index],
                }
            )
    return {"checked": checked, "leaks": leaks, "clean": not leaks}


def check_unclosed_bars(
    candles: list[dict],
    *,
    interval_ms: int,
    now: int | None = None,
    signals: list[int | None] | None = None,
) -> dict[str, Any]:
    """Refuse to score a bar that had not closed when it was used.

    The engine only trades from `warmup` onwards and fills at the next bar's
    open, so the check is about the input: a sample whose last bar is still
    forming would let the run act on a price that was not final.
    """
    ordered = sorted(candles, key=lambda row: int(row["ts"]))
    stamp = int(now if now is not None else time.time() * 1000)
    if not ordered:
        return {"bars": 0, "unclosed": [], "clean": True}
    unclosed = [
        {"bar": int(row["ts"]), "closesAt": int(row["ts"]) + interval_ms}
        for row in ordered
        if int(row["ts"]) + interval_ms > stamp
    ]
    pending = None
    if signals and len(signals) == len(ordered) and signals[-1] is not None:
        pending = {"bar": int(ordered[-1]["ts"]), "signal": signals[-1]}
    return {
        "bars": len(ordered),
        "unclosed": unclosed,
        "clean": not unclosed,
        "lastBarSignalOnFormingBar": pending,
    }


def leakage_report(
    candles: list[dict],
    *,
    signal_source: SignalSource,
    parameters: dict[str, Any],
    interval_ms: int,
    signals: list[int | None] | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Both leakage checks, with a single verdict and the warnings that follow."""
    signal_check = check_signal_leakage(candles, signal_source=signal_source, parameters=parameters)
    bar_check = check_unclosed_bars(candles, interval_ms=interval_ms, now=now, signals=signals)
    warnings: list[str] = []
    if not signal_check["clean"]:
        warnings.append(
            f"未来函数：{len(signal_check['leaks'])} 个采样点的信号在截断未来数据后发生变化"
        )
    if not bar_check["clean"]:
        warnings.append(f"样本包含 {len(bar_check['unclosed'])} 根尚未收盘的K线")
    if bar_check.get("lastBarSignalOnFormingBar"):
        warnings.append("最后一根尚未收盘的K线上仍有信号，该信号按未收盘价计算")
    return {
        "signals": signal_check,
        "bars": bar_check,
        "clean": signal_check["clean"] and bar_check["clean"] and not bar_check.get("lastBarSignalOnFormingBar"),
        "warnings": warnings,
    }


# -- portfolio ---------------------------------------------------------------


@dataclass
class PortfolioLeg:
    symbol: str
    weight_pct: float
    metrics: PerformanceMetrics
    trades: int
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "weightPct": self.weight_pct,
            "trades": self.trades,
            "metrics": self.metrics.as_dict(),
            "warnings": self.warnings,
        }


def run_portfolio(
    samples: dict[str, dict[str, Any]],
    *,
    weights: dict[str, float] | None = None,
    initial_capital: float = 10_000.0,
    interval_ms: int | None = None,
    product_type: str | None = None,
    benchmarks: dict[str, PerformanceMetrics] | None = None,
) -> dict[str, Any]:
    """Combine per-contract runs into one book with explicit capital weights.

    Each leg is a `BacktestResult.as_dict()`; `sample["candles"]` is optional and
    only used to report coverage. Each leg runs on its own slice of the capital,
    and the combined curve is the sum: that is what a desk reads, and it stays
    honest about a leg that never traded because its share was too small to clear
    the venue's minimum notional.
    """
    if not samples:
        raise ValueError("组合回测至少需要一个合约")
    symbols = sorted(samples)
    if weights is None:
        share = 100.0 / len(symbols)
        weights = {symbol: share for symbol in symbols}
    unknown = set(weights) - set(symbols)
    if unknown:
        raise ValueError(f"权重包含未提供的合约：{', '.join(sorted(unknown))}")
    total = sum(float(weights[symbol]) for symbol in symbols)
    if total <= 0:
        raise ValueError("权重之和必须大于 0")

    legs: list[PortfolioLeg] = []
    curves: list[dict[int, float]] = []
    warnings: list[str] = []
    for symbol in symbols:
        share = float(weights[symbol]) / total
        leg_capital = initial_capital * share
        sample = samples[symbol]
        curve_points = sample.get("equity_curve") or []
        if not curve_points:
            warnings.append(f"{symbol} 没有净值曲线，已跳过")
            continue
        # Each leg's curve was produced with the run's own capital, so it is
        # scaled onto this book's share before the legs are summed. Without this
        # the weights would change nothing but the reported percentages.
        source_capital = float(sample.get("initial_capital") or initial_capital) or initial_capital
        factor = leg_capital / source_capital if source_capital > 0 else 0.0
        scaled = [
            {"time": int(point["time"]), "equity": float(point["equity"]) * factor}
            for point in curve_points
            if point.get("equity") is not None
        ]
        curve = {point["time"]: point["equity"] for point in scaled}
        curves.append(curve)
        metrics = compute_metrics(
            scaled,
            sample.get("trades"),
            interval_ms=interval_ms,
            initial_capital=leg_capital,
            calendar_days_per_year=sessions_per_year(product_type or sample.get("productType")),
            total_fees=float(sample.get("total_fees") or 0.0),
            total_funding=float(sample.get("total_funding") or 0.0),
        )
        benchmark = (benchmarks or {}).get(symbol)
        if benchmark is not None:
            compare_to_benchmark(metrics, benchmark)
        leg_warnings: list[str] = []
        if not sample.get("trades"):
            leg_warnings.append(f"{symbol} 在该区间没有成交，分配的资金未参与")
        legs.append(
            PortfolioLeg(
                symbol=symbol,
                weight_pct=round(share * 100, 4),
                metrics=metrics,
                trades=len(sample.get("trades") or []),
                warnings=leg_warnings,
            )
        )
        warnings.extend(leg_warnings)

    if not curves:
        raise ValueError("组合中没有任何可用样本")

    # Combined curve on the union of timestamps; a leg that has not started holds
    # its initial value rather than dropping out of the sum.
    stamps = sorted({stamp for curve in curves for stamp in curve})
    combined: list[dict] = []
    running = [initial_capital * (float(weights[symbol]) / total) for symbol in symbols]
    for stamp in stamps:
        value = 0.0
        for index, curve in enumerate(curves):
            if stamp in curve:
                running[index] = curve[stamp]
            value += running[index]
        combined.append({"time": stamp, "equity": value})
    combined_metrics = compute_metrics(combined, [], interval_ms=interval_ms, initial_capital=initial_capital)
    if benchmarks:
        # The book's benchmark is the same weights held long over the same window,
        # which is the comparison that decides whether trading added anything.
        weighted_benchmark = 0.0
        covered = 0.0
        for symbol in symbols:
            benchmark = benchmarks.get(symbol)
            if benchmark is None:
                continue
            share = float(weights[symbol]) / total
            weighted_benchmark += share * (benchmark.total_return_pct or 0.0)
            covered += share
        if covered > 0:
            combined_metrics.benchmark_return_pct = round(weighted_benchmark / covered, 6)
            combined_metrics.excess_return_pct = round(
                combined_metrics.total_return_pct - combined_metrics.benchmark_return_pct, 6
            )
    combined_metrics.warnings.extend(warnings)
    return {
        "weights": {symbol: round(float(weights[symbol]) / total * 100, 4) for symbol in symbols},
        "legs": [leg.as_dict() for leg in legs],
        "portfolio": combined_metrics.as_dict(),
        "equityCurve": combined,
        "warnings": warnings,
    }


# -- provenance --------------------------------------------------------------


def build_provenance(
    *,
    strategy_id: str,
    parameters: dict[str, Any],
    candles: list[dict],
    config: BacktestConfig,
    symbol: str | None = None,
    universe: list[str] | None = None,
    interval: str | None = None,
    risk_profile: RiskProfile | None = None,
    data_source: str | None = None,
) -> StrategyProvenance:
    """Record what was run, on what data, with which costs."""
    ordered = sorted(candles, key=lambda row: int(row["ts"]))
    cost_model = {
        "feeBps": config.fee_bps,
        "slippageBps": config.slippage_bps,
        "slippageModel": config.slippage_model,
        "impactCoefficient": config.impact_coefficient,
        "leverage": config.leverage,
        "allocationPct": config.allocation_pct,
        "maintenanceMarginFallback": config.maintenance_margin_rate,
        "tickSize": config.tick_size,
        "qtyStep": config.qty_step,
        "minOrderNotional": config.min_order_notional,
        "fillOnThin": config.fill_on_thin,
    }
    return StrategyProvenance(
        strategy_id=strategy_id,
        parameters=dict(parameters),
        symbol=symbol,
        universe=sorted(universe or ([symbol] if symbol else [])),
        interval=interval,
        bars=len(ordered),
        from_ts=int(ordered[0]["ts"]) if ordered else None,
        to_ts=int(ordered[-1]["ts"]) if ordered else None,
        data_hash=data_fingerprint(ordered),
        cost_model=cost_model,
        risk_source=f"{risk_profile.source}@{risk_profile.synced_at}" if risk_profile and risk_profile.tiers else None,
    )


def resolution_check(
    provenance: StrategyProvenance,
    candles: list[dict],
    *,
    risk_profile: RiskProfile | None = None,
) -> dict[str, Any]:
    """Can this result be reproduced from the data on hand?"""
    current = data_fingerprint(candles)
    problems: list[str] = []
    if current != provenance.data_hash:
        problems.append(f"数据指纹不一致：记录 {provenance.data_hash}，当前 {current}")
    if provenance.risk_source and not (risk_profile and risk_profile.tiers):
        problems.append("记录使用了风险档位，但本地没有档位数据")
    return {"reproducible": not problems, "problems": problems, "currentHash": current}
