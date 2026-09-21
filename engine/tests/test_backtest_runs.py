"""Phase 3: the persistent backtest run queue and the result centre.

These tests hold the queue to the promises the result centre makes:

* submitting a study never computes it inside the request - it writes a row;
* progress is visible while the run happens, and a cancelled run is cancelled at
  a checkpoint rather than published;
* a run interrupted by a restart goes back on the queue instead of sitting there
  claiming to be in progress;
* a finished run can be read back with its request, its artifacts, its validation
  verdicts and the strategy version it used, without recomputing anything;
* a study too large to be an interaction is refused by the synchronous path and
  pointed at the queue;
* the same request, run twice, produces the same answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quantdesk.api.server import app
from quantdesk.backtest_runs import (
    BacktestRunWorker,
    RunQueue,
    artifacts_of,
    request_hash,
    verdicts_of,
)
from quantdesk.datahub.db import Database
from quantdesk.studies import StudyError, require_sync_budget, strategy_identity, sync_cost

from test_research_data_entry import BAR_COUNT, ResearchEntryFixture, stable


def body_for(kind: str, **overrides) -> dict:
    if kind == "backtest":
        body = {"symbol": "BTCUSDT", "timeframe": "1h", "bars": 300, "strategyId": "ma_cross"}
    elif kind == "validate":
        body = {"symbol": "BTCUSDT", "timeframe": "1h", "bars": 300, "fastGrid": [5, 9],
                "slowGrid": [21, 50], "walkForwardWindows": 2}
    else:
        body = {"symbols": ["BTCUSDT"], "timeframe": "1h", "bars": 300}
    body.update(overrides)
    return body


class QueueFixture(unittest.IsolatedAsyncioTestCase):
    """A queue on a temporary database, with no venue client anywhere."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"QUANTDESK_HOME": self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db = Database(self.home / "quantdesk.db")

    def queue(self, execute=None) -> RunQueue:
        if execute is None:
            def execute(context, progress):
                progress(0.5, "假装在算")
                return {"net_return_pct": 12.5, "max_drawdown_pct": -3.0, "trades": [{"id": 1}],
                        "equity_curve": [{"time": 1, "equity": 100.0}], "dataReady": True, "degraded": False}
        return RunQueue(self.db, execute=execute)


class SubmissionTests(QueueFixture):
    def test_a_submitted_run_is_a_row_before_it_is_work(self):
        queue = self.queue()
        run = queue.submit("backtest", body_for("backtest"))
        self.assertEqual(run["status"], "queued")
        self.assertEqual(run["kind"], "backtest")
        self.assertEqual(run["symbol"], "BTCUSDT")
        self.assertEqual(run["progress"], 0)
        self.assertIn("排队", run["progressLabel"])
        stored = self.db.query("SELECT * FROM backtest_runs WHERE id=?", (run["id"],))[0]
        self.assertEqual(json.loads(stored["request_json"])["bars"], 300)
        self.assertTrue(stored["strategy_version"])

    def test_the_same_request_does_not_start_a_second_copy(self):
        queue = self.queue()
        first = queue.submit("backtest", body_for("backtest"))
        second = queue.submit("backtest", body_for("backtest"))
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM backtest_runs")[0]["n"], 1)

    def test_a_different_request_is_a_different_run(self):
        queue = self.queue()
        first = queue.submit("backtest", body_for("backtest"))
        second = queue.submit("backtest", body_for("backtest", bars=301))
        self.assertNotEqual(first["id"], second["id"])

    def test_an_invalid_body_is_refused_at_submission(self):
        queue = self.queue()
        with self.assertRaises(StudyError) as caught:
            queue.submit("backtest", {"symbol": "BTCUSDT", "bars": 1})
        self.assertEqual(caught.exception.kind, "invalid")
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM backtest_runs")[0]["n"], 0)

    def test_an_unknown_kind_is_refused(self):
        queue = self.queue()
        with self.assertRaises(StudyError):
            queue.submit("regression", body_for("backtest"))

    def test_the_strategy_version_is_pinned_for_the_run(self):
        queue = self.queue()
        run = queue.submit("backtest", body_for("backtest", strategyParams={"fastPeriod": 5, "slowPeriod": 34}))
        rows = self.db.query(
            "SELECT * FROM strategy_versions WHERE strategy_id=? AND version=?",
            ("ma_cross", run["strategyVersion"]),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["parameters_json"]), {"fastPeriod": 5, "slowPeriod": 34})
        self.assertTrue(rows[0]["code_hash"])


class ExecutionTests(QueueFixture):
    def test_a_claimed_run_records_progress_result_and_artifacts(self):
        queue = self.queue()
        submitted = queue.submit("validate", body_for("validate"))
        claimed = queue.claim_next()
        self.assertEqual(claimed["id"], submitted["id"])
        self.assertEqual(claimed["status"], "running")
        done = queue.run_claimed(claimed)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["progress"], 100)
        self.assertEqual(done["headline"]["netReturnPct"], 12.5)
        self.assertEqual(done["headline"]["trades"], 1)
        self.assertIn("equity", done["artifacts"])
        self.assertIn("metrics", done["artifacts"])
        self.assertTrue(done["durationMs"] is not None)

        detail = queue.get(submitted["id"], with_result=True)
        self.assertEqual(detail["result"]["net_return_pct"], 12.5)
        equity = queue.artifact_payload(submitted["id"], "equity")
        self.assertEqual(json.loads(equity["payload"])[0]["equity"], 100.0)
        self.assertEqual(
            equity["sha256"], hashlib.sha256(equity["payload"].encode()).hexdigest(),
            "产物哈希必须与其内容一致",
        )

    def test_progress_is_written_while_the_run_happens(self):
        seen: list[tuple[float, str]] = []

        def execute(context, progress):
            progress(0.25, "参数搜索 1/4 组合")
            seen.append((self.queue().get(context["run"]["id"])["progress"], "参数搜索 1/4 组合"))
            progress(0.75, "滚动窗口 1/2")
            return {"net_return_pct": 0.0}

        queue = self.queue(execute=execute)
        run = queue.submit("validate", body_for("validate"))
        queue.run_claimed(queue.claim_next())
        self.assertEqual(seen[0][0], 25.0)
        self.assertEqual(queue.get(run["id"])["stage"], "done")

    def test_a_cancelled_run_is_not_published(self):
        def execute(context, progress):
            progress(0.4, "参数搜索 2/4 组合")
            # The operator cancels while the study is mid-flight.
            queue.cancel(context["run"]["id"])
            progress(0.9, "滚动窗口 1/2")
            return {"net_return_pct": 99.0}

        queue = self.queue(execute=execute)
        run = queue.submit("validate", body_for("validate"))
        result = queue.run_claimed(queue.claim_next())
        self.assertEqual(result["status"], "cancelled")
        stored = self.db.query("SELECT result_json FROM backtest_runs WHERE id=?", (run["id"],))[0]
        self.assertIsNone(stored["result_json"], "取消的任务不得留下结果")

    def test_cancellation_is_honoured_at_the_next_checkpoint(self):
        """取消发生在检查点：长研究不会算完再被丢掉。"""
        reached: list[str] = []

        def execute(context, progress):
            progress(0.1, "参数搜索 1/90 组合")
            queue.cancel(context["run"]["id"])
            progress(0.2, "参数搜索 2/90 组合")
            reached.append("算完了")
            return {"net_return_pct": 99.0}

        queue = self.queue(execute=execute)
        run = queue.submit("validate", body_for("validate"))
        result = queue.run_claimed(queue.claim_next())
        self.assertEqual(reached, [], "第二个检查点应抛出取消，而不是继续算完")
        self.assertEqual(result["status"], "cancelled")

    def test_a_cancelled_queued_run_never_starts(self):
        queue = self.queue()
        run = queue.submit("backtest", body_for("backtest"))
        queue.cancel(run["id"])
        self.assertIsNone(queue.claim_next())

    def test_a_gate_refusal_is_a_failed_run_with_the_reason(self):
        def execute(context, progress):
            raise StudyError("not_ready", "缺 120 根K线", status=409,
                             detail={"title": "数据未就绪，已阻止正式研究", "detail": "缺 120 根K线"})

        queue = self.queue(execute=execute)
        run = queue.submit("validate", body_for("validate"))
        result = queue.run_claimed(queue.claim_next())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errorKind"], "not_ready")
        self.assertIn("缺 120 根K线", result["error"])
        self.assertIn("数据未就绪", result["progressLabel"])

    def test_an_unexpected_error_is_recorded_as_internal(self):
        def execute(context, progress):
            raise RuntimeError("boom")

        queue = self.queue(execute=execute)
        run = queue.submit("backtest", body_for("backtest"))
        result = queue.run_claimed(queue.claim_next())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errorKind"], "internal")
        self.assertIn("boom", result["error"])

    def test_retry_requeues_and_clears_the_previous_result(self):
        def failing(context, progress):
            raise RuntimeError("boom")

        queue = self.queue(execute=failing)
        run = queue.submit("backtest", body_for("backtest"))
        queue.run_claimed(queue.claim_next())
        self.assertEqual(queue.get(run["id"])["status"], "failed")
        requeued = queue.retry(run["id"])
        self.assertEqual(requeued["status"], "queued")
        self.assertIsNone(requeued["error"])
        self.assertEqual(requeued["artifacts"], [])

    def test_deleting_a_run_removes_its_parts(self):
        queue = self.queue()
        run = queue.submit("backtest", body_for("backtest"))
        queue.run_claimed(queue.claim_next())
        self.assertTrue(queue.delete(run["id"]))
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM backtest_runs")[0]["n"], 0)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM backtest_artifacts")[0]["n"], 0)

    def test_a_running_run_cannot_be_deleted(self):
        queue = self.queue()
        queue.submit("backtest", body_for("backtest"))
        claimed = queue.claim_next()
        with self.assertRaises(StudyError) as caught:
            queue.delete(claimed["id"])
        self.assertEqual(caught.exception.status, 409)


class LeaseTests(QueueFixture):
    """A killed process's study thread keeps running; its result must not land."""

    def test_a_superseded_attempt_cannot_overwrite_the_newer_result(self):
        old = self.queue()                                    # the process that was killed
        takeover = BacktestRunWorker(self.queue(), idle_seconds=0.05)  # the one that took over
        run = old.submit("backtest", body_for("backtest"))
        self.assertIsNotNone(old.claim_next())
        # The new process reclaims the interrupted run and finishes it.
        self.assertEqual(takeover.reclaim_interrupted(), 1)
        claimed = takeover.queue.claim_next()
        self.assertIsNotNone(claimed, "接管后应能重新认领")
        done = takeover.queue.run_claimed(claimed)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["headline"]["netReturnPct"], 12.5)

        # ...and only now does the old process's thread finish. Its write is
        # discarded: the row still carries the newer attempt's result.
        stale = old.finish(run["id"], {"net_return_pct": -99.0}, started=0)
        self.assertEqual(stale["status"], "done")
        self.assertEqual(stale["headline"]["netReturnPct"], 12.5, "被取代的尝试不得覆盖结果")

    def test_a_superseded_attempt_cannot_mark_a_run_failed(self):
        old = self.queue()
        takeover = BacktestRunWorker(self.queue(), idle_seconds=0.05)
        run = old.submit("backtest", body_for("backtest"))
        old.claim_next()
        takeover.reclaim_interrupted()
        takeover.queue.run_claimed(takeover.queue.claim_next())
        stale = old.fail(run["id"], "旧进程的异常", "internal")
        self.assertEqual(stale["status"], "done")
        self.assertIsNone(stale["error"])

    def test_an_owner_can_still_write(self):
        queue = self.queue()
        run = queue.submit("backtest", body_for("backtest"))
        claimed = queue.claim_next()
        self.assertTrue(queue.holds(run["id"]))
        self.assertEqual(queue.run_claimed(claimed)["status"], "done")
        self.assertEqual(queue.get(run["id"])["status"], "done")


class RestartTests(QueueFixture):
    def test_a_run_interrupted_by_a_restart_goes_back_on_the_queue(self):
        queue = self.queue()
        run = queue.submit("backtest", body_for("backtest"))
        claimed = queue.claim_next()
        self.assertEqual(claimed["status"], "running")
        self.db.execute(
            "UPDATE backtest_runs SET status='running' WHERE id=?", (claimed["id"],)
        )
        # A new process starts: the worker reclaims what the dead one left.
        worker = BacktestRunWorker(RunQueue(self.db))
        self.assertEqual(worker.reclaim_interrupted(), 1)
        after = queue.get(run["id"])
        self.assertEqual(after["status"], "queued")
        self.assertIn("重新排队", after["progressLabel"])
        self.assertEqual(after["progress"], 0)

    def test_a_reclaimed_run_is_recomputed_from_its_stored_request(self):
        queue = self.queue()
        run = queue.submit("backtest", body_for("backtest"))
        queue.claim_next()
        self.db.execute("UPDATE backtest_runs SET status='running' WHERE id=?", (run["id"],))

        worker = BacktestRunWorker(self.queue(), idle_seconds=0.05)
        asyncio.run(_drain_started(worker, run["id"]))
        after = worker.queue.get(run["id"])
        self.assertEqual(after["status"], "done", after.get("error"))
        self.assertEqual(after["attempts"], 2, "重排后是一次新的尝试")
        self.assertEqual(after["headline"]["netReturnPct"], 12.5)

    def test_the_board_survives_a_restart_because_it_is_in_the_database(self):
        queue = self.queue()
        run = queue.submit("validate", body_for("validate"))
        queue.run_claimed(queue.claim_next())
        reopened = RunQueue(Database(self.home / "quantdesk.db"))
        self.assertEqual(reopened.get(run["id"])["status"], "done")
        self.assertEqual(reopened.summary()["done"], 1)


class SyncBudgetTests(unittest.TestCase):
    def test_the_budget_counts_the_work_a_request_implies(self):
        from quantdesk.studies import BacktestRequest, PortfolioRequest, ValidationRequest

        single = BacktestRequest(symbol="BTCUSDT", bars=1_000)
        self.assertEqual(sync_cost("backtest", single), 1_000)
        validate = ValidationRequest(symbol="BTCUSDT", bars=3_000, fastGrid=[5, 9, 20],
                                     slowGrid=[21, 50, 100], walkForwardWindows=4)
        # 9 candidates x (2 segments + 4 windows) x 3,000 bars
        self.assertEqual(sync_cost("validate", validate), 3_000 * 9 * 6)
        portfolio = PortfolioRequest(symbols=["BTCUSDT", "ETHUSDT"], bars=600)
        self.assertEqual(sync_cost("portfolio", portfolio), 1_200)

    def test_a_heavy_study_is_refused_and_pointed_at_the_queue(self):
        from quantdesk.studies import ValidationRequest

        heavy = ValidationRequest(symbol="BTCUSDT", bars=3_000, fastGrid=[5, 9, 20],
                                  slowGrid=[21, 50, 100], walkForwardWindows=4)
        with self.assertRaises(StudyError) as caught:
            require_sync_budget("validate", heavy)
        error = caught.exception
        self.assertEqual(error.status, 409)
        self.assertEqual(error.kind, "too_large")
        self.assertIn("后台", error.detail["action"])

    def test_a_small_study_passes_the_budget(self):
        from quantdesk.studies import BacktestRequest

        self.assertEqual(require_sync_budget("backtest", BacktestRequest(symbol="BTCUSDT", bars=600)), 600)


class ArtifactTests(unittest.TestCase):
    def test_a_validation_headline_is_the_out_of_sample_number(self):
        queue = RunQueue.__new__(RunQueue)  # only the pure summariser is needed
        result = {
            "parameterSearch": {
                "test": {"total_return_pct": 4.0, "max_drawdown_pct": -2.0, "trades": 12},
                "best": {"outOfSample": {"total_return_pct": 9.0}, "inSample": {"total_return_pct": 30.0}},
            }
        }
        headline = queue._headline(result)
        self.assertEqual(headline["netReturnPct"], 4.0, "测试段优先")
        self.assertEqual(headline["trades"], 12.0)
        self.assertIn("测试段", headline["scope"])

    def test_a_validation_headline_falls_back_to_the_windows(self):
        queue = RunQueue.__new__(RunQueue)
        result = {"parameterSearch": {"best": {}}, "walkForward": {"windows": [
            {"validation": {"total_return_pct": 1.0}}, {"validation": {"total_return_pct": 2.5}}]}}
        headline = queue._headline(result)
        self.assertEqual(headline["netReturnPct"], 2.5)
        self.assertIn("滚动窗口", headline["scope"])

    def test_a_portfolio_headline_is_the_combined_book(self):
        queue = RunQueue.__new__(RunQueue)
        result = {"portfolio": {"total_return_pct": 7.5, "max_drawdown_pct": -4.0, "trades": 30},
                  "total_return_pct": 999.0}
        headline = queue._headline(result)
        self.assertEqual(headline["netReturnPct"], 7.5)
        self.assertEqual(headline["scope"], "组合合并账本")

    def test_a_single_backtest_headline_is_the_run_itself(self):
        queue = RunQueue.__new__(RunQueue)
        headline = queue._headline({"net_return_pct": 3.25, "trades": [{"id": 1}, {"id": 2}]})
        self.assertEqual(headline["netReturnPct"], 3.25)
        self.assertEqual(headline["trades"], 2.0)
        self.assertEqual(headline["scope"], "整段回测")

    def test_a_validation_result_becomes_rows_with_numbers(self):
        result = {
            "walkForward": {"windows": [{"window": 1}, {"window": 2}], "positiveWindows": 1,
                            "stableParameters": False},
            "leakage": {"clean": True, "summary": "截断后信号不变", "mismatches": 0},
            "parameterSearch": {"warnings": ["验证段收益低于训练段"]},
        }
        rows = {row["kind"]: row for row in verdicts_of(result)}
        self.assertEqual(rows["walk_forward"]["verdict"], "warn")
        self.assertEqual(rows["walk_forward"]["statistic"], 1.0)
        self.assertEqual(rows["walk_forward"]["threshold"], 2.0)
        self.assertEqual(rows["walk_forward_stability"]["verdict"], "warn")
        self.assertEqual(rows["leakage"]["verdict"], "pass")
        self.assertEqual(rows["overfit"]["verdict"], "warn")

    def test_trades_are_exported_as_csv(self):
        result = {"trades": [{"entry_ts": 1, "side": "long", "pnl": 3.5},
                             {"entry_ts": 2, "side": "short", "pnl": -1.0}]}
        artifacts = {name: payload for name, _media, payload in artifacts_of(result)}
        csv_text = artifacts["trades"]
        self.assertTrue(csv_text.startswith("entry_ts,side,pnl"))
        self.assertIn("long,3.5", csv_text)

    def test_the_readiness_verdict_travels_with_the_run(self):
        result = {"readiness": {"ok": False, "degraded": True, "blocking": []}}
        names = [name for name, _media, _payload in artifacts_of(result)]
        self.assertIn("readiness", names)


class LiveQueueTests(ResearchEntryFixture):
    """The queue against real stored history: the API and the worker together."""

    async def test_the_api_queues_a_study_and_the_worker_completes_it(self):
        self.seed()
        with self.no_venue():
            submitted = await self.client.post(
                "/api/backtest/runs",
                json={"kind": "backtest", "request": body_for("backtest", bars=300)},
            )
            self.assertEqual(submitted.status_code, 202, submitted.text)
            run_id = submitted.json()["run"]["id"]

            # Nothing has been computed yet: the request only wrote a row.
            detail = await self.client.get(f"/api/backtest/runs/{run_id}")
            self.assertEqual(detail.json()["run"]["status"], "queued")
            self.assertIsNone(detail.json()["run"]["result"])

            worker = _worker_for(self.home)
            await worker.start()
            try:
                await _drain(worker, run_id)
            finally:
                await worker.stop()

            finished = (await self.client.get(f"/api/backtest/runs/{run_id}")).json()["run"]
            self.assertEqual(finished["status"], "done", finished.get("error"))
            self.assertTrue(finished["result"]["net_return_pct"] is not None)
            self.assertTrue(finished["result"]["readiness"]["ok"])
            self.assertIn("equity", finished["artifacts"])
            listed = (await self.client.get("/api/backtest/runs")).json()
            self.assertEqual(listed["summary"]["done"], 1)

    async def test_the_same_request_twice_gives_the_same_answer(self):
        self.seed()
        with self.no_venue():
            worker = _worker_for(self.home)
            await worker.start()
            try:
                first = await _run_now(self.client, worker, "backtest", body_for("backtest", bars=300))
                second = await _run_now(self.client, worker, "backtest", body_for("backtest", bars=300))
            finally:
                await worker.stop()
        self.assertEqual(
            json.dumps(stable(first["result"]), sort_keys=True),
            json.dumps(stable(second["result"]), sort_keys=True),
            "同一请求两次运行必须给出同样的结果",
        )
        self.assertEqual(first["strategyVersion"], second["strategyVersion"])

    async def test_a_heavy_validate_moves_to_the_queue_unless_the_caller_opts_out(self):
        self.seed()
        heavy = body_for("validate", bars=3_000, fastGrid=[5, 9, 15, 20, 30],
                         slowGrid=[21, 34, 50, 80, 100], walkForwardWindows=6)
        with self.no_venue():
            moved = await self.client.post("/api/validate", json=heavy)
            refused = await self.client.post("/api/validate?queue=never", json=heavy)

        # Default: the same request becomes a queued run, so the caller has
        # something to watch instead of a refusal to retype.
        self.assertEqual(moved.status_code, 202, moved.text)
        body = moved.json()
        self.assertTrue(body["queued"])
        self.assertIn("自动转入后台队列", body["reason"])
        queued = self.db.query("SELECT kind, status FROM backtest_runs ORDER BY id")
        self.assertEqual([(row["kind"], row["status"]) for row in queued], [("validate", "queued")])

        # Opting out keeps the explicit refusal with the budget numbers.
        self.assertEqual(refused.status_code, 409, refused.text)
        detail = json.loads(refused.json()["detail"])
        self.assertGreater(detail["syncCost"], detail["syncBudget"])
        self.assertTrue(detail["autoQueueAvailable"])

    async def test_a_queued_portfolio_is_recorded_with_its_members(self):
        self.seed("BTCUSDT")
        self.seed("ETHUSDT")
        with self.no_venue():
            worker = _worker_for(self.home)
            await worker.start()
            try:
                run = await _run_now(self.client, worker, "portfolio",
                                     {"symbols": ["BTCUSDT", "ETHUSDT"], "timeframe": "1h", "bars": 200})
            finally:
                await worker.stop()
        self.assertEqual(run["status"], "done", run.get("error"))
        self.assertEqual(run["symbols"], ["BTCUSDT", "ETHUSDT"])
        self.assertIn("members", run["artifacts"])
        self.assertIn("coverage", run["artifacts"])
        self.assertTrue(run["result"]["memberVersions"])


def _worker_for(home: Path) -> BacktestRunWorker:
    return BacktestRunWorker(RunQueue(Database(home / "quantdesk.db")), idle_seconds=0.05)


async def _drain(worker: BacktestRunWorker, run_id: int, timeout: float = 60.0) -> None:
    """Let the worker take the run and finish it, without a second copy."""
    deadline = asyncio.get_event_loop().time() + timeout
    queue = worker.queue
    while asyncio.get_event_loop().time() < deadline:
        run = queue.get(run_id)
        if run["status"] in ("done", "failed", "cancelled"):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"任务 {run_id} 在 {timeout}s 内没有结束")


async def _drain_started(worker: BacktestRunWorker, run_id: int, timeout: float = 60.0) -> None:
    """Start a worker and wait for one run, so the loop stops with it."""
    await worker.start()
    try:
        await _drain(worker, run_id, timeout)
    finally:
        await worker.stop()


async def _run_now(client: httpx.AsyncClient, worker: BacktestRunWorker, kind: str, body: dict) -> dict:
    submitted = await client.post("/api/backtest/runs", json={"kind": kind, "request": body})
    assert submitted.status_code == 202, submitted.text
    run_id = submitted.json()["run"]["id"]
    await _drain(worker, run_id)
    detail = await client.get(f"/api/backtest/runs/{run_id}")
    return detail.json()["run"]


class IdenticalRequestTests(unittest.TestCase):
    def test_the_request_hash_is_stable_and_order_insensitive(self):
        first = request_hash("backtest", {"symbol": "BTCUSDT", "bars": 300})
        second = request_hash("backtest", {"bars": 300, "symbol": "BTCUSDT"})
        self.assertEqual(first, second)

    def test_strategy_identity_changes_with_parameters(self):
        first = strategy_identity("ma_cross", {"fastPeriod": 5, "slowPeriod": 21})
        second = strategy_identity("ma_cross", {"fastPeriod": 9, "slowPeriod": 21})
        self.assertNotEqual(first["version"], second["version"])
        self.assertEqual(first["source"], "builtin")
        self.assertTrue(first["codeHash"])


class WindowTrimTests(ResearchEntryFixture):
    """门禁不能因为"最新一根还没有标记价"就把正式研究判死。"""

    async def test_a_study_window_ends_where_its_marks_end(self):
        from quantdesk.datahub.readiness import window_for

        self.seed()
        newest_candle = self.db.last_open_ts("bybit", "BTCUSDT", "1h")
        self.db.execute("DELETE FROM mark_candles WHERE venue='bybit' AND symbol='BTCUSDT' AND open_ts=?",
                        (newest_candle,))
        plain = window_for(self.db, venue="bybit", symbol="BTCUSDT", interval="1h", bars=100)
        trimmed = window_for(self.db, venue="bybit", symbol="BTCUSDT", interval="1h", bars=100,
                             require_marks=True)
        self.assertEqual(plain[1], newest_candle, "不需要标记价时仍读到最新收盘")
        self.assertLess(trimmed[1], plain[1], "需要标记价时窗口回退到有标记价的一根")
        self.assertEqual(trimmed[1] - trimmed[0], plain[1] - plain[0], "窗口长度不变")

    async def test_the_study_then_runs_instead_of_refusing(self):
        self.seed()
        newest = self.db.last_open_ts("bybit", "BTCUSDT", "1h")
        self.db.execute("DELETE FROM mark_candles WHERE venue='bybit' AND symbol='BTCUSDT' AND open_ts=?",
                        (newest,))
        with self.no_venue():
            response = await self.backtest(bars=120)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["readRange"]["expectedBars"], 120)


class StudyChildLifecycleTests(unittest.TestCase):
    """一个研究进程不得比启动它的引擎活得更久。"""

    def test_the_registry_tracks_and_stops_children(self):
        import subprocess
        import sys

        from quantdesk.backtest_runs import (
            forget_study_child,
            live_study_children,
            register_study_child,
            terminate_study_children,
        )

        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            register_study_child(process)
            self.assertIn(process.pid, live_study_children())
            stopped = terminate_study_children(grace_seconds=3)
            self.assertIn(process.pid, stopped)
            self.assertNotIn(process.pid, live_study_children())
            self.assertIsNotNone(process.poll(), "登记的子进程必须真的被终止")
        finally:
            forget_study_child(process)
            if process.poll() is None:
                process.kill()

    def test_the_watchdog_exits_when_the_parent_is_gone(self):
        """父进程被强杀（没有关停钩子）时，子进程靠看门狗自己退出。

        真正的关系必须是「父→子」。让测试进程当祖父：A 启动 B（B 监视 A），
        A 随即退出，B 必须自己消失。
        """
        import subprocess
        import sys
        import time

        watchdog = (
            "import os, sys, time; sys.path.insert(0, 'src'); "
            "from quantdesk.studies_cli import _watch_parent; "
            "_watch_parent(int(sys.argv[1]), interval=0.2); time.sleep(60)"
        )
        launcher = (
            "import subprocess, sys, time; "
            f"child = subprocess.Popen([sys.executable, '-c', {watchdog!r}, str(os.getpid())]); "
            "print(child.pid, flush=True); time.sleep(0.5)"
        )
        launcher_code = launcher.replace("os.getpid()", "__import__('os').getpid()")
        parent = subprocess.Popen([sys.executable, "-c", launcher_code],
                                  cwd=str(Path(__file__).resolve().parents[1]),
                                  stdout=subprocess.PIPE, text=True)
        try:
            assert parent.stdout is not None
            grandchild = int(parent.stdout.readline().strip())
            parent.wait(timeout=10)
            deadline = time.monotonic() + 15
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(grandchild, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.2)
            self.assertFalse(alive, "父进程已消失，研究子进程却还在运行")
        finally:
            if parent.poll() is None:
                parent.kill()

    def test_the_watchdog_leaves_a_live_parent_alone(self):
        """父进程还在时看门狗不能动手：子进程应当正常跑完自己的活。"""
        import subprocess
        import sys

        child = subprocess.Popen(
            [sys.executable, "-c",
             "import os, sys, time; sys.path.insert(0, 'src'); "
             "from quantdesk.studies_cli import _watch_parent; "
             "_watch_parent(os.getppid(), interval=0.2); time.sleep(1.0)"],
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        child.wait(timeout=30)
        self.assertEqual(child.returncode, 0, "父进程还在，看门狗不该结束子进程")


class ShutdownBookkeepingTests(QueueFixture):
    """引擎关停不是研究失败：行必须留给下一个进程重排。"""

    def test_a_study_killed_by_shutdown_is_left_for_the_next_process(self):
        # The executor raises because the shutdown killed the child process.
        def killed(context, progress):
            raise RuntimeError("研究进程退出码 -15，没有返回结果")

        queue = self.queue(execute=killed)
        run = queue.submit("validate", body_for("validate"))
        claimed = queue.claim_next()
        queue.stopping = True
        # The child was killed by the shutdown, which the executor reports as an error.
        result = queue.run_claimed(claimed)
        self.assertEqual(result["status"], "running", "关停期间不得写成 failed")
        self.assertIn("重新排队", result["progressLabel"])
        self.assertIsNone(result["error"])
        self.assertIsNone(result["errorKind"] or None)

    def test_the_next_process_requeues_and_recomputes_it(self):
        def killed(context, progress):
            raise RuntimeError("研究进程退出码 -15，没有返回结果")

        queue = self.queue(execute=killed)
        run = queue.submit("backtest", body_for("backtest"))
        claimed = queue.claim_next()
        queue.stopping = True
        queue.run_claimed(claimed)

        takeover = BacktestRunWorker(self.queue(), idle_seconds=0.05)
        self.assertEqual(takeover.reclaim_interrupted(), 1, "关停留下的 running 行应当被重排")
        asyncio.run(_drain_started(takeover, run["id"]))
        after = takeover.queue.get(run["id"])
        self.assertEqual(after["status"], "done", after.get("error"))
        self.assertEqual(after["attempts"], 2)

    def test_a_real_failure_still_fails_when_not_stopping(self):
        def execute(context, progress):
            raise RuntimeError("boom")

        queue = self.queue(execute=execute)
        run = queue.submit("backtest", body_for("backtest"))
        result = queue.run_claimed(queue.claim_next())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errorKind"], "internal")


class RunNotificationTests(QueueFixture):
    """研究结束（成功或失败）都要通知已启用的 notifier 插件。

    通知是后台线程发出的（一个慢的 notifier 不该拖住队列），所以测试等它到达，
    而不是假设它已经发生。
    """

    def _wait_for(self, sent: list, count: int = 1, timeout: float = 5.0) -> None:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(sent) < count:
            time.sleep(0.02)

    def test_a_finished_run_notifies_once_with_the_facts(self):
        queue = self.queue()
        run = queue.submit("backtest", body_for("backtest"))
        sent: list[dict] = []
        with patch("quantdesk.plugins.PluginRegistry.notify_all",
                   side_effect=lambda event: sent.append(event.model_dump())), \
             patch("quantdesk.plugins.PluginManager") as manager:
            manager.return_value.discover.return_value = ([], [])
            done = queue.run_claimed(queue.claim_next())
            self._wait_for(sent)
        self.assertEqual(len(sent), 1, f"成功结束应当恰好通知一次，实际 {len(sent)} 次")
        event = sent[0]
        self.assertEqual(event["type"], "backtest_run.done")
        self.assertEqual(event["severity"], "info")
        self.assertIn(f"#{done['id']}", event["title"])
        self.assertEqual(event["data"]["runId"], done["id"])
        self.assertIn("净收益 12.50%", event["message"], "通知要把头部指标说清楚")

    def test_a_failed_run_notifies_as_a_warning_with_the_reason(self):
        queue = self.queue()
        run = queue.submit("backtest", body_for("backtest"))
        sent: list[dict] = []
        with patch("quantdesk.plugins.PluginRegistry.notify_all",
                   side_effect=lambda event: sent.append(event.model_dump())), \
             patch("quantdesk.plugins.PluginManager") as manager:
            manager.return_value.discover.return_value = ([], [])
            queue.fail(run["id"], "数据未就绪：缺 120 根K线", "not_ready")
            self._wait_for(sent)
        self.assertEqual(sent[0]["type"], "backtest_run.failed")
        self.assertEqual(sent[0]["severity"], "warning")
        self.assertIn("缺 120 根K线", sent[0]["message"])

    def test_failing_a_run_nobody_claimed_still_records_the_failure(self):
        """看门狗/操作路径可能在一个还没被认领的任务上报错，这必须写得进去。"""
        queue = self.queue()
        run = queue.submit("backtest", body_for("backtest"))
        failed = queue.fail(run["id"], "提交后参数被拒绝", "invalid")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["errorKind"], "invalid")
        self.assertIn("提交后参数被拒绝", failed["error"])

    def test_a_notification_failure_never_changes_the_run(self):
        from quantdesk.backtest_runs import _notify_run

        queue = self.queue()
        run = queue.submit("backtest", body_for("backtest"))
        done = queue.run_claimed(queue.claim_next())
        with patch("quantdesk.plugins.PluginRegistry.notify_all", side_effect=RuntimeError("no notifier")):
            _notify_run(done)
        self.assertEqual(queue.get(run["id"])["status"], "done")
