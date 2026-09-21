"""HTTP surface for independent, concurrently running AI paper simulations."""

from __future__ import annotations

import csv
import io
import json
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..ai_paper import PROFILE_ID, AiPaperError, AiPaperService
from ..config.settings import quantdesk_home
from ..paper import PaperError

router = APIRouter(prefix="/api/ai-paper", tags=["ai-paper"])


class AiPaperConfigRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=40)
    initial_cash: float | None = Field(None, alias="initialCash", ge=100, le=1_000_000_000)
    max_leverage: float | None = Field(None, alias="maxLeverage", ge=1, le=200)
    horizon: str | None = None
    style: str | None = None
    fib_only: bool | None = Field(None, alias="fibOnly")
    symbols: list[str] | None = None


class AiPaperCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    initial_cash: float = Field(100_000, alias="initialCash", ge=100, le=1_000_000_000)
    max_leverage: float = Field(3, alias="maxLeverage", ge=1, le=200)
    horizon: str = "short"
    style: str = "conservative"
    fib_only: bool = Field(False, alias="fibOnly")
    symbols: list[str]


def _service(profile_id: str = PROFILE_ID) -> AiPaperService:
    try:
        return AiPaperService(
            quantdesk_home(), profile_id=profile_id,
            create_if_missing=profile_id == PROFILE_ID,
        )
    except AiPaperError as exc:
        raise HTTPException(404 if "不存在" in str(exc) else 409, str(exc)) from exc


def _translate(exc: Exception) -> HTTPException:
    return HTTPException(409, str(exc))


def _updates(request: AiPaperConfigRequest) -> dict:
    return {key: value for key, value in request.model_dump().items() if value is not None}


def _export_response(profile_id: str, format: str):
    service = _service(profile_id)
    try:
        snapshot = service.snapshot(decision_limit=10_000)
    finally:
        service.close()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_id = profile_id.replace("/", "-")
    if format == "md":
        return PlainTextResponse(
            snapshot["memory"]["content"], media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="quantdesk-ai-memory-{safe_id}-{stamp}.md"'},
        )
    if format == "csv":
        buffer = io.StringIO()
        columns = ("id", "cycle_ts", "action", "symbol", "side", "leverage", "notional", "confidence", "status", "reason", "lesson_applied", "error", "model_profile", "model")
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(snapshot["decisions"])
        return PlainTextResponse(
            buffer.getvalue(), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="quantdesk-ai-decisions-{safe_id}-{stamp}.csv"'},
        )
    return PlainTextResponse(
        json.dumps(snapshot, ensure_ascii=False, indent=2), media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="quantdesk-ai-paper-{safe_id}-{stamp}.json"'},
    )


@router.get("/profiles")
def ai_paper_profiles():
    return {"profiles": AiPaperService.summaries(quantdesk_home())}


@router.post("/profiles", status_code=201)
def ai_paper_create(request: AiPaperCreateRequest):
    payload = request.model_dump()
    name = payload.pop("name")
    try:
        service = AiPaperService.create(quantdesk_home(), name=name, **payload)
        try:
            return service.snapshot()
        finally:
            service.close()
    except AiPaperError as exc:
        raise _translate(exc) from exc


@router.get("/profiles/{profile_id}")
def ai_paper_profile_snapshot(profile_id: str):
    service = _service(profile_id)
    try:
        return service.snapshot()
    finally:
        service.close()


@router.put("/profiles/{profile_id}/config")
def ai_paper_profile_config(profile_id: str, request: AiPaperConfigRequest):
    service = _service(profile_id)
    try:
        return service.update_profile(**_updates(request))
    except AiPaperError as exc:
        raise _translate(exc) from exc
    finally:
        service.close()


@router.post("/profiles/{profile_id}/start")
def ai_paper_profile_start(profile_id: str, request: AiPaperConfigRequest | None = None):
    """Start a simulation, optionally saving the rules first - atomically.

    With a body, the rules the page is showing are validated, persisted and only then
    enabled, and the response is the full snapshot of the started instance. Without a
    body the legacy behaviour is kept (enable whatever is already stored, return the
    profile) for old clients; the current page always sends the body, because a start
    button that silently runs last week's rules is the bug this fixes.
    """
    service = _service(profile_id)
    try:
        updates = _updates(request) if request is not None else {}
        if request is None:
            return service.set_enabled(True)
        return service.start_with_config(**updates)
    except AiPaperError as exc:
        raise _translate(exc) from exc
    finally:
        service.close()


@router.post("/profiles/{profile_id}/stop")
def ai_paper_profile_stop(profile_id: str):
    service = _service(profile_id)
    try:
        return service.set_enabled(False)
    finally:
        service.close()


@router.post("/profiles/{profile_id}/run")
async def ai_paper_profile_run(profile_id: str):
    service = _service(profile_id)
    try:
        return await run_in_threadpool(service.run_once, force=True)
    except (AiPaperError, PaperError, KeyError) as exc:
        raise _translate(exc) from exc
    finally:
        service.close()


@router.post("/profiles/{profile_id}/positions/{position_id}/close")
async def ai_paper_profile_close(profile_id: str, position_id: int):
    service = _service(profile_id)
    try:
        return await run_in_threadpool(service.close_position, position_id)
    except (AiPaperError, PaperError) as exc:
        raise _translate(exc) from exc
    finally:
        service.close()


@router.post("/profiles/{profile_id}/reset")
def ai_paper_profile_reset(profile_id: str, confirm: bool = Query(False)):
    if not confirm:
        raise HTTPException(422, "重置会清空该 AI 模拟实例及其决策历史；请传 confirm=true")
    service = _service(profile_id)
    try:
        service.reset()
        return {"ok": True}
    except AiPaperError as exc:
        raise _translate(exc) from exc
    finally:
        service.close()


@router.delete("/profiles/{profile_id}")
def ai_paper_profile_delete(profile_id: str, confirm: bool = Query(False)):
    if not confirm:
        raise HTTPException(422, "删除会移除该实例的账户、日志和记忆；请传 confirm=true")
    service = _service(profile_id)
    try:
        service.delete()
        return {"ok": True}
    except AiPaperError as exc:
        raise _translate(exc) from exc
    finally:
        service.close()


@router.get("/profiles/{profile_id}/memory")
def ai_paper_profile_memory(profile_id: str):
    service = _service(profile_id)
    try:
        return service.snapshot(decision_limit=1)["memory"]
    finally:
        service.close()


@router.get("/profiles/{profile_id}/export")
def ai_paper_profile_export(profile_id: str, format: str = Query("json", pattern="^(json|csv|md)$")):
    return _export_response(profile_id, format)


# Backward-compatible default-instance endpoints.
@router.get("")
def ai_paper_snapshot():
    service = AiPaperService(quantdesk_home())
    try:
        return service.snapshot()
    finally:
        service.close()


@router.put("/config")
def ai_paper_config(request: AiPaperConfigRequest):
    service = AiPaperService(quantdesk_home())
    try:
        return service.update_profile(**_updates(request))
    except AiPaperError as exc:
        raise _translate(exc) from exc
    finally:
        service.close()


@router.post("/start")
def ai_paper_start():
    return ai_paper_profile_start(PROFILE_ID)


@router.post("/stop")
def ai_paper_stop():
    return ai_paper_profile_stop(PROFILE_ID)


@router.post("/run")
async def ai_paper_run():
    return await ai_paper_profile_run(PROFILE_ID)


@router.post("/positions/{position_id}/close")
async def ai_paper_close(position_id: int):
    return await ai_paper_profile_close(PROFILE_ID, position_id)


@router.post("/reset")
def ai_paper_reset(confirm: bool = Query(False)):
    return ai_paper_profile_reset(PROFILE_ID, confirm)


@router.get("/memory")
def ai_paper_memory():
    return ai_paper_profile_memory(PROFILE_ID)


@router.get("/export")
def ai_paper_export(format: str = Query("json", pattern="^(json|csv|md)$")):
    return _export_response(PROFILE_ID, format)
