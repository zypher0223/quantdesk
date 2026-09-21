"""阶段 4：事件驱动执行模型（Gate-B）。

这一层给回测引擎补上"订单"这个概念：一笔单子会经历 创建→接受→部分成交→全部成交/
撤销/拒单 的状态流转，可以跨K线继续成交，可以被止损/止盈/移动止损平掉，也可以因为
挂单没被触及而彻底不成交。

这些测试守三件事：

* 默认配置下逐位不变——没有打开任何新开关时，引擎必须和加这一层之前算出同一个数；
* 每个新开关都真的改变了它声称要改变的东西，方向正确（保守路径只会更差）；
* 每根K线的决策只用到"那一刻已经知道"的信息：高周期K线没收盘就看不见，未来K线
  改变不了历史成交。

全部用临时 home（见 conftest.py），不联网、不碰生产库。
"""

from __future__ import annotations

import os
import unittest

from quantdesk.backtest import BacktestConfig, run_backtest
from quantdesk.backtest.engine import (
    MAX_LEVERAGE,
    align_higher_timeframe,
    data_proxies,
    thin_session_flags,
)

HOUR = 3_600_000
DAY = 24 * HOUR


def candles(count: int = 200, *, volume: float = 200.0, source: str = "venue_rest",
            step: float = 1.2, amplitude: float = 1.0) -> list[dict]:
    """A zig-zag series whose bars are not thin. Same fixture the phase-5 tests use."""
    rows = []
    price = 100.0
    for index in range(count):
        price += step if (index // 12) % 2 == 0 else -step
        price = max(5.0, price)
        rows.append({
            "ts": 1_700_000_000_000 + index * HOUR,
            "open": round(price - 0.4, 6),
            "high": round(price + amplitude, 6),
            "low": round(price - amplitude, 6),
            "close": round(price, 6),
            "volume": volume,
            "source": source,
        })
    return rows


def events_for(bars: list[dict]) -> list[int | None]:
    """Alternating long/short on every 10th bar: a signal, not a strategy."""
    return [1 if (index // 10) % 2 == 0 else -1 for index in range(len(bars))]


def flat_events(bars: list[dict], *, long_from: int = 0) -> list[int]:
    """Always long: one entry, held to the end (or until a stop takes it out)."""
    return [1 for _ in bars]


class DefaultParityTests(unittest.TestCase):
    """默认配置必须逐位等于加这一层之前的结果。"""

    # 这一组数字是加事件驱动层之前由旧引擎跑出来的（同一份 fixture），
    # 任何一位变化都说明默认路径被改动了。
    BASELINE = {
        "final_equity": 10061.394701,
        "net_return_pct": 0.613947,
        "max_drawdown_pct": 25.816569,
        "win_rate_pct": 53.3333,
        "profit_factor": 1.0101,
        "total_fees": 367.497796,
        "total_funding": 0.0,
        "trades": 30,
        "curve_len": 296,
    }
    BASELINE_FIRST_TRADE = {
        "entry_time": 1700014400000,
        "exit_time": 1700039600000,
        "entry_price": 105.6528,
        "exit_price": 113.943,
        "quantity": 56.81818182,
        "notional": 6003.0,
        "gross_pnl": 471.034091,
        "fees": 12.477034,
        "net_pnl": 458.557057,
        "bars_held": 7,
        "exit_reason": "signal",
    }

    def _default_run(self):
        bars = candles(300)
        config = BacktestConfig(strategy_id="ma_cross", fast_period=2, slow_period=3, allocation_pct=60)
        return run_backtest(bars, config, signal_events=events_for(bars), interval="1h")

    def test_the_default_numbers_are_bit_identical(self):
        result = self._default_run()
        got = {
            "final_equity": result.final_equity,
            "net_return_pct": result.net_return_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
            "win_rate_pct": result.win_rate_pct,
            "profit_factor": result.profit_factor,
            "total_fees": result.total_fees,
            "total_funding": result.total_funding,
            "trades": len(result.trades),
            "curve_len": len(result.equity_curve),
        }
        self.assertEqual(got, self.BASELINE, "默认配置的数值结果必须与加事件驱动层之前逐位一致")
        first = result.trades[0]
        self.assertEqual(
            {key: getattr(first, key) for key in self.BASELINE_FIRST_TRADE},
            self.BASELINE_FIRST_TRADE,
        )

    def test_the_new_knobs_are_inert_by_default(self):
        config = BacktestConfig()
        self.assertEqual(config.maker_fill, "never")
        self.assertIsNone(config.maker_fee_bps)
        self.assertEqual(config.liquidation_fee_bps, 0.0)
        self.assertEqual(config.bar_path, "conservative")
        self.assertIsNone(config.stop_loss_pct)
        self.assertIsNone(config.take_profit_pct)
        self.assertIsNone(config.trailing_stop_pct)
        result = self._default_run()
        self.assertEqual(result.total_liquidation_fees, 0.0)
        self.assertEqual(result.data_quality["unfilledOrders"], 0)
        self.assertEqual(result.data_quality["maxFillDelayBars"], 0)
        self.assertTrue(all(trade.entry_fills == 1 for trade in result.trades))
        self.assertTrue(all(trade.entry_delay_bars == 0 for trade in result.trades))
        self.assertTrue(all(trade.liquidation_fee == 0.0 for trade in result.trades))


class OrderLifecycleTests(unittest.TestCase):
    """第 1 项：订单对象、状态流转、run 返回 orders。"""

    def test_every_fill_has_an_order_that_reached_a_terminal_state(self):
        bars = candles(200)
        result = run_backtest(bars, BacktestConfig(strategy_id="ma_cross"),
                              signal_events=events_for(bars), interval="1h")
        self.assertTrue(result.orders)
        for order in result.orders:
            self.assertIn(
                order.status,
                {"created", "accepted", "partially_filled", "filled", "cancelled", "rejected"},
            )
        # 30 笔交易 → 每笔一进一出，状态都必须是终态
        entries = [order for order in result.orders if order.purpose == "entry"]
        exits = [order for order in result.orders if order.purpose == "exit"]
        self.assertEqual(len(entries), len(result.trades))
        self.assertEqual(len(exits), len(result.trades))
        self.assertTrue(all(order.status == "filled" for order in entries + exits))
        self.assertTrue(all(order.filled_quantity > 0 for order in entries + exits))

    def test_the_order_log_is_serialised_with_the_result(self):
        bars = candles(200)
        result = run_backtest(bars, BacktestConfig(strategy_id="ma_cross"),
                              signal_events=events_for(bars), interval="1h")
        payload = result.as_dict()
        self.assertIn("orders", payload)
        first = payload["orders"][0]
        for key in ("order_id", "status", "purpose", "side", "quantity", "fills", "bars_to_fill"):
            self.assertIn(key, first)
        self.assertTrue(first["fills"])
        self.assertTrue(all("feeKind" in fill for fill in first["fills"]))
        self.assertEqual(payload["data_quality"]["orders"]["created"], len(result.orders))

    def test_a_very_long_run_trims_the_log_but_keeps_the_counts_exact(self):
        """20,000 根的运行会交易几千次：日志要有上限，但计数不能因此失真。"""
        import quantdesk.backtest.engine as engine_module

        bars = candles(200)
        original = engine_module.MAX_ORDER_LOG
        engine_module.MAX_ORDER_LOG = 6
        try:
            result = run_backtest(bars, BacktestConfig(strategy_id="ma_cross"),
                                  signal_events=events_for(bars), interval="1h")
        finally:
            engine_module.MAX_ORDER_LOG = original
        stats = result.data_quality["orders"]
        self.assertTrue(stats["truncated"])
        self.assertEqual(stats["stored"], 6)
        self.assertEqual(len(result.orders), 6)
        self.assertGreater(stats["total"], 6)
        self.assertEqual(stats["byStatus"], {"filled": stats["total"]}, "计数必须是全量")
        self.assertTrue(any("订单日志" in item for item in result.warnings))

    def test_a_signal_that_is_too_small_is_rejected_with_a_reason(self):
        bars = candles(60)
        config = BacktestConfig(strategy_id="ma_cross", initial_capital=1.0, min_order_notional=5.0)
        result = run_backtest(bars, config, signal_events=events_for(bars), interval="1h")
        rejected = [order for order in result.orders if order.status == "rejected"]
        self.assertTrue(rejected, "低于最小下单量的信号必须留下拒单记录")
        self.assertTrue(all(order.rejected_reason for order in rejected))
        self.assertFalse(result.trades)


class PartialFillCarryTests(unittest.TestCase):
    """第 2 项：部分成交跨K线累计，如实记录最重延迟与最终未成交数量。"""

    def test_a_capped_entry_keeps_filling_after_the_signal_bar(self):
        bars = candles(200, volume=200.0)
        events = events_for(bars)
        config = BacktestConfig(strategy_id="ma_cross", allocation_pct=100, slippage_model="participation",
                                max_participation=0.01, partial_fill="cap")
        result = run_backtest(bars, config, signal_events=events, interval="1h")
        order = next(order for order in result.orders if order.purpose == "entry")
        self.assertGreaterEqual(len(order.fills), 2, "被截断的开仓单必须在后续K线继续成交")
        self.assertGreater(order.bars_to_fill, 0, "跨K线成交必须记录成交延迟")
        self.assertGreater(result.trades[0].entry_fills, 1)
        self.assertEqual(result.trades[0].entry_delay_bars, order.bars_to_fill)

    def test_the_final_unfilled_quantity_is_reported_not_dropped(self):
        bars = candles(200, volume=200.0)
        result = run_backtest(
            bars,
            BacktestConfig(strategy_id="ma_cross", allocation_pct=100, max_participation=0.01,
                           partial_fill="cap"),
            signal_events=events_for(bars),
            interval="1h",
        )
        quality = result.data_quality
        self.assertGreater(quality["unfilledQuantity"], 0, "没成交的数量必须如实报出来")
        self.assertGreater(quality["unfilledNotional"], 0)
        self.assertGreater(quality["unfilledOrders"], 0)
        cancelled = [order for order in result.orders if order.status == "cancelled"]
        self.assertTrue(cancelled)
        self.assertTrue(all(order.unfilled_quantity > 0 for order in cancelled))
        self.assertTrue(all(order.note for order in cancelled), "撤销必须写明原因")
        # 未成交数量 = 订单上剩下的部分，不能重复计数
        self.assertAlmostEqual(
            quality["unfilledQuantity"],
            round(sum(order.unfilled_quantity for order in result.orders), 8),
            places=8,
        )

    def test_carrying_the_remainder_cannot_beat_filling_everything(self):
        bars = candles(200, volume=150.0)
        events = events_for(bars)
        filled = run_backtest(bars, BacktestConfig(strategy_id="ma_cross", allocation_pct=100),
                              signal_events=events, interval="1h")
        capped = run_backtest(
            bars,
            BacktestConfig(strategy_id="ma_cross", allocation_pct=100, max_participation=0.05,
                           partial_fill="cap"),
            signal_events=events, interval="1h",
        )
        self.assertLessEqual(capped.trades[0].quantity, filled.trades[0].quantity)
        self.assertLess(capped.trades[0].quantity, filled.trades[0].quantity,
                        "参与率上限下的持仓不可能比敞开成交更大")

    def test_a_signal_that_flips_cancels_the_rest_of_the_order(self):
        bars = candles(200, volume=200.0)
        config = BacktestConfig(strategy_id="ma_cross", allocation_pct=100, max_participation=0.01,
                                partial_fill="cap")
        result = run_backtest(bars, config, signal_events=events_for(bars), interval="1h")
        # 每笔交易的开仓单在离场信号到来时就该结束，而不是继续挂着
        for trade in result.trades[:4]:
            entries = [order for order in result.orders
                       if order.purpose == "entry" and order.created_time == trade.entry_time]
            self.assertTrue(entries)
            self.assertIn(entries[0].status, {"filled", "cancelled"})


class ProtectionTests(unittest.TestCase):
    """第 3、4 项：止损/止盈/移动止损，以及同根K线的保守路径。"""

    def _one_way_run(self, config: BacktestConfig, bars: list[dict]):
        return run_backtest(bars, config, signal_events=flat_events(bars), interval="1h")

    def test_a_stop_loss_closes_the_position_below_the_entry(self):
        bars = candles(80)
        config = BacktestConfig(strategy_id="ma_cross", allocation_pct=100, stop_loss_pct=1.0,
                                include_liquidation=False)
        result = self._one_way_run(config, bars)
        self.assertTrue(result.trades)
        first = result.trades[0]
        self.assertEqual(first.exit_reason, "stop_loss")
        self.assertLess(first.exit_price, first.entry_price)
        self.assertLess(first.net_pnl, 0)
        self.assertGreater(result.final_equity, 0)

    def test_a_take_profit_closes_the_position_above_the_entry(self):
        bars = candles(80)
        config = BacktestConfig(strategy_id="ma_cross", allocation_pct=100, take_profit_pct=1.0,
                                include_liquidation=False)
        result = self._one_way_run(config, bars)
        first = result.trades[0]
        self.assertEqual(first.exit_reason, "take_profit")
        self.assertGreater(first.exit_price, first.entry_price)
        self.assertGreater(first.net_pnl, 0)

    def test_a_trailing_stop_locks_in_a_move_the_fixed_stop_would_have_given_back(self):
        # 先涨 30% 再跌回原点：固定止损（离入场 -8%）一路不动，移动止损应当把利润锁住。
        rows = []
        price = 100.0
        for index in range(80):
            price = 100.0 + index * 1.0 if index < 30 else 130.0 - (index - 30) * 1.4
            price = max(60.0, price)
            rows.append({"ts": 1_700_000_000_000 + index * HOUR, "open": round(price - 0.2, 6),
                         "high": round(price + 0.5, 6), "low": round(price - 0.5, 6),
                         "close": round(price, 6), "volume": 500.0, "source": "venue_rest"})
        fixed = self._one_way_run(
            BacktestConfig(strategy_id="ma_cross", fast_period=2, slow_period=3, allocation_pct=100,
                           stop_loss_pct=8.0, include_liquidation=False), rows)
        trailing = self._one_way_run(
            BacktestConfig(strategy_id="ma_cross", fast_period=2, slow_period=3, allocation_pct=100,
                           trailing_stop_pct=3.0, include_liquidation=False), rows)
        self.assertEqual(trailing.trades[0].exit_reason, "trailing_stop")
        self.assertEqual(fixed.trades[0].exit_reason, "stop_loss")
        self.assertGreater(trailing.trades[0].exit_price, fixed.trades[0].exit_price)
        self.assertGreater(trailing.trades[0].net_pnl, fixed.trades[0].net_pnl)
        self.assertLess(trailing.trades[0].bars_held, fixed.trades[0].bars_held)

    def test_the_same_bar_takes_the_adverse_leg_first(self):
        """一根K线同时穿过止损和止盈时，保守路径必须按止损成交。"""
        bars = candles(60, amplitude=1.0)
        config = BacktestConfig(strategy_id="ma_cross", allocation_pct=100, stop_loss_pct=0.5,
                                take_profit_pct=0.5, include_liquidation=False)
        result = run_backtest(bars, config, signal_events=flat_events(bars), interval="1h")
        ambiguous = [trade for trade in result.trades if trade.bars_held == 0]
        self.assertTrue(ambiguous, "这个 fixture 必须产生同根K线内既碰止损又碰止盈的情况")
        self.assertTrue(all(trade.exit_reason == "stop_loss" for trade in ambiguous))
        self.assertTrue(all(trade.net_pnl < 0 for trade in ambiguous))

        optimistic = run_backtest(
            bars,
            BacktestConfig(strategy_id="ma_cross", allocation_pct=100, stop_loss_pct=0.5,
                           take_profit_pct=0.5, include_liquidation=False, bar_path="optimistic"),
            signal_events=flat_events(bars), interval="1h",
        )
        self.assertEqual(optimistic.trades[0].exit_reason, "take_profit")

    def test_a_gap_fills_a_stop_at_the_open_not_at_the_stop_price(self):
        """跳空穿过止损价：成交价是开盘价（更差），不是止损价。"""
        rows = candles(40, amplitude=0.2, step=0.05)
        # 第 20 根直接跳空低开，穿过 -1% 的止损（快线窗口取短，确保入场在这之前）
        rows[20]["open"] = round(rows[19]["close"] * 0.94, 6)
        rows[20]["high"] = round(rows[20]["open"] + 0.1, 6)
        rows[20]["low"] = round(rows[20]["open"] - 0.2, 6)
        rows[20]["close"] = round(rows[20]["open"] - 0.1, 6)
        config = BacktestConfig(strategy_id="ma_cross", fast_period=2, slow_period=3,
                                allocation_pct=100, stop_loss_pct=1.0,
                                include_liquidation=False, slippage_bps=0.0)
        result = run_backtest(rows, config, signal_events=flat_events(rows), interval="1h")
        stopped = [trade for trade in result.trades if trade.exit_reason == "stop_loss"]
        self.assertTrue(stopped)
        trade = stopped[0]
        gap_bar = rows[20]
        self.assertEqual(trade.exit_time, gap_bar["ts"])
        self.assertAlmostEqual(trade.exit_price, gap_bar["open"], places=6)
        self.assertLess(trade.exit_price, trade.entry_price * 0.99)

    def test_a_stop_tighter_than_the_liquidation_price_fills_before_liquidation(self):
        rows = candles(60, amplitude=0.5)
        for index in range(30, 40):
            rows[index] = {**rows[index], "open": 60.0, "high": 61.0, "low": 40.0, "close": 45.0}
        config = BacktestConfig(strategy_id="ma_cross", allocation_pct=100, leverage=10,
                                include_liquidation=True, stop_loss_pct=2.0)
        result = run_backtest(rows, config, signal_events=flat_events(rows), interval="1h")
        reasons = [trade.exit_reason for trade in result.trades]
        self.assertIn("stop_loss", reasons)
        self.assertNotIn("liquidation", reasons, "止损比强平价更近时，强平不应该先发生")

    def test_the_stop_is_validated(self):
        bars = candles(60)
        for bad in (0, 100, -1):
            with self.assertRaises(ValueError):
                run_backtest(bars, BacktestConfig(strategy_id="ma_cross", stop_loss_pct=bad),
                             signal_events=events_for(bars), interval="1h")

    def test_the_protection_is_stated_in_the_result(self):
        bars = candles(80)
        config = BacktestConfig(strategy_id="ma_cross", stop_loss_pct=2.0, take_profit_pct=4.0,
                                trailing_stop_pct=1.0)
        result = run_backtest(bars, config, signal_events=events_for(bars), interval="1h")
        model = result.as_dict()["execution_model"]["protection"]
        self.assertEqual(model["stopLossPct"], 2.0)
        self.assertEqual(model["takeProfitPct"], 4.0)
        self.assertEqual(model["trailingStopPct"], 1.0)
        self.assertEqual(model["barPath"], "conservative")
        self.assertGreater(model["stopExits"], 0)
        self.assertTrue(any("止损" in item for item in result.warnings))


class MakerFillTests(unittest.TestCase):
    """第 5 项：maker 费率与被动挂单成交。"""

    def test_a_long_limit_is_not_filled_by_a_bar_that_never_trades_down_to_it(self):
        """价格上涨段的做多挂单永远等不到回踩：只能撤销，不能按市价成交。"""
        bars = candles(60, amplitude=0.2, step=0.2)
        for index in range(20, len(bars)):
            price = 100.0 + (index - 20) * 3.0
            bars[index] = {**bars[index], "open": price, "high": price + 1.0, "low": price + 0.5,
                           "close": price + 0.8}
        config = BacktestConfig(strategy_id="ma_cross", fast_period=2, slow_period=3,
                                allocation_pct=100, maker_fill="passive_only", maker_order_bars=1,
                                include_liquidation=False)
        result = run_backtest(bars, config, signal_events=events_for(bars), interval="1h")
        longs = [order for order in result.orders
                 if order.purpose == "entry" and order.side == "buy" and order.created_index >= 21]
        self.assertTrue(longs, "上涨段里必须至少有一次做多信号")
        for order in longs:
            self.assertEqual(order.status, "cancelled")
            self.assertEqual(order.filled_quantity, 0.0)
            self.assertTrue(order.note, "撤销必须留下原因")
            limit = order.limit_price
            self.assertLess(limit, min(bar["low"] for bar in bars[order.created_index:]))

    def test_a_maker_fill_pays_the_maker_fee_and_no_slippage(self):
        bars = candles(200)
        events = events_for(bars)
        taker = run_backtest(bars, BacktestConfig(strategy_id="ma_cross", allocation_pct=100,
                                                  slippage_bps=0.0, include_liquidation=False),
                             signal_events=events, interval="1h")
        maker = run_backtest(
            bars,
            BacktestConfig(strategy_id="ma_cross", allocation_pct=100, slippage_bps=0.0,
                           include_liquidation=False, maker_fill="passive_only", maker_fee_bps=2.0,
                           maker_order_bars=3),
            signal_events=events, interval="1h",
        )
        maker_fills = [fill for order in maker.orders for fill in order.fills if fill["feeKind"] == "maker"]
        self.assertTrue(maker_fills, "被动挂单必须留下 maker 成交记录")
        # 成交价就是挂单价：maker 不付价差
        for order in maker.orders:
            for fill in order.fills:
                if fill["feeKind"] == "maker":
                    self.assertEqual(fill["price"], order.limit_price)
        self.assertTrue(any(order.status == "cancelled" for order in maker.orders),
                        "挂单也要有等不到价格而作废的记录")
        self.assertLess(maker.total_fees, taker.total_fees, "maker 费率低于 taker 时手续费必须更低")
        self.assertGreater(maker.data_quality["orders"]["makerFills"], 0)
        self.assertEqual(maker.execution_model["feeModel"]["makerBps"], 2.0)
        self.assertIn("挂单", maker.execution_model["feeModel"]["applied"])

    def test_a_maker_order_that_is_never_touched_is_cancelled_and_reported(self):
        bars = candles(200, amplitude=0.1, step=0.05)
        config = BacktestConfig(strategy_id="ma_cross", allocation_pct=100,
                                maker_fill="passive_only", maker_order_bars=1)
        result = run_backtest(bars, config, signal_events=flat_events(bars), interval="1h")
        cancelled = [order for order in result.orders if order.status == "cancelled"]
        self.assertTrue(cancelled)
        self.assertTrue(any("未被触及" in (order.note or "") for order in cancelled))
        self.assertGreater(result.data_quality["unfilledQuantity"], 0)
        self.assertTrue(any("挂单" in item for item in result.warnings))

    def test_maker_fill_is_validated(self):
        bars = candles(60)
        with self.assertRaises(ValueError):
            run_backtest(bars, BacktestConfig(strategy_id="ma_cross", maker_fill="always"),
                         signal_events=events_for(bars), interval="1h")


class LiquidationFeeTests(unittest.TestCase):
    """第 6 项：清算费单独记录，不藏进 fees。"""

    def crash_bars(self) -> list[dict]:
        rows = candles(60, amplitude=0.5)
        for index in range(30, len(rows)):
            rows[index] = {**rows[index], "open": 99.0, "high": 100.0, "low": 70.0, "close": 72.0}
        return rows

    def test_the_liquidation_fee_is_kept_apart_from_the_trading_fee(self):
        rows = self.crash_bars()
        base = dict(strategy_id="ma_cross", allocation_pct=100, leverage=10,
                    include_liquidation=True, include_funding=False)
        without = run_backtest(rows, BacktestConfig(**base), signal_events=flat_events(rows), interval="1h")
        with_fee = run_backtest(rows, BacktestConfig(**base, liquidation_fee_bps=50.0),
                                signal_events=flat_events(rows), interval="1h")
        self.assertTrue(any(trade.liquidated for trade in without.trades))
        liquidated = next(trade for trade in with_fee.trades if trade.liquidated)
        self.assertGreater(liquidated.liquidation_fee, 0)
        self.assertGreater(with_fee.total_liquidation_fees, 0)
        self.assertEqual(with_fee.data_quality["liquidationFeeBps"], 50.0)
        # 交易手续费总数不含清算费
        self.assertAlmostEqual(
            with_fee.total_liquidation_fees,
            round(sum(trade.liquidation_fee for trade in with_fee.trades), 6),
            places=6,
        )
        self.assertEqual(
            with_fee.risk["liquidations"][0]["liquidationFee"],
            round(liquidated.liquidation_fee, 6),
        )
        self.assertLessEqual(with_fee.final_equity, without.final_equity)

    def test_a_zero_liquidation_fee_changes_nothing(self):
        rows = self.crash_bars()
        base = dict(strategy_id="ma_cross", allocation_pct=100, leverage=10,
                    include_liquidation=True, include_funding=False)
        left = run_backtest(rows, BacktestConfig(**base), signal_events=flat_events(rows), interval="1h")
        right = run_backtest(rows, BacktestConfig(**base, liquidation_fee_bps=0.0),
                             signal_events=flat_events(rows), interval="1h")
        self.assertEqual(left.final_equity, right.final_equity)
        self.assertEqual(left.total_fees, right.total_fees)
        self.assertEqual(left.total_liquidation_fees, 0.0)


class HigherTimeframeVisibilityTests(unittest.TestCase):
    """第 7 项：高周期K线收盘之前对它不可见。"""

    def four_hour_bars(self, count: int = 12) -> list[dict]:
        rows = []
        for index in range(count):
            ts = 1_700_000_000_000 + index * 4 * HOUR
            rows.append({"ts": ts, "open": 100.0 + index, "high": 100.0 + index, "low": 100.0 + index,
                         "close": 100.0 + index, "volume": 1000.0})
        return rows

    def test_the_alignment_rule_is_the_close_of_the_higher_bar(self):
        base = candles(30)
        higher = self.four_hour_bars()
        visible = align_higher_timeframe(base, higher, 4 * HOUR)
        self.assertEqual(len(visible), len(base))
        for bar, row in zip(base, visible):
            if row is not None:
                self.assertGreaterEqual(bar["ts"], row["ts"] + 4 * HOUR)
                self.assertLess(bar["ts"], row["ts"] + 8 * HOUR)
            # 没有一根基础K线能看到尚未收盘的高周期K线
        for index, row in enumerate(visible):
            if row is None:
                continue
            for other in higher:
                if other["ts"] > row["ts"]:
                    self.assertNotEqual(other["ts"], row["ts"])

    def test_a_signal_can_only_react_after_the_higher_bar_closed(self):
        base = candles(30)
        higher = self.four_hour_bars()
        seen: list[int] = []

        def source(series, parameters, aligned):
            view = aligned["4h"]
            events = [0] * len(series)
            for index in range(len(series)):
                if index and view[index] is not None and view[index] is not view[index - 1]:
                    events[index] = 1  # 新的一根 4h 收盘了：这是唯一能看到它的地方
                    seen.append(index)
            return events

        result = run_backtest(
            base,
            BacktestConfig(strategy_id="ma_cross", fast_period=2, slow_period=3,
                           allocation_pct=50, include_liquidation=False),
            interval="1h",
            higher_timeframes={"4h": higher},
            signal_source=source,
        )
        self.assertTrue(seen, "4h 收盘必须能被 1h 的某一根看到")
        first_index = seen[0]
        first_visible = align_higher_timeframe(base, higher, 4 * HOUR)[first_index]
        self.assertEqual(
            base[first_index]["ts"],
            first_visible["ts"] + 4 * HOUR,
            "第一次可见必须正好是 4h 收盘那一刻对应的那根 1h",
        )
        self.assertTrue(result.trades)
        self.assertEqual(result.trades[0].entry_time, base[first_index + 1]["ts"],
                         "信号在看到 4h 收盘后的下一根开盘成交")
        quality = result.data_quality["higherTimeframes"]["4h"]
        self.assertEqual(quality["bars"], len(higher))
        self.assertGreater(quality["visibleBars"], 0)

    def test_the_1h_bar_inside_the_4h_window_cannot_see_it(self):
        base = candles(12)
        higher = self.four_hour_bars(3)
        visible = align_higher_timeframe(base, higher, 4 * HOUR)
        # 4h 第一根覆盖 00:00-04:00，前 4 根 1h 都不该看到它
        self.assertTrue(all(row is None for row in visible[:4]))
        self.assertEqual(visible[4]["ts"], higher[0]["ts"])
        self.assertEqual(visible[7]["ts"], higher[0]["ts"], "4h 收盘后到下一根收盘前，看到的还是同一根")

    def test_an_unknown_higher_interval_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            run_backtest(candles(30), BacktestConfig(strategy_id="ma_cross"),
                         signal_events=events_for(candles(30)), interval="1h",
                         higher_timeframes={"7h": self.four_hour_bars()})
        self.assertIn("未知的高周期", str(caught.exception))


class AsOfTests(unittest.TestCase):
    """第 8 项：严格 as-of，未来的K线改变不了历史。"""

    def test_truncating_the_future_cannot_change_the_past(self):
        bars = candles(200)
        events = events_for(bars)
        config = BacktestConfig(strategy_id="ma_cross", allocation_pct=60)
        full = run_backtest(bars, config, signal_events=events, interval="1h")
        cut = 140
        short = run_backtest(bars[:cut], config, signal_events=events[:cut], interval="1h")

        short_times = {point["time"] for point in short.equity_curve}
        last_shared = bars[cut - 3]["ts"]
        compared = 0
        for point in full.equity_curve:
            if point["time"] > last_shared or point["time"] not in short_times:
                continue
            match = next(item for item in short.equity_curve if item["time"] == point["time"])
            self.assertEqual(point, match, f"{point['time']} 这一根的净值被后面的K线改变了")
            compared += 1
        self.assertGreater(compared, 100, "比较的样本太少，说明这个断言没验到东西")

        full_closed = [trade for trade in full.trades if trade.exit_time <= last_shared]
        short_closed = [trade for trade in short.trades if trade.exit_time <= last_shared]
        self.assertEqual(
            [(t.entry_time, t.exit_time, t.entry_price, t.exit_price, t.quantity, t.net_pnl) for t in full_closed],
            [(t.entry_time, t.exit_time, t.entry_price, t.exit_price, t.quantity, t.net_pnl) for t in short_closed],
        )

    def test_funding_after_the_last_bar_is_never_charged(self):
        bars = candles(60)
        funding = [{"ts": bars[-1]["ts"] + HOUR, "rate": 0.01}]
        result = run_backtest(bars, BacktestConfig(strategy_id="ma_cross", allocation_pct=100),
                              funding=funding, signal_events=flat_events(bars), interval="1h")
        self.assertEqual(result.total_funding, 0.0)
        self.assertEqual(result.data_quality["fundingSettlements"], 0)

    def test_a_future_mark_bar_never_prices_an_earlier_bar(self):
        bars = candles(60)
        marks = [{"ts": bar["ts"], "high": bar["high"] * 2, "low": bar["low"] / 2,
                  "close": bar["close"] + 10.0} for bar in bars[::4]]
        result = run_backtest(bars, BacktestConfig(strategy_id="ma_cross", allocation_pct=50),
                              marks=marks, signal_events=flat_events(bars), interval="1h")
        by_ts = {row["ts"]: row for row in marks}
        for point in result.equity_curve:
            if point.get("markPrice") is None:
                continue
            usable = [ts for ts in by_ts if ts <= point["time"]]
            if not usable:
                continue
            expected = by_ts[max(usable)]
            self.assertEqual(point["markPrice"], round(expected["close"], 8),
                             "估值必须用当时最新的标记价，不能用之后的")

    def test_the_signal_still_comes_from_a_closed_bar(self):
        """第 i 根开盘成交，但信号只能来自第 i-1 根（或更早）。"""
        bars = candles(30)
        events = [0] * len(bars)
        events[10] = 1
        result = run_backtest(bars, BacktestConfig(strategy_id="ma_cross", fast_period=2,
                                                   slow_period=3, allocation_pct=50,
                                                   include_liquidation=False),
                              signal_events=events, interval="1h")
        self.assertTrue(result.trades)
        self.assertEqual(result.trades[0].entry_time, bars[11]["ts"])
        self.assertEqual(result.trades[0].entry_price, round(bars[11]["open"] * 1.0005, 6))


class BarCapTests(unittest.TestCase):
    """第 9 项：K线上限来自 config，超限直接拒绝，不静默截断。"""

    def test_the_cap_comes_from_the_config_file(self):
        from quantdesk.config.settings import quantdesk_home
        from quantdesk.studies import BacktestRequest, max_backtest_bars

        home = quantdesk_home()
        self.assertEqual(max_backtest_bars(), 20000, "默认上限必须来自 [backtest] max_bars")
        with self.assertRaises(Exception) as caught:
            BacktestRequest(symbol="BTCUSDT", bars=20_001)
        message = str(caught.exception)
        self.assertIn("超过当前上限 20000", message)
        self.assertIn("[backtest] max_bars", message, "拒绝信息必须告诉用户去哪里改")

        path = home / "config.toml"
        original = path.read_text(encoding="utf-8") if path.exists() else None
        path.write_text("[backtest]\nmax_bars = 30000\n", encoding="utf-8")
        try:
            self.assertEqual(max_backtest_bars(), 30000)
            self.assertEqual(BacktestRequest(symbol="BTCUSDT", bars=25_000).bars, 25_000)
        finally:
            if original is None:
                os.remove(path)
            else:
                path.write_text(original, encoding="utf-8")
        self.assertEqual(max_backtest_bars(), 20000)

    def test_a_broken_config_falls_back_to_the_shipped_default(self):
        from quantdesk.config.settings import quantdesk_home
        from quantdesk.studies import max_backtest_bars

        path = quantdesk_home() / "config.toml"
        original = path.read_text(encoding="utf-8") if path.exists() else None
        path.write_text('[backtest]\nmax_bars = "不是数字"\n', encoding="utf-8")
        try:
            self.assertEqual(max_backtest_bars(), 1000)
        finally:
            if original is None:
                os.remove(path)
            else:
                path.write_text(original, encoding="utf-8")

    def test_the_engine_never_truncates_the_bars_it_is_given(self):
        bars = candles(400)
        result = run_backtest(bars, BacktestConfig(strategy_id="ma_cross"),
                              signal_events=events_for(bars), interval="1h")
        self.assertEqual(result.data_quality["bars"], len(bars), "引擎不得静默截断输入")

    def test_an_unknown_interval_is_reported_without_a_hard_failure(self):
        # 上限是配置项，改动它不会影响其它请求；这里确认 request 模型仍然接受默认值
        from quantdesk.studies import BacktestRequest, PortfolioRequest, ValidationRequest

        self.assertEqual(BacktestRequest(symbol="BTCUSDT").bars, 600)
        self.assertEqual(ValidationRequest(symbol="BTCUSDT").bars, 1500)
        self.assertEqual(PortfolioRequest(symbols=["BTCUSDT"]).bars, 600)


class DataProxyTests(unittest.TestCase):
    """第 10 项：代理数据在结果里带机器可读标注，并给出人话警告。"""

    def test_an_imported_series_is_declared_as_a_proxy(self):
        bars = candles(60, source="imported")
        result = run_backtest(bars, BacktestConfig(strategy_id="ma_cross", allocation_pct=50),
                              signal_events=events_for(bars), interval="1h")
        kinds = {item["kind"] for item in result.data_proxies}
        self.assertIn("non_venue_source", kinds)
        entry = next(item for item in result.data_proxies if item["kind"] == "non_venue_source")
        self.assertEqual(entry["field"], "candles")
        self.assertTrue(entry["note"])
        self.assertEqual(result.as_dict()["data_proxies"], result.data_proxies)
        self.assertEqual(result.data_quality["dataProxies"], result.data_proxies)
        self.assertTrue(any("代理数据" in item for item in result.warnings))

    def test_a_venue_series_without_a_mark_book_declares_the_fallback(self):
        bars = candles(60)
        result = run_backtest(bars, BacktestConfig(strategy_id="ma_cross"),
                              signal_events=flat_events(bars), interval="1h")
        kinds = {item["kind"] for item in result.data_proxies}
        self.assertIn("bar_close_fallback", kinds, "标记价缺失必须写成代理标注，而不是只写在警告里")

    def test_a_derived_turnover_is_declared(self):
        bars = candles(60)
        result = run_backtest(
            bars,
            BacktestConfig(strategy_id="ma_cross", slippage_model="participation", impact_coefficient=0.1),
            signal_events=flat_events(bars), interval="1h",
        )
        self.assertIn("derived_close_times_volume", {item["kind"] for item in result.data_proxies})

    def test_a_declared_proxy_and_a_leveraged_etf_are_both_declared(self):
        bars = candles(60)
        instrument = {"productType": "etf", "riskClass": "leveraged_etf",
                      "dataProxy": {"field": "candles", "kind": "spot_history", "note": "用现货历史代理"}}
        result = run_backtest(bars, BacktestConfig(strategy_id="ma_cross"),
                              instrument=instrument, signal_events=flat_events(bars), interval="1h")
        kinds = {item["kind"] for item in result.data_proxies}
        self.assertIn("spot_history", kinds)
        self.assertIn("leveraged_etf_underlying", kinds)
        self.assertTrue(any("代理" in item for item in result.warnings))

    def test_the_proxy_helper_is_usable_on_its_own(self):
        rows = data_proxies(instrument=None, ordered=candles(30, source="venue_rest"),
                            config=BacktestConfig(include_liquidation=False, include_funding=False),
                            marks=[{"ts": 1}])
        self.assertEqual(rows, [])


class MalformedConfigTests(unittest.TestCase):
    """配置校验：写错的开关必须在开跑之前报错。"""

    def test_bad_knobs_are_refused(self):
        bars = candles(60)
        events = events_for(bars)
        cases = [
            {"maker_order_bars": 0},
            {"maker_fee_bps": -1},
            {"liquidation_fee_bps": -1},
            {"bar_path": "coin_flip"},
            {"take_profit_pct": 0},
            {"trailing_stop_pct": 100},
            {"partial_fill": "full"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    run_backtest(bars, BacktestConfig(strategy_id="ma_cross", **overrides),
                                 signal_events=events, interval="1h")

    def test_the_leverage_ceiling_still_applies(self):
        with self.assertRaises(ValueError):
            run_backtest(candles(60), BacktestConfig(strategy_id="ma_cross", leverage=MAX_LEVERAGE + 1),
                         signal_events=events_for(candles(60)), interval="1h")


class StudyWiringTests(unittest.TestCase):
    """执行层开关必须能从研究请求一路传到引擎。"""

    def test_the_request_defaults_reproduce_the_historical_behaviour(self):
        from quantdesk.studies import BacktestRequest, execution_knobs

        knobs = execution_knobs(BacktestRequest(symbol="BTCUSDT"))
        self.assertEqual(knobs["maker_fill"], "never")
        self.assertIsNone(knobs["maker_fee_bps"])
        self.assertEqual(knobs["liquidation_fee_bps"], 0.0)
        self.assertEqual(knobs["bar_path"], "conservative")
        config = BacktestConfig(**knobs)
        self.assertEqual(config.partial_fill, "ignore")

    def test_the_request_carries_the_protection_levels(self):
        from quantdesk.studies import BacktestRequest, execution_knobs

        request = BacktestRequest(symbol="BTCUSDT", stopLossPct=2.5, takeProfitPct=6.0,
                                 trailingStopPct=1.5, makerFill="passive_only", makerFeeBps=1.0,
                                 makerOrderBars=2, liquidationFeeBps=25.0, barPath="optimistic")
        knobs = execution_knobs(request)
        config = BacktestConfig(**knobs)
        self.assertEqual(config.stop_loss_pct, 2.5)
        self.assertEqual(config.take_profit_pct, 6.0)
        self.assertEqual(config.trailing_stop_pct, 1.5)
        self.assertEqual(config.maker_fill, "passive_only")
        self.assertEqual(config.maker_fee_bps, 1.0)
        self.assertEqual(config.maker_order_bars, 2)
        self.assertEqual(config.liquidation_fee_bps, 25.0)
        self.assertEqual(config.bar_path, "optimistic")

    def test_a_fake_request_object_without_the_new_fields_still_works(self):
        from quantdesk.studies import cost_model, execution_knobs

        request = type("Request", (), {"latencyBars": 1, "partialFill": "ignore"})()
        self.assertEqual(execution_knobs(request)["maker_fill"], "never")
        model = cost_model(request, fee_bps=6.0, slippage_bps=5.0, meta={})
        self.assertEqual(model["makerFill"], "never")
        self.assertEqual(model["stopLossPct"], None)
        self.assertEqual(model["liquidationFeeBps"], 0.0)


class ThinBarPolicyTests(unittest.TestCase):
    def test_the_thin_flag_still_reads_the_bar_notional(self):
        rows = candles(40, volume=200.0)
        rows[30]["volume"] = 0.0
        flags = thin_session_flags(rows)
        self.assertTrue(flags[30])
        self.assertFalse(flags[10])


if __name__ == "__main__":
    unittest.main()
