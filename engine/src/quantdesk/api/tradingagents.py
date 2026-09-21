"""HTTP boundary for the real upstream TradingAgents multi-agent graph."""

from __future__ import annotations

import json
import time
import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ..config.instruments import require_instrument
from ..config.settings import load_app_config, load_llm_settings, quantdesk_home
from ..datahub.db import Database
from ..llm import credential_issue, resolve_api_key
from ..llm.governance import (
    AgentCostGovernor,
    RunBudget,
    report_meta,
    run_governed_symbol,
    run_payload,
)
from ..tradingagents_runner import (
    TradingAgentsError,
    runtime_status,
    target_for,
)
from ..tradingagents_queue import get_tradingagents_queue

router = APIRouter(prefix="/api/tradingagents", tags=["tradingagents"])


class TradingAgentsRequest(BaseModel):
    symbol: str
    tradeDate: str | None = None
    analysts: list[str] | None = None
    timeoutSeconds: int | None = Field(None, ge=60, le=3600)
    # Spend the money again even when an identical cached result exists.
    force: bool = False


class TradingAgentsJobRequest(BaseModel):
    symbol: str
    tradeDate: str | None = None
    analysts: list[str] | None = None


def _profile():
    settings = load_llm_settings(quantdesk_home())
    try:
        return settings.profile_for("tradingagents")
    except KeyError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/readiness")
async def readiness(symbol: str | None = Query(None)):
    profile = _profile()
    key, candidates = resolve_api_key(profile.provider, profile.api_key_env)
    issue = credential_issue(key)
    runtime = await run_in_threadpool(runtime_status)
    target = None
    target_error = None
    if symbol:
        try:
            target = target_for(require_instrument(symbol)).__dict__
        except (ValueError, TradingAgentsError) as exc:
            target_error = str(exc)
    ready = bool(runtime.get("ready")) and bool(key) and issue is None and target_error is None
    return {
        "ready": ready,
        "runtime": runtime,
        "profile": profile.name,
        "provider": profile.provider,
        "models": {"deep": profile.deep_model, "quick": profile.quick_model},
        "credential": {"environmentVariable": candidates[0], "present": bool(key), "issue": issue},
        "target": target,
        "reason": (
            target_error
            or (runtime.get("reason") if not runtime.get("ready") else None)
            or (f"缺少 {candidates[0]}" if not key else None)
            or issue
        ),
    }


@router.post("/run")
async def run(request: TradingAgentsRequest):
    try:
        spec = require_instrument(request.symbol)
        target = target_for(spec)
    except ValueError as exc:
        raise HTTPException(422, "该合约不在固定合约池内") from exc
    except TradingAgentsError as exc:
        raise HTTPException(422, str(exc)) from exc

    trade_date = request.tradeDate or date.today().isoformat()
    try:
        parsed = date.fromisoformat(trade_date)
    except ValueError as exc:
        raise HTTPException(422, "tradeDate 必须是 YYYY-MM-DD") from exc
    if parsed > date.today():
        raise HTTPException(422, "研判日期不能晚于今天")

    profile = _profile()
    runtime = await run_in_threadpool(runtime_status)
    run_id = str(uuid.uuid4())

    outcome = await run_in_threadpool(
        lambda: run_governed_symbol(
            spec=spec,
            profile=profile,
            trade_date=trade_date,
            analysts=request.analysts,
            home=quantdesk_home(),
            runtime_commit=runtime.get("commit"),
            timeout_seconds=request.timeoutSeconds,
            run_id=run_id,
            force=bool(request.force),
        )
    )
    if outcome.get("refused"):
        # A refusal is a 409: the request was understood, the budget said no, and
        # nothing was spent.
        raise HTTPException(409, json.dumps(
            {"title": "已停止付费研判", "detail": outcome.get("reason"), "budget": outcome.get("budgetState")},
            ensure_ascii=False,
        ))

    payload = run_payload(outcome, run_id)
    try:
        Database(quantdesk_home() / "quantdesk.db").execute(
            "INSERT OR REPLACE INTO tradingagents_runs "
            "(id, venue_symbol, analysis_symbol, trade_date, asset_type, profile, rating, reports, debates, meta, error, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, spec.venue_symbol, target.symbol, trade_date, target.asset_type, profile.name,
                outcome.get("rating"),
                json.dumps(payload.get("reports") or {}, ensure_ascii=False),
                json.dumps(payload.get("debates") or {}, ensure_ascii=False),
                json.dumps(report_meta(outcome), ensure_ascii=False),
                outcome.get("reason") if not outcome.get("ok") else None,
                int(time.time() * 1000),
            ),
        )
    except Exception as exc:  # answer remains usable if only archival fails
        payload.setdefault("warnings", []).append(f"结果归档失败：{exc}")
    return payload


@router.post("/jobs")
async def create_job(request: TradingAgentsJobRequest):
    """Persist and enqueue a long-running graph pass; returns immediately."""
    try:
        job_id = await get_tradingagents_queue(quantdesk_home()).enqueue(
            request.symbol, request.tradeDate, request.analysts
        )
    except (ValueError, KeyError, TradingAgentsError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return JSONResponse({"jobId": job_id, "status": "queued"}, status_code=202)


@router.get("/jobs")
def jobs(limit: int = Query(30, ge=1, le=100)):
    return {"jobs": get_tradingagents_queue(quantdesk_home()).list(limit)}


@router.get("/jobs/{job_id}")
def job_detail(job_id: str):
    try:
        return get_tradingagents_queue(quantdesk_home()).get(job_id)
    except KeyError as exc:
        raise HTTPException(404, "没有找到该 TradingAgents 任务") from exc


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    try:
        return get_tradingagents_queue(quantdesk_home()).cancel(job_id)
    except KeyError as exc:
        raise HTTPException(404, "没有找到该 TradingAgents 任务") from exc


@router.get("/runs")
def runs(limit: int = Query(20, ge=1, le=100)):
    rows = Database(quantdesk_home() / "quantdesk.db").query(
        "SELECT id, venue_symbol, analysis_symbol, trade_date, asset_type, profile, rating, meta, created_ts "
        "FROM tradingagents_runs ORDER BY created_ts DESC LIMIT ?", (limit,)
    )
    for row in rows:
        row["meta"] = json.loads(row["meta"] or "{}")
    return {"runs": rows}


@router.get("/costs")
def costs(days: int = Query(7, ge=1, le=90), limit: int = Query(50, ge=1, le=200)):
    """What the paid research has cost, and what is left of today's allowance.

    The same numbers the run response reports, in one place: a budget nobody can
    read is a budget nobody can hold anyone to.
    """
    governor = AgentCostGovernor(quantdesk_home(), RunBudget.from_config(load_app_config().research))
    summary = governor.summary(days=days)
    summary["ledger"] = governor.ledger(limit=limit)
    summary["budgetState"] = governor.check_budget()
    return summary


@router.get("/runs/{run_id}")
def run_detail(run_id: str):
    rows = Database(quantdesk_home() / "quantdesk.db").query(
        "SELECT * FROM tradingagents_runs WHERE id = ?", (run_id,)
    )
    if not rows:
        raise HTTPException(404, "没有找到该 TradingAgents 运行记录")
    row = rows[0]
    for key in ("reports", "debates", "meta"):
        row[key] = json.loads(row[key] or "{}")
    return row
