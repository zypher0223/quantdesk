"""The persistent backtest run queue and its worker.

A formal study can take minutes: a 5,000-bar parameter search with walk-forward
runs hundreds of backtests. Run inside an HTTP request that is a browser timeout
with no record of what was computed, so studies go through this queue instead.

What the queue guarantees:

* a submitted run is a row before it is work - the request, the strategy version
  and the data it will read are all recorded, and the row survives a restart;
* progress is written while the run happens, so the page shows where it is;
* a result is stored once, with the artifacts a caller wants separately, and a
  validation verdict is a row with a number and a threshold behind it;
* a run interrupted by a restart goes back to `queued` and is recomputed. A
  backtest is deterministic, so recomputation is honest; pretending to resume
  half a parameter search would not be;
* a failed run says why, in the same vocabulary the readiness gate uses.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .datahub.db import Database
from .studies import (
    REQUEST_MODELS,
    StudyError,
    parse_request,
    record_strategy_version,
    run_study,
    strategy_identity,
)

RUN_KINDS = ("backtest", "validate", "portfolio", "factors", "campaign", "cpa_ablation")
RUN_STATUSES = ("queued", "running", "done", "failed", "cancelled")
FINAL_STATUSES = ("done", "failed", "cancelled")

# What the list view shows without loading a whole result payload.
HEADLINE_KEYS = ("net_return_pct", "max_drawdown_pct", "trades", "sharpe", "profit_factor", "win_rate_pct")


class RunCancelled(Exception):
    """Raised inside a running study when the operator cancelled it.

    A study reports progress at natural boundaries -每 candidate, 每 window, 每
    member - and that is where a cancellation is honoured. Waiting for the whole
    search to finish before discarding it would keep burning the CPU the operator
    just asked to free.
    """


def _now() -> int:
    return int(time.time() * 1000)


def request_hash(kind: str, body: dict) -> str:
    """The identity of a request: kind plus the body, canonically encoded."""
    material = json.dumps({"kind": kind, "request": body}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=False, default=str)


def _notify_in_background(run: dict) -> None:
    """Notify without making the queue wait for a plugin subprocess.

    A notifier is an external process with its own timeout; a study queue that
    blocks on it would turn one slow notifier into a stalled backfill.
    """
    thread = threading.Thread(target=_notify_run, args=(run,), name="quantdesk-run-notify", daemon=True)
    thread.start()


def _notify_run(run: dict, *, home=None) -> None:
    """Tell enabled notifier plugins that a run ended.

    A study that takes minutes and then fails silently is the worst of both
    worlds: the operator has to keep a page open to find out. The event carries
    the same facts the page shows, and a missing notifier is simply no delivery -
    it never changes the run's own status.
    """
    try:
        from datetime import datetime, timezone

        from .config.settings import quantdesk_home
        from .plugins import NotificationEvent, PluginManager, PluginRegistry

        status = str(run.get("status") or "")
        if status not in ("done", "failed"):
            return
        success = status == "done"
        headline = run.get("headline") or {}
        bits = []
        if headline.get("netReturnPct") is not None:
            bits.append(f"净收益 {float(headline['netReturnPct']):.2f}%")
        if headline.get("trades") is not None:
            bits.append(f"交易 {int(headline['trades'])} 笔")
        if headline.get("scope"):
            bits.append(str(headline["scope"]))
        message = "；".join(bits) or (run.get("error") or "已完成")
        if not success and run.get("error"):
            message = str(run["error"])[:1500]
        event = NotificationEvent(
            id=f"run-{run.get('id')}",
            type=f"backtest_run.{status}",
            severity="info" if success else "warning",
            title=f"研究 #{run.get('id')} {'完成' if success else '失败'}：{run.get('label') or run.get('kind')}",
            message=message[:4000],
            occurredAt=datetime.now(timezone.utc).isoformat(),
            symbol=(run.get("symbol") or None),
            data={
                "runId": run.get("id"), "kind": run.get("kind"), "status": status,
                "durationMs": run.get("durationMs"), "artifacts": run.get("artifacts") or [],
            },
        )
        PluginRegistry(PluginManager(home or quantdesk_home())).notify_all(event)
    except Exception:  # noqa: BLE001 - a notification must never fail a study
        pass


class RunQueue:
    """A persistent queue of formal studies, executed by one background worker."""

    def __init__(self, db: Database, *, execute: Callable[[dict, Callable[[float, str], None]], dict] | None = None,
                 lease: str | None = None):
        self.db = db
        # The executor is injectable so a test can drive the queue without
        # recomputing a backtest, and so a caller can see exactly what runs.
        self._execute = execute or _execute_run_subprocess
        # This process's claim on the runs it takes. A study is CPU work inside a
        # thread that a shutdown cannot interrupt, so the process that lost its
        # lease must not be allowed to publish a result for a run that has since
        # been recomputed by whoever holds it now.
        self.lease = lease or f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        # Set while the engine is shutting down. A study that dies *because the
        # engine is leaving* has not failed: the row is left `running` so the next
        # process requeues and recomputes it, instead of recording a failure that
        # describes the shutdown rather than the study.
        self.stopping = False

    # -- submission ------------------------------------------------------
    def submit(self, kind: str, body: dict, *, label: str = "", deduplicate: bool = True) -> dict:
        """Queue one study and return its row. Never computes anything."""
        if kind not in RUN_KINDS:
            raise StudyError("invalid", f"不支持的研究类型：{kind}；可用：{', '.join(RUN_KINDS)}")
        # Parse now so an invalid body is refused at submission rather than
        # turning into a mysterious failed run five minutes later.
        request = parse_request(kind, body)
        strategy_id = str(getattr(request, "strategyId", "") or "")
        identity = strategy_identity(strategy_id, getattr(request, "strategyParams", None)
                                     or self._parameters_from(request))
        digest = request_hash(kind, body)
        if deduplicate:
            existing = self.find_active(digest)
            if existing is not None:
                return {**self._summary(existing), "deduplicated": True}

        symbols = list(getattr(request, "symbols", None) or [getattr(request, "symbol", "")])
        display = symbols[0] if len(symbols) == 1 else f"{len(symbols)} 个合约"
        record_strategy_version(self.db, identity)
        now = _now()
        self.db.execute(
            "INSERT INTO backtest_runs "
            "(kind, status, label, symbol, display_symbol, interval, strategy_id, strategy_version, "
            " request_hash, request_json, progress, progress_label, stage, queued_ts, updated_ts) "
            "VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, 0, '已排队，等待执行', 'queued', ?, ?)",
            (
                kind, label or self._default_label(kind, request), ",".join(symbols), display,
                getattr(request, "timeframe", "") or getattr(request, "interval", ""),
                strategy_id, identity["version"],
                digest, _json(body), now, now,
            ),
        )
        row = self.db.query("SELECT * FROM backtest_runs WHERE request_hash=? ORDER BY id DESC LIMIT 1", (digest,))
        return self._summary(row[0])

    @staticmethod
    def _parameters_from(request) -> dict:
        if getattr(request, "strategyId", "") == "ma_cross":
            return {"fastPeriod": getattr(request, "fastPeriod", 9), "slowPeriod": getattr(request, "slowPeriod", 21)}
        return {}

    @staticmethod
    def _default_label(kind: str, request) -> str:
        symbols = list(getattr(request, "symbols", None) or [getattr(request, "symbol", "")])
        scope = "、".join(symbols[:3]) + ("…" if len(symbols) > 3 else "")
        names = {"backtest": "单标的回测", "validate": "参数搜索与滚动验证",
                 "portfolio": "组合回测", "factors": "因子计算"}
        factors = getattr(request, "factorIds", None)
        suffix = f"（{len(factors)} 个因子）" if kind == "factors" and factors else ""
        return f"{names.get(kind, kind)}：{scope or '—'}{suffix}"

    def find_active(self, digest: str) -> dict | None:
        rows = self.db.query(
            "SELECT * FROM backtest_runs WHERE request_hash=? AND status IN ('queued','running') "
            "ORDER BY id DESC LIMIT 1",
            (digest,),
        )
        return rows[0] if rows else None

    # -- claiming and running -------------------------------------------
    def claim_next(self) -> dict | None:
        """Take the oldest queued run. The lease is the row's own status."""
        rows = self.db.query(
            "SELECT * FROM backtest_runs WHERE status='queued' AND cancel_requested=0 "
            "ORDER BY id LIMIT 1"
        )
        if not rows:
            return None
        run_id = int(rows[0]["id"])
        now = _now()
        self.db.execute(
            "UPDATE backtest_runs SET status='running', started_ts=?, attempts=attempts+1, "
            "progress=1, progress_label='开始执行', stage='starting', lease=?, updated_ts=? "
            "WHERE id=? AND status='queued'",
            (now, self.lease, now, run_id),
        )
        owned = self.db.query(
            "SELECT status, lease FROM backtest_runs WHERE id=?", (run_id,)
        )
        if not owned or owned[0]["status"] != "running" or owned[0]["lease"] != self.lease:
            return None
        return self.get(run_id)

    def run_claimed(self, run: dict) -> dict:
        """Execute one claimed run and store everything it produced."""
        run_id = int(run["id"])
        started = _now()

        def progress(fraction: float, label: str) -> None:
            if self.is_cancelled(run_id):
                raise RunCancelled(f"任务 {run_id} 已取消")
            value = max(0.0, min(100.0, float(fraction) * 100.0))
            self.db.execute(
                "UPDATE backtest_runs SET progress=?, progress_label=?, stage=?, updated_ts=? WHERE id=?",
                (value, label, _stage_of(label), _now(), run_id),
            )

        if self.is_cancelled(run_id):
            return self.cancel_now(run_id)
        try:
            # Read the stored request rather than trusting the caller's copy: a
            # worker that resumes after a restart only has the row.
            rows = self.db.query(
                "SELECT kind, request_json FROM backtest_runs WHERE id=?", (run_id,)
            )
            if not rows:
                raise StudyError("invalid", f"任务 {run_id} 已不存在")
            kind = rows[0]["kind"]
            body = json.loads(rows[0]["request_json"] or "{}")
            request = parse_request(kind, body)
            result = self._execute(
                {"run": {**run, "kind": kind}, "request": request, "db": self.db}, progress
            )
        except RunCancelled:
            return self.cancel_now(run_id)
        except ChildStudyFailed as exc:
            return self._leave_alone_if_stopping(run_id) or self.fail(run_id, str(exc), exc.kind)
        except StudyError as exc:
            message = exc.message if exc.detail is None else _json(exc.detail)
            return self._leave_alone_if_stopping(run_id) or self.fail(run_id, message, exc.kind)
        except Exception as exc:  # noqa: BLE001 - a failed run stays a failed run
            return self._leave_alone_if_stopping(run_id) or self.fail(
                run_id, f"{type(exc).__name__}: {exc}", "internal")
        if self.is_cancelled(run_id):
            return self.cancel_now(run_id)
        return self.finish(run_id, result, started)

    def holds(self, run_id: int) -> bool:
        """Is this process the owner of the run's current attempt?"""
        rows = self.db.query("SELECT lease FROM backtest_runs WHERE id=?", (int(run_id),))
        return bool(rows) and rows[0]["lease"] == self.lease

    def superseded(self, run_id: int) -> bool:
        """Was this run taken over by *another* process?

        The guard exists so a killed process's late write cannot overwrite a
        recomputed result. A run nobody holds (queued, or failed before it was
        ever claimed) is not superseded by anyone, so writing to it stays allowed -
        which is what an operator action or a direct failure path needs.
        """
        rows = self.db.query("SELECT lease FROM backtest_runs WHERE id=?", (int(run_id),))
        if not rows:
            return True
        lease = rows[0]["lease"]
        return bool(lease) and lease != self.lease

    def finish(self, run_id: int, result: dict, started: int) -> dict:
        """Store a completed run: result, artifacts, verdicts, headline.

        A superseded attempt - one whose process was killed while its thread kept
        computing, and whose run has since been recomputed - drops its result
        here rather than overwriting the newer one.
        """
        if self.superseded(run_id):
            return self.get(run_id)
        summary = self._headline(result)
        now = _now()
        # The result is written first: a row that says `done` must already have
        # something to read. Each statement commits on its own, so a crash between
        # them leaves a done run with a partial artifact set - which the artifact
        # index shows honestly - rather than a run that claims a result it lacks.
        self.db.execute(
            "UPDATE backtest_runs SET status='done', result_json=?, summary_json=?, progress=100, "
            "progress_label='已完成', stage='done', error=NULL, error_kind=NULL, "
            "duration_ms=?, finished_ts=?, updated_ts=? WHERE id=?",
            (_json(result), _json(summary), max(0, now - started), now, now, run_id),
        )
        self._store_artifacts(run_id, result)
        self._store_verdicts(run_id, result)
        finished = self.get(run_id)
        _notify_in_background(finished)
        return finished

    def _store_artifacts(self, run_id: int, result: dict) -> None:
        for name, media_type, payload in artifacts_of(result):
            text = payload if isinstance(payload, str) else _json(payload)
            digest = hashlib.sha256(text.encode()).hexdigest()
            self.db.execute(
                "INSERT OR REPLACE INTO backtest_artifacts "
                "(run_id, name, media_type, bytes, sha256, payload, created_ts) VALUES (?,?,?,?,?,?,?)",
                (run_id, name, media_type, len(text.encode()), digest, text, _now()),
            )

    def _store_verdicts(self, run_id: int, result: dict) -> None:
        self.db.execute("DELETE FROM backtest_validation_results WHERE run_id=?", (run_id,))
        for row in verdicts_of(result):
            self.db.execute(
                "INSERT INTO backtest_validation_results "
                "(run_id, kind, verdict, statistic, p_value, threshold, provider, detail, detail_json, created_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, row["kind"], row["verdict"], row.get("statistic"), row.get("pValue"),
                    row.get("threshold"), row.get("provider") or "engine", row.get("detail") or "",
                    _json(row.get("detailJson")) if row.get("detailJson") is not None else None, _now(),
                ),
            )

    @staticmethod
    def _headline(result: dict) -> dict:
        """The numbers the list view shows, taken from where each kind keeps them.

        A single backtest puts its metrics at the top level. A validation run's
        honest headline is the *out-of-sample* figure, and a portfolio's is the
        combined book, so the scope travels with the numbers instead of the page
        implying they all mean the same thing.
        """
        metrics, scope = _headline_metrics(result)
        if "factorRunId" in result:
            # A factor run has no P&L: its headline is how much of the history each
            # factor could actually speak about, and the page shows that instead of
            # a column of dashes pretending to be returns.
            coverage = [value for value in (result.get("coverage") or {}).values() if value]
            bars = int(result.get("bars") or 0)
            return {
                "scope": "因子覆盖率（无盈亏指标）",
                "netReturnPct": None,
                "maxDrawdownPct": None,
                "trades": None,
                "sharpe": None,
                "profitFactor": None,
                "winRatePct": None,
                "factors": len(result.get("coverage") or {}),
                "coveredFactors": len(coverage),
                "medianCoverage": (sorted(coverage)[len(coverage) // 2] if coverage else None),
                "bars": bars or None,
            }
        return {
            "scope": scope,
            "netReturnPct": _first_number(metrics, "net_return_pct", "total_return_pct", "netReturnPct"),
            "maxDrawdownPct": _first_number(metrics, "max_drawdown_pct", "maxDrawdownPct"),
            "trades": _trade_count(metrics),
            "sharpe": _first_number(metrics, "sharpe", "sharpe_ratio"),
            "profitFactor": _first_number(metrics, "profit_factor", "profitFactor"),
            "winRatePct": _first_number(metrics, "win_rate_pct", "winRatePct"),
        }

    def _leave_alone_if_stopping(self, run_id: int) -> dict | None:
        """During shutdown a study's death is the engine leaving, not a failure."""
        if not self.stopping:
            return None
        self.db.execute(
            "UPDATE backtest_runs SET progress_label='服务重启中，稍后重新排队', updated_ts=? WHERE id=?",
            (_now(), run_id),
        )
        return self.get(run_id)

    def fail(self, run_id: int, message: str, kind: str) -> dict:
        if self.superseded(run_id):
            return self.get(run_id)
        now = _now()
        # A failure still took time, and "how long before it failed" is part of
        # understanding it, so the duration is recorded from the attempt's start.
        self.db.execute(
            "UPDATE backtest_runs SET status='failed', error=?, error_kind=?, progress_label=?, "
            "stage='failed', finished_ts=?, "
            "duration_ms=CASE WHEN started_ts IS NOT NULL THEN ? - started_ts ELSE duration_ms END, "
            "updated_ts=? WHERE id=?",
            (message, kind, _FAILURE_LABELS.get(kind, "执行失败"), now, now, now, run_id),
        )
        failed = self.get(run_id)
        _notify_in_background(failed)
        return failed

    def cancel_now(self, run_id: int) -> dict:
        if self.superseded(run_id):
            return self.get(run_id)
        now = _now()
        self.db.execute(
            "UPDATE backtest_runs SET status='cancelled', progress_label='已取消', stage='cancelled', "
            "finished_ts=COALESCE(finished_ts, ?), updated_ts=? WHERE id=?",
            (now, now, run_id),
        )
        return self.get(run_id)

    # -- controls --------------------------------------------------------
    def is_cancelled(self, run_id: int) -> bool:
        rows = self.db.query("SELECT cancel_requested, status FROM backtest_runs WHERE id=?", (int(run_id),))
        if not rows:
            return True
        return bool(rows[0]["cancel_requested"]) or rows[0]["status"] == "cancelled"

    def cancel(self, run_id: int) -> dict:
        run = self.get(run_id)
        if run["status"] in FINAL_STATUSES:
            return run
        now = _now()
        if run["status"] == "queued":
            # Nothing has started, so it is cancelled outright.
            self.db.execute(
                "UPDATE backtest_runs SET status='cancelled', cancel_requested=1, progress_label='已取消', "
                "stage='cancelled', finished_ts=?, updated_ts=? WHERE id=?",
                (now, now, run_id),
            )
            return self.get(run_id)
        # A running study is a CPU-bound thread; it is stopped at the next
        # checkpoint and its result is discarded rather than published.
        self.db.execute(
            "UPDATE backtest_runs SET cancel_requested=1, progress_label='正在安全停止', updated_ts=? WHERE id=?",
            (now, run_id),
        )
        return self.get(run_id)

    def retry(self, run_id: int) -> dict:
        run = self.get(run_id)
        if run["status"] not in FINAL_STATUSES:
            return run
        now = _now()
        self.db.execute(
            "UPDATE backtest_runs SET status='queued', cancel_requested=0, error=NULL, error_kind=NULL, "
            "progress=0, progress_label='已重新排队', stage='queued', result_json=NULL, summary_json=NULL, "
            "started_ts=NULL, finished_ts=NULL, duration_ms=NULL, lease=NULL, updated_ts=? WHERE id=?",
            (now, run_id),
        )
        self.db.execute("DELETE FROM backtest_artifacts WHERE run_id=?", (run_id,))
        self.db.execute("DELETE FROM backtest_validation_results WHERE run_id=?", (run_id,))
        return self.get(run_id)

    def delete(self, run_id: int) -> bool:
        run = self.get(run_id)
        if run["status"] == "running":
            raise StudyError("invalid", "运行中的任务不能删除，请先取消", status=409)
        self.db.execute("DELETE FROM backtest_artifacts WHERE run_id=?", (run_id,))
        self.db.execute("DELETE FROM backtest_validation_results WHERE run_id=?", (run_id,))
        self.db.execute("DELETE FROM backtest_runs WHERE id=?", (run_id,))
        return True

    # -- reading ---------------------------------------------------------
    def get(self, run_id: int, *, with_result: bool = False) -> dict:
        rows = self.db.query("SELECT * FROM backtest_runs WHERE id=?", (int(run_id),))
        if not rows:
            raise KeyError(f"没有找到回测任务 {run_id}")
        row = rows[0]
        detail = self._summary(row)
        detail["request"] = json.loads(row["request_json"] or "{}")
        detail["validation"] = self.verdicts(int(row["id"]))
        detail["artifactIndex"] = self.artifacts(int(row["id"]))
        if with_result:
            result = json.loads(row["result_json"]) if row.get("result_json") else None
            detail["result"] = result
            detail["readiness"] = (result or {}).get("readiness")
        return detail

    def list(self, *, status: str | None = None, kind: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM backtest_runs WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [self._summary(row) for row in self.db.query(sql, tuple(params))]

    def summary(self) -> dict:
        rows = self.db.query("SELECT status, COUNT(*) AS count FROM backtest_runs GROUP BY status")
        counts = {row["status"]: int(row["count"]) for row in rows}
        for name in RUN_STATUSES:
            counts.setdefault(name, 0)
        counts["total"] = sum(counts[name] for name in RUN_STATUSES)
        return counts

    def artifacts(self, run_id: int) -> list[dict]:
        rows = self.db.query(
            "SELECT name, media_type, bytes, sha256, created_ts FROM backtest_artifacts "
            "WHERE run_id=? ORDER BY name",
            (int(run_id),),
        )
        return [
            {
                "name": row["name"], "mediaType": row["media_type"], "bytes": int(row["bytes"]),
                "sha256": row["sha256"], "createdTs": int(row["created_ts"]),
            }
            for row in rows
        ]

    def artifact_payload(self, run_id: int, name: str) -> dict:
        rows = self.db.query(
            "SELECT name, media_type, payload, sha256, bytes FROM backtest_artifacts "
            "WHERE run_id=? AND name=?",
            (int(run_id), name),
        )
        if not rows:
            raise KeyError(f"任务 {run_id} 没有产物 {name}")
        row = rows[0]
        return {
            "name": row["name"], "mediaType": row["media_type"], "payload": row["payload"],
            "sha256": row["sha256"], "bytes": int(row["bytes"]),
        }

    def verdicts(self, run_id: int) -> list[dict]:
        rows = self.db.query(
            "SELECT kind, verdict, statistic, p_value, threshold, provider, detail, sandbox "
            "FROM backtest_validation_results WHERE run_id=? ORDER BY id",
            (int(run_id),),
        )
        return [
            {
                "kind": row["kind"], "verdict": row["verdict"], "statistic": row["statistic"],
                "pValue": row["p_value"], "threshold": row["threshold"],
                "provider": row["provider"], "detail": row["detail"],
                "sandbox": row["sandbox"] if "sandbox" in row.keys() else "",
            }
            for row in rows
        ]

    def _summary(self, row: dict) -> dict:
        headline = json.loads(row["summary_json"]) if row.get("summary_json") else {
            "netReturnPct": None, "maxDrawdownPct": None, "trades": None,
            "sharpe": None, "profitFactor": None, "winRatePct": None,
        }
        result = json.loads(row["result_json"]) if row.get("result_json") else None
        return {
            "id": int(row["id"]),
            "kind": row["kind"],
            "status": row["status"],
            "label": row["label"] or "",
            "symbol": row["symbol"] or None,
            "displaySymbol": row["display_symbol"],
            "symbols": [item for item in (row["symbol"] or "").split(",") if item],
            "interval": row["interval"] or "",
            "strategyId": row["strategy_id"] or "",
            "strategyVersion": row["strategy_version"] or "",
            "progress": round(float(row["progress"] or 0.0), 2),
            "progressLabel": row["progress_label"] or "",
            "stage": row["stage"] or "",
            "attempts": int(row["attempts"] or 0),
            "queuedTs": int(row["queued_ts"]),
            "startedTs": int(row["started_ts"]) if row["started_ts"] else None,
            "finishedTs": int(row["finished_ts"]) if row["finished_ts"] else None,
            "durationMs": int(row["duration_ms"]) if row["duration_ms"] is not None else None,
            "error": row["error"],
            "errorKind": row["error_kind"] or "",
            "headline": headline,
            "artifacts": [item["name"] for item in self.artifacts(int(row["id"]))],
            "verdicts": self.verdicts(int(row["id"])),
            "dataReady": None if result is None else bool(result.get("dataReady", True)),
            "degraded": None if result is None else bool(result.get("degraded")),
            "missingData": [] if result is None else list(result.get("missingData") or []),
        }


def _stage_of(label: str) -> str:
    if "因子批次" in label or "准备" in label:
        return "factors"
    if "覆盖率" in label:
        return "collecting"
    if "门禁" in label or "读取" in label:
        return "reading"
    if "参数搜索" in label:
        return "searching"
    if "滚动窗口" in label:
        return "walk_forward"
    if "回放" in label or "成员" in label:
        return "backtesting"
    if "整理" in label or "合并" in label:
        return "collecting"
    return "running"


_FAILURE_LABELS = {
    "not_ready": "数据未就绪，已阻止正式研究",
    "invalid": "请求无效",
    "too_large": "工作量超过同步上限",
    "internal": "引擎内部错误",
    "cancelled": "已取消",
}


def _first_number(result: dict, *keys: str) -> float | None:
    for key in keys:
        value = result.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _trade_count(metrics: dict) -> float | None:
    trades = metrics.get("trades")
    if isinstance(trades, list):
        return float(len(trades))
    return _first_number(metrics, "trades", "trade_count", "tradeCount", "total_trades")


def _headline_metrics(result: dict) -> tuple[dict, str]:
    """Where this kind of study keeps the numbers a reader should judge it by."""
    if result.get("parameterSearch") is not None:
        search = result["parameterSearch"] or {}
        # Out-of-sample first: the test segment is scored once, on the parameters
        # the validation segment selected, so it is the figure that means something.
        for candidate, scope in (
            (search.get("test"), "样本外测试段"),
            ((search.get("best") or {}).get("outOfSample"), "样本外验证段"),
            ((search.get("best") or {}).get("inSample"), "样本内训练段"),
        ):
            if candidate:
                return candidate, scope
        windows = [item.get("validation") for item in (result.get("walkForward") or {}).get("windows") or []]
        windows = [item for item in windows if item]
        if windows:
            return windows[-1], f"最近一个滚动窗口（共 {len(windows)} 个）"
    if result.get("portfolio") is not None:
        return result["portfolio"] or {}, "组合合并账本"
    return result, "整段回测"


STUDY_TIMEOUT_SECONDS = 30 * 60

# The study processes this engine has running right now. A thread cannot be
# interrupted, but a child process can be killed, so shutdown terminates them:
# otherwise a restart leaves an orphan computing a result its lease no longer
# allows anyone to publish.
_LIVE_CHILDREN: dict[int, Any] = {}
_CHILDREN_LOCK = threading.Lock()


def register_study_child(process: Any) -> None:
    with _CHILDREN_LOCK:
        _LIVE_CHILDREN[int(process.pid)] = process


def forget_study_child(process: Any) -> None:
    with _CHILDREN_LOCK:
        _LIVE_CHILDREN.pop(int(process.pid), None)


def live_study_children() -> list[int]:
    with _CHILDREN_LOCK:
        return sorted(_LIVE_CHILDREN)


def terminate_study_children(*, grace_seconds: float = 3.0) -> list[int]:
    """Stop every study process this engine started, and say which were running."""
    with _CHILDREN_LOCK:
        children = list(_LIVE_CHILDREN.items())
    stopped: list[int] = []
    for pid, process in children:
        try:
            process.terminate()
            stopped.append(pid)
        except Exception:  # noqa: BLE001 - already gone is the normal race
            continue
    deadline = time.monotonic() + max(0.0, grace_seconds)
    for pid, process in children:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining or 0.1)
        except Exception:  # noqa: BLE001 - still alive after the grace period
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
        finally:
            forget_study_child(process)
    return stopped


def _execute_run(context: dict, progress: Callable[[float, str], None]) -> dict:
    """Run a study inside this process.

    Kept for callers that already have the data in hand (and for tests). The
    queue does *not* use it: a study here holds the interpreter and the API stops
    answering, which is the whole reason the queue exists.
    """
    return run_study(context["db"], context["run"]["kind"], context["request"], progress=progress)


class ChildStudyFailed(RuntimeError):
    """The study process ended without producing a result."""

    def __init__(self, message: str, *, kind: str = "internal", detail: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.detail = detail


def _execute_run_subprocess(context: dict, progress: Callable[[float, str], None]) -> dict:
    """Run a study in a child process and stream its progress back.

    The parent keeps serving requests while the child computes, so the page that
    submitted the study keeps working. Cancellation kills the child: the operator
    asked for the CPU back, and a study that keeps computing after "cancel" is
    not cancelled.
    """
    import queue as queue_module
    import subprocess
    import tempfile
    import threading

    request = context["request"]
    kind = context["run"]["kind"]
    with tempfile.TemporaryDirectory(prefix="quantdesk-study-") as tmp:
        result_path = Path(tmp) / "result.json"
        error_path = Path(tmp) / "stderr.log"
        command = [
            sys.executable, "-m", "quantdesk.studies_cli", "--result-file", str(result_path),
            # The child watches this pid and exits if it disappears, so a study
            # cannot outlive the engine even when the engine was killed outright.
            "--parent-pid", str(os.getpid()),
        ]
        with error_path.open("w", encoding="utf-8") as errors:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                text=True, cwd=str(Path(__file__).resolve().parents[2]),
                env={**os.environ},
            )
            assert process.stdin is not None and process.stdout is not None
            register_study_child(process)
            process.stdin.write(_json({"kind": kind, "request": request.model_dump()}))
            process.stdin.close()

            lines: "queue_module.Queue[str | None]" = queue_module.Queue()

            def pump() -> None:
                for line in process.stdout:  # type: ignore[union-attr]
                    lines.put(line)
                lines.put(None)

            reader = threading.Thread(target=pump, name="study-progress", daemon=True)
            reader.start()

            deadline = time.monotonic() + STUDY_TIMEOUT_SECONDS
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        process.kill()
                        raise ChildStudyFailed(
                            f"研究超过 {STUDY_TIMEOUT_SECONDS // 60} 分钟仍未结束，已终止", kind="timeout"
                        )
                    try:
                        line = lines.get(timeout=1.0)
                    except queue_module.Empty:
                        if process.poll() is not None:
                            break
                        continue
                    if line is None:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a stray print is not progress
                    if "progress" in event:
                        # Raises RunCancelled when the operator cancelled; the
                        # caller kills the child and marks the run cancelled.
                        progress(float(event["progress"]), str(event.get("label") or ""))
            except RunCancelled:
                process.kill()
                process.wait(timeout=10)
                forget_study_child(process)
                raise
            code = process.wait()
            forget_study_child(process)
        tail = ""
        if error_path.exists():
            tail = error_path.read_text(encoding="utf-8", errors="replace").strip()[-600:]

        payload: dict = {}
        if result_path.exists():
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
        if code == 0 and "result" in payload:
            return payload["result"]
        error = payload.get("error") or {}
        message = error.get("message") or tail or f"研究进程退出码 {code}，没有返回结果"
        raise ChildStudyFailed(message, kind=str(error.get("kind") or "internal"),
                               detail=error.get("detail"))


def artifacts_of(result: dict) -> list[tuple[str, str, Any]]:
    """The parts of a result worth handing over without the whole payload."""
    out: list[tuple[str, str, Any]] = []
    metrics = {
        key: result.get(key)
        for key in (
            "symbol", "displaySymbol", "interval", "bars", "initial_capital", "final_equity",
            "net_return_pct", "max_drawdown_pct", "win_rate_pct", "profit_factor", "sharpe",
            "sortino", "calmar", "total_fees", "total_funding", "exposure_pct",
        )
        if key in result
    }
    if metrics:
        out.append(("metrics", "application/json", metrics))
    curve = result.get("equity_curve") or result.get("equityCurve")
    if curve:
        out.append(("equity", "application/json", curve))
    trades = result.get("trades")
    if isinstance(trades, list) and trades:
        out.append(("trades", "text/csv", _trades_csv(trades)))
    if result.get("walkForward"):
        out.append(("walkForward", "application/json", result["walkForward"]))
    if result.get("kind") == "cpa-ablation" and result.get("table"):
        out.append(("cpa-ablation", "application/json", result))
    if result.get("cpaOrders"):
        # The intent model's order ledger: which order carried which fee, what was
        # added or reduced, and what the risk budget refused.
        out.append(("cpa-orders", "application/json", result["cpaOrders"]))
    if result.get("cpaPhases"):
        # The phase series a CPA run was built from, as its own attachment: a reader
        # can line the trades up against the phases that produced them without
        # re-running the detector.
        out.append(("cpa-phases", "application/json", result["cpaPhases"]))
    selected = result.get("selectedRun") or {}
    if selected.get("equityCurve"):
        out.append(("selectedEquity", "application/json", selected["equityCurve"]))
    if selected.get("trades"):
        out.append(("selectedTrades", "text/csv", _trades_csv(selected["trades"])))
    if selected:
        out.append(("selectedMetrics", "application/json",
                    {key: value for key, value in selected.items()
                     if key not in ("equityCurve", "trades")}))
    if result.get("parameterSearch"):
        out.append(("parameterSearch", "application/json", result["parameterSearch"]))
    if result.get("leakage"):
        out.append(("leakage", "application/json", result["leakage"]))
    if result.get("portfolioCoverage"):
        out.append(("coverage", "application/json", result["portfolioCoverage"]))
    if result.get("members"):
        out.append(("members", "application/json", result["members"]))
    if result.get("readiness"):
        out.append(("readiness", "application/json", result["readiness"]))
    if result.get("factorRunId"):
        out.append(("coverage", "application/json", result.get("coverage") or {}))
        out.append(("factors", "application/json", {
            "factorRunId": result["factorRunId"],
            "symbol": result.get("symbol"), "interval": result.get("interval"),
            "bars": result.get("bars"), "batches": result.get("batches"),
            "snapshotHash": result.get("snapshotHash"),
            "factorIds": sorted((result.get("coverage") or {})),
            "readBack": result.get("readBack"),
        }))
    return out


def _trades_csv(trades: list[dict]) -> str:
    columns: list[str] = []
    for trade in trades:
        for key in trade:
            if key not in columns:
                columns.append(key)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for trade in trades:
        writer.writerow({key: trade.get(key) for key in columns})
    return buffer.getvalue()


def verdicts_of(result: dict) -> list[dict]:
    """Turn a study's validation sections into rows with numbers behind them.

    A validation run answers three questions: are the chosen parameters stable
    across windows, do the windows make money at all, and does a signal only use
    the past? Each becomes one row so the answer travels with the result.
    """
    rows: list[dict] = []
    walk = result.get("walkForward") or {}
    windows = walk.get("windows") or []
    if windows:
        positive = int(walk.get("positiveWindows") or 0)
        total = len(windows)
        rows.append({
            "kind": "walk_forward",
            "verdict": "pass" if positive == total else ("warn" if positive * 2 >= total else "fail"),
            "statistic": float(positive),
            "threshold": float(total),
            "detail": f"{positive}/{total} 个滚动窗口验证段为正收益",
            "detailJson": {"positiveWindows": positive, "windows": total,
                           "stableParameters": bool(walk.get("stableParameters"))},
        })
        rows.append({
            "kind": "walk_forward_stability",
            "verdict": "pass" if walk.get("stableParameters") else "warn",
            "statistic": 1.0 if walk.get("stableParameters") else 0.0,
            "threshold": 1.0,
            "detail": "各窗口选出的参数一致" if walk.get("stableParameters")
            else "各窗口选出的参数不一致，说明参数对样本敏感",
        })
    leakage = result.get("leakage") or {}
    if leakage:
        clean = bool(leakage.get("clean", leakage.get("leakFree")))
        rows.append({
            "kind": "leakage",
            "verdict": "pass" if clean else "fail",
            "statistic": _first_number(leakage, "mismatches", "changed"),
            "threshold": 0.0,
            "detail": leakage.get("summary") or ("信号不随未来数据变化" if clean else "信号使用了未来数据"),
            "detailJson": {k: v for k, v in leakage.items() if k != "probes"},
        })
    search = result.get("parameterSearch") or {}
    if search.get("warnings"):
        rows.append({
            "kind": "overfit",
            "verdict": "warn",
            "statistic": _first_number(search, "degradation"),
            "threshold": None,
            "detail": "；".join(str(item) for item in search["warnings"]),
        })
    return rows


_RUN_QUEUES: dict[str, RunQueue] = {}


def get_run_queue(home: str | None = None) -> RunQueue:
    """One queue per database file, as the API workers use it.

    The API layer already keeps a process-wide worker; this accessor exists so a
    service function (the study endpoints' auto-queue path) can reach the *same*
    queue without importing the API module and creating a second worker.
    """
    from .config.settings import quantdesk_home

    key = str(home or quantdesk_home())
    if key not in _RUN_QUEUES:
        _RUN_QUEUES[key] = RunQueue(Database(Path(key) / "quantdesk.db"))
    return _RUN_QUEUES[key]


class BacktestRunWorker:
    """One process-wide consumer for the persistent run queue."""

    def __init__(self, queue: RunQueue, *, idle_seconds: float = 3.0):
        self.queue = queue
        self.idle_seconds = max(0.25, float(idle_seconds))
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def start(self) -> None:
        if self.running:
            return
        self.reclaim_interrupted()
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="quantdesk-backtest-runs")
        self._wake.set()

    def reclaim_interrupted(self) -> int:
        """Put runs left `running` by a dead process back on the queue.

        A study cannot be resumed half-way, and a run left `running` would
        otherwise sit there forever claiming to be in progress. It goes back on
        the queue and is recomputed from the same request.
        """
        rows = self.queue.db.query("SELECT COUNT(*) AS n FROM backtest_runs WHERE status='running'")
        count = int(rows[0]["n"]) if rows else 0
        if not count:
            return 0
        self.queue.db.execute(
            "UPDATE backtest_runs SET status='queued', started_ts=NULL, progress=0, lease=NULL, "
            "progress_label='服务重启后重新排队', stage='queued', updated_ts=? "
            "WHERE status='running'",
            (_now(),),
        )
        return count

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        # Order matters: the queue must know it is shutting down *before* the
        # children are killed, or the thread mid-study would record the kill as a
        # study failure and the run would never be recomputed.
        self.queue.stopping = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        # An in-flight study lives in a thread that cannot be cancelled; its child
        # process can be, and a restart must not leave one computing for nothing.
        await asyncio.to_thread(terminate_study_children)

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                run = await asyncio.to_thread(self.queue.claim_next)
                if run is not None:
                    await asyncio.to_thread(self.queue.run_claimed, run)
                    self.last_error = None
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad run cannot kill the queue
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.idle_seconds)
            except TimeoutError:
                pass

    def status(self) -> dict:
        counts = self.queue.summary()
        active = self.queue.list(status="running", limit=1)
        return {
            "workerRunning": self.running,
            "workerError": self.last_error,
            "concurrency": 1,
            "counts": counts,
            "activeRunId": active[0]["id"] if active else None,
            "activeLabel": active[0]["label"] if active else None,
            "activeProgress": active[0]["progress"] if active else None,
        }


def request_model_fields(kind: str) -> list[str]:
    """The accepted field names of a study request, for a client that asks."""
    model = REQUEST_MODELS.get(kind)
    return sorted(model.model_fields) if model else []
