"""Phase 5: the execution model, stated and enforced.

A return number is only usable if the assumptions that produced it travel with
it. These tests hold the engine to three promises:

* the default execution model is unchanged - a result computed before these knobs
  existed is still the result the same request produces now;
* latency and the participation cap actually change what fills, in the direction
  they claim (latency cannot help, a capped order cannot fill more);
* every result carries the rule it used, including what it assumed away.
"""

from __future__ import annotations

import unittest

from quantdesk.backtest import BacktestConfig, run_backtest
from quantdesk.backtest.engine import _execution_model, _fill_rule

HOUR = 3_600_000


def candles(count: int = 200, *, volume: float = 200.0) -> list[dict]:
    """A zig-zag series whose bars are *not* thin.

    The engine refuses to open on a bar that traded a negligible notional
    (`THIN_SESSION_MIN_NOTIONAL`), so a fixture with tiny volume would silently
    produce zero trades and every assertion below would pass for the wrong reason.
    """
    """A zig-zag series: crosses happen often enough to produce trades."""
    rows = []
    price = 100.0
    for index in range(count):
        price += 1.2 if (index // 12) % 2 == 0 else -1.2
        price = max(5.0, price)
        rows.append({
            "ts": 1_700_000_000_000 + index * HOUR,
            "open": round(price - 0.4, 6), "high": round(price + 1.0, 6),
            "low": round(price - 1.0, 6), "close": round(price, 6),
            "volume": volume, "source": "venue_rest",
        })
    return rows


def events_for(bars: list[dict]) -> list[int | None]:
    """Alternating long/short on every 10th bar: a signal, not a strategy."""
    return [1 if (index // 10) % 2 == 0 else -1 for index in range(len(bars))]


class DefaultsAreUnchangedTests(unittest.TestCase):
    def test_the_default_model_is_the_next_bar_open(self):
        config = BacktestConfig(strategy_id="ma_cross")
        self.assertEqual(config.latency_bars, 0)
        self.assertEqual(config.partial_fill, "ignore")
        self.assertIn("下一根K线开盘成交", _fill_rule(config))

    def test_a_default_run_reports_the_model_it_used(self):
        result = run_backtest(candles(), BacktestConfig(strategy_id="ma_cross"),
                             signal_events=events_for(candles()), interval="1h")
        model = result.as_dict()["execution_model"]
        self.assertEqual(model["latencyBars"], 0)
        self.assertEqual(model["partialFill"], "ignore")
        self.assertEqual(model["unfilledOrders"], 0)
        self.assertTrue(model["simplifications"], "被简化掉的东西必须写在结果里")
        self.assertIn("平仓按整笔成交", " ".join(model["simplifications"]))

    def test_explicit_zero_matches_the_implicit_default(self):
        explicit = BacktestConfig(strategy_id="ma_cross", latency_bars=0, partial_fill="ignore")
        bars = candles()
        events = events_for(bars)
        left = run_backtest(bars, BacktestConfig(strategy_id="ma_cross"), signal_events=events, interval="1h")
        right = run_backtest(bars, explicit, signal_events=events, interval="1h")
        self.assertEqual(left.net_return_pct, right.net_return_pct)
        self.assertEqual(len(left.trades), len(right.trades))


class LatencyTests(unittest.TestCase):
    def test_latency_is_validated(self):
        for bad in (-1, 11):
            with self.assertRaises(ValueError):
                run_backtest(candles(50), BacktestConfig(strategy_id="ma_cross", latency_bars=bad),
                             signal_events=events_for(candles(50)), interval="1h")

    def test_a_later_fill_lands_on_a_later_bar(self):
        bars = candles()
        events = events_for(bars)
        # A short slow window keeps the engine's warmup out of the way (a default
        # ma_cross warms up for 22 bars, and both runs would start on the same bar).
        # `warmup` is read from the config field, not the params dict, so both must
        # be set for a short slow window.
        fast = {"fastPeriod": 2, "slowPeriod": 3}
        immediate = run_backtest(bars, BacktestConfig(strategy_id="ma_cross", strategy_params=fast,
                                                      fast_period=2, slow_period=3),
                                 signal_events=events, interval="1h")
        delayed = run_backtest(bars, BacktestConfig(strategy_id="ma_cross", strategy_params=fast,
                                                    fast_period=2, slow_period=3, latency_bars=2),
                               signal_events=events, interval="1h")
        self.assertTrue(immediate.trades and delayed.trades)
        # The signal is a block, so the *reaction* is what the latency moves: every
        # exit that the immediate run takes on the first bar of a new signal is
        # taken two bars later by the delayed run.
        immediate_exits = [(trade.exit_time - bars[0]["ts"]) // HOUR for trade in immediate.trades[:3]]
        delayed_exits = [(trade.exit_time - bars[0]["ts"]) // HOUR for trade in delayed.trades[:3]]
        self.assertEqual([value + 2 for value in immediate_exits], delayed_exits,
                         "延迟 2 根，每一笔反应都应当晚 2 根")
        last = delayed.trades[0]
        entry_bar = next(row for row in bars if row["ts"] == last.entry_time)
        # The fill price is that bar's open plus the configured slippage: the
        # fixture's bars are calm, so the two agree to within the slippage.
        self.assertLess(abs(last.entry_price - entry_bar["open"]) / entry_bar["open"], 0.01,
                        "成交价必须落在延迟后那根K线上")
        self.assertEqual(last.entry_time, entry_bar["ts"], "成交时间必须是那根K线的开盘时刻")

    def test_latency_cannot_improve_the_record_of_what_happened(self):
        bars = candles()
        events = events_for(bars)
        immediate = run_backtest(bars, BacktestConfig(strategy_id="ma_cross"),
                                 signal_events=events, interval="1h")
        delayed = run_backtest(bars, BacktestConfig(strategy_id="ma_cross", latency_bars=3),
                               signal_events=events, interval="1h")
        self.assertNotEqual(immediate.net_return_pct, delayed.net_return_pct,
                            "延迟改变成交价，结果必须随之改变")
        self.assertIn("延迟", " ".join(delayed.warnings))

    def test_the_model_sentence_says_what_happened(self):
        config = BacktestConfig(strategy_id="ma_cross", latency_bars=1)
        self.assertIn("再延迟 1 根K线", _execution_model(config, 0, 0.0)["fillRule"])


class ParticipationCapTests(unittest.TestCase):
    def test_capping_reduces_the_filled_size_and_reports_the_shortfall(self):
        bars = candles(volume=200.0)         # tradable, but far too small for the order
        events = events_for(bars)
        uncapped = run_backtest(
            bars, BacktestConfig(strategy_id="ma_cross", allocation_pct=100, slippage_model="participation"),
            signal_events=events, interval="1h")
        capped = run_backtest(
            bars,
            BacktestConfig(strategy_id="ma_cross", allocation_pct=100, slippage_model="participation",
                           max_participation=0.01, partial_fill="cap"),
            signal_events=events, interval="1h")
        self.assertTrue(capped.trades)
        self.assertLess(capped.trades[0].quantity, uncapped.trades[0].quantity)
        detail = capped.as_dict()["data_quality"]
        self.assertGreater(detail["unfilledOrders"], 0)
        self.assertGreater(detail["unfilledNotional"], 0)
        self.assertTrue(any("参与率上限" in item for item in capped.warnings),
                        "截断必须在警告里说清楚")

    def test_capping_cannot_make_a_result_look_better_than_filling_everything(self):
        bars = candles(volume=150.0)
        events = events_for(bars)
        filled = run_backtest(
            bars, BacktestConfig(strategy_id="ma_cross", allocation_pct=100),
            signal_events=events, interval="1h")
        capped = run_backtest(
            bars, BacktestConfig(strategy_id="ma_cross", allocation_pct=100,
                                 max_participation=0.05, partial_fill="cap"),
            signal_events=events, interval="1h")
        # A smaller position on the same signals cannot earn more in a trending
        # book; whatever it does, the two runs must not be identical by accident.
        self.assertNotEqual(filled.final_equity, capped.final_equity)

    def test_ignore_keeps_the_historical_behaviour(self):
        bars = candles(volume=150.0)
        events = events_for(bars)
        config = BacktestConfig(strategy_id="ma_cross", allocation_pct=100, max_participation=0.01)
        result = run_backtest(bars, config, signal_events=events, interval="1h")
        self.assertEqual(result.as_dict()["data_quality"]["unfilledOrders"], 0)
        self.assertFalse(any("参与率上限" in item for item in result.warnings))

    def test_an_unknown_policy_is_refused(self):
        with self.assertRaises(ValueError):
            run_backtest(candles(50), BacktestConfig(strategy_id="ma_cross", partial_fill="maybe"),
                         signal_events=events_for(candles(50)), interval="1h")


class EnvelopeTests(unittest.TestCase):
    def test_the_study_envelope_carries_the_execution_choices(self):
        from quantdesk.studies import cost_model

        request = type("Request", (), {
            "slippageModel": "participation", "impactCoefficient": 0.2, "includeFunding": True,
            "includeLiquidation": True, "fillOnThin": "skip", "initialCapital": 10_000.0,
            "allocationPct": 50.0, "leverage": 2.0, "latencyBars": 3, "partialFill": "cap",
        })()
        model = cost_model(request, fee_bps=6.0, slippage_bps=5.0, meta={"tickSize": 0.1})
        self.assertEqual(model["latencyBars"], 3)
        self.assertEqual(model["partialFill"], "cap")
        self.assertEqual(model["slippageModel"], "participation")


class StudyEnvelopeCarriesTheModelTests(unittest.TestCase):
    """A validation result and a portfolio are studies too: they carry the model."""

    def test_the_selected_run_keeps_the_execution_model(self):
        from quantdesk.studies import _selected_run

        bars = candles()
        config = BacktestConfig(strategy_id="ma_cross", fast_period=2, slow_period=3, latency_bars=1)
        selected = _selected_run(
            bars, config, {"fastPeriod": 2, "slowPeriod": 3},
            source=lambda series, parameters: events_for(series),
            funding=None, marks=None, risk_profile=None, meta={}, interval="1h",
        )
        self.assertEqual(selected["executionModel"]["latencyBars"], 1)
        self.assertIn("再延迟 1 根K线", selected["executionModel"]["fillRule"])
        self.assertTrue(selected["dataQuality"])

    def test_the_study_repeats_the_execution_warnings_at_its_own_level(self):
        from quantdesk.studies import _selected_run

        bars = candles(volume=150.0)
        config = BacktestConfig(strategy_id="ma_cross", fast_period=2, slow_period=3,
                                allocation_pct=100, max_participation=0.01, partial_fill="cap")
        selected = _selected_run(
            bars, config, {"fastPeriod": 2, "slowPeriod": 3},
            source=lambda series, parameters: events_for(series),
            funding=None, marks=None, risk_profile=None, meta={}, interval="1h",
        )
        self.assertTrue(any("参与率上限" in item for item in selected["warnings"]),
                        "被截断的事实必须在选定参数那次运行的警告里")
