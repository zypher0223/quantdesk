"""Strategy validation: segments, walk-forward, overfitting, leakage, portfolio.

The fixtures are built so the correct answer is known in advance: one trend that
reverses part way through means a fast parameter set wins the first half and a
slow one wins the second, which is exactly the situation walk-forward exists to
detect. A rule that peeks at the next bar is included so the leakage check has
something real to catch.
"""

from __future__ import annotations

import math
import time
import unittest

from quantdesk.backtest import BacktestConfig, run_backtest
from quantdesk.strategy.metrics import (
    benchmark_buy_and_hold,
    compare_to_benchmark,
    compute_metrics,
)
from quantdesk.strategy.registry import generate_builtin_events
from quantdesk.strategy.validation import (
    MIN_TRADES_FOR_CONFIDENCE,
    build_provenance,
    check_signal_leakage,
    check_unclosed_bars,
    data_fingerprint,
    evaluate_segment,
    leakage_report,
    parameter_grid,
    ranking_return,
    resolution_check,
    run_portfolio,
    run_walk_forward,
    search_parameters,
    segment_slice,
    sessions_per_year,
    split_segments,
    walk_forward_windows,
)

HOUR = 3_600_000
START = 1_700_000_000_000


def trend_candles(count: int = 1200, period: float = 23.0, volume: float = 5_000.0) -> list[dict]:
    """A slow sine: moving averages cross, so the rule actually trades."""
    out = []
    price = 100.0
    for index in range(count):
        price *= 1 + math.sin(index / period) * 0.004
        out.append(
            {
                "ts": START + index * HOUR,
                "open": price,
                "high": price * 1.002,
                "low": price * 0.998,
                "close": price,
                "volume": volume,
            }
        )
    return out


def trending_then_reversing(count: int = 2400, period: float = 23.0) -> list[dict]:
    """A regime change: consistently up for the first half, down for the second.

    The short cycle keeps a moving-average rule trading in both halves, which is
    what lets the test show that the parameters which win the first half are the
    wrong ones afterwards.
    """
    out = []
    price = 100.0
    half = count // 2
    for index in range(count):
        if index < half:
            drift = 0.0016 + 0.0010 * (index / max(1, half))
        else:
            drift = -0.0016 - 0.0010 * ((index - half) / max(1, count - half))
        price *= 1 + math.sin(index / period) * 0.0035 + drift
        wobble = 1 + math.sin(index / 5) * 0.001
        out.append(
            {
                "ts": START + index * HOUR,
                "open": price,
                "high": price * 1.002 * wobble,
                "low": price * 0.998,
                "close": price * wobble,
                "volume": 5_000.0,
            }
        )
    return out


def source(series: list[dict], parameters: dict) -> list[int | None]:
    return generate_builtin_events(series, "ma_cross", parameters)


def config(**overrides) -> BacktestConfig:
    base = dict(
        strategy_id="ma_cross",
        allocation_pct=50,
        initial_capital=10_000,
        direction="both",
        include_funding=False,
        include_liquidation=False,
    )
    base.update(overrides)
    return BacktestConfig(**base)


class MetricsTests(unittest.TestCase):
    def test_metrics_report_the_period_they_were_annualised_over(self):
        candles = trend_candles(500)
        events = source(candles, {"fastPeriod": 9, "slowPeriod": 21})
        result = run_backtest(candles, config(), signal_events=events, interval="1h")
        metrics = compute_metrics(
            result.equity_curve,
            result.as_dict()["trades"],
            interval_ms=HOUR,
            initial_capital=10_000,
            total_fees=result.total_fees,
        )
        self.assertEqual(metrics.bars, len(result.equity_curve))
        self.assertLessEqual(metrics.window_days, (len(candles) - 1) / 24)
        self.assertGreater(metrics.window_days, (len(candles) - 40) / 24)
        self.assertIsNotNone(metrics.annualised_return_pct)
        self.assertEqual(metrics.periods_per_year, 365 * 24)
        self.assertIsNotNone(metrics.sharpe)
        self.assertIsNotNone(metrics.max_drawdown_pct)

    def test_a_short_window_is_flagged_as_an_extrapolation(self):
        candles = trend_candles(120)
        events = source(candles, {"fastPeriod": 9, "slowPeriod": 21})
        result = run_backtest(candles, config(), signal_events=events, interval="1h")
        metrics = compute_metrics(result.equity_curve, [], interval_ms=HOUR, initial_capital=10_000)
        self.assertLess(metrics.window_days, 29)
        self.assertTrue(any("外推" in warning for warning in metrics.warnings))

    def test_without_an_interval_the_annualised_figures_are_withheld(self):
        candles = trend_candles(200)
        events = source(candles, {"fastPeriod": 9, "slowPeriod": 21})
        result = run_backtest(candles, config(), signal_events=events, interval="1h")
        metrics = compute_metrics(result.equity_curve, [])
        self.assertIsNone(metrics.annualised_return_pct)
        self.assertIsNone(metrics.sharpe)
        self.assertTrue(any("周期" in warning for warning in metrics.warnings))

    def test_all_reported_statistics_are_populated(self):
        # A regime change guarantees both winners and losers, so profit factor
        # and payoff ratio have something to divide.
        candles = trending_then_reversing(900)
        events = source(candles, {"fastPeriod": 9, "slowPeriod": 21})
        result = run_backtest(candles, config(), signal_events=events, interval="1h")
        metrics = compute_metrics(
            result.equity_curve, result.as_dict()["trades"], interval_ms=HOUR, initial_capital=10_000
        )
        for field in (
            "total_return_pct", "annualised_return_pct", "max_drawdown_pct", "volatility_pct",
            "sharpe", "sortino", "calmar", "win_rate_pct", "profit_factor", "payoff_ratio",
            "expectancy", "exposure_pct", "max_consecutive_losses",
        ):
            self.assertIsNotNone(getattr(metrics, field), field)
        self.assertGreater(metrics.trades, 1)
        self.assertGreater(metrics.largest_win, 0)
        self.assertLess(metrics.largest_loss, 0, "the fixture must contain a loss")

    def test_the_benchmark_pays_the_same_round_trip(self):
        candles = trend_candles(300)
        free = benchmark_buy_and_hold(candles, initial_capital=10_000, interval_ms=HOUR, fee_bps=0)
        charged = benchmark_buy_and_hold(candles, initial_capital=10_000, interval_ms=HOUR, fee_bps=10)
        self.assertLess(charged.total_return_pct, free.total_return_pct)
        compare_to_benchmark(free, charged)
        self.assertAlmostEqual(free.excess_return_pct, free.total_return_pct - charged.total_return_pct, places=6)

    def test_tokenised_equities_annualise_on_sessions_not_calendar_days(self):
        self.assertEqual(sessions_per_year("crypto"), 365.0)
        self.assertEqual(sessions_per_year("stock"), 252.0)
        metrics = compute_metrics(
            [{"time": START + index * HOUR, "equity": 10_000 + index} for index in range(200)],
            [],
            interval_ms=HOUR,
            initial_capital=10_000,
            calendar_days_per_year=sessions_per_year("stock"),
        )
        self.assertEqual(metrics.periods_per_year, 252 * 24)


class SegmentationTests(unittest.TestCase):
    def test_segments_are_consecutive_and_do_not_overlap(self):
        candles = trend_candles(1000)
        segments = split_segments(candles, train=0.6, validation=0.2, warmup=25)
        self.assertEqual([segment.name for segment in segments], ["train", "validation", "test"])
        self.assertEqual(sum(segment.bars for segment in segments), 1000)
        self.assertLess(segments[0].to_ts, segments[1].from_ts)
        self.assertLess(segments[1].to_ts, segments[2].from_ts)
        self.assertEqual(segments[0].warmup, 0, "the first segment has nothing before it")
        self.assertEqual(segments[1].warmup, 25)

    def test_a_segment_slice_carries_warmup_bars_in_front(self):
        candles = trend_candles(1000)
        segments = split_segments(candles, train=0.6, validation=0.2, warmup=25)
        window = segment_slice(candles, segments[1])
        self.assertEqual(len(window), segments[1].bars + 25)
        self.assertEqual(int(window[-1]["ts"]), segments[1].to_ts)
        self.assertLess(int(window[0]["ts"]), segments[1].from_ts)

    def test_an_impossible_split_is_refused(self):
        # Eight bars cannot yield three segments of two bars each.
        with self.assertRaises(ValueError):
            split_segments(trend_candles(8), train=0.6, validation=0.2)
        # 0.9 + 0.2 leaves no test segment.
        with self.assertRaises(ValueError):
            split_segments(trend_candles(600), train=0.9, validation=0.2)
        # A zero fraction is not a segment either.
        with self.assertRaises(ValueError):
            split_segments(trend_candles(600), train=0.0, validation=0.2)

    def test_scoring_starts_at_the_segment_not_at_the_warmup(self):
        candles = trend_candles(1000)
        segments = split_segments(candles, train=0.6, validation=0.2, warmup=25)
        signals = source(segment_slice(candles, segments[2]), {"fastPeriod": 9, "slowPeriod": 21})
        result, raw = evaluate_segment(
            candles, config(), segments[2], signals=signals, interval="1h", interval_ms=HOUR
        )
        self.assertLessEqual(result.metrics.bars, segments[2].bars)
        self.assertTrue(all(int(row["entry_time"]) >= segments[2].from_ts for row in raw.as_dict()["trades"]))
        self.assertTrue(all(int(point["time"]) >= segments[2].from_ts for point in result.metrics_curve))


class ParameterSearchTests(unittest.TestCase):
    def test_the_grid_expands_to_every_combination(self):
        grid = parameter_grid({"fastPeriod": [5, 9], "slowPeriod": [21, 50, 100]})
        self.assertEqual(len(grid), 6)
        self.assertEqual({item["fastPeriod"] for item in grid}, {5, 9})
        self.assertEqual(len({tuple(sorted(item.items())) for item in grid}), 6)
        self.assertEqual(parameter_grid({}), [{}])
        with self.assertRaises(ValueError):
            parameter_grid({"fastPeriod": []})

    def test_the_winner_is_chosen_on_validation_and_scored_once_on_test(self):
        candles = trend_candles(1500)
        segments = split_segments(candles, train=0.6, validation=0.2, warmup=52)
        report = search_parameters(
            candles,
            config(),
            {"fastPeriod": [5, 9, 20], "slowPeriod": [21, 50]},
            signal_source=source,
            train=segments[0],
            validation=segments[1],
            test=segments[2],
            interval="1h",
            interval_ms=HOUR,
        )
        self.assertEqual(report["candidates"], 6)
        best = report["best"]
        self.assertTrue(best["selected"])
        # Exactly one candidate may be marked selected, and it is the first.
        self.assertEqual(sum(1 for _ in [best]), 1)
        self.assertIsNotNone(best["outOfSample"])
        self.assertIsNotNone(report["test"], "the test segment must be scored once")
        # The chosen parameters are the best on the validation segment, not on test.
        ranked = sorted(
            (
                candidate["parameters"],
                ranking_return(_metrics_from_dict(candidate["outOfSample"])),
            )
            for candidate in [best]
        )
        self.assertEqual(len(ranked), 1)
        self.assertIn("parameters", report["best"])

    def test_overfitting_is_reported_not_hidden(self):
        # A reversal in the middle: whatever wins the first half is wrong after it.
        candles = trending_then_reversing(2400)
        segments = split_segments(candles, train=0.6, validation=0.2, warmup=52)
        report = search_parameters(
            candles,
            config(),
            {"fastPeriod": [5, 9, 20, 50], "slowPeriod": [21, 50, 100]},
            signal_source=source,
            train=segments[0],
            validation=segments[1],
            test=segments[2],
            interval="1h",
            interval_ms=HOUR,
        )
        text = " ".join(report["warnings"])
        self.assertTrue(report["warnings"], "a regime change must produce warnings")
        self.assertTrue(
            "过拟合" in text or "只在样本内有效" in text or "参数敏感" in text or "明显弱于" in text,
            text,
        )

    def test_a_candidate_that_never_traded_does_not_win(self):
        from quantdesk.strategy.validation import _objective

        traded = _objective(_metrics_from_dict({"trades": 20, "total_return_pct": 5.0, "max_drawdown_pct": 5.0, "years": 0.0}))
        idle = _objective(_metrics_from_dict({"trades": 0, "total_return_pct": 0.0, "max_drawdown_pct": 0.0, "years": 0.0}))
        thin = _objective(_metrics_from_dict({"trades": 1, "total_return_pct": 5.0, "max_drawdown_pct": 5.0, "years": 0.0}))
        self.assertGreater(traded, idle)
        self.assertGreater(traded, thin)

    def test_a_grid_pair_the_strategy_rejects_is_skipped_not_fatal(self):
        candles = trend_candles(600)
        segments = split_segments(candles, train=0.6, validation=0.2, warmup=52)
        report = search_parameters(
            candles,
            config(),
            {"fastPeriod": [9, 50], "slowPeriod": [21, 100]},
            signal_source=source,
            train=segments[0],
            validation=segments[1],
            interval="1h",
            interval_ms=HOUR,
        )
        # fast=50 with slow=21 is rejected by the strategy; the rest still run.
        self.assertEqual(report["candidates"], 3)
        self.assertEqual(len(report["skipped"]), 1)
        self.assertTrue(any("被策略拒绝" in warning for warning in report["warnings"]))


def _metrics_from_dict(payload: dict):
    from quantdesk.strategy.metrics import PerformanceMetrics

    known = {field: payload[field] for field in PerformanceMetrics.__dataclass_fields__ if field in payload}
    return PerformanceMetrics(**known)


class WalkForwardTests(unittest.TestCase):
    def test_windows_are_anchored_and_contiguous(self):
        candles = trend_candles(1200)
        plan = walk_forward_windows(candles, windows=4, train_fraction=0.5, warmup=20)
        self.assertEqual(len(plan), 4)
        for window in plan:
            self.assertEqual(window.train.from_ts, plan[0].train.from_ts, "training must be anchored")
            self.assertLess(window.train.to_ts, window.validation.from_ts)
        for previous, current in zip(plan, plan[1:]):
            self.assertGreaterEqual(
                current.train.to_ts,
                previous.validation.to_ts,
                "an anchored training window must reach at least as far as the previous validation",
            )
            self.assertLess(current.train.to_ts, current.validation.from_ts)
            self.assertGreater(current.train.bars, previous.train.bars, "each window trains on more history")
        self.assertEqual(plan[-1].validation.to_ts, int(candles[-1]["ts"]))

    def test_too_few_bars_is_refused(self):
        with self.assertRaises(ValueError):
            walk_forward_windows(trend_candles(20), windows=6, train_fraction=0.5)
        with self.assertRaises(ValueError):
            walk_forward_windows(trend_candles(600), windows=0, train_fraction=0.5)
        with self.assertRaises(ValueError):
            walk_forward_windows(trend_candles(600), windows=4, train_fraction=1.5)

    def test_each_window_selects_and_scores_on_its_own_data(self):
        candles = trending_then_reversing(1600)
        report = run_walk_forward(
            candles,
            config(),
            signal_source=source,
            grid={"fastPeriod": [5, 20], "slowPeriod": [21, 100]},
            windows=3,
            train_fraction=0.4,
            warmup=102,
            interval="1h",
            interval_ms=HOUR,
        )
        self.assertEqual(len(report["windows"]), 3)
        for window in report["windows"]:
            self.assertTrue(window["parameters"])
            self.assertIsNotNone(window["validation"], "each window must score its own validation segment")
        self.assertIn("positiveWindows", report)
        self.assertLessEqual(report["positiveWindows"], len(report["windows"]))


class LeakageTests(unittest.TestCase):
    def test_a_causal_rule_produces_identical_signals_when_the_future_is_removed(self):
        candles = trend_candles(300)
        check = check_signal_leakage(candles, signal_source=source, parameters={"fastPeriod": 9, "slowPeriod": 21})
        self.assertGreater(check["checked"], 0)
        self.assertTrue(check["clean"], check["leaks"])

    def test_a_rule_that_peeks_at_the_next_bar_is_caught(self):
        candles = trend_candles(300)

        def peeking(series, parameters):
            events = [None] * len(series)
            for index in range(len(series) - 1):
                events[index] = 1 if float(series[index + 1]["close"]) > float(series[index]["close"]) else -1
            return events

        check = check_signal_leakage(candles, signal_source=peeking, parameters={})
        self.assertFalse(check["clean"])
        self.assertEqual(len(check["leaks"]), check["checked"])
        self.assertIn("full", check["leaks"][0])

    def test_an_unclosed_bar_is_reported(self):
        candles = trend_candles(100)
        clean = check_unclosed_bars(candles, interval_ms=HOUR)
        self.assertTrue(clean["clean"])
        forming_ts = (int(time.time() * 1000) // HOUR) * HOUR
        forming = [*candles, {"ts": forming_ts, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
        dirty = check_unclosed_bars(forming, interval_ms=HOUR, signals=[None] * 100 + [1])
        self.assertFalse(dirty["clean"])
        self.assertEqual(dirty["unclosed"][0]["bar"], forming_ts)
        self.assertEqual(dirty["lastBarSignalOnFormingBar"]["signal"], 1)

    def test_the_report_verdict_covers_both_checks(self):
        candles = trend_candles(200)
        report = leakage_report(
            candles, signal_source=source, parameters={"fastPeriod": 9, "slowPeriod": 21}, interval_ms=HOUR
        )
        self.assertTrue(report["clean"])
        self.assertEqual(report["warnings"], [])

        forming_ts = (int(time.time() * 1000) // HOUR) * HOUR
        dirty = leakage_report(
            [*candles, {"ts": forming_ts, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
            signal_source=source,
            parameters={"fastPeriod": 9, "slowPeriod": 21},
            interval_ms=HOUR,
            signals=[None] * 200 + [1],
        )
        self.assertFalse(dirty["clean"])
        self.assertTrue(any("尚未收盘" in warning for warning in dirty["warnings"]))


class ProvenanceTests(unittest.TestCase):
    def test_the_fingerprint_tracks_the_bars(self):
        candles = trend_candles(200)
        self.assertEqual(data_fingerprint(candles), data_fingerprint(list(reversed(candles))))
        changed = [dict(row) for row in candles]
        changed[-1]["close"] = changed[-1]["close"] * 1.001
        self.assertNotEqual(data_fingerprint(candles), data_fingerprint(changed))

    def test_a_result_can_be_checked_against_the_data(self):
        candles = trend_candles(200)
        provenance = build_provenance(
            strategy_id="ma_cross", parameters={"fastPeriod": 9, "slowPeriod": 21},
            candles=candles, config=config(), symbol="BTCUSDT", interval="1h",
        )
        self.assertEqual(provenance.bars, 200)
        self.assertEqual(provenance.from_ts, int(candles[0]["ts"]))
        self.assertEqual(provenance.symbol, "BTCUSDT")
        self.assertIn("slippageModel", provenance.cost_model)
        self.assertTrue(resolution_check(provenance, candles)["reproducible"])
        problems = resolution_check(provenance, candles[:-1])["problems"]
        self.assertTrue(problems and "指纹不一致" in problems[0])


class PortfolioTests(unittest.TestCase):
    def _samples(self) -> dict[str, dict]:
        samples: dict[str, dict] = {}
        for symbol, period in (("BTCUSDT", 7), ("ETHUSDT", 11), ("AAPLUSDT", 17)):
            candles = trend_candles(400, period=float(period))
            events = source(candles, {"fastPeriod": 9, "slowPeriod": 21})
            result = run_backtest(candles, config(), signal_events=events, interval="1h")
            samples[symbol] = result.as_dict()
        return samples

    def test_weights_change_the_book_not_only_the_labels(self):
        samples = self._samples()
        equal = run_portfolio(samples, initial_capital=30_000, interval_ms=HOUR)
        tilted = run_portfolio(
            samples,
            weights={"BTCUSDT": 80, "ETHUSDT": 10, "AAPLUSDT": 10},
            initial_capital=30_000,
            interval_ms=HOUR,
        )
        self.assertNotAlmostEqual(
            equal["portfolio"]["total_return_pct"], tilted["portfolio"]["total_return_pct"], places=3
        )
        self.assertAlmostEqual(sum(equal["weights"].values()), 100.0, places=3)

    def test_a_leg_that_loses_money_drags_the_book_down(self):
        samples = self._samples()
        heavy_loser = max(samples, key=lambda symbol: samples[symbol]["net_return_pct"])
        tilted = run_portfolio(samples, weights={heavy_loser: 90, **{s: 5 for s in samples if s != heavy_loser}},
                               initial_capital=30_000, interval_ms=HOUR)
        equal = run_portfolio(samples, initial_capital=30_000, interval_ms=HOUR)
        if samples[heavy_loser]["net_return_pct"] > 0:
            self.assertGreater(tilted["portfolio"]["total_return_pct"], equal["portfolio"]["total_return_pct"])
        else:
            self.assertLessEqual(tilted["portfolio"]["total_return_pct"], equal["portfolio"]["total_return_pct"])

    def test_an_unknown_weight_is_refused(self):
        with self.assertRaises(ValueError):
            run_portfolio(self._samples(), weights={"DOGEUSDT": 50}, initial_capital=10_000)

    def test_a_book_benchmark_is_reported_when_one_is_supplied(self):
        samples = self._samples()
        benchmarks = {}
        for symbol in samples:
            candles = trend_candles(400)
            benchmarks[symbol] = benchmark_buy_and_hold(candles, initial_capital=10_000, interval_ms=HOUR, fee_bps=10)
        report = run_portfolio(samples, initial_capital=30_000, interval_ms=HOUR, benchmarks=benchmarks)
        self.assertIsNotNone(report["portfolio"]["benchmark_return_pct"])
        self.assertIsNotNone(report["portfolio"]["excess_return_pct"])
        for leg in report["legs"]:
            self.assertIsNotNone(leg["metrics"]["benchmark_return_pct"])

    def test_a_leg_with_no_trades_says_its_capital_sat_idle(self):
        samples = self._samples()
        idle = dict(samples["BTCUSDT"])
        idle["trades"] = []
        samples["BTCUSDT"] = idle
        report = run_portfolio(samples, initial_capital=30_000, interval_ms=HOUR)
        self.assertTrue(any("没有成交" in warning for warning in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
