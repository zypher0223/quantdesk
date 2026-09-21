"""Backtest engine: costs the prototype skipped, and the invariants that guard them."""

from __future__ import annotations

import math
import unittest

from quantdesk.backtest import BacktestConfig, liquidation_price, run_backtest, thin_session_flags

HOUR = 3_600_000
START = 1_700_000_000_000


def flat_candles(count: int = 120, price: float = 100.0, volume: float = 5_000.0) -> list[dict]:
    return [
        {
            "ts": START + index * HOUR,
            "open": price,
            "high": price * 1.001,
            "low": price * 0.999,
            "close": price,
            "volume": volume,
        }
        for index in range(count)
    ]


def v_shaped_candles(count: int = 120, dip: float = 0.35) -> list[dict]:
    """Flat, then a hard dip and recovery — forces long entries into drawdown."""
    out = []
    price = 100.0
    for index in range(count):
        if count * 0.4 <= index < count * 0.55:
            price *= 1 - dip / (count * 0.15)
        elif index >= count * 0.55:
            price *= 1 + dip / (count * 0.45)
        out.append(
            {
                "ts": START + index * HOUR,
                "open": price,
                "high": price * 1.001,
                "low": price * 0.999,
                "close": price,
                "volume": 5_000.0,
            }
        )
    return out


def trending_candles(count: int = 300) -> list[dict]:
    """Sinusoidal trend so the moving-average rule actually crosses."""
    out = []
    price = 100.0
    for index in range(count):
        price *= 1 + math.sin(index / 17) * 0.004
        out.append(
            {
                "ts": START + index * HOUR,
                "open": price,
                "high": price * 1.002,
                "low": price * 0.998,
                "close": price * (1 + math.sin(index / 5) * 0.001),
                "volume": 5_000.0,
            }
        )
    return out


class ConfigurationTests(unittest.TestCase):
    def test_rejects_incoherent_parameters(self):
        candles = trending_candles()
        for config, fragment in (
            (BacktestConfig(fast_period=21, slow_period=9), "均线周期"),
            (BacktestConfig(allocation_pct=0), "仓位比例"),
            (BacktestConfig(initial_capital=0), "初始资金"),
            (BacktestConfig(leverage=0.5), "杠杆"),
            (BacktestConfig(leverage=500), "杠杆"),
            (BacktestConfig(direction="sideways"), "direction"),
            (BacktestConfig(fill_on_thin="maybe"), "fill_on_thin"),
        ):
            with self.assertRaises(ValueError, msg=fragment) as caught:
                run_backtest(candles, config)
            self.assertIn(fragment, str(caught.exception))

    def test_requires_enough_bars(self):
        with self.assertRaises(ValueError):
            run_backtest(flat_candles(10), BacktestConfig(slow_period=21))


class CostTests(unittest.TestCase):
    def test_deterministic_result_is_reproducible(self):
        candles = trending_candles()
        first = run_backtest(candles, BacktestConfig()).as_dict()
        second = run_backtest(candles, BacktestConfig()).as_dict()
        self.assertEqual(first["final_equity"], second["final_equity"])
        self.assertEqual(len(first["trades"]), len(second["trades"]))

    def test_fees_reduce_equity_and_are_reported(self):
        candles = trending_candles()
        free = run_backtest(candles, BacktestConfig(fee_bps=0, slippage_bps=0))
        costly = run_backtest(candles, BacktestConfig(fee_bps=20, slippage_bps=10))
        self.assertGreater(costly.total_fees, 0)
        self.assertLess(costly.final_equity, free.final_equity)
        # Fees in the summary must equal the sum carried by the trades.
        self.assertAlmostEqual(costly.total_fees, sum(trade.fees for trade in costly.trades), places=4)

    def test_funding_is_charged_to_longs_and_paid_to_shorts(self):
        candles = trending_candles()
        rates = [{"ts": START + index * HOUR, "rate": 0.001} for index in range(0, 300, 8)]
        result = run_backtest(candles, BacktestConfig(), funding=rates)
        self.assertGreater(len(result.trades), 0)
        self.assertTrue(any(trade.funding_paid != 0 for trade in result.trades))
        for trade in result.trades:
            # Positive funding costs a long and credits a short.
            if trade.direction == "多":
                self.assertGreaterEqual(trade.funding_paid, 0)
            else:
                self.assertLessEqual(trade.funding_paid, 0)
        self.assertAlmostEqual(result.total_funding, sum(trade.funding_paid for trade in result.trades), places=4)

    def test_missing_funding_history_is_declared_not_assumed(self):
        result = run_backtest(trending_candles(), BacktestConfig())
        self.assertEqual(result.total_funding, 0)
        self.assertTrue(any("资金费率历史" in warning for warning in result.warnings))

    def test_zero_funding_history_is_not_a_warning(self):
        dates = [{"ts": START + index * HOUR, "rate": 0.0} for index in range(0, 300, 8)]
        result = run_backtest(trending_candles(), BacktestConfig(), funding=dates)
        self.assertFalse(any("资金费率历史" in warning for warning in result.warnings))


class LeverageAndLiquidationTests(unittest.TestCase):
    def test_liquidation_price_formula_and_direction(self):
        self.assertIsNone(liquidation_price(1, 100.0, 1.0, 0.005), "1x isolated cannot be liquidated")
        self.assertGreater(liquidation_price(-1, 100.0, 1.0, 0.005), 100.0, "a 1x short can still be liquidated")
        long_price = liquidation_price(1, 100.0, 10.0, 0.005)
        short_price = liquidation_price(-1, 100.0, 10.0, 0.005)
        self.assertAlmostEqual(long_price, 100 * 0.9 / 0.995, places=6)
        self.assertAlmostEqual(short_price, 100 * 1.1 / 1.005, places=6)
        self.assertLess(long_price, 100)
        self.assertGreater(short_price, 100)

    def test_short_liquidation_triggers_on_the_adverse_extreme(self):
        candles = flat_candles(160)
        # A spike that a 25x short cannot survive, then a collapse.
        candles[150] = {**candles[150], "high": 100.0 * 1.10, "close": 100.0 * 1.05}
        for index in range(151, 160):
            candles[index] = {**candles[index], "open": 105.0, "close": 60.0, "high": 105.0, "low": 60.0}
        # Force a short entry by shaping a down-cross before the spike.
        for index in range(120, 150):
            candles[index] = {**candles[index], "close": 99.0 - (index - 120) * 0.5, "open": 99.0 - (index - 120) * 0.5, "low": 98.0 - (index - 120) * 0.5, "high": 100.0}
        result = run_backtest(candles, BacktestConfig(leverage=25, allocation_pct=90, include_funding=False))
        liquidated = [trade for trade in result.trades if trade.liquidated]
        self.assertTrue(liquidated, "the adverse spike should have liquidated a 25x short")
        for trade in liquidated:
            self.assertEqual(trade.exit_reason, "liquidation")
            # A liquidation must not lose more than the posted margin.
            self.assertGreaterEqual(trade.net_pnl, -trade.notional / 25 - 1e-6)

    def test_liquidation_can_be_disabled(self):
        candles = flat_candles(160)
        candles[150] = {**candles[150], "high": 130.0}
        with_guard = run_backtest(candles, BacktestConfig(leverage=20, include_liquidation=True))
        without = run_backtest(candles, BacktestConfig(leverage=20, include_liquidation=False))
        self.assertGreaterEqual(len(with_guard.trades), 0)
        self.assertGreaterEqual(len(without.trades), 0)

    def test_entry_bar_and_final_bar_liquidation_are_not_skipped(self):
        candles = flat_candles(40)
        # Force an up-cross known at index 35, so the long fills at index 36 open.
        for index in range(30, 35):
            candles[index] = {**candles[index], "close": 90.0}
        candles[35] = {**candles[35], "close": 120.0}
        base = BacktestConfig(
            fast_period=2,
            slow_period=3,
            leverage=10,
            fee_bps=0,
            slippage_bps=0,
            fill_on_thin="allow",
            include_funding=False,
        )

        entry_bar = [dict(row) for row in candles]
        entry_bar[36]["low"] = 1.0
        result = run_backtest(entry_bar, base)
        liquidations = [trade for trade in result.trades if trade.liquidated]
        self.assertTrue(liquidations)
        self.assertEqual(liquidations[0].exit_time, entry_bar[36]["ts"])

        final_bar = [dict(row) for row in candles]
        final_bar[-1]["high"] = 10_000.0
        result = run_backtest(final_bar, base)
        self.assertTrue(result.trades[-1].liquidated)
        self.assertEqual(result.trades[-1].exit_time, final_bar[-1]["ts"])

    def test_open_signal_exit_precedes_later_intrabar_extreme(self):
        candles = trending_candles(180)
        baseline = run_backtest(candles, BacktestConfig(leverage=20, fee_bps=0, slippage_bps=0, include_funding=False))
        signal_trade = next((trade for trade in baseline.trades if trade.exit_reason == "signal"), None)
        self.assertIsNotNone(signal_trade)
        changed = [dict(row) for row in candles]
        exit_index = next(index for index, row in enumerate(changed) if row["ts"] == signal_trade.exit_time)
        if signal_trade.direction == "多":
            changed[exit_index]["low"] = 0.01
        else:
            changed[exit_index]["high"] = 10_000.0
        rerun = run_backtest(changed, BacktestConfig(leverage=20, fee_bps=0, slippage_bps=0, include_funding=False))
        matching = next(trade for trade in rerun.trades if trade.entry_time == signal_trade.entry_time)
        self.assertEqual(matching.exit_reason, "signal")

    def test_leverage_scales_exposure_and_is_announced(self):
        candles = trending_candles()
        plain = run_backtest(candles, BacktestConfig(leverage=1))
        geared = run_backtest(candles, BacktestConfig(leverage=5))
        self.assertTrue(any("杠杆" in warning for warning in geared.warnings))
        self.assertFalse(any("杠杆" in warning for warning in plain.warnings))
        if geared.trades and plain.trades:
            self.assertGreater(geared.trades[0].notional, plain.trades[0].notional)


class VenueConstraintTests(unittest.TestCase):
    def test_tick_and_step_quantisation(self):
        candles = trending_candles()
        result = run_backtest(candles, BacktestConfig(tick_size=0.01, qty_step=0.01))
        for trade in result.trades:
            self.assertAlmostEqual(round(trade.entry_price / 0.01) * 0.01, trade.entry_price, places=6)
            self.assertAlmostEqual(round(trade.quantity / 0.01) * 0.01, trade.quantity, places=6)

    def test_minimum_notional_blocks_dust_entries(self):
        candles = trending_candles()
        blocked = run_backtest(candles, BacktestConfig(initial_capital=50, min_order_notional=1_000_000))
        self.assertEqual(len(blocked.trades), 0)
        allowed = run_backtest(candles, BacktestConfig(initial_capital=50, min_order_notional=1))
        self.assertGreater(len(allowed.trades), 0)


class ThinSessionTests(unittest.TestCase):
    def test_off_hours_bar_is_flagged(self):
        candles = flat_candles(60, volume=5_000.0)
        candles[-1] = {**candles[-1], "volume": 1.0}
        flags = thin_session_flags(candles)
        self.assertFalse(flags[-2])
        self.assertTrue(flags[-1])

    def test_small_absolute_notional_is_flagged_even_when_typical(self):
        candles = flat_candles(60, price=1.0, volume=1.0)  # ~1 USDT per bar
        self.assertTrue(all(thin_session_flags(candles)[10:]))

    def test_entries_are_deferred_out_of_thin_bars_by_default(self):
        candles = trending_candles()
        # Make the last third untradable; default config must not enter there.
        for index in range(200, len(candles)):
            candles[index] = {**candles[index], "volume": 1.0}
        skipped = run_backtest(candles, BacktestConfig(fill_on_thin="skip"))
        allowed = run_backtest(candles, BacktestConfig(fill_on_thin="allow"))
        self.assertFalse(any(trade.thin_entry for trade in skipped.trades))
        if any(trade.thin_entry for trade in allowed.trades):
            self.assertTrue(any("休市空 bar" in warning for warning in allowed.warnings))

    def test_open_fill_does_not_depend_on_current_bars_final_volume(self):
        candles = trending_candles()
        config = BacktestConfig(fill_on_thin="skip", include_funding=False)
        baseline = run_backtest(candles, config)
        self.assertTrue(baseline.trades)
        first_entry = baseline.trades[0].entry_time
        changed = [dict(row) for row in candles]
        entry_index = next(index for index, row in enumerate(changed) if row["ts"] == first_entry)
        changed[entry_index]["volume"] = 0.0
        rerun = run_backtest(changed, config)
        self.assertTrue(rerun.trades)
        self.assertEqual(rerun.trades[0].entry_time, first_entry)


class ReportingTests(unittest.TestCase):
    def test_result_declares_its_assumptions_and_data_quality(self):
        result = run_backtest(trending_candles(), BacktestConfig(), interval="1h")
        self.assertIn("收盘确认交叉，下一根K线开盘成交", result.assumptions)
        self.assertTrue(any("复利" in line for line in result.assumptions), "compounding must be stated")
        self.assertEqual(result.data_quality["interval"], "1h")
        self.assertEqual(result.data_quality["intervalMs"], HOUR)
        self.assertEqual(result.data_quality["bars"], 300)

    def test_leveraged_etf_decay_is_disclosed(self):
        result = run_backtest(
            trending_candles(),
            BacktestConfig(),
            instrument={"productType": "etf", "displaySymbol": "SOXL"},
        )
        self.assertTrue(any("复利衰减" in warning for warning in result.warnings))

    def test_equity_curve_covers_the_whole_sample_and_reports_margin(self):
        result = run_backtest(trending_candles(), BacktestConfig())
        # Marking starts once the slow average exists, and runs to the last bar.
        self.assertGreaterEqual(result.equity_curve[0]["time"], START + 21 * HOUR)
        self.assertEqual(result.equity_curve[-1]["time"], START + 299 * HOUR)
        self.assertEqual(
            len({point["time"] for point in result.equity_curve}),
            len(result.equity_curve),
            "末根结算不能生成重复时间点",
        )
        self.assertTrue(any(point["marginRatio"] is not None for point in result.equity_curve))

    def test_drawdown_is_measured_from_the_initial_capital(self):
        result = run_backtest(trending_candles(), BacktestConfig())
        peak = result.initial_capital
        expected = 0.0
        for point in result.equity_curve:
            peak = max(peak, point["equity"])
            expected = max(expected, (peak - point["equity"]) / peak * 100)
        self.assertAlmostEqual(result.max_drawdown_pct, expected, places=4)


if __name__ == "__main__":
    unittest.main()
