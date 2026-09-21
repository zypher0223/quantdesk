"""The structured position model: adds, partial reductions, fees, stops, risk budget.

The event contract can only say "be long", so every test here is about something it
cannot express. Two properties are load-bearing and are asserted first:

* the event path is untouched - a run that mirrors an event series with intents must
  land on exactly the same trades and equity, and the default configuration must still
  produce the numbers `test_execution_lifecycle.DefaultParityTests` locked in;
* an intent on bar `i` fills at bar `i + 1`'s open, never at bar `i`'s - the same
  latency rule the event path follows, because the intent is decided from a closed bar.
"""

from __future__ import annotations

import unittest

from quantdesk.backtest.engine import (
    BacktestConfig,
    PositionIntent,
    _close_partial,
    run_backtest,
    stop_fill_price,
)

HOUR = 3_600_000
START = 1_700_000_000_000


def bar(index: int, *, open_: float, high: float, low: float, close: float,
        volume: float = 500.0) -> dict:
    return {"ts": START + index * HOUR, "open": open_, "high": high, "low": low,
            "close": close, "volume": volume}


def walk(count: int = 120, *, start: float = 100.0, step: float = 0.4,
         wiggle: float = 0.3, volume: float = 500.0) -> list[dict]:
    """A gently rising series whose bars are not thin unless asked to be."""
    rows = []
    price = start
    for index in range(count):
        price += step
        rows.append(bar(index, open_=price - 0.1, high=price + wiggle,
                         low=price - wiggle, close=price, volume=volume))
    return rows


def config(**overrides) -> BacktestConfig:
    payload = {
        "strategy_id": "cpa_cycle",
        "initial_capital": 10_000.0,
        "allocation_pct": 50.0,
        "fee_bps": 6.0,
        "slippage_bps": 0.0,
        "include_funding": False,
        "include_liquidation": False,
        "qty_step": None,
        "tick_size": None,
    }
    payload.update(overrides)
    return BacktestConfig(**payload)


def hold(count: int) -> list[PositionIntent | None]:
    return [None] * count


def summary(result) -> dict:
    return result.data_quality.get("positionIntents") or {}


class EventPathParityTests(unittest.TestCase):
    """Nothing about the event path may have moved."""

    def test_a_mirrored_intent_series_reproduces_the_event_run_exactly(self):
        bars = walk(120)
        events: list[int | None] = [None] * len(bars)
        intents: list[PositionIntent | None] = [None] * len(bars)
        events[20] = 1
        intents[20] = PositionIntent("open", "long", 50.0, "test", None, "mirror")
        events[80] = 0
        intents[80] = PositionIntent("exit", "long", None, "test", None, "mirror")

        by_event = run_backtest(bars, config(), signal_events=events, interval="1h")
        by_intent = run_backtest(bars, config(), position_intents=intents, interval="1h")

        self.assertEqual(len(by_event.trades), len(by_intent.trades))
        self.assertAlmostEqual(by_event.final_equity, by_intent.final_equity, places=6)
        self.assertAlmostEqual(by_event.total_fees, by_intent.total_fees, places=6)
        self.assertAlmostEqual(by_event.net_return_pct, by_intent.net_return_pct, places=6)
        left, right = by_event.trades[0], by_intent.trades[0]
        self.assertAlmostEqual(left.entry_price, right.entry_price, places=8)
        self.assertAlmostEqual(left.exit_price, right.exit_price, places=8)
        self.assertAlmostEqual(left.quantity, right.quantity, places=8)

    def test_an_all_hold_intent_series_does_nothing(self):
        bars = walk(120)
        result = run_backtest(bars, config(), position_intents=hold(len(bars)), interval="1h")
        self.assertEqual(result.trades, [])
        self.assertEqual(result.final_equity, 10_000.0)
        self.assertEqual(result.total_fees, 0.0)
        self.assertEqual(summary(result)["opens"], 0)

    def test_the_two_signal_paths_cannot_be_combined(self):
        bars = walk(60)
        with self.assertRaisesRegex(ValueError, "不能同时传入"):
            run_backtest(bars, config(), signal_events=[None] * len(bars),
                         position_intents=hold(len(bars)), interval="1h")

    def test_an_intent_series_must_match_the_bars(self):
        bars = walk(60)
        with self.assertRaisesRegex(ValueError, "必须与K线数量"):
            run_backtest(bars, config(), position_intents=hold(10), interval="1h")

    def test_an_unknown_action_is_refused_with_its_index(self):
        bars = walk(60)
        intents = hold(len(bars))
        intents[5] = PositionIntent("scale_in", "long", 50.0)
        with self.assertRaisesRegex(ValueError, "第 5 根"):
            run_backtest(bars, config(), position_intents=intents, interval="1h")


class AlignmentTests(unittest.TestCase):
    def test_an_intent_fills_at_the_next_bars_open(self):
        bars = walk(60)
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "long", 50.0, "wedge_pop", None, "entry")
        intents[30] = PositionIntent("exit", "long", None, "wedge_drop", None, "exit")
        result = run_backtest(bars, config(), position_intents=intents, interval="1h")
        trade = result.trades[0]
        self.assertEqual(trade.entry_time, bars[11]["ts"], "第 10 根的意图必须在第 11 根开盘成交")
        self.assertEqual(trade.exit_time, bars[31]["ts"])
        self.assertAlmostEqual(trade.entry_price, bars[11]["open"], places=8)
        self.assertAlmostEqual(trade.exit_price, bars[31]["open"], places=8)

    def test_the_intent_series_is_read_with_the_configured_latency(self):
        bars = walk(60)
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "long", 50.0, "wedge_pop")
        intents[30] = PositionIntent("exit", "long", None, "wedge_drop")
        result = run_backtest(bars, config(latency_bars=2), position_intents=intents, interval="1h")
        self.assertEqual(result.trades[0].entry_time, bars[13]["ts"])


class PositionManagementTests(unittest.TestCase):
    def test_an_add_on_averages_the_entry_price_and_sums_the_size(self):
        bars = walk(120)
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "long", 50.0, "wedge_pop")
        intents[20] = PositionIntent("increase", "long", 75.0, "ema_crossback")
        intents[60] = PositionIntent("exit", "long", None, "wedge_drop")
        result = run_backtest(bars, config(), position_intents=intents, interval="1h")

        first, second = result.orders[0], result.orders[1]
        expected_quantity = first.filled_quantity + second.filled_quantity
        expected_entry = (
            first.filled_quantity * first.avg_fill_price
            + second.filled_quantity * second.avg_fill_price
        ) / expected_quantity
        trade = result.trades[0]
        self.assertAlmostEqual(trade.quantity, expected_quantity, places=8)
        self.assertAlmostEqual(trade.entry_price, expected_entry, places=8)
        self.assertEqual(summary(result)["increases"], 1)
        self.assertGreater(trade.quantity, first.filled_quantity, "加仓后仓位必须变大")

    def test_a_partial_reduction_keeps_the_remainder_open(self):
        bars = walk(160)
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "long", 100.0, "wedge_pop")
        intents[40] = PositionIntent("reduce", "long", 40.0, "exhaustion_extension")
        intents[90] = PositionIntent("exit", "long", None, "wedge_drop")
        result = run_backtest(bars, config(), position_intents=intents, interval="1h")

        self.assertEqual(len(result.trades), 2, "分批减仓与最终离场各产生一笔已实现交易")
        reduced, final = result.trades
        self.assertAlmostEqual(reduced.quantity + final.quantity,
                               result.orders[0].filled_quantity, places=6)
        self.assertGreater(reduced.quantity, 0.0)
        self.assertGreater(final.quantity, 0.0)
        report = summary(result)
        self.assertEqual(report["reduces"], 1)
        self.assertEqual(report["exits"], 1)
        self.assertEqual(report["records"][1]["status"], "reduced")
        self.assertLess(report["records"][1]["positionQuantity"],
                        report["records"][0]["positionQuantity"])

    def test_a_full_exit_leaves_no_position(self):
        bars = walk(120)
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "long", 60.0, "wedge_pop")
        intents[50] = PositionIntent("exit", "long", None, "wedge_drop")
        result = run_backtest(bars, config(), position_intents=intents, interval="1h")
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_reason, "signal")
        # Nothing is left open at the end, so the final equity is exactly the realised
        # result: no position was carried to the closing mark.
        self.assertAlmostEqual(result.final_equity,
                               10_000.0 + sum(t.net_pnl for t in result.trades), places=6)
        self.assertEqual(summary(result)["exits"], 1)

    def test_a_reduce_with_nothing_open_is_reported_not_guessed(self):
        bars = walk(80)
        intents = hold(len(bars))
        intents[20] = PositionIntent("reduce", "long", 50.0, "exhaustion_extension")
        result = run_backtest(bars, config(), position_intents=intents, interval="1h")
        self.assertEqual(result.trades, [])
        self.assertEqual(summary(result)["ignored"], 1)
        self.assertEqual(summary(result)["records"][0]["note"], "没有仓位可减")


class OrderFeeTests(unittest.TestCase):
    def test_every_order_carries_its_own_fee_and_they_sum_to_the_run_total(self):
        bars = walk(160)
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "long", 50.0, "wedge_pop")
        intents[20] = PositionIntent("increase", "long", 80.0, "ema_crossback")
        intents[40] = PositionIntent("reduce", "long", 40.0, "exhaustion_extension")
        intents[90] = PositionIntent("exit", "long", None, "wedge_drop")
        result = run_backtest(bars, config(), position_intents=intents, interval="1h")

        report = summary(result)
        orders = report["orderFees"]
        self.assertEqual(len(orders), len(result.orders))
        self.assertTrue(all(entry["fee"] > 0 for entry in orders), "每笔订单都必须有费用")
        self.assertAlmostEqual(report["orderFeeTotal"], result.total_fees, places=6)
        # …and the per-order fees are the same money the trades report.
        self.assertAlmostEqual(sum(trade.fees for trade in result.trades),
                               result.total_fees, places=6)

    def test_the_fee_of_a_partial_exit_is_only_the_closing_leg(self):
        bars = walk(160)
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "long", 100.0, "wedge_pop")
        intents[40] = PositionIntent("reduce", "long", 50.0, "exhaustion_extension")
        intents[90] = PositionIntent("exit", "long", None, "wedge_drop")
        result = run_backtest(bars, config(), position_intents=intents, interval="1h")

        reduced = result.trades[0]
        entry_fee = result.orders[0].fills[0]["fee"]
        self.assertLess(reduced.exit_fee, entry_fee * 1.01,
                        "减仓那一笔只应承担被平掉部分的费用")
        closed_share = reduced.quantity / result.orders[0].filled_quantity
        self.assertAlmostEqual(reduced.fees, reduced.exit_fee + entry_fee * closed_share, places=6)


class StructuralStopTests(unittest.TestCase):
    def test_a_stop_is_hit_and_fills_at_the_stop_price(self):
        bars = walk(80)
        # A bar that trades well below the stop, but opens above it: the fill is the
        # stop level, not the bar's open, because the stop was reachable after the open.
        entry_index = 11
        stop = bars[entry_index]["open"] * 0.97
        victim = entry_index + 5
        bars[victim] = bar(victim, open_=bars[victim - 1]["close"],
                           high=bars[victim - 1]["close"] + 0.2,
                           low=stop - 5.0, close=stop - 4.0)
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "long", 50.0, "wedge_pop", stop, "structure low")
        result = run_backtest(bars, config(), position_intents=intents, interval="1h")

        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertAlmostEqual(trade.exit_price, stop, places=6)
        self.assertLess(trade.exit_time, bars[-1]["ts"], "止损应在数据结束前发生")
        self.assertEqual(summary(result)["stops"], 1)

    def test_a_gap_through_the_stop_fills_at_the_open_which_is_worse(self):
        bars = walk(80)
        entry_index = 11
        stop = bars[entry_index]["open"] * 0.97
        victim = entry_index + 5
        gapped_open = stop - 3.0
        bars[victim] = bar(victim, open_=gapped_open, high=gapped_open + 0.2,
                           low=gapped_open - 1.0, close=gapped_open - 0.5)
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "long", 50.0, "wedge_pop", stop)
        result = run_backtest(bars, config(), position_intents=intents, interval="1h")
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertAlmostEqual(trade.exit_price, gapped_open, places=6)
        self.assertLess(trade.exit_price, stop, "跳空成交价必须比止损价更差")

    def test_the_stop_price_helper_prefers_the_worse_of_open_and_stop(self):
        self.assertEqual(stop_fill_price(1, 100.0, 95.0), 95.0)
        self.assertEqual(stop_fill_price(1, 100.0, 105.0), 100.0)
        self.assertEqual(stop_fill_price(-1, 100.0, 105.0), 105.0)
        self.assertEqual(stop_fill_price(-1, 100.0, 95.0), 100.0)

    def test_a_short_is_stopped_on_the_way_up(self):
        bars = walk(80, step=-0.4)
        entry_index = 11
        stop = bars[entry_index]["open"] * 1.03
        victim = entry_index + 4
        bars[victim] = bar(victim, open_=bars[victim - 1]["close"],
                           high=stop + 4.0, low=bars[victim - 1]["close"] - 0.2,
                           close=stop + 3.0)
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "short", 50.0, "wedge_drop", stop)
        result = run_backtest(bars, config(direction="both"), position_intents=intents, interval="1h")
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertEqual(trade.direction, "空")
        self.assertAlmostEqual(trade.exit_price, stop, places=6)


class RiskBudgetTests(unittest.TestCase):
    def test_an_entry_beyond_the_risk_budget_is_refused_with_a_reason(self):
        bars = walk(80)
        entry_open = bars[11]["open"]
        stop = entry_open * 0.5          # a 50% stop distance: far beyond a 2% budget
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "long", 100.0, "wedge_pop", stop, "wide structure")
        result = run_backtest(bars, config(), position_intents=intents,
                              max_portfolio_risk_pct=2.0, interval="1h")
        self.assertEqual(result.trades, [], "超预算的开仓不得成交")
        report = summary(result)
        self.assertEqual(report["rejected"], 1)
        self.assertIn("风险预算超限", report["rejectedReasons"][0])
        self.assertEqual(report["riskBudget"], 200.0)
        self.assertEqual(report["maxRiskCarried"], 0.0)

    def test_an_entry_inside_the_budget_is_allowed_and_the_risk_is_reported(self):
        bars = walk(80)
        entry_open = bars[11]["open"]
        stop = entry_open * 0.98        # a 2% stop distance
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "long", 50.0, "wedge_pop", stop)
        intents[60] = PositionIntent("exit", "long", None, "wedge_drop")
        # 这一条只问入场判定，所以先关掉持仓浮动风险的约束（默认开启）；开启后同一段
        # 行情会被反复减仓，那部分由 CarriedRiskTests 正面断言。
        result = run_backtest(bars, config(), position_intents=intents,
                              max_portfolio_risk_pct=5.0, interval="1h",
                              enforce_open_risk=False)
        self.assertEqual(len(result.trades), 1)
        report = summary(result)
        self.assertEqual(report["rejected"], 0)
        self.assertEqual(report["opens"], 1)
        # 50% of 10,000 at 1x = 5,000 notional, a 2% stop = ~100 at risk, under 500,
        # so the entry is admitted. `maxRiskCarried` is now marked at each close against
        # the stop rather than at entry, so a position held all the way to the exit shows
        # more than the 500 budget - exactly the drift the carried-risk constraint
        # exists to stop (see CarriedRiskTests), and why it is switched off here.
        self.assertGreater(report["maxRiskCarried"], report["riskBudget"])
        self.assertFalse(report["riskBudgetEnforced"])
        self.assertEqual(report["riskAlerts"], 0)

    def test_an_add_on_that_would_break_the_budget_is_refused_while_the_first_leg_stands(self):
        bars = walk(120)
        entry_open = bars[11]["open"]
        stop = entry_open * 0.99
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "long", 50.0, "wedge_pop", stop)
        intents[20] = PositionIntent("increase", "long", 100.0, "ema_crossback", stop)
        intents[80] = PositionIntent("exit", "long", None, "wedge_drop")
        # 同上：这一条问的是超预算的加仓能否被拒，风控减仓会先改掉持仓数量。
        result = run_backtest(bars, config(), position_intents=intents,
                              max_portfolio_risk_pct=0.6, interval="1h",
                              enforce_open_risk=False)
        report = summary(result)
        self.assertEqual(report["opens"], 1)
        self.assertEqual(report["increases"], 0, "超预算的加仓必须被拒")
        self.assertEqual(report["rejected"], 1)
        self.assertEqual(len(result.trades), 1, "被拒的加仓不影响已有的首仓")

    def test_an_out_of_range_budget_is_refused(self):
        bars = walk(60)
        with self.assertRaisesRegex(ValueError, "max_portfolio_risk_pct"):
            run_backtest(bars, config(), position_intents=hold(len(bars)),
                         max_portfolio_risk_pct=0.0, interval="1h")


def flat(count: int, *, price: float = 100.0, start: int = 0) -> list[dict]:
    """Bars that go nowhere: no alert can be armed by price drift alone."""
    return [
        bar(start + index, open_=price, high=price * 1.002, low=price * 0.998,
            close=price)
        for index in range(count)
    ]


def risk_records(result) -> list[dict]:
    return [item for item in summary(result)["records"] if item.get("stage") == "risk_budget"]


class CarriedRiskTests(unittest.TestCase):
    """The budget also caps the risk a position is *already* carrying.

    Entry risk is measured once, against the entry price. A trade that runs in favour
    ends up further from its stop, so the same size risks more than it did when it was
    opened - on the shipped CPA defaults the old code reported 1023 carried against an
    800 budget. The correction follows the same latency rule as the intents: the close
    that breaches the budget arms the next bar, and the trim fills at that bar's open,
    never at the close it just observed.
    """

    # Entry at 100 with a 99.6 stop, 50% of 10,000 = 50 units = 20 at risk, inside a
    # 1% budget of ~100. Bar 10 closes at 105: 50 x 5.4 = 270 carried, way outside.
    STOP = 99.6

    def breaching_series(self) -> list[dict]:
        bars = flat(10)
        bars.append(bar(10, open_=100.0, high=105.2, low=99.9, close=105.0))
        bars.append(bar(11, open_=104.5, high=104.6, low=104.4, close=104.5))
        return bars + flat(11, price=104.5, start=12)

    def entry_intents(self, bars: list[dict], extra: dict | None = None) -> list:
        intents = hold(len(bars))
        intents[5] = PositionIntent("open", "long", 50.0, "wedge_pop", self.STOP)
        for index, intent in (extra or {}).items():
            intents[index] = intent
        return intents

    def assert_inside_the_budget(self, records: list[dict], stop: float | None = None) -> None:
        """Every correction left the position inside the budget it cited, at its fill."""
        self.assertTrue(records, "没有任何风控记录可断言")
        level = self.STOP if stop is None else stop
        for item in records:
            carried = abs(item["price"] - level) * item["positionQuantity"]
            self.assertLessEqual(
                carried, item["riskBudget"] + 1e-6,
                f"第 {item['index']} 根减仓后浮动风险 {carried:.6f} 仍超预算 {item['riskBudget']}",
            )

    def test_a_breaching_close_arms_the_next_bar_and_the_trim_fills_at_that_open(self):
        bars = self.breaching_series()
        result = run_backtest(bars, config(), position_intents=self.entry_intents(bars),
                              max_portfolio_risk_pct=1.0, interval="1h")
        report = summary(result)
        records = risk_records(result)
        self.assertEqual(report["riskAlerts"], 1, "只有第 10 根收盘越过预算")
        self.assertEqual(report["riskReductions"], 1)
        self.assertEqual(report["riskExits"], 0)
        self.assertEqual(len(records), 1)
        trimmed = records[0]
        # 越界的是第 10 根的收盘，成交必须在第 11 根的开盘：104.5 而不是 105.0。
        self.assertEqual(trimmed["index"], 11)
        self.assertEqual(trimmed["price"], 104.5)
        self.assertEqual(trimmed["action"], "reduce")
        self.assertEqual(trimmed["status"], "risk_reduced")
        self.assertEqual(trimmed["direction"], "long")
        self.assertEqual(trimmed["stopPrice"], self.STOP)
        self.assertIn("浮动风险", trimmed["reason"])
        self.assertLess(trimmed["positionQuantity"], 50.0, "必须真的减掉了数量")
        self.assert_inside_the_budget(records)
        # 减仓后同一根的收盘不再越界，所以不会连着每一根都砍。
        self.assertEqual(report["riskAlerts"], 1)

    def test_an_excess_too_small_to_trade_closes_the_whole_position_instead(self):
        bars = self.breaching_series()
        # 预算 244.8 对上 49.96 的保留量：超出部分只剩 0.035 个单位（约 3.7 名义额），
        # 低于 min_order_notional=5，所以应当整笔平掉而不是留下一笔不合规的风险。
        result = run_backtest(bars, config(), position_intents=self.entry_intents(bars),
                              max_portfolio_risk_pct=2.449, interval="1h")
        report = summary(result)
        records = risk_records(result)
        self.assertEqual(report["riskExits"], 1)
        self.assertEqual(report["riskReductions"], 0)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["action"], "exit")
        self.assertEqual(records[0]["status"], "risk_exited")
        self.assertEqual(records[0]["positionQuantity"], 0.0)
        self.assertEqual(records[0]["price"], 104.5)
        self.assertIn("整笔平仓", records[0]["reason"])
        # 开仓不产生 trade 记录，只有平仓产生：这一笔就是风控整笔平仓。
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_reason, "risk_budget")
        self.assertEqual(result.trades[0].quantity, 50.0)
        exit_fees = [item["fee"] for item in report["orderFees"] if item["reason"] == "risk_budget"]
        self.assertEqual(len(exit_fees), 1)

    def test_an_exit_at_the_same_open_is_not_trimmed_first(self):
        """An exit intent filling at this open already closes everything at this price."""
        bars = self.breaching_series()
        intents = self.entry_intents(
            bars, {10: PositionIntent("exit", "long", None, "wedge_drop")}
        )
        result = run_backtest(bars, config(), position_intents=intents,
                              max_portfolio_risk_pct=1.0, interval="1h")
        report = summary(result)
        self.assertEqual(report["riskAlerts"], 1, "警报仍然被记下来")
        self.assertEqual(report["riskReductions"], 0)
        self.assertEqual(report["riskExits"], 0)
        self.assertEqual(report["exits"], 1)
        self.assertEqual(risk_records(result), [])
        self.assertEqual(len(result.trades), 1, "只有离场那一笔平仓记录")
        self.assertEqual(result.trades[-1].exit_price, 104.5)
        self.assertEqual(result.trades[-1].exit_reason, "signal")

    def test_the_trim_moves_with_the_direction_of_the_position(self):
        bars = walk(40, start=104.0, step=-0.4)
        entry_open = bars[6]["open"]
        stop = entry_open * 1.004          # 空头：结构失效价在上方
        intents = hold(len(bars))
        intents[5] = PositionIntent("open", "short", 50.0, "wedge_drop", stop)
        result = run_backtest(bars, config(), position_intents=intents,
                              max_portfolio_risk_pct=1.0, interval="1h")
        report = summary(result)
        records = risk_records(result)
        self.assertGreaterEqual(report["riskReductions"], 1)
        self.assertEqual(records[0]["direction"], "short")
        self.assertEqual(records[0]["index"], 12, "第 11 根收盘越界，第 12 根开盘成交")
        self.assertAlmostEqual(records[0]["price"], bars[12]["open"], places=8)
        # 入场 5,000 名义额 / 101.1 ≈ 49.46 个单位，减仓后必须小于它。
        self.assertLess(records[0]["positionQuantity"], 5_000.0 / entry_open)
        for item in records:
            carried = abs(item["price"] - stop) * item["positionQuantity"]
            self.assertLessEqual(carried, item["riskBudget"] + 1e-6)

    def test_the_constraint_only_exists_because_a_budget_was_set(self):
        bars = self.breaching_series()
        intents = self.entry_intents(bars)
        without_budget = run_backtest(bars, config(), position_intents=intents, interval="1h")
        explicit_off = run_backtest(bars, config(), position_intents=intents,
                                    max_portfolio_risk_pct=1.0, interval="1h",
                                    enforce_open_risk=False)
        report = summary(without_budget)
        self.assertIsNone(report["riskBudget"])
        self.assertFalse(report["riskBudgetEnforced"])
        self.assertEqual(report["riskAlerts"], 0)
        self.assertEqual(report["riskReductions"], 0)
        # 没有预算仍然照旧度量已承担的风险，只是没人来约束它。
        self.assertGreater(report["maxRiskCarried"], 0.0)
        self.assertEqual(len(without_budget.trades), 1)
        self.assertEqual(len(explicit_off.trades), 1)
        self.assertAlmostEqual(without_budget.final_equity, explicit_off.final_equity, places=6)

    def test_switching_it_off_reproduces_the_entry_only_behaviour(self):
        """The comparison the parameter exists for, on the shape that motivated it."""
        bars = walk(80)
        entry_open = bars[11]["open"]
        stop = entry_open * 0.98
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "long", 50.0, "wedge_pop", stop)
        intents[60] = PositionIntent("exit", "long", None, "wedge_drop")
        on = run_backtest(bars, config(), position_intents=intents,
                          max_portfolio_risk_pct=5.0, interval="1h")
        off = run_backtest(bars, config(), position_intents=intents,
                           max_portfolio_risk_pct=5.0, interval="1h",
                           enforce_open_risk=False)
        on_report, off_report = summary(on), summary(off)
        self.assertTrue(on_report["riskBudgetEnforced"])
        self.assertFalse(off_report["riskBudgetEnforced"])
        # 关掉：一路持有，风险涨到预算的两倍也没人管。
        self.assertEqual(off_report["riskAlerts"], 0)
        self.assertEqual(off_report["riskReductions"], 0)
        self.assertEqual(len(off.trades), 1)
        self.assertGreater(off_report["maxRiskCarried"], off_report["riskBudget"])
        # 打开：同一个入场被反复削回预算，超出部分只有一根K线的漂移。
        self.assertGreaterEqual(on_report["riskReductions"], 1)
        self.assertGreater(len(on.trades), len(off.trades))
        self.assertLessEqual(on_report["maxRiskCarried"], on_report["riskBudget"] * 1.06)
        self.assert_inside_the_budget(risk_records(on), stop)
        # 减仓是真的落了账：权益与费用都与不约束时不同。
        self.assertNotAlmostEqual(on.final_equity, off.final_equity, places=2)
        self.assertGreater(on_report["orderFeeTotal"], 0.0)

    def test_a_position_without_a_stop_has_no_risk_to_constrain(self):
        bars = self.breaching_series()
        intents = hold(len(bars))
        intents[5] = PositionIntent("open", "long", 50.0, "wedge_pop", None)
        intents[15] = PositionIntent("exit", "long", None, "wedge_drop")
        result = run_backtest(bars, config(), position_intents=intents,
                              max_portfolio_risk_pct=1.0, interval="1h")
        report = summary(result)
        self.assertEqual(report["maxRiskCarried"], 0.0, "没有结构失效价就无从度量")
        self.assertEqual(report["riskAlerts"], 0)
        self.assertEqual(report["riskReductions"], 0)
        self.assertEqual(len(result.trades), 1)

    def test_the_event_path_cannot_reach_the_constraint(self):
        bars = walk(120)
        events: list[int | None] = [None] * len(bars)
        events[20], events[80] = 1, 0
        plain = run_backtest(bars, config(), signal_events=events, interval="1h")
        flagged = run_backtest(bars, config(), signal_events=events, interval="1h",
                               max_portfolio_risk_pct=1.0, enforce_open_risk=True)
        self.assertAlmostEqual(plain.final_equity, flagged.final_equity, places=6)
        self.assertAlmostEqual(plain.total_fees, flagged.total_fees, places=6)
        self.assertEqual(len(plain.trades), len(flagged.trades))
        self.assertNotIn("positionIntents", flagged.data_quality)


class CancelTests(unittest.TestCase):
    def test_a_cancel_without_a_pending_order_is_recorded_and_does_nothing(self):
        bars = walk(80)
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "long", 50.0, "wedge_pop")
        intents[11] = PositionIntent("cancel", "long", None, "pivot_failure")
        intents[40] = PositionIntent("exit", "long", None, "wedge_drop")
        result = run_backtest(bars, config(), position_intents=intents, interval="1h")
        report = summary(result)
        self.assertEqual(report["cancels"], 0)
        cancelled = [item for item in report["records"] if item["action"] == "cancel"]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["status"], "ignored")
        self.assertEqual(cancelled[0]["note"], "没有可撤销的挂单")
        self.assertEqual(len(result.trades), 1, "撤单本身不产生成交")

    def test_a_cancel_drops_the_remainder_of_a_participation_capped_entry(self):
        # The bar must be liquid enough not to be a thin session (20,000 notional) but
        # small enough that a 5% participation cap cannot absorb a 50-unit order: the
        # cap leaves a remainder, and the cancel is what has to drop it.
        bars = walk(80, volume=200.0)
        intents = hold(len(bars))
        intents[10] = PositionIntent("open", "long", 50.0, "wedge_pop")
        intents[11] = PositionIntent("cancel", "long", None, "pivot_failure")
        intents[60] = PositionIntent("exit", "long", None, "wedge_drop")
        result = run_backtest(
            bars, config(partial_fill="cap", max_participation=0.05, min_order_notional=5.0),
            position_intents=intents, interval="1h",
        )
        report = summary(result)
        self.assertEqual(report["cancels"], 1, "有挂单余量时撤单必须真的撤掉")
        self.assertEqual(report["records"][1]["status"], "cancelled")
        cancelled_orders = [order for order in result.orders if order.status == "cancelled"]
        self.assertTrue(cancelled_orders)
        self.assertGreater(cancelled_orders[0].unfilled_quantity, 0.0)


class PartialCloseUnitTests(unittest.TestCase):
    def test_the_closed_share_carries_its_part_of_the_entry_fee_and_funding(self):
        from quantdesk.backtest.engine import _Position

        position = _Position(
            direction=1, entry_time=START, entry_price=100.0, quantity=10.0,
            notional=1_000.0, entry_fee=6.0, margin=1_000.0, liq_price=None,
        )
        position.funding_paid = 2.0
        equity, trade = _close_partial(
            10_000.0, position, walk(1)[0], 110.0, 1, config(), "signal", quantity=4.0,
        )
        self.assertAlmostEqual(position.quantity, 6.0, places=8)
        self.assertAlmostEqual(position.entry_fee, 3.6, places=8)
        self.assertAlmostEqual(position.funding_paid, 1.2, places=8)
        self.assertAlmostEqual(position.notional, 600.0, places=8)
        self.assertAlmostEqual(position.entry_price, 100.0, places=8, msg="分批平仓不改均价")
        self.assertAlmostEqual(trade.quantity, 4.0, places=8)
        self.assertAlmostEqual(trade.gross_pnl, 40.0, places=6)
        self.assertAlmostEqual(trade.fees, 4.0 * 110.0 * 0.0006 + 2.4, places=6)
        # equity gains the gross minus the exit fee; the closed share's entry fee and
        # funding were already charged when they were incurred.
        self.assertAlmostEqual(equity, 10_000.0 + 40.0 - 4.0 * 110.0 * 0.0006, places=6)

    def test_a_partial_close_of_everything_empties_the_position(self):
        from quantdesk.backtest.engine import _Position

        position = _Position(
            direction=1, entry_time=START, entry_price=100.0, quantity=3.0,
            notional=300.0, entry_fee=1.8, margin=300.0, liq_price=None,
        )
        _close_partial(10_000.0, position, walk(1)[0], 101.0, 1, config(), "signal",
                       quantity=3.0)
        self.assertAlmostEqual(position.quantity, 0.0, places=8)

class CpaIntentWiringTests(unittest.TestCase):
    """The CPA side: parameters, notices, and the intents themselves."""

    def test_the_position_defaults_resolve_before_the_engine_reads_them(self):
        """The bug this locks: reading the raw request missed every CPA default.

        A study asking only for `positionModel=intent` must still execute with the
        advertised risk budget; otherwise the catalogue publishes 2% and the run uses
        none.
        """
        from quantdesk.strategy.cpa import resolve_parameters

        resolved = resolve_parameters({"positionModel": "intent"}, asset_class="crypto",
                                      interval="1h")
        self.assertEqual(resolved["maxPortfolioRiskPct"], 2.0)
        self.assertEqual(resolved["initialExposurePct"], 50.0)
        self.assertEqual(resolved["addExposurePct"], 25.0)
        self.assertTrue(resolved["pivotFailureExit"])
        # 同一份预算默认也约束持仓的浮动风险。
        self.assertTrue(resolved["enforceOpenRisk"])

    def test_the_parameter_version_moved_with_the_new_parameters(self):
        from quantdesk.strategy import cpa

        self.assertEqual(cpa.PARAMETER_VERSION, "cpa-qd/1.3.0")
        keys = {spec["key"] for spec in cpa.PARAMETER_SPECS}
        for key in ("positionModel", "initialExposurePct", "addExposurePct",
                    "reduceExposurePct", "maxPortfolioRiskPct", "enforceOpenRisk",
                    "pivotFailureExit"):
            self.assertIn(key, keys)
        for spec in cpa.PARAMETER_SPECS:
            self.assertTrue(spec["label"] and spec["help"], spec["key"])
            self.assertIn("unit", spec)
        enforced = next(item for item in cpa.PARAMETER_SPECS
                        if item["key"] == "enforceOpenRisk")
        self.assertEqual(enforced["type"], "boolean")
        self.assertIs(enforced["default"], True)

    def test_the_notice_follows_the_mode_instead_of_covering_both_with_one_sentence(self):
        from quantdesk.strategy.cpa import position_notice

        single = position_notice({"positionModel": "single"})
        intent = position_notice({"positionModel": "intent"})
        self.assertIn("简化版本", single)
        self.assertNotIn("已启用结构化仓位意图模型", single)
        self.assertIn("分批建仓", intent)
        self.assertIn("不接实盘", intent)
        self.assertNotEqual(single, intent)

    def test_only_the_intent_model_is_selected_by_the_switch(self):
        from quantdesk.strategy.registry import wants_intent_model

        self.assertFalse(wants_intent_model("cpa_cycle", {}))
        self.assertFalse(wants_intent_model("cpa_cycle", {"positionModel": "single"}))
        self.assertTrue(wants_intent_model("cpa_cycle", {"positionModel": "intent"}))
        self.assertFalse(wants_intent_model("ma_cross", {"positionModel": "intent"}))

    def test_intents_for_carries_a_stop_and_a_reason_on_every_entry(self):
        from quantdesk.strategy import cpa

        bars = walk(200)
        parameters = cpa.resolve_parameters({"positionModel": "intent"}, asset_class="stock",
                                           interval="1h")
        intents = cpa.intents_for(bars, parameters)
        entries = [item for item in intents if item is not None
                   and item.action in ("open", "increase")]
        for entry in entries:
            self.assertIsNotNone(entry.stop_price, "每个入场意图都必须带结构失效价")
            self.assertTrue(entry.reason.strip(), "每个入场意图都必须写明理由")
            self.assertIsNotNone(entry.target_exposure_pct)
        # The same phase series the event path uses, so the two models cannot disagree
        # about what happened.
        self.assertEqual(len(intents), len(bars))

    def test_the_intent_model_never_asks_for_more_than_full_exposure(self):
        from quantdesk.strategy import cpa

        bars = walk(240)
        parameters = cpa.resolve_parameters(
            {"positionModel": "intent", "initialExposurePct": 60, "addExposurePct": 50},
            asset_class="stock", interval="1h")
        intents = cpa.intents_for(bars, parameters)
        for item in intents:
            if item is not None and item.target_exposure_pct is not None:
                self.assertLessEqual(item.target_exposure_pct, 100.0)

    def test_the_cpa_orders_artifact_appears_when_the_intent_model_ran(self):
        from quantdesk.backtest_runs import artifacts_of

        payload = {"symbol": "BTCUSDT", "cpaOrders": {"orders": [], "records": []}}
        names = [name for name, _media, _body in artifacts_of(payload)]
        self.assertIn("cpa-orders", names)

    def test_the_artifact_is_absent_for_a_run_that_did_not_use_intents(self):
        from quantdesk.backtest_runs import artifacts_of

        names = [name for name, _media, _body in artifacts_of({"symbol": "BTCUSDT"})]
        self.assertNotIn("cpa-orders", names)


if __name__ == "__main__":
    unittest.main()
