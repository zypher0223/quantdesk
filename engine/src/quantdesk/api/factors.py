"""Factors and statistical validation, over the plugin boundary.

Read-only except for two actions: computing a factor set over stored history, and
asking the validator for a statistical reading of a finished run. Neither reaches
the network; both are recorded so the numbers can be attributed later.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .. import factors as factor_service
from ..config.settings import quantdesk_home
from ..datahub.db import Database
from ..plugins import PluginError
from ..studies import StudyError
from .trading import auto_queue_enabled

router = APIRouter(prefix="/api/factors", tags=["factors"])

MAX_FACTOR_BARS = 20_000


def _db() -> Database:
    home = quantdesk_home()
    try:
        return Database(home / "quantdesk.db")
    except Exception as exc:  # noqa: BLE001 - sqlite raises OperationalError
        raise HTTPException(503, f"无法打开本地数据库 {home / 'quantdesk.db'}：{exc}") from exc


class FactorComputeBody(BaseModel):
    symbol: str
    interval: str = "1h"
    bars: int = Field(2_000, ge=30, le=MAX_FACTOR_BARS)
    factorIds: list[str] | None = Field(None, max_length=64)
    parameters: dict = Field(default_factory=dict)
    # Values are the point of computing factors, but a caller that only wants the
    # coverage of a wide run can ask for the catalogue's shape without shipping
    # every point back over HTTP.
    includeValues: bool = True


class ValidationBody(BaseModel):
    seed: int = Field(42, ge=0)
    tests: dict = Field(default_factory=dict)


@router.get("")
async def factor_catalog(refresh: bool = Query(True)):
    """The factor library: from the enabled provider, or the stored catalogue."""
    return await run_in_threadpool(factor_service.catalog, _db(), None, refresh=refresh)


@router.get("/runs")
async def factor_runs(symbol: str | None = Query(None), limit: int = Query(20, ge=1, le=200)):
    """What factor computations have been run, without their values."""
    return {"runs": await run_in_threadpool(factor_service.runs, _db(), symbol=symbol, limit=limit)}


@router.get("/runs/{run_id}")
async def factor_run(run_id: int, values: bool = Query(False, description="是否返回整条因子序列")):
    """One factor computation: its coverage, and the values only if asked for."""
    try:
        return {"run": await run_in_threadpool(
            factor_service.run_detail, _db(), run_id, include_values=values)}
    except KeyError as exc:
        raise HTTPException(404, exc.args[0] if exc.args else str(exc)) from exc


class FactorQueuedBody(BaseModel):
    """A factor computation for the background queue."""

    symbol: str
    interval: str = "1h"
    bars: int = Field(2_000, ge=30, le=200_000)
    factorIds: list[str] | None = Field(None, max_length=64)
    parameters: dict = Field(default_factory=dict)
    label: str = ""


@router.post("/runs", status_code=202)
async def submit_factor_run(body: FactorQueuedBody):
    """Queue a factor computation and return immediately.

    The synchronous endpoint is bounded so a request stays an interaction; this
    one is not, because the work is done by the run queue's own process and the
    page watches it like any other run.
    """
    from .runs import get_run_worker

    worker = get_run_worker()
    payload = {
        "symbol": body.symbol, "interval": body.interval, "bars": body.bars,
        "factorIds": body.factorIds, "parameters": body.parameters,
    }
    try:
        run = await run_in_threadpool(
            worker.queue.submit, "factors", payload, label=body.label,
        )
    except StudyError as exc:
        if exc.detail is not None:
            raise HTTPException(exc.status, json.dumps(exc.detail, ensure_ascii=False)) from exc
        raise HTTPException(exc.status, exc.message) from exc
    worker.wake()
    return {"run": run, "deduplicated": bool(run.get("deduplicated"))}


@router.post("/compute")
async def factor_compute(body: FactorComputeBody, queue: str = Query("auto")):
    """Compute factors over one contract's stored history and record the run."""
    try:
        # The budget is checked before any work happens: a request that would run
        # for twenty seconds is a queued job, not an interaction - so unless the
        # caller opted out, it becomes one instead of being refused.
        known = factor_service.stored_catalog(_db())
        wanted = len(body.factorIds) if body.factorIds else len(known)
        if auto_queue_enabled(queue):
            units = factor_service.factor_units(body.bars, wanted)
            if units > factor_service.SYNC_FACTOR_UNIT_BUDGET:
                from .runs import get_run_worker

                worker = get_run_worker()
                queued = await run_in_threadpool(
                    worker.queue.submit, "factors",
                    {"symbol": body.symbol, "interval": body.interval, "bars": body.bars,
                     "factorIds": body.factorIds, "parameters": body.parameters},
                )
                worker.wake()
                return JSONResponse(status_code=202, content={
                    "queued": True, "run": queued,
                    "reason": (f"该因子请求预计 {units:,} 单位工作量，超过单次上限 "
                               f"{factor_service.SYNC_FACTOR_UNIT_BUDGET:,} 单位，已自动转入后台队列"),
                    "factorUnits": units,
                    "factorBudget": factor_service.SYNC_FACTOR_UNIT_BUDGET,
                })
        factor_service.require_factor_budget(body.bars, wanted)
        outcome = await run_in_threadpool(
            factor_service.compute, _db(),
            symbol=body.symbol, interval=body.interval, bars=body.bars,
            factor_ids=body.factorIds, parameters=body.parameters,
            include_values=body.includeValues,
        )
    except StudyError as exc:
        if exc.detail is not None:
            raise HTTPException(exc.status, json.dumps(exc.detail, ensure_ascii=False)) from exc
        raise HTTPException(exc.status, exc.message) from exc
    except PluginError as exc:
        raise HTTPException(502, f"因子插件执行失败：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not outcome.get("available"):
        raise HTTPException(409, outcome.get("reason") or "因子服务不可用")
    return outcome


@router.post("/validate/{run_id}")
async def validate_run(run_id: int, body: ValidationBody | None = None):
    """Statistical diagnostics for a finished backtest run.

    The validator describes the engine's result; it never recomputes it. A missing
    plugin is answered as unavailable rather than as an error, because the run
    itself is still perfectly readable.
    """
    request = body or ValidationBody()
    try:
        outcome = await run_in_threadpool(
            factor_service.analyze_run, _db(), run_id,
            seed=request.seed, tests=request.tests,
        )
    except KeyError as exc:
        raise HTTPException(404, exc.args[0] if exc.args else str(exc)) from exc
    except PluginError as exc:
        raise HTTPException(502, f"统计验证插件执行失败：{exc}") from exc
    if not outcome.get("available"):
        raise HTTPException(409, outcome.get("reason") or "统计验证服务不可用")
    return outcome
