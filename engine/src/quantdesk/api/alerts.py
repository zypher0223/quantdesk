"""Persistent alert-rule registry and event history."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from ..alerts import AlertEngine, AlertRuleError, CONDITION_CATALOG
from ..config.instruments import INSTRUMENTS
from ..config.settings import quantdesk_home

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class AlertConditionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    condition_type: str = Field(alias="conditionType", min_length=2, max_length=40)
    timeframe: str | None = None
    threshold: float | None = None
    strategy_id: str | None = Field(None, alias="strategyId", max_length=120)
    strategy_parameters: dict = Field(default_factory=dict, alias="strategyParameters")
    signal_direction: str = Field("any", alias="signalDirection")


class AlertRuleRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=80)
    venue_symbol: str = Field(alias="venueSymbol", min_length=2, max_length=30)
    condition_type: str | None = Field(None, alias="conditionType", min_length=2, max_length=40)
    timeframe: str | None = None
    threshold: float | None = None
    conditions: list[AlertConditionRequest] = Field(default_factory=list, min_length=0, max_length=5)
    cooldown_seconds: int = Field(3600, alias="cooldownSeconds", ge=60, le=604_800)
    enabled: bool = True
    severity: str = "warning"
    quiet_start: str | None = Field(None, alias="quietStart")
    quiet_end: str | None = Field(None, alias="quietEnd")
    timezone: str = "Asia/Shanghai"
    daily_limit: int = Field(10, alias="dailyLimit", ge=1, le=1000)
    confirmation_count: int = Field(1, alias="confirmationCount", ge=1, le=10)
    hysteresis: float = Field(0, ge=0)


class StrategyAlertRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    venue_symbol: str = Field(alias="venueSymbol", min_length=2, max_length=30)
    timeframe: str
    strategy_id: str = Field(alias="strategyId", min_length=1, max_length=120)
    strategy_parameters: dict = Field(default_factory=dict, alias="strategyParameters")
    signal_direction: str = Field("any", alias="signalDirection")
    name: str | None = Field(None, max_length=80)
    severity: str = "warning"


def _engine() -> AlertEngine:
    return AlertEngine(quantdesk_home())


@router.get("")
def alerts_index(event_limit: int = Query(50, alias="eventLimit", ge=1, le=200)):
    engine = _engine()
    return {
        "conditions": [{"id": key, **value} for key, value in CONDITION_CATALOG.items()],
        "instruments": [
            {"venueSymbol": item.venue_symbol, "displaySymbol": item.display_symbol, "name": item.name}
            for item in INSTRUMENTS
        ],
        "rules": engine.list_rules(),
        "events": engine.list_events(event_limit),
    }


@router.post("/rules")
def create_rule(request: AlertRuleRequest):
    try:
        return _engine().create_rule(request.model_dump())
    except (AlertRuleError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/from-strategy")
def create_from_strategy(request: StrategyAlertRequest):
    try:
        return _engine().create_rule(
            {
                "name": request.name or f"{request.strategy_id} 策略信号",
                "venue_symbol": request.venue_symbol,
                "conditions": [
                    {
                        "condition_type": "strategy_signal",
                        "timeframe": request.timeframe,
                        "strategy_id": request.strategy_id,
                        "strategy_parameters": request.strategy_parameters,
                        "signal_direction": request.signal_direction,
                    }
                ],
                "cooldown_seconds": 60,
                "enabled": True,
                "severity": request.severity,
                "daily_limit": 10,
                "confirmation_count": 1,
                "hysteresis": 0,
            }
        )
    except (AlertRuleError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/rules/{rule_id}")
def update_rule(rule_id: str, request: AlertRuleRequest):
    try:
        return _engine().update_rule(rule_id, request.model_dump())
    except KeyError as exc:
        raise HTTPException(404, "没有找到该告警规则") from exc
    except (AlertRuleError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: str):
    try:
        _engine().delete_rule(rule_id)
    except KeyError as exc:
        raise HTTPException(404, "没有找到该告警规则") from exc
    return {"id": rule_id, "removed": True}


@router.post("/evaluate")
async def evaluate_rules(symbol: str):
    try:
        return await run_in_threadpool(_engine().evaluate_symbol, symbol)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
