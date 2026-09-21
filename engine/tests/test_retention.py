"""P0-2：运行记录要有上限——压缩存值、按策略清理，且从不碰正在跑的任务。"""

from __future__ import annotations

import json
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from quantdesk.datahub.db import Database
from quantdesk.retention import (
    DEFAULT_KEEP_DAYS,
    DEFAULT_KEEP_FACTOR_RUNS,
    DEFAULT_KEEP_RUNS,
    plan,
    policy_from_config,
    prune,
)


class RetentionFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def run_row(self, *, status: str, finished_ts: int, run_id: str = "", kind: str = "backtest"):
        self.db.execute(
            "INSERT INTO backtest_runs (kind, status, label, symbol, interval, strategy_id, "
            "strategy_version, request_hash, request_json, result_json, queued_ts, finished_ts, updated_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, status, "t", "BTCUSDT", "1h", "ma_cross", "v", run_id or str(finished_ts),
             "{}", json.dumps({"payload": "x" * 100}), finished_ts, finished_ts, finished_ts),
        )
        rows = self.db.query("SELECT id FROM backtest_runs ORDER BY id DESC LIMIT 1")
        return int(rows[0]["id"])

    def factor_row(self, *, status: str, finished_ts: int, series: list[dict] | None = None):
        blob = zlib.compress(json.dumps(series or [], ensure_ascii=False).encode(), 6)
        self.db.execute(
            "INSERT INTO factor_runs (provider, symbol, interval, factor_ids, status, bars, "
            "series_count, coverage_json, values_blob, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("vibe-factors", "BTCUSDT", "1h", "[]", status, 10, 1, "{}", blob, finished_ts),
        )
        rows = self.db.query("SELECT id FROM factor_runs ORDER BY id DESC LIMIT 1")
        return int(rows[0]["id"])


class PlanTests(RetentionFixture):
    def test_the_newest_runs_are_kept_and_the_rest_are_planned_away(self):
        now = 1_700_000_000_000
        keep = [self.run_row(status="done", finished_ts=now - index * 1000) for index in range(3)]
        old = [self.run_row(status="done", finished_ts=now - 40 * 86_400_000) for _ in range(2)]
        report = plan(self.db, keep_runs=3, keep_days=30, now_ms=now)
        self.assertEqual(sorted(report["runs"]), sorted(old))
        self.assertTrue(report["bytes"] > 0, "计划必须报告能释放多少空间")
        del keep

    def test_a_running_or_queued_run_is_never_planned_away(self):
        now = 1_700_000_000_000
        live = self.run_row(status="running", finished_ts=now - 90 * 86_400_000)
        queued = self.run_row(status="queued", finished_ts=now - 90 * 86_400_000)
        report = plan(self.db, keep_runs=1, keep_days=1, now_ms=now)
        self.assertNotIn(live, report["runs"])
        self.assertNotIn(queued, report["runs"])

    def test_a_dry_run_changes_nothing(self):
        now = 1_700_000_000_000
        self.run_row(status="done", finished_ts=now - 90 * 86_400_000)
        report = prune(self.db, keep_runs=1, keep_days=1, now_ms=now, dry_run=True)
        self.assertEqual(report["deletedRuns"], 0)
        self.assertTrue(report["dryRun"])
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM backtest_runs")[0]["n"], 1)


class PruneTests(RetentionFixture):
    def test_pruning_takes_the_artifacts_and_verdicts_with_the_run(self):
        now = 1_700_000_000_000
        run_id = self.run_row(status="done", finished_ts=now - 90 * 86_400_000)
        self.db.execute(
            "INSERT INTO backtest_artifacts (run_id, name, media_type, bytes, sha256, payload, created_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, "equity", "application/json", 2, "h", "[]", now),
        )
        self.db.execute(
            "INSERT INTO backtest_validation_results (run_id, kind, verdict, provider, detail, created_ts) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, "leakage", "pass", "engine", "ok", now),
        )
        report = prune(self.db, keep_runs=1, keep_days=1, now_ms=now)
        self.assertEqual(report["deletedRuns"], 1)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM backtest_artifacts")[0]["n"], 0)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM backtest_validation_results")[0]["n"], 0)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM backtest_runs")[0]["n"], 0)

    def test_factor_runs_have_their_own_cap(self):
        """因子任务更占空间，条数上限与普通研究分开。"""
        now = 1_700_000_000_000
        for index in range(5):
            self.factor_row(status="ok", finished_ts=now - index * 1000)
        report = plan(self.db, keep_runs=50, keep_factor_runs=2, keep_days=0, now_ms=now)
        self.assertEqual(len(report["factorRuns"]), 3, "只保留最近 2 条因子任务")

    def test_factor_runs_are_pruned_too(self):
        now = 1_700_000_000_000
        old = self.factor_row(status="ok", finished_ts=now - 90 * 86_400_000)
        self.db.execute("UPDATE factor_runs SET created_ts=? WHERE id=?", (now - 90 * 86_400_000, old))
        report = prune(self.db, keep_runs=1, keep_days=1, now_ms=now)
        self.assertEqual(report["deletedFactorRuns"], 1)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM factor_runs")[0]["n"], 0)

    def test_the_policy_comes_from_config(self):
        self.assertEqual(policy_from_config(Path(self._tmp.name)), {
            "keepRuns": DEFAULT_KEEP_RUNS, "keepFactorRuns": DEFAULT_KEEP_FACTOR_RUNS,
            "keepDays": DEFAULT_KEEP_DAYS,
        })
        config = Path(self._tmp.name) / "config.toml"
        config.write_text("[retention]\nruns = 12\ndays = 3\n", encoding="utf-8")
        with patch("quantdesk.retention.load_app_config") as loader:
            loader.return_value.retention = {"runs": 12, "factorRuns": 4, "days": 3}
            self.assertEqual(policy_from_config(Path(self._tmp.name)),
                             {"keepRuns": 12, "keepFactorRuns": 4, "keepDays": 3})


class CompressionTests(RetentionFixture):
    def test_stored_factor_values_are_compressed_and_read_back(self):
        from quantdesk.factors import _record_run, run_detail

        series = [{"factorId": "vibe.rsi.14", "values": [{"time": 1 + i, "value": 1.0} for i in range(500)]}]
        run_id = _record_run(self.db, "vibe-factors", "BTCUSDT", "1h", "snap", ["vibe.rsi.14"], {},
                             status="ok", bars=500, series=series, warnings=[], error=None, started=0)
        row = self.db.query("SELECT values_json, values_blob FROM factor_runs WHERE id=?", (run_id,))[0]
        self.assertIsNone(row["values_json"], "新行不再写未压缩文本")
        self.assertTrue(row["values_blob"])
        self.assertLess(len(row["values_blob"]), len(json.dumps(series)), "压缩后必须更小")
        self.assertEqual(len(run_detail(self.db, run_id, include_values=True)["series"]), 1)

    def test_a_row_written_before_compression_still_reads_back(self):
        from quantdesk.factors import run_detail

        series = [{"factorId": "old", "values": [{"time": 1, "value": 2.0}]}]
        self.db.execute(
            "INSERT INTO factor_runs (provider, symbol, interval, factor_ids, status, bars, series_count, "
            "coverage_json, values_json, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("vibe-factors", "BTCUSDT", "1h", "[]", "ok", 1, 1, "{}", json.dumps(series), 0),
        )
        run_id = int(self.db.query("SELECT id FROM factor_runs ORDER BY id DESC LIMIT 1")[0]["id"])
        self.assertEqual(run_detail(self.db, run_id, include_values=True)["series"], series)

    def test_the_startup_migration_compresses_old_rows_in_batches(self):
        series = [{"factorId": "old", "values": [{"time": 1, "value": 2.0}]}]
        for _ in range(3):
            self.db.execute(
                "INSERT INTO factor_runs (provider, symbol, interval, factor_ids, status, bars, "
                "series_count, coverage_json, values_json, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("vibe-factors", "BTCUSDT", "1h", "[]", "ok", 1, 1, "{}", json.dumps(series), 0),
            )
        converted = self.db._compress_stored_factor_values(limit=2)
        self.assertEqual(converted, 2, "一次只转换一批，启动不能被一次大重写拖住")
        left = self.db.query("SELECT COUNT(*) AS n FROM factor_runs WHERE values_json IS NOT NULL")[0]["n"]
        self.assertEqual(left, 1)
        self.assertEqual(self.db._compress_stored_factor_values(), 1)
