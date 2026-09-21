"""Full-history backfill: pagination, resumption, snapshots, failure kinds.

The store is the deliverable here, so the tests check what actually landed in it,
what the run recorded about where it got to, and how it named each way a run can
fail to finish.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quantdesk.datahub.backfill import (
    BUDGET_EXHAUSTED,
    INVALID_INTERVAL,
    NETWORK,
    NO_DATA,
    RATE_LIMITED,
    TIMEOUT,
    UNKNOWN,
    UPSTREAM_ERROR,
    HistoryBackfill,
    classify_failure,
)
from quantdesk.datahub.db import Database

HOUR = 3_600_000
# Hour-aligned so "the bar at `now` is still forming" is unambiguous, and the
# venue stub's bar grid lines up with the listing timestamp.
NOW = 1_700_000_000_000 - (1_700_000_000_000 % HOUR)


def bar(ts: int) -> dict:
    return {"ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}


class _Venue:
    """A venue with a fixed listing date, paginating 1000 bars per call."""

    def __init__(self, *, listing_ts: int, now: int = NOW, page: int = 1000, fail_on: int | None = None,
                 error: BaseException | None = None):
        self.listing_ts = listing_ts
        self.now = now
        self.page = page
        self.fail_on = fail_on
        self.error = error
        self.calls: list[tuple[int, int]] = []

    def fetch(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
        self.calls.append((start_ms, end_ms))
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise self.error or RuntimeError("boom")
        first = max(start_ms, self.listing_ts)
        # Align to the hour so the bars look like a real series.
        first = ((first + HOUR - 1) // HOUR) * HOUR
        rows = [bar(ts) for ts in range(first, end_ms + 1, HOUR)]
        return rows[-self.page:]


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def _backfill(self, venue: _Venue, **kwargs) -> HistoryBackfill:
        return HistoryBackfill(
            self.db, venue.fetch, now=lambda: NOW, sleep=lambda _: None,
            page_bars=kwargs.pop("page_bars", 1000),
        )

    def test_a_full_walk_pages_back_to_the_listing_and_stops(self):
        venue = _Venue(listing_ts=NOW - 5 * HOUR)
        outcome = self._backfill(venue).run("BTCUSDT", "1h", max_pages=50)
        self.assertTrue(outcome.complete)
        # The walk ends on an empty window, not on a short page: a short page can
        # simply be a window whose newest bar is still forming.
        self.assertEqual(outcome.stopped_because, "no_more_data")
        self.assertEqual(outcome.failure_kind, "")
        # The fixture clock is synthetic, so every bar the stub produces is in the
        # past and therefore closed: the walk reaches listing .. now inclusive.
        # (That a still-forming bar is refused has its own test in the data suite.)
        self.assertEqual(self.db.count_candles("bybit", "BTCUSDT", "1h"), 6)
        self.assertEqual(self.db.first_open_ts("bybit", "BTCUSDT", "1h"), venue.listing_ts)
        self.assertEqual(self.db.last_open_ts("bybit", "BTCUSDT", "1h"), NOW)

    def test_pages_do_not_overlap(self):
        venue = _Venue(listing_ts=NOW - 3000 * HOUR)
        outcome = self._backfill(venue).run("BTCUSDT", "1h", max_pages=3)
        self.assertEqual(outcome.pages, 3)
        # Each page ends before the previous page's oldest bar.
        for (_, previous_end), (next_start, _) in zip(venue.calls, venue.calls[1:]):
            self.assertLess(next_start, previous_end)

    def test_the_budget_bounds_one_run_and_is_reported_as_incomplete(self):
        venue = _Venue(listing_ts=NOW - 5000 * HOUR)
        outcome = self._backfill(venue).run("BTCUSDT", "1h", max_pages=2)
        self.assertFalse(outcome.complete)
        self.assertEqual(outcome.stopped_because, "budget_exhausted")
        self.assertEqual(outcome.failure_kind, BUDGET_EXHAUSTED)
        self.assertIn("页数预算", outcome.as_dict()["failureLabel"])

    def test_an_interrupted_walk_resumes_from_its_own_frontier(self):
        venue = _Venue(listing_ts=NOW - 5000 * HOUR)
        backfill = self._backfill(venue)
        first = backfill.run("BTCUSDT", "1h", max_pages=2)
        frontier = first.oldest_ts
        before = len(venue.calls)
        second = backfill.run("BTCUSDT", "1h", max_pages=1)
        self.assertEqual(second.resumed_from, frontier)
        self.assertLess(second.oldest_ts, frontier)
        # The resumed run asked only for older data, never for what it had.
        resumed_calls = venue.calls[before:]
        self.assertTrue(all(end <= frontier for _, end in resumed_calls))

    def test_a_completed_walk_starts_over_from_now_instead_of_resuming(self):
        venue = _Venue(listing_ts=NOW - 3 * HOUR)
        backfill = self._backfill(venue)
        backfill.run("BTCUSDT", "1h", max_pages=10)
        calls_before = len(venue.calls)
        again = backfill.run("BTCUSDT", "1h", max_pages=10)
        self.assertIsNone(again.resumed_from)
        self.assertEqual(again.pages, 1, "已完成的历史只需刷新最新窗口")
        # Two requests: the refresh, then the empty window that proves the series
        # really is finished. Cheap, and it is what tells "short" from "done".
        self.assertEqual(len(venue.calls) - calls_before, 2)

    def test_restart_ignores_the_saved_frontier(self):
        venue = _Venue(listing_ts=NOW - 5000 * HOUR)
        backfill = self._backfill(venue)
        backfill.run("BTCUSDT", "1h", max_pages=2)
        restarted = backfill.run("BTCUSDT", "1h", max_pages=1, restart=True)
        self.assertIsNone(restarted.resumed_from)

    def test_progress_is_written_after_every_page(self):
        venue = _Venue(listing_ts=NOW - 5000 * HOUR)
        self._backfill(venue).run("BTCUSDT", "1h", max_pages=3)
        state = self.db.load_backfill_state("bybit", "BTCUSDT", "1h")
        self.assertEqual(state["pages"], 3)
        self.assertEqual(state["attempts"], 1)
        self.assertIsNotNone(state["oldest_ts"])
        self.assertIsNotNone(state["last_run_ts"])

    def test_an_unknown_interval_is_refused_without_calling_the_venue(self):
        venue = _Venue(listing_ts=NOW - HOUR)
        outcome = self._backfill(venue).run("BTCUSDT", "3m", max_pages=2)
        self.assertEqual(outcome.failure_kind, INVALID_INTERVAL)
        self.assertEqual(venue.calls, [])

    def test_a_venue_failure_is_classified_and_saved(self):
        for error, expected in (
            (TimeoutError("read timed out"), TIMEOUT),
            (ConnectionError("connect failed"), NETWORK),
            (RuntimeError("bybit /v5/market/kline retCode=10006: rate limit"), RATE_LIMITED),
            (RuntimeError("something odd"), UNKNOWN),
        ):
            with self.subTest(error=type(error).__name__):
                venue = _Venue(listing_ts=NOW - 5000 * HOUR, fail_on=1, error=error)
                outcome = self._backfill(venue).run("BTCUSDT", "1h", max_pages=2)
                self.assertEqual(outcome.failure_kind, expected)
                self.assertIn(type(error).__name__, outcome.failure)
                state = self.db.load_backfill_state("bybit", "BTCUSDT", "1h")
                self.assertEqual(state["last_error_kind"], expected)
                self.assertFalse(state["complete"])

    def test_a_failed_run_keeps_what_it_already_stored(self):
        venue = _Venue(listing_ts=NOW - 5000 * HOUR, fail_on=3)
        outcome = self._backfill(venue).run("BTCUSDT", "1h", max_pages=5)
        self.assertEqual(outcome.pages, 2, "前两页已经落库")
        self.assertGreater(self.db.count_candles("bybit", "BTCUSDT", "1h"), 0)
        self.assertEqual(outcome.failure_kind, UNKNOWN)

    def test_an_empty_window_ends_the_walk_as_complete(self):
        class Empty:
            def fetch(self, symbol, interval, start_ms, end_ms):
                return []

        outcome = HistoryBackfill(self.db, Empty().fetch, now=lambda: NOW).run("BTCUSDT", "1h")
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.stopped_because, "no_more_data")
        self.assertEqual(self.db.count_candles("bybit", "BTCUSDT", "1h"), 0)

    def test_the_outcome_explains_itself(self):
        venue = _Venue(listing_ts=NOW - 5000 * HOUR)
        payload = self._backfill(venue).run("BTCUSDT", "1h", max_pages=1).as_dict()
        for key in ("pages", "barsStored", "oldestTs", "newestTs", "complete", "stoppedBecause",
                    "failureKind", "failureLabel", "durationMs"):
            self.assertIn(key, payload)
        self.assertEqual(payload["symbol"], "BTCUSDT")


class ShortPageTests(unittest.TestCase):
    """A page that comes back short is not the end of the history.

    The venue omits the still-forming bar, so a window holding exactly one page
    returns one row fewer. Treating that as "finished" is how a walk that had 228
    pages left declared itself complete after one.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def test_a_walk_continues_past_a_page_that_is_one_row_short(self):
        step = HOUR
        listing = NOW - 5000 * HOUR
        pages_served: list[tuple[int, int]] = []

        def fetch(symbol, interval, start, end):
            first = max(start, listing)
            first = ((first + step - 1) // step) * step
            if first > end:
                return []
            rows = [bar(ts) for ts in range(first, end + 1, step)]
            # Every page is one row short of the page size, exactly as a window
            # containing the forming bar would be.
            pages_served.append((start, end))
            return rows[:-1][-1000:]

        outcome = HistoryBackfill(self.db, fetch, now=lambda: NOW, sleep=lambda _: None).run(
            "BTCUSDT", "1h", max_pages=6
        )
        self.assertGreater(outcome.pages, 1, "短页不能当作走完")
        self.assertGreater(outcome.bars_fetched, 1000)
        self.assertEqual(outcome.oldest_ts, self.db.first_open_ts("bybit", "BTCUSDT", "1h"))

    def test_the_walk_still_finishes_when_the_venue_runs_out(self):
        def fetch(symbol, interval, start, end):
            if start < NOW - 3000 * HOUR:
                return []
            first = ((max(start, NOW - 3000 * HOUR) + HOUR - 1) // HOUR) * HOUR
            return [bar(ts) for ts in range(first, end + 1, HOUR)][:-1][-1000:]

        outcome = HistoryBackfill(self.db, fetch, now=lambda: NOW, sleep=lambda _: None).run(
            "BTCUSDT", "1h", max_pages=20
        )
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.stopped_because, "no_more_data")

    def test_the_count_is_reported_while_the_walk_is_still_running(self):
        seen: list[tuple[int, int]] = []

        def fetch(symbol, interval, start, end):
            first = ((max(start, NOW - 3000 * HOUR) + HOUR - 1) // HOUR) * HOUR
            if first > end:
                return []
            return [bar(ts) for ts in range(first, end + 1, HOUR)][:-1][-1000:]

        HistoryBackfill(self.db, fetch, now=lambda: NOW, sleep=lambda _: None).run(
            "BTCUSDT", "1h", max_pages=3,
            on_page=lambda pages, rows: (seen.append((pages, rows)), True)[1],
        )
        self.assertTrue(seen)
        for pages, rows in seen:
            self.assertGreater(rows, 0, f"第 {pages} 页之后可用记录不应为 0")
        self.assertEqual(seen[-1][1], self.db.count_candles("bybit", "BTCUSDT", "1h"))


class ClassificationTests(unittest.TestCase):
    def test_http_and_retcode_signals_win_over_message_text(self):
        self.assertEqual(classify_failure(None, http_status=429), RATE_LIMITED)
        self.assertEqual(classify_failure(None, ret_code=10006), RATE_LIMITED)
        self.assertEqual(classify_failure(None, http_status=503), UPSTREAM_ERROR)
        self.assertEqual(classify_failure(None, http_status=404), "invalid_symbol")

    def test_exception_types_are_classified(self):
        self.assertEqual(classify_failure(TimeoutError("x")), TIMEOUT)
        self.assertEqual(classify_failure(ConnectionError("x")), NETWORK)
        self.assertEqual(classify_failure(ValueError("unsupported interval '3m'")), INVALID_INTERVAL)
        self.assertEqual(classify_failure(RuntimeError("weird")), UNKNOWN)

    def test_every_kind_has_an_operator_sentence(self):
        from quantdesk.datahub.backfill import FAILURE_LABELS

        for kind in (RATE_LIMITED, TIMEOUT, NETWORK, UPSTREAM_ERROR, "invalid_symbol",
                     INVALID_INTERVAL, NO_DATA, BUDGET_EXHAUSTED, UNKNOWN):
            self.assertIn(kind, FAILURE_LABELS)
            self.assertTrue(FAILURE_LABELS[kind])


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def test_the_snapshot_names_the_range_the_bars_and_a_stable_version(self):
        venue = _Venue(listing_ts=NOW - 10 * HOUR)
        backfill = HistoryBackfill(self.db, venue.fetch, now=lambda: NOW)
        backfill.run("BTCUSDT", "1h", max_pages=5)
        first = backfill.snapshot("BTCUSDT", "1h")
        second = backfill.snapshot("BTCUSDT", "1h")
        self.assertEqual(first["bars"], 11)
        self.assertEqual(first["from_ts"], venue.listing_ts)
        self.assertTrue(first["version"])
        self.assertEqual(first["version"], second["version"], "同一份数据必须得到同一个版本")
        stored = self.db.list_history_snapshots(symbol="BTCUSDT")
        self.assertEqual(len(stored), 1, "相同版本只保留一条快照记录")
        self.assertEqual(self.db.find_history_snapshot(first["version"])["bars"], 11)

    def test_more_data_produces_a_different_version(self):
        venue = _Venue(listing_ts=NOW - 10 * HOUR)
        backfill = HistoryBackfill(self.db, venue.fetch, now=lambda: NOW)
        backfill.run("BTCUSDT", "1h", max_pages=5)
        before = backfill.snapshot("BTCUSDT", "1h")["version"]
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", [bar(venue.listing_ts - HOUR)],
                               source="venue_rest_backfill")
        after = backfill.snapshot("BTCUSDT", "1h")["version"]
        self.assertNotEqual(before, after)
        self.assertEqual(len(self.db.list_history_snapshots(symbol="BTCUSDT")), 2)

    def test_an_empty_symbol_says_so_instead_of_pinning_nothing(self):
        backfill = HistoryBackfill(self.db, _Venue(listing_ts=NOW).fetch, now=lambda: NOW)
        record = backfill.snapshot("ETHUSDT", "1h")
        self.assertFalse(record["available"])
        self.assertEqual(record["bars"], 0)
        self.assertIn("没有", record["reason"])

    def test_backfill_and_snapshot_returns_both_halves(self):
        venue = _Venue(listing_ts=NOW - 4 * HOUR)
        result = HistoryBackfill(self.db, venue.fetch, now=lambda: NOW).backfill_and_snapshot("BTCUSDT", "1h")
        self.assertTrue(result["outcome"]["complete"])
        self.assertTrue(result["snapshot"]["version"])


if __name__ == "__main__":
    unittest.main()


class BackfillApiTests(unittest.IsolatedAsyncioTestCase):
    """The read-only status surface an operator checks after a long walk."""

    async def asyncSetUp(self):
        import os

        from unittest.mock import patch

        import httpx

        from quantdesk.api.server import app

        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"QUANTDESK_HOME": self.tmp.name}, clear=False)
        self.env.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.env.stop()
        self.tmp.cleanup()

    async def test_the_status_lists_progress_and_a_readable_failure(self):
        db = Database(self.home / "quantdesk.db")
        db.upsert_backfill_state({
            "venue": "bybit", "symbol": "NVDAUSDT", "interval": "1h",
            "oldest_ts": 1, "newest_ts": 2, "complete": False, "pages": 4, "bars": 4000,
            "attempts": 2, "last_error": "429", "last_error_kind": "rate_limited",
        })
        db.record_history_snapshot({
            "venue": "bybit", "symbol": "NVDAUSDT", "interval": "1h", "version": "history/1:abc",
            "from_ts": 1, "to_ts": 2, "bars": 4000, "barsAvailable": 4000,
            "complete": True, "sources": {"venue_rest": 4000},
        })
        response = await self.client.get("/api/data/backfill?symbol=NVDAUSDT")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["states"][0]["last_error_kind"], "rate_limited")
        self.assertIn("限速", body["states"][0]["failureLabel"])
        self.assertEqual(body["snapshots"][0]["version"], "history/1:abc")
        self.assertEqual(body["snapshots"][0]["sources"], {"venue_rest": 4000})

    async def test_an_empty_database_reports_nothing_rather_than_failing(self):
        response = await self.client.get("/api/data/backfill")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["states"], [])
        self.assertIn("rate_limited", response.json()["failureKinds"])


class CumulativeAccountingTests(unittest.TestCase):
    """The saved state describes the whole walk, not just its latest run."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def test_pages_and_bars_accumulate_across_resumed_runs(self):
        venue = _Venue(listing_ts=NOW - 20_000 * HOUR)
        backfill = HistoryBackfill(self.db, venue.fetch, now=lambda: NOW, sleep=lambda _: None)
        first = backfill.run("BTCUSDT", "1h", max_pages=2)
        self.assertEqual(first.pages, 2)
        self.assertEqual(first.pages_total, 2)
        second = backfill.run("BTCUSDT", "1h", max_pages=3)
        self.assertEqual(second.pages, 3, "本次页数只算这一次")
        self.assertEqual(second.pages_total, 5, "累计页数延续上一次的断点")
        state = self.db.load_backfill_state("bybit", "BTCUSDT", "1h")
        self.assertEqual(state["pages"], 5)
        self.assertEqual(state["attempts"], 2)
        self.assertEqual(state["rows_available"], second.bars_available)

    def test_a_restart_starts_the_totals_over(self):
        venue = _Venue(listing_ts=NOW - 20_000 * HOUR)
        backfill = HistoryBackfill(self.db, venue.fetch, now=lambda: NOW, sleep=lambda _: None)
        backfill.run("BTCUSDT", "1h", max_pages=2)
        restarted = backfill.run("BTCUSDT", "1h", max_pages=1, restart=True)
        self.assertEqual(restarted.pages_total, 1)
        self.assertEqual(self.db.load_backfill_state("bybit", "BTCUSDT", "1h")["pages"], 1)


class ProvenanceAndAccountingTests(unittest.TestCase):
    """Where a bar came from, how it arrived, and what a write actually did."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def test_a_backfilled_bar_is_a_venue_rest_read_taken_by_a_walk(self):
        venue = _Venue(listing_ts=NOW - 3 * HOUR)
        HistoryBackfill(self.db, venue.fetch, now=lambda: NOW).run("BTCUSDT", "1h")
        rows = self.db.load_candles("bybit", "BTCUSDT", "1h")
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["source"], "venue_rest", "来源是交易所 REST 读数")
            self.assertEqual(row["ingestion_mode"], "backfill", "到达方式是历史回填")

    def test_the_backfill_source_string_is_no_longer_used_anywhere(self):
        venue = _Venue(listing_ts=NOW - 3 * HOUR)
        HistoryBackfill(self.db, venue.fetch, now=lambda: NOW).run("BTCUSDT", "1h")
        sources = {row["source"] for row in self.db.query("SELECT DISTINCT source FROM candles")}
        self.assertNotIn("venue_rest_backfill", sources)
        self.assertEqual(sources, {"venue_rest"})

    def test_legacy_backfill_rows_are_migrated_on_open(self):
        # A row written by the previous version carried its own source string.
        self.db.execute(
            "INSERT INTO candles (venue, symbol, interval, open_ts, open, high, low, close, volume, "
            " source, ingestion_mode, received_ts, collector) "
            "VALUES ('bybit','ETHUSDT','1h',3600000,1,1,1,1,1,'venue_rest_backfill',NULL,1,'collector/1')"
        )
        # Re-opening the database runs the migration.
        reopened = Database(self.db.path)
        row = reopened.load_candles("bybit", "ETHUSDT", "1h")[0]
        self.assertEqual(row["source"], "venue_rest")
        self.assertEqual(row["ingestion_mode"], "backfill")

    def test_every_source_is_ranked_so_none_is_silently_lowest(self):
        for source in ("venue_ws", "venue_rest", "local_derived", "imported"):
            self.assertIn(source, Database.SOURCE_RANKS,
                          f"{source} 必须有明确优先级，否则会被当成 unknown 而无法更新")

    def test_derived_rows_still_cannot_overwrite_venue_rows(self):
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", [bar(NOW)], source="venue_rest")
        report = self.db.upsert_candles(
            "bybit", "BTCUSDT", "1h", [dict(bar(NOW), close=99.0)], source="local_derived"
        )
        self.assertEqual(report.lower_rank, 1)
        self.assertEqual(report.written, 0)
        self.assertEqual(self.db.load_candles("bybit", "BTCUSDT", "1h")[0]["close"], 1.0)


class IdempotencyTests(unittest.TestCase):
    """Re-running a backfill must not invent work or duplicate rows."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def _run(self, venue: _Venue, **kwargs):
        return HistoryBackfill(self.db, venue.fetch, now=lambda: NOW, sleep=lambda _: None).run(
            "BTCUSDT", "1h", **kwargs
        )

    def test_repeating_the_same_walk_adds_nothing(self):
        venue = _Venue(listing_ts=NOW - 6 * HOUR)
        first = self._run(venue, max_pages=5)
        before = self.db.count_candles("bybit", "BTCUSDT", "1h")
        second = self._run(venue, max_pages=5, restart=True)
        self.assertGreater(first.bars_stored, 0)
        self.assertEqual(second.bars_stored, 0, "重复回填不写入任何行")
        self.assertEqual(second.bars_unchanged, first.bars_stored)
        self.assertEqual(self.db.count_candles("bybit", "BTCUSDT", "1h"), before)

    def test_overlapping_pages_do_not_inflate_the_available_count(self):
        venue = _Venue(listing_ts=NOW - 40 * HOUR)
        backfill = HistoryBackfill(self.db, venue.fetch, now=lambda: NOW, sleep=lambda _: None)
        first = backfill.run("BTCUSDT", "1h", max_pages=2)
        # Resume with a window that deliberately overlaps what is already stored.
        second = backfill.run("BTCUSDT", "1h", max_pages=2)
        snapshot = backfill.snapshot("BTCUSDT", "1h")
        stored = self.db.count_candles("bybit", "BTCUSDT", "1h")
        self.assertEqual(snapshot["barsAvailable"], stored)
        self.assertEqual(snapshot["bars"], stored)
        self.assertLessEqual(second.bars_stored, stored)
        self.assertGreaterEqual(first.bars_available, 0)

    def test_bars_available_is_the_distinct_row_count_not_a_running_total(self):
        venue = _Venue(listing_ts=NOW - 5 * HOUR)
        outcome = self._run(venue, max_pages=5)
        self.assertEqual(outcome.bars_available, self.db.count_candles("bybit", "BTCUSDT", "1h"))
        # Three runs, one series: the count never grows past the real row count.
        for _ in range(3):
            self._run(venue, max_pages=5, restart=True)
        self.assertEqual(self.db.count_candles("bybit", "BTCUSDT", "1h"), outcome.bars_available)

    def test_an_interrupted_then_resumed_walk_stores_each_bar_once(self):
        # Deep enough that page one is full, so the walk actually reaches page two
        # and the injected failure lands mid-walk.
        venue = _Venue(listing_ts=NOW - 1200 * HOUR, fail_on=2)
        first = self._run(venue, max_pages=5)
        self.assertFalse(first.complete)
        venue.fail_on = None
        after_failure = self.db.count_candles("bybit", "BTCUSDT", "1h")
        resumed = self._run(venue, max_pages=5)
        total = self.db.count_candles("bybit", "BTCUSDT", "1h")
        self.assertGreater(total, after_failure, "断点续传继续向下回溯")
        self.assertEqual(resumed.bars_available, total)
        # No bar was written twice: the row count equals the distinct timestamps.
        distinct = self.db.query(
            "SELECT COUNT(DISTINCT open_ts) AS n FROM candles WHERE venue='bybit' AND symbol='BTCUSDT'"
        )[0]["n"]
        self.assertEqual(distinct, total)
