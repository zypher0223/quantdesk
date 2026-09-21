"""Historical data layer: calendar, provenance, coverage and replay.

The calendar arithmetic is checked against known NYSE dates, and the coverage
logic against a window that deliberately contains one missing session bar, a
weekend and a holiday, so each classification is exercised rather than assumed.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import time
import unittest
from pathlib import Path

from quantdesk.datahub import calendar as cal
from quantdesk.datahub.db import COLLECTOR_VERSION, Database
from quantdesk.datahub.snapshot import (
    DataSnapshot,
    classify_gap,
    expected_stamps,
    load_snapshot,
    snapshot_summary,
    snapshot_version,
)
from quantdesk.datahub.venue import INTERVAL_MS

UTC = cal.UTC
HOUR = INTERVAL_MS["1h"]


def stamp(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() * 1000)


class HolidayCalendarTests(unittest.TestCase):
    def test_known_nyse_holidays(self):
        cases = {
            2025: ["2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
                   "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25"],
            2026: ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
                   "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"],
            2027: ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
                   "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24", "2027-12-31"],
        }
        for year, expected in cases.items():
            days = {day.isoformat() for day in cal.us_market_holidays(year)}
            self.assertEqual(days, set(expected), f"{year} holidays")
            for day in cal.us_market_holidays(year):
                self.assertLess(day.weekday(), 5, f"{day} must be observed on a weekday")

    def test_new_years_day_on_a_saturday_is_observed_in_december(self):
        # 2022-01-01 was a Saturday; the exchange was shut on 2021-12-31.
        self.assertIn(dt.date(2021, 12, 31), cal.us_market_holidays(2021))
        self.assertIsNone(cal.equity_session(dt.date(2021, 12, 31)))

    def test_half_days_close_early_and_never_land_on_a_holiday(self):
        for year in (2025, 2026, 2027):
            halves = cal.half_days(year)
            self.assertFalse(set(halves) & set(cal.us_market_holidays(year)), year)
            for day in halves:
                session = cal.equity_session(day)
                self.assertIsNotNone(session, day)
                self.assertTrue(session.half_day, day)
                span = session.close_ts - session.open_ts
                self.assertLess(span, 7 * 3_600_000, "a half day must be shorter than a full one")

    def test_session_windows_follow_daylight_saving(self):
        summer = cal.equity_session(dt.date(2026, 7, 2))
        winter = cal.equity_session(dt.date(2026, 12, 2))
        self.assertIsNotNone(summer)
        self.assertIsNotNone(winter)
        self.assertEqual(dt.datetime.fromtimestamp(summer.open_ts / 1000, UTC).hour, 13)
        self.assertEqual(dt.datetime.fromtimestamp(winter.open_ts / 1000, UTC).hour, 14)

    def test_classification_of_every_absence_reason(self):
        for moment, expected in (
            (dt.datetime(2026, 9, 15, 15, 0, tzinfo=UTC), "open"),
            (dt.datetime(2026, 9, 15, 12, 0, tzinfo=UTC), "off_hours"),
            (dt.datetime(2026, 9, 15, 21, 0, tzinfo=UTC), "off_hours"),
            (dt.datetime(2026, 9, 13, 15, 0, tzinfo=UTC), "weekend"),
            (dt.datetime(2026, 7, 3, 15, 0, tzinfo=UTC), "holiday"),
        ):
            kind, reason = cal.classify_timestamp(int(moment.timestamp() * 1000))
            self.assertEqual(kind, expected, f"{moment} -> {kind} {reason}")
            self.assertTrue(reason or kind == "open")

    def test_sessions_between_skips_weekends_and_holidays(self):
        sessions = cal.sessions_between(stamp(2026, 7, 2), stamp(2026, 7, 7))
        dates = [session.date for session in sessions]
        self.assertIn("2026-07-02", dates)
        self.assertNotIn("2026-07-03", dates, "observed Independence Day")
        self.assertNotIn("2026-07-04", dates, "weekend")
        self.assertNotIn("2026-07-05", dates, "weekend")
        self.assertIn("2026-07-06", dates)

    def test_observed_session_is_measured_from_the_bars(self):
        # Three sessions of hourly bars: several observations per hour bucket.
        bars = []
        for day in (14, 15, 16):
            for hour in range(24):
                volume = 500.0 if 13 <= hour <= 20 else 2.0
                bars.append({"ts": stamp(2026, 9, day, hour), "close": 100.0, "volume": volume})
        observed = cal.observed_session(bars, "AAPLUSDT")
        self.assertTrue(observed.measured)
        self.assertEqual(observed.active_hours_utc, list(range(13, 21)), "the session is where the notional is")
        self.assertEqual(observed.thin_hours_utc, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 21, 22, 23])

    def test_an_unmeasured_session_says_so(self):
        # A single day of hourly bars is enough to find the session, because the
        # busiest bar of each hour is a stable statistic.
        single_day = [{"ts": stamp(2026, 9, 15, hour), "close": 1, "volume": 500 if hour > 12 else 2}
                      for hour in range(24)]
        measured = cal.observed_session(single_day, "X")
        self.assertTrue(measured.measured)
        self.assertIn(13, measured.active_hours_utc)
        # Too little data is reported as unmeasured rather than guessed at.
        too_few = cal.observed_session([{"ts": stamp(2026, 9, 15, 3), "close": 1, "volume": 1}], "X")
        self.assertFalse(too_few.measured)
        self.assertIsNone(too_few.peak_hour_utc)


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def test_bars_are_stored_with_their_origin_and_stamps(self):
        self.db.upsert_candles(
            "bybit", "BTCUSDT", "1h",
            [{"ts": stamp(2026, 9, 15, 3), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 3,
              "exchange_ts": stamp(2026, 9, 15, 4), "received_ts": stamp(2026, 9, 15, 4) + 250}],
            source="venue_ws",
        )
        row = self.db.load_candles("bybit", "BTCUSDT", "1h")[0]
        self.assertEqual(row["source"], "venue_ws")
        self.assertEqual(row["exchange_ts"], stamp(2026, 9, 15, 4))
        self.assertEqual(row["collector"], COLLECTOR_VERSION)

    def test_a_venue_read_replaces_a_locally_derived_bar(self):
        self.db.upsert_candles("bybit", "X", "1h", [{"ts": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
                               source="local_derived")
        self.db.upsert_candles("bybit", "X", "1h", [{"ts": 1, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2}],
                               source="venue_rest")
        row = self.db.load_candles("bybit", "X", "1h")[0]
        self.assertEqual(row["source"], "venue_rest")
        self.assertEqual(row["close"], 2.0)

    def test_a_derived_bar_cannot_downgrade_a_venue_bar(self):
        self.db.upsert_candles("bybit", "X", "1h", [{"ts": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
                               source="venue_ws")
        self.db.upsert_candles("bybit", "X", "1h", [{"ts": 1, "open": 9, "high": 9, "low": 9, "close": 9, "volume": 9}],
                               source="local_derived")
        row = self.db.load_candles("bybit", "X", "1h")[0]
        self.assertEqual(row["source"], "venue_ws", "the stronger source survives")
        self.assertEqual(row["close"], 1.0, "weaker values cannot masquerade as venue data")

    def test_a_rest_bar_reconciles_a_websocket_bar_with_truthful_provenance(self):
        self.db.upsert_candles(
            "bybit", "X", "1h",
            [{"ts": 1, "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1}],
            source="venue_ws",
        )
        self.db.upsert_candles(
            "bybit", "X", "1h",
            [{"ts": 1, "open": 9, "high": 9, "low": 9, "close": 9, "volume": 9}],
            source="venue_rest",
        )
        row = self.db.load_candles("bybit", "X", "1h")[0]
        self.assertEqual(row["source"], "venue_rest")
        self.assertEqual((row["open"], row["high"], row["low"], row["close"]), (9.0, 9.0, 9.0, 9.0))

    def test_received_time_marks_a_content_change_only(self):
        row = {"ts": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
        self.db.upsert_candles("bybit", "X", "1h", [row], received_ts=1_000)
        self.assertEqual(self.db.load_candles("bybit", "X", "1h")[0]["received_ts"], 1_000)
        self.db.upsert_candles("bybit", "X", "1h", [row], received_ts=2_000)
        self.assertEqual(self.db.load_candles("bybit", "X", "1h")[0]["received_ts"], 1_000,
                         "an identical repeat is not a new observation")
        self.db.upsert_candles("bybit", "X", "1h", [dict(row, close=5)], received_ts=3_000)
        updated = self.db.load_candles("bybit", "X", "1h")[0]
        self.assertEqual(updated["received_ts"], 3_000)
        self.assertEqual(updated["close"], 5.0)

    def test_received_time_marks_high_or_low_changes_too(self):
        row = {"ts": 1, "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1}
        self.db.upsert_candles("bybit", "X", "1h", [row], source="venue_rest", received_ts=1_000)
        self.db.upsert_candles(
            "bybit", "X", "1h", [dict(row, high=3)], source="venue_rest", received_ts=2_000
        )
        updated = self.db.load_candles("bybit", "X", "1h")[0]
        self.assertEqual(updated["high"], 3.0)
        self.assertEqual(updated["received_ts"], 2_000)

    def test_an_existing_database_gains_the_provenance_columns(self):
        # A database created before provenance existed must be migrated in place.
        import sqlite3

        path = Path(self._tmp.name) / "legacy.db"
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE candles (venue TEXT NOT NULL, symbol TEXT NOT NULL, interval TEXT NOT NULL, "
                "open_ts INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, "
                "close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0, trades INTEGER, "
                "PRIMARY KEY (venue, symbol, interval, open_ts)) WITHOUT ROWID"
            )
            connection.execute(
                "INSERT INTO candles VALUES ('bybit','OLD','1h',1,1,1,1,1,1,NULL)"
            )
        migrated = Database(path)
        columns = {row["name"] for row in migrated.query("PRAGMA table_info(candles)")}
        for column in ("source", "exchange_ts", "received_ts", "collector"):
            self.assertIn(column, columns)
        rows = migrated.load_candles("bybit", "OLD", "1h")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["source"], "a legacy bar has no origin and says so")


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")
        self.session = cal.equity_session(dt.date(2026, 9, 15))
        self.from_ts = self.session.open_ts - 6 * HOUR
        self.to_ts = self.session.close_ts + 6 * HOUR

    def _write_session_bars(self, drop_index: int | None = None, source: str = "venue_ws") -> list[dict]:
        rows = []
        current = (self.session.open_ts // HOUR) * HOUR
        while current < self.session.close_ts:
            if current >= self.session.open_ts:
                rows.append({
                    "ts": current, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10,
                    "source": source, "exchange_ts": current + 1_000, "received_ts": current + 1_500,
                })
            current += HOUR
        if drop_index is not None:
            rows = [row for row in rows if row["ts"] != rows[drop_index]["ts"]]
        self.db.upsert_candles("bybit", "AAPLUSDT", "1h", rows, source=source)
        return rows

    def test_an_in_session_gap_is_repairable_and_the_rest_is_not(self):
        rows = self._write_session_bars(drop_index=3)
        snapshot = load_snapshot(
            self.db, symbols=["AAPLUSDT"], interval="1h", from_ts=self.from_ts, to_ts=self.to_ts,
            product_types={"AAPLUSDT": "stock"},
        )
        coverage = snapshot.coverage["AAPLUSDT"]
        self.assertEqual(coverage.present, len(rows))
        self.assertEqual(coverage.missing_in_session, 1)
        self.assertFalse(coverage.complete)
        self.assertTrue(coverage.session_gated)
        self.assertGreater(coverage.off_hours, 0, "the pre-market hours are off-hours, not gaps")
        repair = snapshot.repair_list("AAPLUSDT")
        self.assertEqual(repair["bars"], 1)
        self.assertEqual(repair["gaps"][0]["from_ts"], rows[2]["ts"] + HOUR)
        self.assertTrue(all(not gap["repairable"] for gap in repair["unrepairable"]))

    def test_a_complete_range_reports_no_repair(self):
        self._write_session_bars()
        snapshot = load_snapshot(
            self.db, symbols=["AAPLUSDT"], interval="1h", from_ts=self.from_ts, to_ts=self.to_ts,
            product_types={"AAPLUSDT": "stock"},
        )
        self.assertTrue(snapshot.coverage["AAPLUSDT"].complete)
        self.assertEqual(snapshot.repair_list("AAPLUSDT")["bars"], 0)

    def test_weekend_and_holiday_bars_are_never_repaired(self):
        window_from = stamp(2026, 7, 2)
        window_to = stamp(2026, 7, 7)
        snapshot = load_snapshot(
            self.db, symbols=["AAPLUSDT"], interval="1h", from_ts=window_from, to_ts=window_to,
            product_types={"AAPLUSDT": "stock"},
        )
        coverage = snapshot.coverage["AAPLUSDT"]
        self.assertGreater(coverage.weekend, 0)
        self.assertGreater(coverage.holiday, 0)
        kinds = {gap.kind for gap in snapshot.gaps["AAPLUSDT"]}
        self.assertIn("holiday", kinds)
        self.assertIn("weekend", kinds)
        self.assertNotIn("missing_in_session", {gap.kind for gap in snapshot.gaps["AAPLUSDT"] if not gap.repairable})

    def test_a_crypto_contract_has_no_session_to_hide_behind(self):
        # Three bars out of a twenty-bar window: all seventeen absences are defects.
        self.db.upsert_candles(
            "bybit", "BTCUSDT", "1h",
            [{"ts": self.from_ts + index * HOUR, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
             for index in range(3)],
            source="venue_rest",
        )
        snapshot = load_snapshot(
            self.db, symbols=["BTCUSDT"], interval="1h", from_ts=self.from_ts, to_ts=self.to_ts,
            product_types={"BTCUSDT": "crypto"},
        )
        coverage = snapshot.coverage["BTCUSDT"]
        self.assertFalse(coverage.session_gated)
        self.assertEqual(coverage.off_hours, 0)
        self.assertEqual(coverage.missing_in_session, coverage.expected - coverage.present)
        self.assertTrue(snapshot.repair_list("BTCUSDT")["bars"] > 0)

    def test_crypto_weekend_and_equity_holiday_gaps_are_repairable(self):
        # Independence Day is observed on Friday 2026-07-03, followed by a
        # weekend; none of these dates closes the BTC perpetual market.
        window_from = stamp(2026, 7, 2)
        window_to = stamp(2026, 7, 7)
        self.db.upsert_candles(
            "bybit", "BTCUSDT", "1h",
            [{"ts": window_from, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
            source="venue_rest",
        )
        snapshot = load_snapshot(
            self.db, symbols=["BTCUSDT"], interval="1h",
            from_ts=window_from, to_ts=window_to,
            product_types={"BTCUSDT": "crypto"},
        )
        coverage = snapshot.coverage["BTCUSDT"]
        self.assertEqual(coverage.weekend, 0)
        self.assertEqual(coverage.holiday, 0)
        self.assertEqual(coverage.missing_in_session, coverage.expected - coverage.present)
        self.assertFalse(coverage.complete)
        self.assertEqual(snapshot.repair_list("BTCUSDT")["bars"], coverage.missing_in_session)
        self.assertTrue(all(gap.repairable for gap in snapshot.gaps["BTCUSDT"]))

    def test_an_outage_window_is_named_as_such(self):
        # Every hour of the window is stored except six, and those six fall inside
        # a period when no writer was running.
        outage = (self.from_ts + 4 * HOUR, self.from_ts + 10 * HOUR)
        rows = [
            {"ts": self.from_ts + index * HOUR, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
            for index in range((self.to_ts - self.from_ts) // HOUR + 1)
            if not (outage[0] <= self.from_ts + index * HOUR < outage[1])
        ]
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", rows, source="venue_rest")
        snapshot = load_snapshot(
            self.db, symbols=["BTCUSDT"], interval="1h", from_ts=self.from_ts, to_ts=self.to_ts,
            product_types={"BTCUSDT": "crypto"}, service_windows=[outage],
        )
        coverage = snapshot.coverage["BTCUSDT"]
        self.assertEqual(coverage.service_down, 6)
        self.assertEqual(coverage.missing_in_session, 0)
        self.assertFalse(coverage.complete, "an outage still means the range is not intact")
        self.assertTrue(any("服务未运行" in gap.reason for gap in snapshot.gaps["BTCUSDT"]))

    def test_an_empty_range_is_reported_not_scored(self):
        snapshot = load_snapshot(
            self.db, symbols=["AAPLUSDT"], interval="1h", from_ts=self.from_ts, to_ts=self.to_ts,
            product_types={"AAPLUSDT": "stock"},
        )
        self.assertEqual(snapshot.coverage["AAPLUSDT"].expected, 0)
        self.assertTrue(any("没有任何K线" in note for note in snapshot.notes))

    def test_sources_and_collectors_are_counted(self):
        self.db.upsert_candles("bybit", "AAPLUSDT", "1h",
                              [{"ts": self.from_ts, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
                              source="venue_ws")
        self.db.upsert_candles("bybit", "AAPLUSDT", "1h",
                              [{"ts": self.from_ts + HOUR, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
                              source="imported")
        snapshot = load_snapshot(self.db, symbols=["AAPLUSDT"], interval="1h", from_ts=self.from_ts,
                                 to_ts=self.from_ts + 2 * HOUR, product_types={"AAPLUSDT": "stock"})
        provenance = snapshot.provenance
        self.assertEqual(provenance.sources.get("venue_ws"), 1)
        self.assertEqual(provenance.sources.get("imported"), 1)
        self.assertEqual(provenance.collectors.get(COLLECTOR_VERSION), 2)
        self.assertTrue(any("导入" in note for note in snapshot.notes))


class ReplayAndVersionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")
        self.start = stamp(2026, 9, 15, 0)
        for index in range(6):
            self.db.upsert_candles(
                "bybit", "BTCUSDT", "1h",
                [{"ts": self.start + index * HOUR, "open": 1 + index, "high": 2 + index, "low": 1 + index,
                  "close": 1.5 + index, "volume": 10 + index, "source": "venue_ws",
                  "exchange_ts": self.start + index * HOUR + 500, "received_ts": self.start + index * HOUR + 900}],
                source="venue_ws",
            )
        self.db.upsert_funding("bybit", "BTCUSDT", [{"ts": self.start + 3 * HOUR, "rate": 0.0001}])

    def test_replay_walks_the_stored_history_in_order(self):
        snapshot = load_snapshot(self.db, symbols=["BTCUSDT"], interval="1h", from_ts=self.start,
                                 to_ts=self.start + 8 * HOUR, product_types={"BTCUSDT": "crypto"})
        events = list(snapshot.replay())
        kinds = [event["kind"] for event in events]
        self.assertEqual(kinds.count("candle"), 6)
        self.assertIn("funding", kinds)
        self.assertIn("gap", kinds, "the replay must show the hole, not skip it")
        stamps = [event["ts"] for event in events]
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(all("iso" in event for event in events))

    def test_the_version_is_stable_for_the_same_data_and_changes_with_it(self):
        first = load_snapshot(self.db, symbols=["BTCUSDT"], interval="1h", from_ts=self.start,
                              to_ts=self.start + 6 * HOUR, product_types={"BTCUSDT": "crypto"})
        again = load_snapshot(self.db, symbols=["BTCUSDT"], interval="1h", from_ts=self.start,
                              to_ts=self.start + 6 * HOUR, product_types={"BTCUSDT": "crypto"})
        self.assertEqual(snapshot_version(first), snapshot_version(again))
        self.assertTrue(first.verify()["reproducible"])
        self.db.upsert_candles(
            "bybit", "BTCUSDT", "1h",
            [{"ts": self.start + 9 * HOUR, "open": 9, "high": 9, "low": 9, "close": 9, "volume": 9}],
            source="venue_ws",
        )
        third = load_snapshot(self.db, symbols=["BTCUSDT"], interval="1h", from_ts=self.start,
                              to_ts=self.start + 10 * HOUR, product_types={"BTCUSDT": "crypto"})
        self.assertNotEqual(snapshot_version(first), snapshot_version(third))
        # A snapshot pins the range it read: re-verifying it against a window that
        # now extends further cannot match, which is the point of the hash.
        problems = first.verify_against(self.db)["problems"]
        self.assertTrue(problems, "a changed range must invalidate the recorded version")
        self.assertTrue(first.verify()["reproducible"], "the pinned window itself did not change")

    def test_the_summary_is_json_shaped(self):
        snapshot = load_snapshot(self.db, symbols=["BTCUSDT"], interval="1h", from_ts=self.start,
                                 to_ts=self.start + 6 * HOUR, product_types={"BTCUSDT": "crypto"})
        summary = snapshot_summary(snapshot)
        self.assertIn("provenance", summary)
        self.assertIn("BTCUSDT", summary["symbols"])
        self.assertIn("coverage", summary["symbols"]["BTCUSDT"])
        self.assertIn("repair", summary["symbols"]["BTCUSDT"])
        # The version is the canonical content id, and it matches the snapshot.
        self.assertEqual(summary["provenance"]["version"], snapshot_version(snapshot))
        self.assertRegex(summary["provenance"]["version"], r"^[0-9a-f]{16}$")
        self.assertIsInstance(summary["provenance"]["data_hash"], str)

    def test_expected_stamps_anchor_to_a_stored_bar(self):
        # A grid anchored on the epoch would miss bars the exchange stamped offset.
        offset = self.start + 137_000
        stamps = expected_stamps(self.start, self.start + 3 * HOUR, HOUR, anchor=offset)
        self.assertIn(offset, stamps)
        self.assertTrue(all((value - offset) % HOUR == 0 for value in stamps))
        self.assertEqual(expected_stamps(self.start, self.start, HOUR, anchor=None), [self.start])

    def test_gap_classification_helper_agrees_with_the_calendar(self):
        kind, _reason, repairable = classify_gap(stamp(2026, 9, 15, 15))
        self.assertEqual(kind, "missing_in_session")
        self.assertTrue(repairable)
        kind, reason, repairable = classify_gap(stamp(2026, 7, 3, 15))
        self.assertEqual(kind, "holiday")
        self.assertFalse(repairable)
        self.assertIn("休市", reason)


if __name__ == "__main__":
    unittest.main()


class SharedHistoryTests(unittest.TestCase):
    """One pinned read, shared by every consumer that needs history."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")
        self.start = stamp(2026, 9, 15, 0)
        rows = [
            {"ts": self.start + index * HOUR, "open": 100.0 + index, "high": 101.0 + index,
             "low": 99.0 + index, "close": 100.5 + index, "volume": 10.0 + index,
             "source": "venue_ws", "exchange_ts": self.start + index * HOUR + 400,
             "received_ts": self.start + index * HOUR + 900}
            for index in range(10)
        ]
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", rows, source="venue_ws")
        self.db.upsert_funding("bybit", "BTCUSDT", [{"ts": self.start + 5 * HOUR, "rate": 0.0002}])

    def test_two_reads_of_the_same_range_agree_on_the_version(self):
        from quantdesk.datahub.view import read_history

        first = read_history(self.db, symbol="BTCUSDT", interval="1h", bars=10)
        second = read_history(self.db, symbol="BTCUSDT", interval="1h", bars=10)
        self.assertEqual(first.version, second.version)
        self.assertEqual(len(first.bars), 10)
        self.assertEqual(first.funding, second.funding)

    def test_a_different_range_or_content_changes_the_version(self):
        from quantdesk.datahub.view import read_history

        base = read_history(self.db, symbol="BTCUSDT", interval="1h", bars=5)
        wider = read_history(self.db, symbol="BTCUSDT", interval="1h", bars=10)
        self.assertNotEqual(base.version, wider.version)
        self.db.upsert_candles(
            "bybit", "BTCUSDT", "1h",
            [{"ts": self.start + 9 * HOUR, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
            source="venue_ws",
        )
        changed = read_history(self.db, symbol="BTCUSDT", interval="1h", bars=10)
        self.assertNotEqual(wider.version, changed.version)

    def test_funding_is_part_of_the_pinned_version(self):
        from quantdesk.datahub.view import read_history

        with_funding = read_history(self.db, symbol="BTCUSDT", interval="1h", bars=5, with_funding=True)
        without = read_history(self.db, symbol="BTCUSDT", interval="1h", bars=5, with_funding=False)
        self.assertEqual(len(with_funding.funding), 1)
        self.assertEqual(without.funding, [])
        self.assertNotEqual(with_funding.version, without.version)

    def test_the_provenance_block_is_what_a_result_stores(self):
        from quantdesk.datahub.view import read_history

        history = read_history(self.db, symbol="BTCUSDT", interval="1h", bars=10, display_symbol="BTC")
        provenance = history.provenance()
        self.assertEqual(provenance["version"], history.version)
        self.assertEqual(provenance["bars"], 10)
        self.assertEqual(provenance["sources"], {"venue_ws": 10})
        self.assertEqual(provenance["collectors"], {"collector/1": 10})
        self.assertTrue(provenance["complete"])
        self.assertEqual(provenance["interval"], "1h")

    def test_a_read_never_hands_back_a_forming_bar(self):
        from quantdesk.datahub.view import read_history

        # A bar whose close is in the future must not be treated as history: a
        # stream writes the bar as it forms, and handing that to a backtest or an
        # alert is handing it a price that had not happened yet.
        #
        # The fixture is anchored on the wall clock on purpose: the guard compares
        # bar close times against *now*, so a test that pins "today" to a literal
        # date silently stops testing anything once that date is in the past.
        now = int(time.time() * 1000)
        forming = (now // HOUR) * HOUR
        closed = [forming - (10 - index) * HOUR for index in range(10)]
        self.db.upsert_candles(
            "bybit", "BTCUSDT", "1h",
            [{"ts": stamp_, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
              "source": "venue_ws"} for stamp_ in [*closed, forming]],
            source="venue_ws",
        )
        history = read_history(self.db, symbol="BTCUSDT", interval="1h", bars=20)
        returned = [int(row["ts"]) for row in history.bars]
        self.assertNotIn(forming, returned, "the forming bar is not history yet")
        # ...and the guard must not have emptied the read: every closed bar stored
        # above comes back. The class fixture's older bars are in the store too, so
        # the assertion is about *these* ten rather than about the window's width.
        self.assertTrue(set(closed) <= set(returned), "已收盘的K线必须全部返回")
        self.assertEqual(max(returned), closed[-1])

    def test_an_explicit_range_still_ends_where_the_caller_said(self):
        from quantdesk.datahub.view import read_history

        # The default ends at the last closed bar; a caller that names its range
        # keeps control of it, which is what replay and coverage checks rely on.
        history = read_history(
            self.db, symbol="BTCUSDT", interval="1h", bars=20,
            to_ts=self.start + 9 * HOUR,
        )
        self.assertEqual(len(history.bars), 10)
        self.assertEqual(int(history.bars[-1]["ts"]), self.start + 9 * HOUR)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")
        # A session in the past: a repair refuses bars that have not closed yet, so
        # a fixture dated in the future would be rejected for the wrong reason.
        self.session = cal.equity_session(dt.date(2026, 5, 15))
        self.from_ts = self.session.open_ts - 4 * HOUR
        self.to_ts = self.session.close_ts + 4 * HOUR
        self.rows = []
        current = (self.session.open_ts // HOUR) * HOUR
        while current < self.session.close_ts:
            if current >= self.session.open_ts:
                self.rows.append({"ts": current, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10})
            current += HOUR
        # Store everything except two bars in the middle.
        self.missing = {self.rows[2]["ts"], self.rows[3]["ts"]}
        kept = [row for row in self.rows if row["ts"] not in self.missing]
        self.db.upsert_candles("bybit", "AAPLUSDT", "1h", kept, source="venue_ws")

    def _snapshot(self):
        return load_snapshot(
            self.db, symbols=["AAPLUSDT"], interval="1h", from_ts=self.from_ts, to_ts=self.to_ts,
            product_types={"AAPLUSDT": "stock"},
        )

    def test_only_the_missing_bars_are_requested(self):
        from quantdesk.datahub.snapshot import repair_gaps

        class FakeClient:
            def __init__(self, rows):
                self.rows = rows
                self.calls: list[tuple[int, int]] = []

            def kline(self, category, symbol, interval, start_ms, end_ms):
                self.calls.append((start_ms, end_ms))
                return [row for row in self.rows if start_ms <= row["ts"] <= end_ms]

        client = FakeClient(self.rows)
        snapshot = self._snapshot()
        self.assertEqual(snapshot.repair_list("AAPLUSDT")["bars"], 2)
        report = repair_gaps(self.db, snapshot, client)
        self.assertEqual(report["bars"], 2)
        self.assertEqual(report["written"], 2)
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(client.calls), 1, "one contiguous hole must cost one request")
        start, end = client.calls[0]
        self.assertLessEqual(start, min(self.missing))
        self.assertGreaterEqual(end, max(self.missing))
        # And the range is the hole, not the whole sample.
        self.assertLess(end - start, (self.to_ts - self.from_ts))
        after = self._snapshot()
        self.assertEqual(after.repair_list("AAPLUSDT")["bars"], 0)
        self.assertTrue(after.coverage["AAPLUSDT"].complete)

    def test_a_bar_that_has_not_closed_is_not_written_from_the_venue(self):
        from quantdesk.datahub.snapshot import repair_gaps

        forming = (int(time.time() * 1000) // HOUR) * HOUR

        class FakeClient:
            def kline(self, category, symbol, interval, start_ms, end_ms):
                return [{"ts": forming, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]

        # A snapshot whose only hole is the currently forming bar.
        snapshot = load_snapshot(
            self.db, symbols=["AAPLUSDT"], interval="1h", from_ts=forming, to_ts=forming,
            product_types={"AAPLUSDT": "stock"},
        )
        report = repair_gaps(self.db, snapshot, FakeClient())
        self.assertEqual(report["written"], 0)
        self.assertEqual(self.db.load_candles("bybit", "AAPLUSDT", "1h", start_ts=forming), [])

    def test_a_venue_error_on_one_range_is_reported_not_raised(self):
        from quantdesk.datahub.snapshot import repair_gaps

        class BrokenClient:
            def kline(self, *args, **kwargs):
                raise OSError("proxy refused")

        report = repair_gaps(self.db, self._snapshot(), BrokenClient())
        self.assertEqual(report["written"], 0)
        self.assertTrue(report["errors"])
        self.assertIn("proxy refused", report["errors"][0])

    def test_off_hours_absences_are_never_requested(self):
        from quantdesk.datahub.snapshot import repair_gaps

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def kline(self, *args, **kwargs):
                self.calls += 1
                return []

        client = FakeClient()
        snapshot = self._snapshot()
        # The window deliberately extends beyond the session on both sides.
        self.assertGreater(snapshot.coverage["AAPLUSDT"].off_hours, 0)
        repair_gaps(self.db, snapshot, client)
        self.assertLessEqual(client.calls, 1, "only the in-session hole is worth a request")


class TradingAgentsDataReferenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")
        self._env = __import__("unittest.mock", fromlist=["patch"]).patch.dict(
            "os.environ", {"QUANTDESK_HOME": self._tmp.name}
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        rows = []
        # A window that has closed and cannot collide with the forming-bar rule:
        # provenance names the newest closed bar, so a fixture dated in the future
        # would be asserting the wrong thing.
        day = stamp(2026, 6, 1)
        for index in range(30):
            rows.append({"ts": day + index * 86_400_000, "open": 100, "high": 101, "low": 99, "close": 100.5,
                         "volume": 10, "source": "venue_rest"})
        self.db.upsert_candles("bybit", "BTCUSDT", "1d", rows, source="venue_rest")
        self.last_day = stamp(2026, 6, 1) + 29 * 86_400_000

    def test_the_reference_names_the_data_time_and_version(self):
        from quantdesk.tradingagents_runner import data_reference

        reference = data_reference("BTCUSDT", "2026-06-30")
        self.assertTrue(reference["available"])
        self.assertEqual(reference["asOf"], "2026-06-30")
        self.assertEqual(reference["tradeDate"], "2026-06-30")
        self.assertEqual(reference["bars"], 30)
        self.assertTrue(reference["version"])
        self.assertEqual(reference["sources"], {"venue_rest": 30})

    def test_a_stored_bar_that_has_not_closed_is_not_the_data_time(self):
        from quantdesk.tradingagents_runner import data_reference

        # The stream writes today's bar while it is still forming. Provenance must
        # not claim the run could read a day that has not finished. "Today" comes
        # from the clock, not from a literal: the assertion is about a bar that has
        # not closed *yet*, and that only means anything relative to now.
        today = (int(time.time() * 1000) // 86_400_000) * 86_400_000
        self.db.upsert_candles(
            "bybit", "BTCUSDT", "1d",
            [{"ts": today, "open": 1, "high": 1, "low": 1, "close": 1,
              "volume": 1, "source": "venue_ws"}],
            source="venue_ws",
        )
        reference = data_reference("BTCUSDT", "2026-06-30")
        self.assertEqual(reference["asOf"], "2026-06-30")
        self.assertEqual(reference["bars"], 30)

    def test_a_trade_date_after_the_data_is_stale(self):
        from quantdesk.tradingagents_runner import data_reference

        reference = data_reference("BTCUSDT", "2026-10-20")
        self.assertTrue(reference["stale"])
        self.assertGreater(reference["staleByDays"], 1)
        self.assertIn("未覆盖", reference["staleReason"])

    def test_a_trade_date_before_the_data_is_stale(self):
        from quantdesk.tradingagents_runner import data_reference

        reference = data_reference("BTCUSDT", "2020-01-01")
        self.assertTrue(reference["stale"])
        self.assertIn("没有该日期", reference["staleReason"])

    def test_a_symbol_outside_the_pool_is_refused(self):
        from quantdesk.tradingagents_runner import data_reference

        reference = data_reference("DOGEUSDT", "2026-09-30")
        self.assertFalse(reference["available"])
        self.assertIsNone(reference.get("version"))

    def test_an_empty_store_is_reported_not_guessed(self):
        from quantdesk.tradingagents_runner import data_reference

        reference = data_reference("NVDAUSDT", "2026-09-30")
        self.assertFalse(reference["available"])
        self.assertIn("没有", reference["reason"])


class CrossConsumerVersionTests(unittest.TestCase):
    """A coverage report and a consumer's pin must agree on the same range."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")
        self.start = stamp(2026, 9, 15, 0)
        rows = [
            {"ts": self.start + index * HOUR, "open": 1 + index, "high": 2 + index, "low": 0.5 + index,
             "close": 1.5 + index, "volume": 10 + index, "source": "venue_ws",
             "exchange_ts": self.start + index * HOUR + 100, "received_ts": self.start + index * HOUR + 200}
            for index in range(24)
        ]
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", rows, source="venue_ws")

    def test_the_reader_and_the_snapshot_report_one_version(self):
        from quantdesk.datahub.snapshot import load_snapshot, snapshot_version
        from quantdesk.datahub.view import read_history

        history = read_history(self.db, symbol="BTCUSDT", interval="1h", bars=24, with_funding=False)
        snapshot = load_snapshot(
            self.db,
            symbols=["BTCUSDT"],
            interval="1h",
            from_ts=history.from_ts,
            to_ts=history.to_ts,
            product_types={"BTCUSDT": "crypto"},
            with_funding=False,
            with_marks=False,
        )
        self.assertEqual(history.version, snapshot_version(snapshot))

    def test_the_version_is_independent_of_when_it_was_read(self):
        from quantdesk.datahub.view import read_history

        first = read_history(self.db, symbol="BTCUSDT", interval="1h", bars=24, with_funding=False)
        self.db.upsert_candles(
            "bybit", "BTCUSDT", "1h",
            [{"ts": self.start + 40 * HOUR, "open": 9, "high": 9, "low": 9, "close": 9, "volume": 9}],
            source="venue_ws",
        )
        # A newer bar outside the pinned range does not change the pinned version,
        # which is what makes it usable as a record of what a result read.
        again = read_history(
            self.db, symbol="BTCUSDT", interval="1h", bars=24, from_ts=first.from_ts, to_ts=first.to_ts,
            with_funding=False,
        )
        self.assertEqual(first.version, again.version)
