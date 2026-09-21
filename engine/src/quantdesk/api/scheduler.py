"""Status and manual controls for the background task scheduler."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config.instruments import require_instrument
from ..config.settings import quantdesk_home, write_scheduler_settings
from ..datahub.db import Database
from ..scheduler import get_scheduler

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


class SchedulerSettingsRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    market_collection_enabled: bool = Field(alias="marketCollectionEnabled")
    market_symbol_interval_sec: int = Field(alias="marketSymbolIntervalSec", ge=5, le=3600)
    market_backfill_bars: int = Field(alias="marketBackfillBars", ge=30, le=1000)
    daily_ta_enabled: bool = Field(alias="dailyTradingAgentsEnabled")
    daily_ta_time: str = Field(alias="dailyTradingAgentsTime", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    daily_ta_symbols: list[str] = Field(alias="dailyTradingAgentsSymbols", min_length=1, max_length=17)

    @field_validator("daily_ta_symbols")
    @classmethod
    def validate_symbols(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            symbol = require_instrument(value).venue_symbol
            if symbol not in normalized:
                normalized.append(symbol)
        return normalized


@router.get("/status")
def scheduler_status():
    return get_scheduler(quantdesk_home()).status()


@router.put("/settings")
def save_scheduler_settings(request: SchedulerSettingsRequest):
    values = request.model_dump()
    try:
        path = write_scheduler_settings(values, quantdesk_home())
    except OSError as exc:
        raise HTTPException(503, f"无法保存调度配置：{exc}") from exc
    return {"saved": True, "configFile": str(path), **get_scheduler(quantdesk_home()).status()}


@router.get("/runs")
def scheduler_runs(limit: int = Query(30, ge=1, le=200)):
    rows = Database(quantdesk_home() / "quantdesk.db").query(
        "SELECT * FROM scheduler_runs ORDER BY started_ts DESC LIMIT ?", (limit,)
    )
    return {"runs": rows}


@router.post("/market/run")
async def run_market_collection(symbol: str | None = None):
    try:
        return await get_scheduler(quantdesk_home()).run_market_once(symbol)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"行情采集失败：{exc}") from exc
