"""Persistent single-worker queue for long-running TradingAgents jobs."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .config.instruments import require_instrument
from .config.settings import load_app_config, load_llm_settings
from .datahub.db import Database
from .llm.governance import report_meta, run_governed_symbol, run_payload
from .plugins import NotificationEvent, PluginManager, PluginRegistry
from .tradingagents_runner import runtime_status, target_for


FINAL_STATUSES = {"succeeded", "failed", "cancelled"}


class TradingAgentsJobQueue:
    def __init__(self, home: Path):
        self.home = Path(home)
        self._worker: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self.db = Database(self.home / "quantdesk.db")

    def _db(self) -> Database:
        return self.db

    async def start(self) -> None:
        if self._worker and not self._worker.done():
            return
        # A host restart cannot resume an in-memory graph. Requeue it from the
        # persisted request so no paid task disappears silently.
        self._db().execute(
            "UPDATE tradingagents_jobs SET status='queued', progress='服务重启后重新排队', started_ts=NULL "
            "WHERE status='running'"
        )
        self._stop.clear()
        self._worker = asyncio.create_task(self._loop(), name="quantdesk-tradingagents-queue")
        self._wake.set()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None

    async def enqueue(
        self,
        symbol: str,
        trade_date: str | None = None,
        analysts: list[str] | None = None,
        *,
        deduplicate: bool = False,
    ) -> str:
        spec = require_instrument(symbol)
        target = target_for(spec)
        value = trade_date or date.today().isoformat()
        parsed = date.fromisoformat(value)
        if parsed > date.today():
            raise ValueError("研判日期不能晚于今天")
        selected = list(analysts or target.analysts)
        allowed = {"market", "social", "news", "fundamentals"}
        if not selected or set(selected) - allowed:
            raise ValueError("分析师列表无效")
        profile = load_llm_settings(self.home).profile_for("tradingagents")
        db = self._db()
        if deduplicate:
            rows = db.query(
                "SELECT id FROM tradingagents_jobs WHERE venue_symbol=? AND trade_date=? "
                "AND status IN ('queued','running','succeeded') ORDER BY created_ts DESC LIMIT 1",
                (spec.venue_symbol, value),
            )
            if rows:
                return str(rows[0]["id"])
        job_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO tradingagents_jobs "
            "(id, venue_symbol, trade_date, analysts, profile, status, progress, created_ts) "
            "VALUES (?, ?, ?, ?, ?, 'queued', '等待执行', ?)",
            (
                job_id, spec.venue_symbol, value,
                json.dumps(selected, ensure_ascii=False), profile.name, int(time.time() * 1000),
            ),
        )
        self._wake.set()
        return job_id

    def get(self, job_id: str) -> dict[str, Any]:
        rows = self._db().query("SELECT * FROM tradingagents_jobs WHERE id=?", (job_id,))
        if not rows:
            raise KeyError(job_id)
        return self._decode(rows[0])

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._db().query(
            "SELECT * FROM tradingagents_jobs ORDER BY created_ts DESC LIMIT ?", (int(limit),)
        )
        return [self._decode(row) for row in rows]

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] in FINAL_STATUSES:
            return job
        now = int(time.time() * 1000)
        if job["status"] == "queued":
            self._db().execute(
                "UPDATE tradingagents_jobs SET status='cancelled', progress='已取消', cancel_requested=1, finished_ts=? WHERE id=?",
                (now, job_id),
            )
        else:
            # Upstream graph is a blocking subprocess. The current pass finishes
            # safely, then its result is discarded instead of being published.
            self._db().execute(
                "UPDATE tradingagents_jobs SET cancel_requested=1, progress='正在安全停止' WHERE id=?",
                (job_id,),
            )
        return self.get(job_id)

    def status(self) -> dict[str, Any]:
        rows = self._db().query(
            "SELECT status, COUNT(*) AS count FROM tradingagents_jobs GROUP BY status"
        )
        counts = {row["status"]: int(row["count"]) for row in rows}
        running = self._db().query(
            "SELECT id, venue_symbol, trade_date, progress, started_ts FROM tradingagents_jobs "
            "WHERE status='running' ORDER BY started_ts LIMIT 1"
        )
        return {
            "workerRunning": bool(self._worker and not self._worker.done()),
            "counts": counts,
            "active": running[0] if running else None,
        }

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["analysts"] = json.loads(value.get("analysts") or "[]")
        value["result"] = json.loads(value["result"]) if value.get("result") else None
        value["cancel_requested"] = bool(value.get("cancel_requested"))
        return value

    async def _loop(self) -> None:
        while not self._stop.is_set():
            rows = self._db().query(
                "SELECT id FROM tradingagents_jobs WHERE status='queued' AND cancel_requested=0 "
                "ORDER BY created_ts LIMIT 1"
            )
            if not rows:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=5)
                except TimeoutError:
                    pass
                continue
            await self._execute(str(rows[0]["id"]))

    async def _execute(self, job_id: str) -> None:
        started = int(time.time() * 1000)
        db = self._db()
        db.execute(
            "UPDATE tradingagents_jobs SET status='running', progress='多智能体正在研判', started_ts=?, error=NULL WHERE id=?",
            (started, job_id),
        )
        job = self.get(job_id)
        spec = require_instrument(job["venue_symbol"])
        profile = load_llm_settings(self.home).profiles.get(job["profile"])
        if profile is None:
            await self._fail(job_id, f"profile「{job['profile']}」不存在")
            return
        timeout = max(60, min(int(load_app_config(self.home).research.get("timeout_seconds", 1800)), 3600))
        run_id = str(uuid.uuid4())
        try:
            # The queue is a second door onto the same paid run, so it goes through
            # the same budget, reuse and ledger code as the synchronous route. A
            # job that spends money the ledger never saw is not a feature.
            runtime = await asyncio.to_thread(runtime_status)
            outcome = await asyncio.to_thread(
                run_governed_symbol,
                spec=spec,
                profile=profile,
                trade_date=job["trade_date"],
                analysts=job["analysts"],
                home=self.home,
                runtime_commit=runtime.get("commit"),
                timeout_seconds=timeout,
                run_id=run_id,
            )
            if outcome.get("refused"):
                await self._fail(job_id, f"已停止付费研判：{outcome.get('reason')}")
                return
            refreshed = self.get(job_id)
            if refreshed["cancel_requested"]:
                db.execute(
                    "UPDATE tradingagents_jobs SET status='cancelled', progress='已取消，结果未发布', finished_ts=? WHERE id=?",
                    (int(time.time() * 1000), job_id),
                )
                return
            result = run_payload(outcome, run_id)
            self._archive(outcome, result, spec.venue_symbol, job["trade_date"], profile.name)
            db.execute(
                "UPDATE tradingagents_jobs SET status='succeeded', progress='完成', result=?, finished_ts=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), int(time.time() * 1000), job_id),
            )
            await asyncio.to_thread(self._notify, job_id, spec.venue_symbol, "succeeded", result.get("rating"))
        except Exception as exc:  # noqa: BLE001 - job boundary records every failure
            # The type is kept because some failures carry no message at all, and
            # "failed: " tells the reader nothing about what to fix.
            await self._fail(job_id, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)

    def _archive(self, outcome: dict, result: dict, venue_symbol: str, trade_date: str, profile: str) -> str:
        run_id = result.get("runId") or str(uuid.uuid4())
        target = target_for(require_instrument(venue_symbol))
        self._db().execute(
            "INSERT INTO tradingagents_runs "
            "(id, venue_symbol, analysis_symbol, trade_date, asset_type, profile, rating, reports, debates, meta, error, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, venue_symbol, target.symbol, trade_date, target.asset_type, profile,
                outcome.get("rating"), json.dumps(result.get("reports") or {}, ensure_ascii=False),
                json.dumps(result.get("debates") or {}, ensure_ascii=False),
                json.dumps(report_meta(outcome), ensure_ascii=False),
                None if outcome.get("ok") else str((outcome.get("failure") or {}).get("message") or "运行失败"),
                int(time.time() * 1000),
            ),
        )
        return run_id

    async def _fail(self, job_id: str, message: str) -> None:
        self._db().execute(
            "UPDATE tradingagents_jobs SET status='failed', progress='失败', error=?, finished_ts=? WHERE id=?",
            (message[:8000], int(time.time() * 1000), job_id),
        )
        job = self.get(job_id)
        await asyncio.to_thread(self._notify, job_id, job["venue_symbol"], "failed", message)

    def _notify(self, job_id: str, symbol: str, status: str, detail: str | None) -> None:
        success = status == "succeeded"
        event = NotificationEvent(
            id=f"tradingagents-{job_id}",
            type=f"tradingagents.{status}",
            severity="info" if success else "warning",
            title=f"{symbol} 多智能体研判{'完成' if success else '失败'}",
            message=str(detail or ("研判报告已生成" if success else "未提供错误信息"))[:4000],
            occurredAt=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
            data={"jobId": job_id},
        )
        PluginRegistry(PluginManager(self.home)).notify_all(event)


_QUEUES: dict[Path, TradingAgentsJobQueue] = {}


def get_tradingagents_queue(home: Path) -> TradingAgentsJobQueue:
    path = Path(home)
    if path not in _QUEUES:
        _QUEUES[path] = TradingAgentsJobQueue(path)
    return _QUEUES[path]
