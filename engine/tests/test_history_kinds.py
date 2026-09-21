"""Every historical data family, its own walk, its own snapshot, its own truth.

The rules under test: a series that the venue does not publish is recorded as
unsupported with a reason rather than filled with zeros, each family keeps its own
progress, and mark prices never end up in the trade-candle table.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quantdesk.datahub.db import Database
from quantdesk.datahub.history import (
    DATA_KINDS,
    FUNDING,
    KIND_LABELS,
    MARK_CANDLE,
    OPEN_INTEREST,
    RISK_LIMIT,
    TRADE_CANDLE,
    HistoryCollector,
    series_version,
)

HOUR = 3_600_000
NOW = 1_700_000_000_000 - (1_700_000_000_000 % HOUR)

META_SUPPORTED = {
    "symbol": "BTCUSDT", "launchTime": "1600000000000", "contractType": "LinearPerpetual",
    "status": "Trading", "fundingInterval": 480,
    "priceFilter": {"tickSize": "0.1"},
    "lotSizeFilter": {"qtyStep": "0.001", "minNotionalValue": "5"},
}
META_NO_FUNDING = {
    "symbol": "SPCXUSDT", "launchTime": "1750000000000", "contractType": "LinearPerpetual",
    "status": "Trading",
    "priceFilter": {"tickSize": "0.01"},
    "lotSizeFilter": {"qtyStep": "0.1", "minNotionalValue": "5"},
}


class StubClient:
    def __init__(self, *, meta=META_SUPPORTED, funding=True, oi=True, tiers=True, fail=None):
        self.meta, self.funding, self.oi, self.tiers, self.fail = meta, funding, oi, tiers, fail
        self.calls: list[str] = []

    def instruments(self, category, symbol=None):
        return [self.meta] if self.meta else []

    def kline(self, category, symbol, interval, start, end, max_bars=1000):
        self.calls.append("kline")
        return [{"ts": t, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
                for t in range(start, end + 1, HOUR)][-1000:]

    def mark_price_kline(self, symbol, interval, *, limit=1000, category="linear",
                         start_ms=None, end_ms=None, completed_only=True):
        self.calls.append("mark")
        return [{"ts": t, "open": 2, "high": 2, "low": 2, "close": 2}
                for t in range(start_ms, end_ms + 1, HOUR)][-1000:]

    def funding_history_window(self, symbol, start_ms, end_ms, limit=200):
        self.calls.append("funding")
        if not self.funding:
            return []
        return [{"ts": t, "rate": 0.0001} for t in range(start_ms, end_ms + 1, 8 * HOUR)][-200:]

    def open_interest_window(self, symbol, *, interval_time="1h", start_ms, end_ms, limit=200):
        self.calls.append("oi")
        if not self.oi:
            return []
        return [{"ts": t, "oi": 1234.5} for t in range(start_ms, end_ms + 1, HOUR)][-200:]

    def risk_limit(self, symbol, category="linear"):
        self.calls.append("risk")
        if not self.tiers:
            return []
        return [{"riskLimitValue": "300000", "maintenanceMargin": "0.5", "maxLeverage": "150"}]


class DataKindTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def _collector(self, client=None) -> HistoryCollector:
        return HistoryCollector(self.db, client or StubClient(), now=lambda: NOW, sleep=lambda _: None)

    def test_every_kind_has_a_label_and_its_own_walk(self):
        self.assertEqual(len(DATA_KINDS), 5)
        for kind in DATA_KINDS:
            self.assertIn(kind, KIND_LABELS)

    def test_each_kind_stores_into_its_own_series(self):
        collector = self._collector()
        collector.run("BTCUSDT", TRADE_CANDLE, "1h", max_pages=2)
        collector.run("BTCUSDT", MARK_CANDLE, "1h", max_pages=2)
        collector.run("BTCUSDT", FUNDING, "", max_pages=2)
        collector.run("BTCUSDT", OPEN_INTEREST, "", max_pages=2)
        collector.run("BTCUSDT", RISK_LIMIT, "")
        self.assertGreater(self.db.count_candles("bybit", "BTCUSDT", "1h"), 0)
        self.assertGreater(self.db.count_mark_candles("bybit", "BTCUSDT", "1h"), 0)
        self.assertGreater(self.db.count_funding("bybit", "BTCUSDT"), 0)
        self.assertGreater(self.db.count_oi("bybit", "BTCUSDT"), 0)
        self.assertTrue(self.db.load_risk_tiers("bybit", "BTCUSDT"))

    def test_mark_prices_never_land_in_the_trade_candle_table(self):
        collector = self._collector()
        collector.run("BTCUSDT", MARK_CANDLE, "1h", max_pages=1)
        self.assertEqual(self.db.count_candles("bybit", "BTCUSDT", "1h"), 0,
                         "标记价格不得写进成交K线表")
        rows = self.db.load_mark_candles("bybit", "BTCUSDT", "1h")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["close"], 2.0)

    def test_a_contract_without_funding_is_unsupported_not_zero_filled(self):
        collector = self._collector(StubClient(meta=META_NO_FUNDING))
        collector.collect_instrument_meta("SPCXUSDT")
        outcome = collector.run("SPCXUSDT", FUNDING, "")
        self.assertEqual(outcome.status, "unsupported")
        self.assertIn("资金费", outcome.reason)
        self.assertEqual(self.db.count_funding("bybit", "SPCXUSDT"), 0)
        state = self.db.load_backfill_state("bybit", "SPCXUSDT", "", FUNDING)
        self.assertEqual(state["status"], "unsupported")
        self.assertTrue(state["reason"])

    def test_a_kind_the_venue_returns_nothing_for_is_named_unsupported(self):
        collector = self._collector(StubClient(oi=False))
        outcome = collector.run("BTCUSDT", OPEN_INTEREST, "", max_pages=1)
        self.assertEqual(outcome.status, "unsupported")
        self.assertIn("未返回", outcome.reason)
        self.assertEqual(self.db.count_oi("bybit", "BTCUSDT"), 0)
        state = self.db.load_backfill_state("bybit", "BTCUSDT", "1h", OPEN_INTEREST)
        self.assertEqual(state["status"], "unsupported")

    def test_each_kind_keeps_its_own_progress_row(self):
        collector = self._collector()
        collector.run("BTCUSDT", TRADE_CANDLE, "1h", max_pages=1)
        collector.run("BTCUSDT", MARK_CANDLE, "1h", max_pages=2)
        collector.run("BTCUSDT", FUNDING, "", max_pages=1)
        states = {row["data_kind"]: row for row in self.db.list_backfill_state(symbol="BTCUSDT")}
        self.assertEqual(set(states), {TRADE_CANDLE, MARK_CANDLE, FUNDING})
        self.assertEqual(states[TRADE_CANDLE]["pages"], 1)
        self.assertEqual(states[MARK_CANDLE]["pages"], 2)
        self.assertEqual(states[TRADE_CANDLE]["interval"], "1h")
        self.assertEqual(states[FUNDING]["interval"], "")

    def test_instrument_metadata_records_the_launch_time_and_contract_facts(self):
        collector = self._collector()
        meta = collector.collect_instrument_meta("BTCUSDT")
        stored = self.db.load_instrument_meta("bybit", "BTCUSDT")
        self.assertEqual(stored["launch_ts"], 1_600_000_000_000)
        self.assertEqual(stored["tick_size"], 0.1)
        self.assertEqual(stored["qty_step"], 0.001)
        self.assertEqual(stored["min_notional"], 5.0)
        self.assertEqual(stored["funding_interval_hours"], 8.0)
        self.assertTrue(meta["available"])

    def test_an_unknown_kind_is_refused_without_touching_the_client(self):
        client = StubClient()
        outcome = self._collector(client).run("BTCUSDT", "order_book", "")
        self.assertEqual(outcome.status, "unsupported")
        self.assertEqual(client.calls, [])

    def test_a_timeframed_kind_still_requires_a_supported_timeframe(self):
        client = StubClient()
        outcome = self._collector(client).run("BTCUSDT", MARK_CANDLE, "3m")
        self.assertEqual(outcome.failure_kind, "invalid_interval")
        self.assertEqual(client.calls, [])

    def test_a_failing_kind_is_classified_and_leaves_the_others_alone(self):
        class Broken(StubClient):
            def mark_price_kline(self, *args, **kwargs):
                raise TimeoutError("read timed out")

        collector = self._collector(Broken())
        mark = collector.run("BTCUSDT", MARK_CANDLE, "1h", max_pages=1)
        candles = collector.run("BTCUSDT", TRADE_CANDLE, "1h", max_pages=1)
        self.assertEqual(mark.failure_kind, "timeout")
        self.assertEqual(candles.status, "ok")
        self.assertGreater(self.db.count_candles("bybit", "BTCUSDT", "1h"), 0)
        state = self.db.load_backfill_state("bybit", "BTCUSDT", "1h", MARK_CANDLE)
        self.assertEqual(state["last_error_kind"], "timeout")

    def test_each_kind_gets_its_own_stable_snapshot_version(self):
        collector = self._collector()
        collector.run("BTCUSDT", TRADE_CANDLE, "1h", max_pages=1)
        collector.run("BTCUSDT", MARK_CANDLE, "1h", max_pages=1)
        collector.run("BTCUSDT", FUNDING, "", max_pages=1)
        collector.run("BTCUSDT", OPEN_INTEREST, "", max_pages=1)
        collector.run("BTCUSDT", RISK_LIMIT, "")
        versions = {}
        for kind, interval in ((TRADE_CANDLE, "1h"), (MARK_CANDLE, "1h"), (FUNDING, ""),
                               (OPEN_INTEREST, ""), (RISK_LIMIT, "")):
            first = collector.snapshot("BTCUSDT", kind, interval)
            second = collector.snapshot("BTCUSDT", kind, interval)
            self.assertTrue(first["version"], kind)
            self.assertEqual(first["version"], second["version"], f"{kind} 的版本必须稳定")
            versions[kind] = first["version"]
        self.assertEqual(len(set(versions.values())), 5, "五个数据族各自独立版本")

    def test_a_snapshot_without_data_says_so_rather_than_pinning_an_empty_range(self):
        record = self._collector().snapshot("ETHUSDT", FUNDING)
        self.assertFalse(record["available"])
        self.assertEqual(record["bars"], 0)

    def test_the_series_version_changes_when_the_data_changes(self):
        rows = [{"ts": 1, "rate": 0.001}]
        first = series_version(FUNDING, "bybit", "BTCUSDT", rows)
        self.assertEqual(first, series_version(FUNDING, "bybit", "BTCUSDT", rows))
        self.assertNotEqual(first, series_version(FUNDING, "bybit", "BTCUSDT", [{"ts": 1, "rate": 0.002}]))
        self.assertNotEqual(first, series_version(OPEN_INTEREST, "bybit", "BTCUSDT", rows))

    def test_an_older_state_table_is_migrated_to_the_four_part_key(self):
        # The previous release keyed state on (venue, symbol, interval) only.
        path = Path(self._tmp.name) / "legacy.db"
        legacy = Database(path)
        legacy.execute("DROP TABLE backfill_state")
        legacy.execute(
            "CREATE TABLE backfill_state (venue TEXT NOT NULL, symbol TEXT NOT NULL, "
            " interval TEXT NOT NULL, oldest_ts INTEGER, newest_ts INTEGER, "
            " complete INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0, "
            " bars INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, "
            " last_error TEXT, last_error_kind TEXT, last_run_ts INTEGER, updated_ts INTEGER NOT NULL, "
            " PRIMARY KEY (venue, symbol, interval)) WITHOUT ROWID"
        )
        legacy.execute(
            "INSERT INTO backfill_state VALUES ('bybit','BTCUSDT','1h',1,2,1,3,3000,1,NULL,NULL,1,1)"
        )
        migrated = Database(path)
        row = migrated.load_backfill_state("bybit", "BTCUSDT", "1h", TRADE_CANDLE)
        self.assertIsNotNone(row, "旧状态必须迁移成 trade_candle 行")
        self.assertEqual(row["pages"], 3)
        self.assertEqual(row["rows_available"], 3000)


class CollectorResumeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def test_a_kind_resumes_from_its_own_frontier(self):
        client = StubClient()
        collector = HistoryCollector(self.db, client, now=lambda: NOW, sleep=lambda _: None)
        first = collector.run("BTCUSDT", OPEN_INTEREST, "", max_pages=2)
        resumed = collector.run("BTCUSDT", OPEN_INTEREST, "", max_pages=1)
        self.assertEqual(resumed.resumed_from, first.oldest_ts)
        self.assertLess(resumed.oldest_ts, first.oldest_ts)
        self.assertEqual(resumed.rows_available, self.db.count_oi("bybit", "BTCUSDT"))

    def test_repeating_a_kind_adds_no_rows(self):
        collector = HistoryCollector(self.db, StubClient(), now=lambda: NOW, sleep=lambda _: None)
        collector.run("BTCUSDT", OPEN_INTEREST, "", max_pages=2)
        before = self.db.count_oi("bybit", "BTCUSDT")
        collector.run("BTCUSDT", OPEN_INTEREST, "", max_pages=2, restart=True)
        self.assertEqual(self.db.count_oi("bybit", "BTCUSDT"), before)


if __name__ == "__main__":
    unittest.main()
