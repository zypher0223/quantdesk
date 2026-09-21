"""The batch backfill queue: matrix, limits, controls, progress.

The matrix is the thing an operator looks at after a long run, so these tests
check the numbers it reports (pages, rows available, progress, what is left),
the decisions it has to honour (pause, cancel, retry), and that a browser refresh
shows the same board - which it does because the board lives in the database.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from quantdesk.datahub.db import Database
from quantdesk.datahub.history import (
    FUNDING,
    MARK_CANDLE,
    OPEN_INTEREST,
    RISK_LIMIT,
    TRADE_CANDLE,
    HistoryCollector,
)
from quantdesk.datahub.tasks import (
    DEFAULT_TIMEFRAMES,
    BackfillQueue,
    BackfillWorker,
    TokenBucket,
    pages_estimate,
)

HOUR = 3_600_000
NOW = 1_700_000_000_000 - (1_700_000_000_000 % HOUR)
LISTING = NOW - 600 * HOUR          # a contract with 600 hours of life
STEP = {"15m": 900_000, "1h": HOUR, "4h": 4 * HOUR, "1d": 24 * HOUR}


class StubClient:
    """A venue with finite history that starts at the contract's listing date."""

    def __init__(self, *, symbols=("BTCUSDT", "ETHUSDT"), launch_ts=LISTING, funding=True, tiers=True):
        self.symbols = symbols
        self.launch_ts = launch_ts
        self.funding = funding
        self.tiers = tiers
        self.pages = 0

    def instruments(self, category, symbol=None):
        return [{
            "symbol": symbol, "launchTime": str(self.launch_ts), "contractType": "LinearPerpetual",
            "status": "Trading", "fundingInterval": 480,
            "priceFilter": {"tickSize": "0.1"}, "lotSizeFilter": {"qtyStep": "0.001"},
        }]

    def _window(self, start, end, step):
        first = max(start, self.launch_ts)
        first = ((first + step - 1) // step) * step
        if first > end:
            return []
        self.pages += 1
        return first

    def kline(self, category, symbol, interval, start, end, max_bars=1000):
        step = STEP[interval]
        first = self._window(start, end, step)
        if not first:
            return []
        return [{"ts": t, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
                for t in range(first, end + 1, step)][-1000:]

    def mark_price_kline(self, symbol, interval, *, limit=1000, category="linear",
                         start_ms=None, end_ms=None, completed_only=True):
        step = STEP[interval]
        first = self._window(start_ms, end_ms, step)
        if not first:
            return []
        return [{"ts": t, "open": 2, "high": 2, "low": 2, "close": 2}
                for t in range(first, end_ms + 1, step)][-1000:]

    def funding_history_window(self, symbol, start_ms, end_ms, limit=200):
        if not self.funding:
            return []
        first = self._window(start_ms, end_ms, 8 * HOUR)
        if not first:
            return []
        return [{"ts": t, "rate": 0.0001} for t in range(first, end_ms + 1, 8 * HOUR)][-200:]

    def open_interest_window(self, symbol, *, interval_time="1h", start_ms, end_ms, limit=200):
        first = self._window(start_ms, end_ms, HOUR)
        if not first:
            return []
        return [{"ts": t, "oi": 1234.5} for t in range(first, end_ms + 1, HOUR)][-200:]

    def risk_limit(self, symbol, category="linear"):
        if not self.tiers:
            return []
        return [{"riskLimitValue": "300000", "maintenanceMargin": "0.5", "maxLeverage": "150"}]


class QueueFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")
        self.client = StubClient()

    def _queue(self, **kwargs) -> BackfillQueue:
        def factory():
            return HistoryCollector(self.db, self.client, now=lambda: NOW, sleep=lambda _: None)

        return BackfillQueue(
            self.db, factory, concurrency=kwargs.pop("concurrency", 2),
            pages_per_minute=kwargs.pop("pages_per_minute", 6000),
            pages_per_run=kwargs.pop("pages_per_run", 20),
            sleep=lambda _: None, now=lambda: NOW, **kwargs,
        )

    def _meta(self, symbols=("BTCUSDT", "ETHUSDT")):
        collector = HistoryCollector(self.db, self.client, now=lambda: NOW, sleep=lambda _: None)
        for symbol in symbols:
            collector.collect_instrument_meta(symbol)

    def _clocked_queue(self, **kwargs):
        """A queue whose cool-down clock the test advances by hand."""
        self.clock = {"t": 1000.0}

        def factory():
            return HistoryCollector(self.db, self.client, now=lambda: NOW, sleep=lambda _: None)

        return BackfillQueue(
            self.db, factory, concurrency=kwargs.pop("concurrency", 1),
            pages_per_minute=kwargs.pop("pages_per_minute", 6000),
            pages_per_run=kwargs.pop("pages_per_run", 20),
            sleep=lambda _: None, now=lambda: NOW, clock=lambda: self.clock["t"], **kwargs,
        )


class MatrixTests(QueueFixture):
    def test_the_full_matrix_is_68_candle_tasks_plus_the_symbol_series(self):
        # The report's matrix: 17 contracts x 4 timeframes of candles.
        symbols = [f"S{i}USDT" for i in range(17)]
        self.client.symbols = symbols
        self._meta(symbols)
        queue = self._queue()
        queue.build_matrix(symbols=symbols)
        tasks = self.db.list_backfill_tasks()
        candles = [t for t in tasks if t["data_kind"] == TRADE_CANDLE]
        marks = [t for t in tasks if t["data_kind"] == MARK_CANDLE]
        self.assertEqual(len(candles), 68)
        self.assertEqual(len(marks), 68)
        self.assertEqual(len([t for t in tasks if t["data_kind"] == FUNDING]), 17)
        self.assertEqual(len([t for t in tasks if t["data_kind"] == OPEN_INTEREST]), 17)
        self.assertEqual(len([t for t in tasks if t["data_kind"] == RISK_LIMIT]), 17)
        self.assertEqual({t["interval"] for t in candles}, set(DEFAULT_TIMEFRAMES))

    def test_building_the_matrix_twice_does_not_duplicate_tasks(self):
        self._meta(("BTCUSDT",))
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT"])
        before = len(self.db.list_backfill_tasks())
        queue.build_matrix(symbols=["BTCUSDT"])
        self.assertEqual(len(self.db.list_backfill_tasks()), before)

    def test_a_finished_task_is_not_reset_by_rebuilding_unless_asked(self):
        self._meta(("BTCUSDT",))
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(RISK_LIMIT,))
        task = self.db.list_backfill_tasks()[0]
        queue.run_task(task["id"])
        self.assertEqual(self.db.get_backfill_task(task["id"])["status"], "done")
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(RISK_LIMIT,))
        self.assertEqual(self.db.get_backfill_task(task["id"])["status"], "done", "重建矩阵不重跑已完成任务")
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(RISK_LIMIT,), reset=True)
        self.assertEqual(self.db.get_backfill_task(task["id"])["status"], "pending")

    def test_the_page_estimate_comes_from_the_listing_date(self):
        self.assertEqual(pages_estimate("1h", launch_ts=NOW - 600 * HOUR, now_ms=NOW), 1)
        self.assertEqual(pages_estimate("15m", launch_ts=NOW - 600 * HOUR, now_ms=NOW), 3)
        self.assertEqual(pages_estimate("1h", launch_ts=None, now_ms=NOW), 0)
        self.assertEqual(pages_estimate("1h", launch_ts=NOW - 100 * HOUR, now_ms=NOW, pages_done=1), 0)


class DrainTests(QueueFixture):
    def test_resumed_task_pages_are_not_accumulated_twice(self):
        self.client = StubClient(symbols=("BTCUSDT",), launch_ts=NOW - 5_000 * HOUR)
        self._meta(("BTCUSDT",))
        queue = self._queue(pages_per_run=1)
        queue.build_matrix(symbols=["BTCUSDT"], timeframes=["1h"], data_kinds=(TRADE_CANDLE,))
        task_id = queue.status()["tasks"][0]["id"]
        queue.run_task(task_id)
        self.assertEqual(queue.status()["tasks"][0]["pages"], 1)
        queue.run_task(task_id)
        task = queue.status()["tasks"][0]
        state = self.db.load_backfill_state("bybit", "BTCUSDT", "1h", TRADE_CANDLE)
        self.assertEqual(task["pages"], 2)
        self.assertEqual(task["pages"], state["pages"])

    def test_the_whole_matrix_drains_and_every_task_is_classified(self):
        self._meta()
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT", "ETHUSDT"])
        asyncio.run(queue.run_pending())
        tasks = queue.status()["tasks"]
        self.assertTrue(tasks)
        unfinished = [t for t in tasks if t["status"] not in ("done", "unsupported")]
        self.assertEqual(unfinished, [], "全部任务都应有明确状态")
        for task in tasks:
            self.assertGreaterEqual(task["rowsAvailable"], 0)
            if task["status"] == "done" and task["dataKind"] in (TRADE_CANDLE, MARK_CANDLE):
                self.assertGreater(task["rowsAvailable"], 0, task["kindLabel"])

    def test_progress_is_reported_per_task_and_in_total(self):
        self._meta()
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT"])
        asyncio.run(queue.run_pending())
        board = queue.status()
        # One contract: 4 candle series + 4 mark series + funding + OI + ladder.
        self.assertEqual(board["summary"]["total"], 4 + 4 + 3)
        self.assertGreater(board["summary"]["finished"], 0)
        for task in board["tasks"]:
            self.assertLessEqual(task["progressPct"], 100.0)
            self.assertGreaterEqual(task["progressPct"], 0.0)
            self.assertIn(task["status"], Database.TASK_STATUSES)
            self.assertTrue(task["kindLabel"])

    def test_the_board_survives_a_restart_because_it_is_in_the_database(self):
        self._meta()
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(TRADE_CANDLE, FUNDING))
        asyncio.run(queue.run_pending())
        first = queue.status()["tasks"]
        rebuilt = self._queue().status()["tasks"]
        self.assertEqual(
            [(t["symbol"], t["dataKind"], t["status"], t["rowsAvailable"]) for t in first],
            [(t["symbol"], t["dataKind"], t["status"], t["rowsAvailable"]) for t in rebuilt],
        )

    def test_a_retryable_failure_waits_its_turn_before_giving_up(self):
        class Flaky(StubClient):
            def funding_history_window(self, symbol, start_ms, end_ms, limit=200):
                raise TimeoutError("read timed out")

        self.client = Flaky()
        self._meta(("BTCUSDT",))
        queue = self._clocked_queue()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(TRADE_CANDLE, FUNDING))
        asyncio.run(queue.run_pending())
        tasks = {t["dataKind"]: t for t in queue.status()["tasks"]}
        self.assertEqual(tasks[TRADE_CANDLE]["status"], "done")
        funding = tasks[FUNDING]
        self.assertEqual(funding["status"], "pending", "超时是可重试的，不该立刻判死")
        self.assertEqual(funding["failureKind"], "timeout")
        self.assertIn("超时", funding["failureLabel"])
        self.assertEqual(funding["failureAttempts"], 1)
        self.assertIn("后重试", funding["failure"])

        # 冷却期内不再重跑，也不空转。
        board = asyncio.run(queue.run_pending())
        self.assertEqual(board["ran"], 0)
        self.assertEqual(
            {t["dataKind"]: t["status"] for t in queue.status()["tasks"]}[FUNDING], "pending")

        self.clock["t"] += 5.0
        asyncio.run(queue.run_pending())
        second = {t["dataKind"]: t for t in queue.status()["tasks"]}[FUNDING]
        self.assertEqual(second["failureAttempts"], 2)
        self.assertEqual(second["status"], "pending")

        self.clock["t"] += 60.0
        asyncio.run(queue.run_pending())
        last = {t["dataKind"]: t for t in queue.status()["tasks"]}[FUNDING]
        self.assertEqual(last["failureAttempts"], 3, "重试预算用满")
        self.assertEqual(last["status"], "failed")

    def test_an_exception_escaping_the_walk_is_classified_and_retried(self):
        import httpx

        class Broken:
            def collect_instrument_meta(self, symbol):
                return {}

            def run(self, *args, **kwargs):
                raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING]")

            def snapshot(self, *args, **kwargs):
                return None

        self._meta(("BTCUSDT",))
        queue = self._clocked_queue()
        queue._collector_factory = lambda: Broken()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(FUNDING,))
        asyncio.run(queue.run_pending())
        task = queue.status()["tasks"][0]
        self.assertEqual(task["failureKind"], "network")
        self.assertEqual(task["status"], "pending", "网络抖动应留待重试，而不是直接 failed")
        self.assertEqual(task["failureAttempts"], 1)
        self.assertIn("网络不可达", task["failureLabel"])

    def test_a_refused_series_is_not_retried(self):
        class Refusing(StubClient):
            def funding_history_window(self, symbol, start_ms, end_ms, limit=200):
                raise ValueError("unsupported interval")

        self.client = Refusing()
        self._meta(("BTCUSDT",))
        queue = self._clocked_queue()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(FUNDING,))
        asyncio.run(queue.run_pending())
        task = queue.status()["tasks"][0]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["failureAttempts"], 1, "不可重试的失败不消耗重试预算")

    def test_an_unsupported_series_is_marked_not_failed(self):
        class NoFunding(StubClient):
            def funding_history_window(self, symbol, start_ms, end_ms, limit=200):
                return []

        self.client = NoFunding()
        self._meta(("BTCUSDT",))
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(FUNDING,))
        asyncio.run(queue.run_pending())
        task = queue.status()["tasks"][0]
        self.assertEqual(task["status"], "unsupported")
        self.assertTrue(task["reason"])


class ControlTests(QueueFixture):
    def test_a_retry_requeues_a_failed_task(self):
        class Flaky(StubClient):
            fail = True

            def funding_history_window(self, symbol, start_ms, end_ms, limit=200):
                if self.fail:
                    raise TimeoutError("read timed out")
                return super().funding_history_window(symbol, start_ms, end_ms, limit=limit)

        self.client = Flaky()
        self._meta(("BTCUSDT",))
        queue = self._clocked_queue()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(FUNDING,))
        task_id = queue.status()["tasks"][0]["id"]
        # 三次可重试的失败把重试预算用满，任务才会落到 failed。
        for _ in range(3):
            queue.run_task(task_id)
            self.clock["t"] += 60.0
        self.assertEqual(self.db.get_backfill_task(task_id)["status"], "failed")
        self.client.fail = False
        queue.retry(task_id)
        self.assertEqual(self.db.get_backfill_task(task_id)["status"], "pending")
        queue.run_task(task_id)
        self.assertEqual(self.db.get_backfill_task(task_id)["status"], "done")

    def test_pausing_the_queue_stops_the_drain(self):
        self._meta()
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT"])
        queue.pause_all()
        result = asyncio.run(queue.run_pending())
        self.assertTrue(result["paused"])
        self.assertEqual(result["ran"], 0)
        self.assertTrue(all(t["status"] == "paused" for t in queue.status()["tasks"]))

    def test_resuming_puts_the_tasks_back(self):
        self._meta(("BTCUSDT",))
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(RISK_LIMIT,))
        queue.pause_all()
        queue.resume_all()
        self.assertFalse(queue.status()["paused"])
        self.assertEqual(queue.status()["tasks"][0]["status"], "pending")
        asyncio.run(queue.run_pending())
        self.assertEqual(queue.status()["tasks"][0]["status"], "done")

    def test_cancelling_one_task_leaves_the_others_alone(self):
        self._meta(("BTCUSDT",))
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(RISK_LIMIT, FUNDING))
        target = [t for t in queue.status()["tasks"] if t["dataKind"] == FUNDING][0]
        queue.cancel(target["id"])
        self.assertEqual(queue.db.get_backfill_task(target["id"])["status"], "cancelled")
        asyncio.run(queue.run_pending())
        tasks = {t["dataKind"]: t["status"] for t in queue.status()["tasks"]}
        self.assertEqual(tasks[FUNDING], "cancelled")
        self.assertEqual(tasks[RISK_LIMIT], "done", "取消一个任务不影响其它任务")

    def test_a_paused_task_is_not_claimed_by_the_drain(self):
        self._meta(("BTCUSDT",))
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(RISK_LIMIT,))
        task_id = queue.status()["tasks"][0]["id"]
        queue.pause(task_id)
        asyncio.run(queue.run_pending())
        self.assertEqual(queue.db.get_backfill_task(task_id)["status"], "paused")
        queue.resume(task_id)
        asyncio.run(queue.run_pending())
        self.assertEqual(queue.db.get_backfill_task(task_id)["status"], "done")

    def test_an_unknown_task_id_is_refused(self):
        with self.assertRaises(KeyError):
            self._queue().cancel(999)


class RateLimitTests(unittest.TestCase):
    def test_the_bucket_allows_a_burst_then_makes_the_caller_wait(self):
        now = [0.0]
        bucket = TokenBucket(60, now=lambda: now[0])
        for _ in range(60):
            self.assertEqual(bucket.take(1), 0.0)
        wait = bucket.take(1)
        self.assertGreater(wait, 0, "超过限速后必须等待")
        now[0] += wait
        self.assertAlmostEqual(bucket.tokens_available, 0.0, places=6, msg="等待为刚才的请求预留一个令牌")
        self.assertGreater(bucket.take(1), 0.0, "同一时刻的下一页仍需等待，不能并发穿透限速")

    def test_the_bucket_refills_over_a_minute(self):
        now = [0.0]
        bucket = TokenBucket(60, now=lambda: now[0])
        bucket.take(60)
        now[0] += 30.0
        self.assertAlmostEqual(bucket.tokens_available, 30.0, places=1)


class BackgroundWorkerTests(QueueFixture):
    def test_startup_recovers_an_interrupted_claim_and_drains_it(self):
        self._meta(("BTCUSDT",))
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(RISK_LIMIT,))
        task_id = queue.status()["tasks"][0]["id"]
        self.db.update_backfill_task(task_id, status="running")

        async def exercise() -> None:
            worker = BackfillWorker(queue, idle_seconds=0.25)
            await worker.start()
            try:
                for _ in range(100):
                    if self.db.get_backfill_task(task_id)["status"] == "done":
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(worker.status()["workerRunning"])
                self.assertEqual(self.db.get_backfill_task(task_id)["status"], "done")
            finally:
                await worker.stop()
            self.assertFalse(worker.status()["workerRunning"])

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()


class ProgressHonestyTests(QueueFixture):
    """Progress must not read as finished while history is still being fetched."""

    def test_an_unfinished_task_never_reports_full_progress(self):
        self._meta(("BTCUSDT",))
        queue = self._queue(pages_per_run=2)
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(FUNDING,))
        task_id = queue.status()["tasks"][0]["id"]
        for _ in range(6):
            queue.run_task(task_id)
            task = queue.status()["tasks"][0]
            if task["status"] != "pending":
                break
            self.assertLess(task["progressPct"], 100.0,
                            "仍在回溯的任务不能显示 100%")
            self.assertFalse(task["complete"])

    def test_a_finished_task_reports_complete(self):
        self._meta(("BTCUSDT",))
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(RISK_LIMIT,))
        task_id = queue.status()["tasks"][0]["id"]
        # Completion overrides an imperfect listing-date estimate.
        self.db.update_backfill_task(task_id, pages_estimate=3)
        queue.run_task(task_id)
        task = queue.status()["tasks"][0]
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["progressPct"], 100.0)
        self.assertTrue(task["complete"])
        self.assertEqual(task["pagesRemaining"], 0)
        self.assertFalse(task["estimateExhausted"])

    def test_an_exhausted_estimate_is_flagged_rather_than_shown_as_done(self):
        self._meta(("BTCUSDT",))
        queue = self._queue(pages_per_run=1)
        # An estimate of zero with history still to fetch is exactly the case that
        # used to render as 100%.
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(FUNDING,))
        task_id = queue.status()["tasks"][0]["id"]
        self.db.update_backfill_task(task_id, pages_estimate=0)
        queue.run_task(task_id)
        task = queue.status()["tasks"][0]
        if task["status"] == "pending":
            self.assertTrue(task["estimateExhausted"])
            self.assertLess(task["progressPct"], 100.0)


class BacktestableRangeTests(QueueFixture):
    """The panel's question: what can be studied *now*, and what is still missing."""

    def test_event_series_are_not_accused_of_having_gaps(self):
        """资金费率/持仓量/风险档位没有固定周期：“区间无缺口”对它们不适用。"""
        from quantdesk.datahub.readiness import backtestable_ranges

        settlements = [
            {"ts": NOW - index * 8 * HOUR, "rate": 0.0001, "symbol": "BTCUSDT",
             "source": "venue_rest"}
            for index in range(40)
        ]
        self.db.upsert_funding("bybit", "BTCUSDT", settlements)
        rows = {
            row["dataKind"]: row
            for row in backtestable_ranges(
                self.db, symbols=["BTCUSDT"], intervals=("1h",), kinds=(FUNDING,)
            )["rows"]
        }
        funding = rows[FUNDING]
        self.assertEqual(funding["status"], "ok")
        self.assertIsNone(funding["gapFree"], "事件序列不做逐根连续性判断")
        self.assertFalse(funding["hasGaps"])
        self.assertEqual(funding["usableBars"], funding["barsAvailable"])
        self.assertIn("结算事件", funding["reason"])

    def test_a_gap_free_range_is_reported_separately_from_reaching_the_listing(self):
        # The live situation: a continuous recent window, but the contract listed
        # long before it. "Usable" and "complete" are different answers.
        from quantdesk.datahub.readiness import backtestable_ranges

        rows = [
            {"ts": NOW - index * HOUR, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
             "source": "venue_rest"}
            for index in range(200)
        ]
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", rows, source="venue_rest")
        self.db.upsert_instrument_meta({
            "venue": "bybit", "symbol": "BTCUSDT", "launch_ts": NOW - 5000 * HOUR,
        })
        result = backtestable_ranges(self.db, symbols=["BTCUSDT"], intervals=("1h",),
                                     kinds=(TRADE_CANDLE,))
        row = result["rows"][0]
        self.assertTrue(row["gapFree"], "连续段应被识别")
        self.assertEqual(row["usableBars"], row["barsAvailable"])
        self.assertFalse(row["reachedListing"])
        self.assertEqual(row["status"], "not_reached_listing")
        # ...and once the walk really does reach the listing, the verdict changes.
        self.db.upsert_candles(
            "bybit", "BTCUSDT", "1h",
            [{"ts": NOW - 5000 * HOUR, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
            source="venue_rest",
        )
        after = backtestable_ranges(self.db, symbols=["BTCUSDT"], intervals=("1h",),
                                    kinds=(TRADE_CANDLE,))["rows"][0]
        self.assertTrue(after["reachedListing"])
        self.assertEqual(after["status"], "gapped", "中间的大洞必须仍然可见")

    def test_a_hole_shrinks_the_usable_range_but_not_the_stored_count(self):
        from quantdesk.datahub.readiness import backtestable_ranges

        self._meta(("BTCUSDT",))
        rows = [
            {"ts": NOW - index * HOUR, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
            for index in range(50)
        ]
        rows = [row for row in rows if row["ts"] != NOW - 10 * HOUR]   # one missing bar
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", rows, source="venue_rest")
        result = backtestable_ranges(self.db, symbols=["BTCUSDT"], intervals=("1h",),
                                     kinds=(TRADE_CANDLE,))
        row = result["rows"][0]
        self.assertEqual(row["barsAvailable"], 49)
        self.assertEqual(row["usableBars"], 10, "缺口之后到最新一根是可用区间")
        # The usable window is itself unbroken, but the history behind it is not:
        # those are the two facts the panel must not merge.
        self.assertTrue(row["gapFree"])
        self.assertTrue(row["hasGaps"])
        self.assertEqual(row["status"], "gapped")

    def test_an_unsupported_family_says_why_instead_of_showing_zero(self):
        from quantdesk.datahub.readiness import backtestable_ranges

        self.db.upsert_backfill_state({
            "venue": "bybit", "symbol": "SPCXUSDT", "interval": "", "data_kind": FUNDING,
            "status": "unsupported", "reason": "交易所元数据未给出资金费结算周期",
        })
        result = backtestable_ranges(self.db, symbols=["SPCXUSDT"], intervals=("1h",), kinds=(FUNDING,))
        row = result["rows"][0]
        self.assertEqual(row["status"], "unsupported")
        self.assertIn("资金费结算周期", row["reason"])
        self.assertEqual(row["barsAvailable"], 0)
        self.assertEqual(result["summary"]["unsupported"], 1)

    def test_bars_available_is_the_database_count_not_the_task_counter(self):
        self._meta(("BTCUSDT",))
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(TRADE_CANDLE,), timeframes=("1h",))
        asyncio.run(queue.run_pending())
        task = queue.status()["tasks"][0]
        stored = self.db.count_candles("bybit", "BTCUSDT", "1h")
        self.assertEqual(task["rowsAvailable"], stored)
        ranged = queue.ranges(symbol="BTCUSDT")["rows"][0]
        self.assertEqual(ranged["barsAvailable"], stored)

    def test_the_board_only_computes_ranges_when_asked(self):
        self._meta(("BTCUSDT",))
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(TRADE_CANDLE,), timeframes=("1h",))
        self.assertNotIn("ranges", queue.status())
        self.assertIn("ranges", queue.status(include_ranges=True))


class ReadinessFollowsTheDataTests(QueueFixture):
    """Acceptance: the verdict changes as the data is actually filled in."""

    def _assess(self, spec_symbol="BTCUSDT", interval="1h", bars=200):
        from quantdesk.config.instruments import require_instrument
        from quantdesk.datahub.readiness import assess, window_for

        spec = require_instrument(spec_symbol)
        from_ts, to_ts = window_for(self.db, venue="bybit", symbol=spec_symbol,
                                    interval=interval, bars=bars)
        return assess(self.db, venue="bybit", symbol=spec_symbol, interval=interval,
                      from_ts=from_ts, to_ts=to_ts, product_type=spec.product_type)

    def test_the_gate_opens_only_once_every_family_is_present(self):
        import time

        from quantdesk.risk import RiskProfile, tier_rows_for_db

        interval, bars = "1h", 200
        step = HOUR
        stored = NOW
        rows = [
            {"ts": stored - index * step, "open": 100.0, "high": 101.0, "low": 99.0,
             "close": 100.5, "volume": 10.0, "source": "venue_rest"}
            for index in range(bars)
        ]
        self.db.upsert_candles("bybit", "BTCUSDT", interval, rows)
        self.db.upsert_instrument_meta({"venue": "bybit", "symbol": "BTCUSDT",
                                        "launch_ts": rows[-1]["ts"]})
        # Candles alone are not enough: marks price liquidation, so they are blocking.
        first = self._assess()
        self.assertFalse(first.ok)
        self.assertIn("marks", [check.key for check in first.blocking])

        self.db.upsert_mark_candles("bybit", "BTCUSDT", interval,
                                    [{"ts": row["ts"], "open": 1, "high": 1, "low": 1, "close": 1}
                                     for row in rows])
        self.db.upsert_funding("bybit", "BTCUSDT",
                               [{"ts": row["ts"], "rate": 0.0001} for row in rows[::8]])
        stamp = int(time.time() * 1000)
        profile = RiskProfile.from_rows(
            "BTCUSDT", [{"riskLimitValue": "300000", "maintenanceMargin": "0.5", "maxLeverage": "150"}],
            synced_at=stamp,
        )
        self.db.upsert_risk_tiers("bybit", "BTCUSDT", tier_rows_for_db(profile),
                                  source=profile.source, synced_at=stamp)
        for kind, kind_interval, version in (
            ("trade_candle", interval, "history/1:btc"),
            ("mark_candle", interval, "series/1:btc-marks"),
            ("funding", "", "series/1:btc-funding"),
            ("risk_limit", "", "series/1:btc-risk"),
        ):
            self.db.record_history_snapshot({
                "venue": "bybit", "symbol": "BTCUSDT", "interval": kind_interval,
                "data_kind": kind, "version": version,
                "from_ts": rows[-1]["ts"], "to_ts": rows[0]["ts"], "bars": len(rows),
            })
        second = self._assess()
        self.assertTrue(second.ok, [check.detail for check in second.blocking])
        self.assertFalse(second.degraded, [check.detail for check in second.checks
                                           if check.status == "degraded"])


class SnapshotOnCompletionTests(QueueFixture):
    """A finished series must be citable, or a study stays degraded for ever."""

    def test_a_completed_task_pins_its_series_version(self):
        self._meta(("BTCUSDT",))
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(TRADE_CANDLE, MARK_CANDLE, FUNDING),
                           timeframes=("1h",))
        asyncio.run(queue.run_pending())
        for kind, interval in ((TRADE_CANDLE, "1h"), (MARK_CANDLE, "1h"), (FUNDING, "")):
            rows = self.db.list_history_snapshots(symbol="BTCUSDT", data_kind=kind, limit=1)
            self.assertTrue(rows, f"{kind} 完成后必须有快照版本")
            self.assertTrue(rows[0]["version"])
            self.assertEqual(rows[0]["interval"], interval)
        self.assertEqual(queue.status()["summary"]["byStatus"].get("done"), 3)

    def test_the_pinned_version_is_stable_across_a_second_run(self):
        self._meta(("BTCUSDT",))
        queue = self._queue()
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(TRADE_CANDLE,), timeframes=("1h",))
        asyncio.run(queue.run_pending())
        first = self.db.list_history_snapshots(symbol="BTCUSDT", data_kind=TRADE_CANDLE, limit=1)[0]
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(TRADE_CANDLE,), timeframes=("1h",),
                           reset=True)
        asyncio.run(queue.run_pending())
        rows = self.db.list_history_snapshots(symbol="BTCUSDT", data_kind=TRADE_CANDLE)
        self.assertEqual(len(rows), 1, "同一份数据只应留下一个版本")
        self.assertEqual(rows[0]["version"], first["version"])


class RestartResumeTests(QueueFixture):
    """Acceptance: a restart continues from the frontier instead of starting over."""

    def test_an_interrupted_walk_continues_after_a_restart(self):
        # Deep enough that two pages cannot finish it, so the first pass really
        # does stop mid-walk and the restart has something to continue.
        self.client = StubClient(launch_ts=NOW - 5000 * HOUR)
        self._meta(("BTCUSDT",))
        queue = self._queue(pages_per_run=2)
        queue.build_matrix(symbols=["BTCUSDT"], data_kinds=(TRADE_CANDLE,), timeframes=("1h",))
        task_id = queue.status()["tasks"][0]["id"]
        queue.run_task(task_id)
        after_first = self.db.get_backfill_task(task_id)
        self.assertEqual(after_first["status"], "pending", "页数预算内未走完")

        # A restart: the worker releases any `running` row back to the queue.
        async def restart() -> None:
            worker = BackfillWorker(queue, idle_seconds=0.2)
            await worker.start()
            try:
                for _ in range(200):
                    if self.db.get_backfill_task(task_id)["status"] in ("done", "failed"):
                        break
                    await asyncio.sleep(0.01)
            finally:
                await worker.stop()

        asyncio.run(restart())
        final = self.db.get_backfill_task(task_id)
        self.assertEqual(final["status"], "done")
        self.assertGreater(final["pages"], after_first["pages"], "重启后继续向前回溯")
        self.assertEqual(final["rows_available"], self.db.count_candles("bybit", "BTCUSDT", "1h"))
        state = self.db.load_backfill_state("bybit", "BTCUSDT", "1h", TRADE_CANDLE)
        self.assertTrue(state["complete"])


class ScanLimitTests(unittest.TestCase):
    """A bounded contiguity scan must not be reported as a hole."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def test_a_whole_contiguous_series_needs_no_walk(self):
        """行数与跨度一致时整段都被证明连续，不该被扫描上限截短。"""
        from quantdesk.datahub.readiness import backtestable_ranges

        rows = [
            {"ts": NOW - index * HOUR, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
             "source": "venue_rest"}
            for index in range(500)
        ]
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", rows, source="venue_rest")
        row = backtestable_ranges(self.db, symbols=["BTCUSDT"], intervals=("1h",),
                                  kinds=(TRADE_CANDLE,), scan_limit=100)["rows"][0]
        self.assertEqual(row["barsAvailable"], 500)
        self.assertEqual(row["usableBars"], 500)
        self.assertFalse(row["scanLimitReached"])
        self.assertTrue(row["gapFree"])
        self.assertFalse(row["hasGaps"])
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["reason"], "")

    def test_only_the_walk_is_bounded_when_a_hole_sits_far_behind(self):
        """缺口在扫描窗口之外时，只报出被证明的尾部并注明边界。"""
        from quantdesk.datahub.readiness import backtestable_ranges

        rows = [
            {"ts": NOW - index * HOUR, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
             "source": "venue_rest"}
            for index in range(500)
        ]
        rows = [row for row in rows if row["ts"] != NOW - 300 * HOUR]
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", rows, source="venue_rest")
        row = backtestable_ranges(self.db, symbols=["BTCUSDT"], intervals=("1h",),
                                  kinds=(TRADE_CANDLE,), scan_limit=100)["rows"][0]
        self.assertEqual(row["barsAvailable"], 499)
        self.assertEqual(row["usableBars"], 100, "被证明的只有扫描到的尾部")
        self.assertTrue(row["scanLimitReached"])
        self.assertTrue(row["gapFree"], "已证明的窗口自身无缺口")
        self.assertFalse(row["hasGaps"], "上限不是缺口")
        self.assertEqual(row["status"], "ok")
        self.assertIn("仅扫描最近 100 根", row["reason"])

    def test_a_real_hole_inside_the_scanned_window_is_still_reported(self):
        from quantdesk.datahub.readiness import backtestable_ranges

        rows = [
            {"ts": NOW - index * HOUR, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
             "source": "venue_rest"}
            for index in range(500)
        ]
        rows = [row for row in rows if row["ts"] != NOW - 20 * HOUR]
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", rows, source="venue_rest")
        row = backtestable_ranges(self.db, symbols=["BTCUSDT"], intervals=("1h",),
                                  kinds=(TRADE_CANDLE,), scan_limit=100)["rows"][0]
        self.assertTrue(row["hasGaps"])
        self.assertEqual(row["status"], "gapped")
        self.assertEqual(row["usableBars"], 20)


class SnapshotPinTests(QueueFixture):
    """一条有数据但没有版本的序列，应当能就地固定版本，且不访问交易所。"""

    def test_pinning_records_the_version_of_what_is_stored(self):
        from quantdesk.datahub.history import HistoryCollector

        self._meta(("BTCUSDT",))
        rows = [
            {"ts": NOW - index * HOUR, "rate": 0.0001} for index in range(40)
        ]
        self.db.upsert_funding("bybit", "BTCUSDT", rows)
        collector = HistoryCollector(self.db, None)
        record = collector.snapshot("BTCUSDT", FUNDING, "")
        self.assertTrue(record["available"])
        self.assertTrue(record["version"].startswith("series/"))
        stored = self.db.list_history_snapshots(symbol="BTCUSDT", data_kind=FUNDING, limit=1)
        self.assertEqual(stored[0]["version"], record["version"])

    def test_pinning_an_empty_series_is_refused_rather_than_invented(self):
        from quantdesk.datahub.history import HistoryCollector

        collector = HistoryCollector(self.db, None)
        record = collector.snapshot("BTCUSDT", FUNDING, "")
        self.assertFalse(record["available"])
        self.assertIn("没有", record["reason"])
        self.assertEqual(self.db.list_history_snapshots(symbol="BTCUSDT", data_kind=FUNDING, limit=5), [])
