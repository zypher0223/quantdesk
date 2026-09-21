"""Paper trading, the append-only journal, and the trading endpoints."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quantdesk.api.server import app
from quantdesk.datahub.db import Database
from quantdesk.paper import PaperConfig, PaperEngine, PaperError


def make_engine(tmp: str, **overrides) -> tuple[Database, PaperEngine]:
    db = Database(Path(tmp) / "quantdesk.db")
    engine = PaperEngine(db, PaperConfig(initial_cash=overrides.pop("initial_cash", 10_000.0), **overrides))
    engine.reset()
    return db, engine


class PaperEngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_open_uses_slippage_step_and_reports_liquidation_distance(self):
        _, engine = make_engine(self._tmp.name, taker_fee_bps=10, slippage_bps=5)
        view = engine.open_position(
            "AMD", "long", notional=5_000, leverage=5, mark_price=500.0, tick_size=0.01, qty_step=0.01
        )
        # Entry pays slippage, so it fills above the mark.
        self.assertAlmostEqual(view.entry_price, 500.25, places=2)
        self.assertEqual(view.qty, 10.0)
        self.assertAlmostEqual(view.margin, 1_000.5, places=1)
        self.assertLess(view.liq_price, view.entry_price)
        self.assertIsNotNone(view.distance_to_liq_pct)

    def test_account_values_positions_at_the_mark_price(self):
        _, engine = make_engine(self._tmp.name, taker_fee_bps=10, slippage_bps=0)
        engine.open_position("AMD", "long", notional=1_000, leverage=1, mark_price=500.0)
        account = engine.account({"AMDSTOCKUSDT": 550.0})
        self.assertAlmostEqual(account.unrealized_pnl, 100.0, places=2)
        self.assertAlmostEqual(account.equity, account.cash + 100.0, places=2)
        self.assertAlmostEqual(account.margin_used, 1_000.0, places=2)
        self.assertAlmostEqual(account.free_margin, account.equity - account.margin_used, places=2)
        self.assertIsNotNone(account.positions[0].margin_ratio)

    def test_missing_mark_price_is_declared_not_invented(self):
        _, engine = make_engine(self._tmp.name)
        engine.open_position("AMD", "long", notional=1_000, mark_price=500.0)
        account = engine.account({})  # venue unreachable
        self.assertIsNone(account.positions[0].unrealized_pnl)
        self.assertIsNone(account.positions[0].margin_ratio)
        self.assertTrue(any("暂无标记价" in warning for warning in account.warnings))

    def test_insufficient_margin_is_rejected_with_numbers(self):
        _, engine = make_engine(self._tmp.name, initial_cash=1_000)
        with self.assertRaises(PaperError) as caught:
            engine.open_position("AMD", "long", notional=50_000, leverage=5, mark_price=500.0)
        message = str(caught.exception)
        self.assertIn("保证金不足", message)
        self.assertIn("可用", message)

    def test_leverage_bounds_and_mutually_exclusive_sizing(self):
        _, engine = make_engine(self._tmp.name)
        with self.assertRaises(PaperError):
            engine.open_position("AMD", "long", notional=1_000, leverage=500, mark_price=500.0)
        with self.assertRaises(PaperError):
            engine.open_position("AMD", "long", mark_price=500.0)
        with self.assertRaises(PaperError):
            engine.open_position("AMD", "long", notional=1_000, qty=2, mark_price=500.0)
        with self.assertRaises(PaperError):
            engine.open_position("AMD", "sideways", notional=1_000, mark_price=500.0)

    def test_live_symbol_leverage_limit_is_enforced(self):
        _, engine = make_engine(self._tmp.name)
        with self.assertRaises(PaperError) as caught:
            engine.open_position(
                "AMD", "long", notional=1_000, leverage=6, max_leverage=5, mark_price=500.0
            )
        self.assertIn("最大杠杆为 5x", str(caught.exception))

    def test_protective_levels_are_validated_and_returned(self):
        _, engine = make_engine(self._tmp.name, slippage_bps=0)
        view = engine.open_position(
            "BTC", "long", notional=1_000, mark_price=100,
            stop_loss=90, take_profit_1=110, take_profit_2=120,
        )
        self.assertEqual(
            [order["type"] for order in view.protective_orders],
            ["stop_loss", "take_profit_1", "take_profit_2"],
        )
        self.assertEqual(view.protective_orders[1]["close_fraction"], 0.5)
        # 已有 BTC 持仓时再开 ETH：显式给出 BTC 的标记价，测试才不会去敲交易所，
        # 校验才会走到“止损方向不对”这一步，而不是先撞上“缺标记价”。
        held = {"BTCUSDT": 100.0}
        with self.assertRaises(PaperError) as long_stop:
            engine.open_position("ETH", "long", notional=1_000, mark_price=100,
                                 mark_prices=held, stop_loss=101)
        self.assertIn("止损", str(long_stop.exception))
        with self.assertRaises(PaperError) as short_target:
            engine.open_position("ETH", "short", notional=1_000, mark_price=100,
                                 mark_prices=held, take_profit_1=105)
        self.assertIn("止盈", str(short_target.exception))

    def test_two_stage_take_profit_reduces_then_closes(self):
        db, engine = make_engine(self._tmp.name, taker_fee_bps=0, slippage_bps=0)
        view = engine.open_position(
            "BTC", "long", notional=1_000, mark_price=100,
            stop_loss=90, take_profit_1=110, take_profit_2=120,
        )
        first = engine.reconcile({"BTCUSDT": 111}, now_ms=2_000_000_000_000)
        self.assertEqual(first[0]["type"], "take_profit_1")
        self.assertEqual(first[0]["closed_qty"], 5)
        self.assertEqual(first[0]["remaining_qty"], 5)
        remaining = engine.account({"BTCUSDT": 111}).positions[0]
        self.assertEqual(remaining.qty, 5)
        self.assertEqual([order["type"] for order in remaining.protective_orders], ["stop_loss", "take_profit_2"])

        second = engine.reconcile({"BTCUSDT": 121}, now_ms=2_000_000_001_000)
        self.assertEqual(second[0]["type"], "take_profit_2")
        self.assertEqual(engine.account({}).positions, [])
        self.assertEqual([row["exit_reason"] for row in db.journal_entries()], ["take_profit_2", "take_profit_1"])

    def test_stop_loss_closes_the_full_position(self):
        db, engine = make_engine(self._tmp.name, taker_fee_bps=0, slippage_bps=0)
        view = engine.open_position("ETH", "short", notional=1_000, mark_price=100, stop_loss=110)
        events = engine.reconcile({"ETHUSDT": 111}, now_ms=2_000_000_000_000)
        self.assertEqual(events[0]["type"], "stop_loss")
        self.assertEqual(events[0]["closed_qty"], view.qty)
        self.assertEqual(db.journal_entries()[0]["exit_reason"], "stop_loss")

    def test_liquidation_has_priority_over_stop_loss(self):
        db, engine = make_engine(self._tmp.name, taker_fee_bps=0, slippage_bps=0)
        view = engine.open_position("BTC", "long", notional=1_000, leverage=10, mark_price=100, stop_loss=95)
        events = engine.reconcile({"BTCUSDT": float(view.liq_price) - 1}, now_ms=2_000_000_000_000)
        self.assertEqual(events[0]["type"], "liquidation")
        self.assertEqual(db.journal_entries()[0]["exit_reason"], "liquidation")

    def test_automatic_funding_is_idempotent(self):
        db, engine = make_engine(self._tmp.name, taker_fee_bps=0, slippage_bps=0)
        view = engine.open_position("BTC", "long", notional=1_000, mark_price=100)
        opened = engine.open_positions()[0]["updated_ts"]
        history = {"BTCUSDT": [{"ts": opened + 1, "rate": 0.001}]}
        first = engine.reconcile({"BTCUSDT": 100}, funding_by_symbol=history, now_ms=opened + 2)
        second = engine.reconcile({"BTCUSDT": 100}, funding_by_symbol=history, now_ms=opened + 3)
        self.assertEqual(len([event for event in first if event["type"] == "funding"]), 1)
        self.assertEqual(second, [])
        self.assertAlmostEqual(engine.account({"BTCUSDT": 100}).funding_paid, 1.0, places=6)
        self.assertEqual(len(db.query("SELECT * FROM paper_funding_settlements")), 1)

    def test_close_moves_cash_by_net_pnl_and_books_it_once(self):
        db, engine = make_engine(self._tmp.name, taker_fee_bps=10, slippage_bps=0)
        view = engine.open_position("AMD", "long", notional=1_000, leverage=1, mark_price=500.0)
        result = engine.close_position(view.id, mark_price=550.0)
        account = engine.account({})
        self.assertAlmostEqual(result["gross_pnl"], 100.0, places=2)
        self.assertAlmostEqual(account.cash, account.initial_cash + result["net_pnl"], places=4)
        self.assertAlmostEqual(account.realized_pnl, result["net_pnl"], places=4)
        self.assertAlmostEqual(account.fees_paid, result["fees"], places=4)
        self.assertEqual(len(account.positions), 0)
        self.assertEqual(len(db.journal_entries()), 1)
        # Closing twice must not double-book.
        with self.assertRaises(PaperError):
            engine.close_position(view.id, mark_price=560.0)

    def test_round_trip_fees_reconcile_cash_account_and_journal(self):
        db, engine = make_engine(self._tmp.name, taker_fee_bps=10, slippage_bps=0)
        view = engine.open_position("AMD", "long", notional=500, mark_price=100.0)
        result = engine.close_position(view.id, mark_price=100.0)
        account = engine.account({})
        self.assertAlmostEqual(result["fees"], 1.0, places=6)
        self.assertAlmostEqual(result["net_pnl"], -1.0, places=6)
        self.assertAlmostEqual(account.cash, 9999.0, places=6)
        self.assertAlmostEqual(account.fees_paid, 1.0, places=6)
        self.assertAlmostEqual(db.journal_entries()[0]["net_pnl"], -1.0, places=6)

    def test_new_position_uses_every_open_positions_mark_price(self):
        _, engine = make_engine(self._tmp.name, initial_cash=1_000, taker_fee_bps=0, slippage_bps=0)
        engine.open_position("BTC", "long", notional=800, mark_price=100.0)
        with self.assertRaises(PaperError) as caught:
            engine.open_position(
                "ETH", "long", notional=150, mark_price=100.0,
                mark_prices={"BTCUSDT": 50.0, "ETHUSDT": 100.0},
            )
        self.assertIn("保证金不足", str(caught.exception))

    def test_missing_existing_mark_blocks_risk_increase(self):
        _, engine = make_engine(self._tmp.name, taker_fee_bps=0, slippage_bps=0)
        engine.open_position("BTC", "long", notional=500, mark_price=100.0)
        with self.assertRaises(PaperError) as caught:
            engine.open_position("ETH", "long", notional=100, mark_price=100.0, mark_prices={"ETHUSDT": 100.0})
        self.assertIn("暂无标记价", str(caught.exception))

    def test_the_market_service_mark_is_used_before_any_rest_read(self):
        # One market state for the whole process: paper risk must read the same
        # mark the chart shows instead of opening its own venue connection.
        from quantdesk.datahub import market_service as ms

        _, engine = make_engine(self._tmp.name, taker_fee_bps=0, slippage_bps=0)
        engine.open_position("BTC", "long", notional=1_000, mark_price=100.0)

        service = ms.get_market_service(engine.db.path.parent)
        try:
            service._feeds["BTCUSDT"].snapshot = {
                "venue": "bybit",
                "symbol": "BTCUSDT",
                "last_price": 150.0,
                "mark_price": 150.0,
                "received_ts": int(time.time() * 1000),
                "source": "sqlite",
            }
            prices = engine.mark_prices(["BTCUSDT"])
        finally:
            ms.reset_market_service()

        self.assertEqual(prices, {"BTCUSDT": 150.0})
        self.assertAlmostEqual(engine.account(prices).unrealized_pnl, 500.0, places=2)

    def test_a_service_without_a_mark_falls_back_to_the_venue_read(self):
        from quantdesk.datahub import market_service as ms

        _, engine = make_engine(self._tmp.name, taker_fee_bps=0, slippage_bps=0)

        class FakeClient:
            def __init__(self):
                self.calls: list[str] = []

            def ticker(self, category, symbol):
                self.calls.append(symbol)
                return {"markPrice": "77.5"}

            def close(self):
                return None

        client = FakeClient()
        service = ms.get_market_service(engine.db.path.parent)
        try:
            self.assertIsNone(service.mark_price("BTCUSDT"))
            prices = engine.mark_prices(["BTCUSDT"], client=client)
        finally:
            ms.reset_market_service()

        self.assertEqual(prices, {"BTCUSDT": 77.5})
        # Nothing was held in memory, so the venue read is the only source left.
        self.assertEqual(client.calls, ["BTCUSDT"])

    def test_the_venue_ladder_decides_margin_and_the_leverage_cap(self):
        # The paper book must margin a position in the rung the venue would use,
        # not in one constant rate shared by every contract.
        from quantdesk.risk import RiskBook, RiskProfile, tier_rows_for_db

        db, engine = make_engine(self._tmp.name, taker_fee_bps=0, slippage_bps=0)
        ladder = RiskProfile.from_rows(
            "AMDSTOCKUSDT",
            [
                {"id": 1, "riskLimitValue": "10000", "maintenanceMargin": "0.0125", "maxLeverage": "50"},
                {"id": 2, "riskLimitValue": "100000", "maintenanceMargin": "0.02", "maxLeverage": "20"},
            ],
        )
        db.upsert_risk_tiers("bybit", "AMDSTOCKUSDT", tier_rows_for_db(ladder), synced_at=ladder.synced_at or 1)
        engine = PaperEngine(db, engine.config, risk_book=RiskBook(db))

        view = engine.open_position("AMD", "long", notional=5_000, leverage=20, mark_price=500.0)
        self.assertEqual(view.risk_tier_id, 1)
        self.assertAlmostEqual(view.maintenance_margin_rate or 0.0, 0.0125, places=6)
        self.assertEqual(view.max_leverage, 50)
        self.assertIsNotNone(view.liq_price)
        # The fixed 0.5% the first prototype used would have placed it further away.
        loose = 500.0 * (1 - 1 / 20) / (1 - 0.005)
        self.assertGreater(view.liq_price or 0.0, loose)

    def test_a_position_beyond_the_rung_cap_is_refused(self):
        from quantdesk.risk import RiskBook, RiskProfile, tier_rows_for_db

        db, engine = make_engine(self._tmp.name, taker_fee_bps=0, slippage_bps=0)
        ladder = RiskProfile.from_rows(
            "AMDSTOCKUSDT",
            [{"id": 1, "riskLimitValue": "10000", "maintenanceMargin": "0.0125", "maxLeverage": "20"}],
        )
        db.upsert_risk_tiers("bybit", "AMDSTOCKUSDT", tier_rows_for_db(ladder), synced_at=ladder.synced_at or 1)
        engine = PaperEngine(db, engine.config, risk_book=RiskBook(db))

        with self.assertRaises(PaperError) as caught:
            engine.open_position("AMD", "long", notional=5_000, leverage=50, mark_price=500.0)
        self.assertIn("最高杠杆", str(caught.exception))

    def test_without_a_ladder_the_constant_rate_is_disclosed(self):
        # A missing ladder must not silently look like a tiered answer.
        from quantdesk.risk import RiskBook

        db, engine = make_engine(self._tmp.name, taker_fee_bps=0, slippage_bps=0)
        engine = PaperEngine(db, engine.config, risk_book=RiskBook(db))
        view = engine.open_position("AMD", "long", notional=5_000, leverage=10, mark_price=500.0)
        self.assertIsNone(view.risk_tier_id)
        self.assertTrue(view.risk_warnings, "the fallback rate must be reported on the position")

    def test_a_stale_mark_is_not_used_to_price_a_position(self):
        # A quote the market service itself calls stale must be treated as absent:
        # sizing or closing a position from a frozen price would write that price
        # into the append-only journal as if it were a real fill.
        from quantdesk.datahub import market_service as ms

        _, engine = make_engine(self._tmp.name, taker_fee_bps=0, slippage_bps=0)

        class FakeClient:
            def __init__(self):
                self.calls: list[str] = []

            def ticker(self, category, symbol):
                self.calls.append(symbol)
                return {"markPrice": "88.0"}

            def close(self):
                return None

        service = ms.get_market_service(engine.db.path.parent)
        client = FakeClient()
        try:
            service._feeds["BTCUSDT"].snapshot = {
                "venue": "bybit",
                "symbol": "BTCUSDT",
                "last_price": 150.0,
                "mark_price": 150.0,
                "received_ts": int(time.time() * 1000) - service.stale_after_ms - 60_000,
                "source": "websocket",
            }
            self.assertTrue(service.stale("BTCUSDT"), "the fixture must actually be stale")
            prices = engine.mark_prices(["BTCUSDT"], client=client)
        finally:
            ms.reset_market_service()

        self.assertEqual(prices, {"BTCUSDT": 88.0}, "a stale mark must fall through to the venue read")
        self.assertEqual(client.calls, ["BTCUSDT"])

    def test_a_failing_market_service_never_breaks_paper_risk(self):
        from quantdesk.datahub import market_service as ms

        _, engine = make_engine(self._tmp.name, taker_fee_bps=0, slippage_bps=0)

        class FakeClient:
            def ticker(self, category, symbol):
                return {"lastPrice": "12.5"}

            def close(self):
                return None

        service = ms.get_market_service(engine.db.path.parent)
        try:
            with patch.object(service, "mark_price", side_effect=RuntimeError("service exploded")):
                prices = engine.mark_prices(["BTCUSDT"], client=FakeClient())
        finally:
            ms.reset_market_service()

        self.assertEqual(prices, {"BTCUSDT": 12.5})

    def test_funding_credits_a_short_and_costs_a_long(self):
        _, engine = make_engine(self._tmp.name)
        # notional 500 at mark 500 is exactly 1 unit, so a 0.1% rate is 0.5.
        long_view = engine.open_position("AMD", "long", notional=500, mark_price=500.0)
        self.assertEqual(long_view.qty, 1.0)
        cash_before = engine.account({}).cash
        engine.settle_funding(long_view.id, 0.001, mark_price=500.0)
        after_long = engine.account({})
        self.assertLess(after_long.cash, cash_before)
        self.assertAlmostEqual(after_long.funding_paid, 0.5, places=6)
        self.assertEqual(after_long.funding_received, 0)
        self.assertAlmostEqual(after_long.funding_net, 0.5, places=6)

        short_view = engine.open_position("AMD", "short", notional=500, mark_price=500.0)
        cash_before = engine.account({}).cash
        engine.settle_funding(short_view.id, 0.001, mark_price=500.0)
        after_short = engine.account({})
        self.assertGreater(after_short.cash, cash_before)
        # Paid and received are reported separately: a long paying 0.5 and a
        # short receiving 0.5 is not "zero funding cost".
        self.assertAlmostEqual(after_short.funding_paid, 0.5, places=6)
        self.assertAlmostEqual(after_short.funding_received, 0.5, places=6)
        self.assertAlmostEqual(after_short.funding_net, 0.0, places=6)

    def test_settled_funding_is_included_once_in_lifecycle_pnl(self):
        db, engine = make_engine(self._tmp.name, taker_fee_bps=0, slippage_bps=0)
        view = engine.open_position("AMD", "long", notional=500, mark_price=500.0)
        engine.settle_funding(view.id, 0.001, mark_price=500.0)
        result = engine.close_position(view.id, mark_price=500.0)
        account = engine.account({})
        self.assertAlmostEqual(result["funding_paid"], 0.5, places=6)
        self.assertAlmostEqual(result["net_pnl"], -0.5, places=6)
        self.assertAlmostEqual(account.cash, account.initial_cash - 0.5, places=6)
        self.assertAlmostEqual(account.realized_pnl, -0.5, places=6)
        self.assertAlmostEqual(db.journal_entries()[0]["funding_paid"], 0.5, places=6)

    def test_stale_account_snapshot_cannot_open_a_second_position(self):
        path = Path(self._tmp.name) / "quantdesk.db"
        db1 = Database(path)
        db2 = Database(path)
        first = db1.open_paper_position(
            venue="bybit", symbol="BTCUSDT", side="long", qty=1, avg_price=100,
            leverage=1, liq_price=None, entry_fee=1, rationale="", updated_ts=1,
            expected_cash=10_000, expected_open_ids=[],
        )
        self.assertIsNotNone(first)
        stale = db2.open_paper_position(
            venue="bybit", symbol="ETHUSDT", side="long", qty=1, avg_price=100,
            leverage=1, liq_price=None, entry_fee=1, rationale="", updated_ts=2,
            expected_cash=10_000, expected_open_ids=[],
        )
        self.assertIsNone(stale)

    def test_note_is_editable_and_lands_on_the_journal_entry(self):
        db, engine = make_engine(self._tmp.name)
        view = engine.open_position("AMD", "long", notional=1_000, mark_price=500.0, rationale="首版理由")
        engine.set_note(view.id, "复盘后修正的理由")
        engine.close_position(view.id, mark_price=510.0)
        entry = db.journal_entries()[0]
        self.assertEqual(entry["rationale"], "复盘后修正的理由")
        self.assertTrue(entry["entry_hash"])


class FundingLoadTests(unittest.TestCase):
    """load_funding is on the hot path for research and the backtest; keep its
    signature honest so a caller passing limit is not silently broken."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def test_existing_database_gets_paper_account_columns(self):
        path = Path(self._tmp.name) / "legacy.db"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE positions (id INTEGER PRIMARY KEY, venue TEXT, symbol TEXT, side TEXT, qty REAL, "
            "avg_price REAL, leverage REAL, liq_price REAL, updated_ts INTEGER, closed_ts INTEGER)"
        )
        connection.commit()
        connection.close()
        migrated = Database(path)
        columns = {row["name"] for row in migrated.query("PRAGMA table_info(positions)")}
        self.assertIn("entry_fee", columns)
        self.assertIn("funding_paid", columns)

    def test_limit_keeps_the_newest_rows_oldest_first(self):
        rows = [{"ts": 1_000 * index, "rate": 0.001 * index} for index in range(1, 11)]
        self.db.upsert_funding("bybit", "AAPLUSDT", rows)
        self.assertEqual(len(self.db.load_funding("bybit", "AAPLUSDT")), 10)

        limited = self.db.load_funding("bybit", "AAPLUSDT", limit=3)
        self.assertEqual([row["ts"] for row in limited], [8_000, 9_000, 10_000], "newest three, oldest first")

    def test_window_and_limit_combine(self):
        rows = [{"ts": 1_000 * index, "rate": 0.0} for index in range(1, 11)]
        self.db.upsert_funding("bybit", "AAPLUSDT", rows)
        windowed = self.db.load_funding("bybit", "AAPLUSDT", start_ts=3_000, end_ts=8_000, limit=2)
        self.assertEqual([row["ts"] for row in windowed], [7_000, 8_000])

    def test_absent_symbol_returns_empty(self):
        self.assertEqual(self.db.load_funding("bybit", "MSFTUSDT", limit=5), [])


class JournalImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db, engine = make_engine(self._tmp.name)
        view = engine.open_position("AMD", "long", notional=1_000, mark_price=500.0)
        engine.close_position(view.id, mark_price=510.0)

    def test_updates_are_rejected_by_the_database(self):
        with self.assertRaises(Exception) as caught:
            self.db.execute("UPDATE journal SET net_pnl = 999999 WHERE id = 1")
        self.assertIn("append-only", str(caught.exception))

    def test_deletes_are_rejected_by_the_database(self):
        with self.assertRaises(Exception) as caught:
            self.db.execute("DELETE FROM journal WHERE id = 1")
        self.assertIn("append-only", str(caught.exception))

    def test_hash_verifies_and_detects_tampering(self):
        self.assertTrue(self.db.journal_integrity()["intact"])
        # Rewrite the row directly, bypassing the trigger, to simulate an edit
        # made outside this application.
        with self.db._lock:  # noqa: SLF001 - deliberate tamper simulation
            self.db._conn.execute("DROP TRIGGER journal_no_update")  # noqa: SLF001
            self.db._conn.execute("UPDATE journal SET net_pnl = 1 WHERE id = 1")  # noqa: SLF001
            self.db._conn.commit()
        report = self.db.journal_integrity()
        self.assertFalse(report["intact"])
        self.assertEqual(report["tampered"], [1])


class TradingEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"QUANTDESK_HOME": self._tmp.name}, clear=False)
        self._env.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self._env.stop()
        self._tmp.cleanup()

    @staticmethod
    def candles(count: int = 300) -> list[dict]:
        import math

        out = []
        price = 100.0
        for index in range(count):
            price *= 1 + math.sin(index / 17) * 0.004
            out.append(
                {
                    "time": 1_700_000_000_000 + index * 3_600_000,
                    "open": price,
                    "high": price * 1.002,
                    "low": price * 0.998,
                    "close": price,
                    "volume": 5_000,
                }
            )
        return out

    async def test_backtest_runs_on_supplied_candles(self):
        response = await self.client.post(
            "/api/backtest",
            json={"symbol": "AAPL", "timeframe": "1h", "initialCapital": 10_000, "allocationPct": 50, "candles": self.candles()},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["data_quality"]["source"], "upload")
        self.assertEqual(body["data_quality"]["venueSymbol"], "AAPLUSDT")
        self.assertTrue(body["assumptions"])
        self.assertIn("net_return_pct", body)

    async def test_strategy_catalog_and_non_ma_backtest(self):
        catalog = await self.client.get("/api/strategies")
        self.assertEqual(catalog.status_code, 200)
        ids = {item["id"] for item in catalog.json()["strategies"]}
        self.assertTrue({"ma_cross", "channel_breakout", "rsi_reversal"}.issubset(ids))

        response = await self.client.post(
            "/api/backtest",
            json={
                "symbol": "AAPL",
                "timeframe": "1h",
                "strategyId": "channel_breakout",
                "strategyParams": {"lookback": 12},
                "candles": self.candles(),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["config"]["strategy_id"], "channel_breakout")
        self.assertEqual(response.json()["config"]["strategy_params"], {"lookback": 12})

    async def test_legacy_ma_fields_feed_registered_strategy(self):
        response = await self.client.post(
            "/api/backtest",
            json={"symbol": "AAPL", "fastPeriod": 5, "slowPeriod": 17, "candles": self.candles()},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["config"]["strategy_params"], {"fastPeriod": 5, "slowPeriod": 17})

    async def test_backtest_rejects_bad_requests(self):
        for payload, status in (
            ({"symbol": "DOGEUSDT", "candles": self.candles()}, 422),
            ({"symbol": "AAPL", "candles": self.candles(10)}, 422),
            ({"symbol": "AAPL", "fastPeriod": 21, "slowPeriod": 9, "candles": self.candles()}, 422),
            ({"symbol": "AAPL", "timeframe": "5m", "candles": self.candles()}, 422),
        ):
            response = await self.client.post("/api/backtest", json=payload)
            self.assertEqual(response.status_code, status, payload)

    async def test_account_starts_flat_and_resets(self):
        response = await self.client.get("/api/paper/account")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["positions"], [])
        self.assertEqual(body["unrealized_pnl"], 0)
        self.assertEqual((await self.client.post("/api/paper/reset")).status_code, 200)

    async def test_reset_refuses_while_a_position_is_open(self):
        from quantdesk.datahub.db import Database
        from quantdesk.config.settings import quantdesk_home

        db = Database(quantdesk_home() / "quantdesk.db")
        engine = PaperEngine(db, PaperConfig(initial_cash=10_000))
        engine.open_position("AMD", "long", notional=1_000, mark_price=500.0)
        response = await self.client.post("/api/paper/reset")
        self.assertEqual(response.status_code, 409)
        self.assertIn("请先平仓", response.json()["detail"])

    async def test_journal_is_empty_then_reports_integrity(self):
        response = await self.client.get("/api/journal")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["immutable"])
        self.assertTrue(body["integrity"]["intact"])

    async def test_journal_exports_json_and_csv(self):
        from quantdesk.config.settings import quantdesk_home
        from quantdesk.datahub.db import Database

        db = Database(quantdesk_home() / "quantdesk.db")
        engine = PaperEngine(db, PaperConfig(initial_cash=10_000))
        view = engine.open_position("AMD", "long", notional=1_000, mark_price=500.0, rationale="导出用例")
        engine.close_position(view.id, mark_price=505.0)

        csv_response = await self.client.get("/api/journal/export", params={"format": "csv"})
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("attachment", csv_response.headers["content-disposition"])
        self.assertIn("entry_hash", csv_response.text.splitlines()[0])
        self.assertIn("导出用例", csv_response.text)

        json_response = await self.client.get("/api/journal/export", params={"format": "json"})
        body = json.loads(json_response.text)
        self.assertEqual(len(body["entries"]), 1)
        self.assertTrue(body["integrity"]["intact"])

    async def test_journal_filters_by_symbol(self):
        from quantdesk.config.settings import quantdesk_home
        from quantdesk.datahub.db import Database

        db = Database(quantdesk_home() / "quantdesk.db")
        engine = PaperEngine(db, PaperConfig(initial_cash=10_000))
        view = engine.open_position("AMD", "long", notional=1_000, mark_price=500.0)
        engine.close_position(view.id, mark_price=505.0)

        self.assertEqual(len((await self.client.get("/api/journal", params={"symbol": "AMD"})).json()["entries"]), 1)
        self.assertEqual(len((await self.client.get("/api/journal", params={"symbol": "AAPL"})).json()["entries"]), 0)
        self.assertEqual((await self.client.get("/api/journal", params={"symbol": "DOGEUSDT"})).status_code, 422)


if __name__ == "__main__":
    unittest.main()
