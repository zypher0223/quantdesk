"""The backtest result centre: submit a study, watch it, read what it produced.

A study submitted here is a row before it is work. The endpoints never compute a
study inside a request: they queue it, report its progress, and hand back the
stored result, its artifacts and its validation verdicts afterwards.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse

from ..backtest_runs import RUN_KINDS, BacktestRunWorker, RunQueue, get_run_queue as _queue_for_home
from ..config.settings import quantdesk_home
from ..datahub.db import Database
from ..studies import StudyError

router = APIRouter(prefix="/api/backtest", tags=["backtest-runs"])

_RUN_WORKER: BacktestRunWorker | None = None
_RUN_HOME = None


def get_run_queue() -> RunQueue:
    """The process-wide run queue.

    One queue per home, shared with the study endpoints' auto-queue path: two
    instances would mean two lease identities over the same rows, and a run
    claimed by one would look superseded to the other.
    """
    home = quantdesk_home()
    try:
        return _queue_for_home(str(home))
    except Exception as exc:  # noqa: BLE001 - sqlite raises OperationalError
        raise HTTPException(503, f"无法打开本地数据库 {home / 'quantdesk.db'}：{exc}") from exc


def get_run_worker() -> BacktestRunWorker:
    """The process-wide run queue. One study at a time: a backtest is CPU work."""
    global _RUN_WORKER, _RUN_HOME

    home = quantdesk_home()
    if _RUN_WORKER is not None and _RUN_HOME == home:
        return _RUN_WORKER
    _RUN_WORKER = BacktestRunWorker(get_run_queue())
    _RUN_HOME = home
    return _RUN_WORKER


def _queue() -> RunQueue:
    return get_run_worker().queue


def _translate(exc: StudyError) -> HTTPException:
    if exc.detail is not None:
        return HTTPException(exc.status, json.dumps(exc.detail, ensure_ascii=False))
    return HTTPException(exc.status, exc.message)


@router.post("/runs", status_code=202)
async def submit_run(payload: dict):
    """Queue one study. Returns immediately with the row it created.

    The body is `{"kind": "backtest" | "validate" | "portfolio", "request": {...},
    "label": "optional"}`. Submitting the same request twice while the first is
    still queued or running returns the existing run instead of starting a second
    copy of the same work.
    """
    kind = str(payload.get("kind") or "backtest")
    body = payload.get("request")
    if not isinstance(body, dict):
        raise HTTPException(422, "request 必须是对象：{'kind': ..., 'request': {...}}")
    if kind not in RUN_KINDS:
        raise HTTPException(422, f"不支持的研究类型：{kind}；可用：{', '.join(RUN_KINDS)}")
    worker = get_run_worker()
    try:
        run = await run_in_threadpool(
            worker.queue.submit, kind, body,
            label=str(payload.get("label") or ""),
            deduplicate=bool(payload.get("deduplicate", True)),
        )
    except StudyError as exc:
        raise _translate(exc) from exc
    worker.wake()
    return {"run": run, "deduplicated": bool(run.get("deduplicated"))}


@router.get("/runs")
async def list_runs(status: str | None = Query(None), kind: str | None = Query(None),
                    limit: int = Query(50, ge=1, le=500)):
    """The result centre's list, plus the queue's own state."""
    worker = get_run_worker()
    runs = await run_in_threadpool(worker.queue.list, status=status, kind=kind, limit=limit)
    # The active run is asked of the worker, not inferred from this page of rows:
    # a filter or a small limit must not make a busy queue look idle.
    state = worker.status()
    return {
        "summary": worker.queue.summary(),
        "queue": {
            "workerRunning": state["workerRunning"],
            "workerError": state["workerError"],
            "concurrency": state["concurrency"],
            "activeRunId": state["activeRunId"],
            "activeLabel": state.get("activeLabel"),
            "activeProgress": state.get("activeProgress"),
        },
        "runs": runs,
    }


@router.post("/maintenance/prune")
async def prune_history(dry_run: bool = Query(False, description="只报告，不删除")):
    """Apply the retention policy to finished runs, or report what it would do."""
    from ..retention import plan, policy_from_config, prune

    queue = _queue()
    policy = policy_from_config()
    if dry_run:
        return await run_in_threadpool(
            plan, queue.db, keep_runs=policy["keepRuns"],
            keep_factor_runs=policy["keepFactorRuns"], keep_days=policy["keepDays"])
    report = await run_in_threadpool(
        prune, queue.db, keep_runs=policy["keepRuns"],
        keep_factor_runs=policy["keepFactorRuns"], keep_days=policy["keepDays"])
    return {**report, "remaining": queue.summary()}


@router.get("/runs/queue")
async def queue_status():
    """Worker state on its own, for a header that polls faster than the list."""
    return get_run_worker().status()


@router.get("/runs/{run_id}")
async def get_run(run_id: int):
    """One run: its request, its result, its artifacts and its verdicts."""
    queue = _queue()
    try:
        run = await run_in_threadpool(queue.get, run_id, with_result=True)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0] if exc.args else str(exc)) from exc
    return {"run": run}


@router.get("/runs/{run_id}/artifacts/{name}")
async def get_artifact(run_id: int, name: str):
    """One artifact, exactly as it was stored, with its hash."""
    queue = _queue()
    try:
        artifact = await run_in_threadpool(queue.artifact_payload, run_id, name)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0] if exc.args else str(exc)) from exc
    headers = {
        "X-QuantDesk-Sha256": artifact["sha256"],
        "X-QuantDesk-Bytes": str(artifact["bytes"]),
        "Content-Disposition": f'inline; filename="run{run_id}-{name}"',
    }
    if artifact["mediaType"] == "text/csv":
        return PlainTextResponse(artifact["payload"], media_type="text/csv; charset=utf-8", headers=headers)
    try:
        parsed = json.loads(artifact["payload"])
    except json.JSONDecodeError:
        return PlainTextResponse(artifact["payload"], media_type="text/plain; charset=utf-8", headers=headers)
    return JSONResponse(parsed, headers=headers)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int):
    """Cancel a queued run outright; a running one stops at its next checkpoint."""
    queue = _queue()
    try:
        run = await run_in_threadpool(queue.cancel, run_id)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0] if exc.args else str(exc)) from exc
    return {"run": run}


@router.post("/runs/{run_id}/retry")
async def retry_run(run_id: int):
    """Queue the same request again, discarding the previous result."""
    worker = get_run_worker()
    try:
        run = await run_in_threadpool(worker.queue.retry, run_id)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0] if exc.args else str(exc)) from exc
    worker.wake()
    return {"run": run}


@router.delete("/runs/{run_id}")
async def delete_run(run_id: int):
    """Remove a finished run and everything stored with it."""
    queue = _queue()
    try:
        await run_in_threadpool(queue.delete, run_id)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0] if exc.args else str(exc)) from exc
    except StudyError as exc:
        raise _translate(exc) from exc
    return {"deleted": True, "id": int(run_id)}
