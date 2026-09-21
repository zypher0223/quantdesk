"""Backtest, paper trading, and the append-only journal."""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..backtest import DEFAULT_SLIPPAGE_BPS, DEFAULT_TAKER_FEE_BPS
from ..config.instruments import TIMEFRAMES, require_instrument
from ..config.settings import configured_proxy, load_app_config, quantdesk_home
from ..datahub.db import Database
from ..paper import PaperConfig, PaperEngine, PaperError
from ..paper.engine import DEFAULT_MAINTENANCE_MARGIN_RATE
from ..plugins import PluginError, PluginManager
from ..risk import RiskBook
from ..strategy import StrategyRegistry
from ..studies import (
    MIN_CANDLES,
    BacktestRequest,
    auto_queue,
    queued_response,
    PortfolioRequest,
    StudyError,
    ValidationRequest,
    instrument_meta,
    load_or_refuse,
    open_db,
    paper_defaults,
    require_sync_budget,
    run_study,
)

router = APIRouter(prefix="/api", tags=["trading"])

MAX_CANDLES = 1_000

# The study request models and their implementation live in `quantdesk.studies`,
# so the API and the background run queue cannot drift apart: the queue parses
# the same shapes this module accepts and runs the same code.


def auto_queue_enabled(queue: str) -> bool:
    """Should an over-budget study move to the queue, or be refused outright?

    The default is to move it: the caller asked for a result, and a refusal to
    retype is not one. `?queue=never` keeps the old behaviour (409 with the reason
    and the budget), which is what a script deciding for itself will use.
    """
    return str(queue or "auto").strip().lower() != "never"


def _db() -> Database:
    """The local database, or the HTTP shape of "it cannot be opened"."""
    try:
        return open_db()
    except StudyError as exc:
        raise HTTPException(exc.status, exc.message) from exc


def _translate(exc: StudyError) -> HTTPException:
    """A study's refusal, in the shape the browser already understands."""
    if exc.detail is not None:
        return HTTPException(exc.status, json.dumps(exc.detail, ensure_ascii=False))
    return HTTPException(exc.status, exc.message)


def _paper_defaults(spec) -> tuple[float, float]:
    """Paper trading prices the same way a study does: one source for both."""
    return paper_defaults(spec)


def _instrument_meta(spec) -> dict:
    """Paper trading reads the same stored venue metadata a study does."""
    return instrument_meta(_db(), spec)


def _paper_config() -> PaperConfig:
    config = load_app_config()
    paper = config.paper or {}
    return PaperConfig(
        initial_cash=float(config.default_cash_usd),
        taker_fee_bps=float(paper.get("taker_fee_bps", DEFAULT_TAKER_FEE_BPS)),
        slippage_bps=float(paper.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)),
        maintenance_margin_rate=float(paper.get("maintenance_margin_rate", DEFAULT_MAINTENANCE_MARGIN_RATE)),
    )


@router.get("/strategies")
async def strategies_index():
    """Built-in strategies plus validated contributions from enabled plugins."""
    registry = StrategyRegistry(PluginManager(quantdesk_home()))
    strategies, errors = await run_in_threadpool(registry.catalog)
    return {"strategies": strategies, "pluginErrors": errors}


@router.post("/backtest")
async def backtest(request: BacktestRequest, queue: str = Query("auto")):
    """Run one registered strategy with funding, leverage, and liquidation.

    A small run answers inline; a request too large for an interaction is
    refused here and points at the background queue, which records progress,
    the result and the reason it failed.
    """
    try:
        if auto_queue_enabled(queue):
            queued = await run_in_threadpool(auto_queue, "backtest", request)
            if queued is not None:
                # Too large for a request: the same body goes to the queue and the
                # caller gets the run to watch, instead of a refusal to retype.
                run, cost = queued
                from .runs import get_run_worker

                get_run_worker().wake()
                return JSONResponse(status_code=202, content=queued_response(run, cost))
        require_sync_budget("backtest", request)
        return await run_in_threadpool(run_study, _db(), "backtest", request)
    except StudyError as exc:
        raise _translate(exc) from exc


@router.post("/validate")
async def validate_strategy(request: ValidationRequest, queue: str = Query("auto")):
    """Train/validation/test split, walk-forward, overfit and leakage checks.

    The test segment is scored once, after the parameters are fixed on the
    validation segment; every figure in the reply says which segment it came from.
    """
    try:
        if auto_queue_enabled(queue):
            queued = await run_in_threadpool(auto_queue, "validate", request)
            if queued is not None:
                # Too large for a request: the same body goes to the queue and the
                # caller gets the run to watch, instead of a refusal to retype.
                run, cost = queued
                from .runs import get_run_worker

                get_run_worker().wake()
                return JSONResponse(status_code=202, content=queued_response(run, cost))
        require_sync_budget("validate", request)
        return await run_in_threadpool(run_study, _db(), "validate", request)
    except StudyError as exc:
        raise _translate(exc) from exc


@router.post("/portfolio")
async def portfolio_backtest(request: PortfolioRequest, queue: str = Query("auto")):
    """Run one strategy across several contracts and combine the books."""
    try:
        if auto_queue_enabled(queue):
            queued = await run_in_threadpool(auto_queue, "portfolio", request)
            if queued is not None:
                # Too large for a request: the same body goes to the queue and the
                # caller gets the run to watch, instead of a refusal to retype.
                run, cost = queued
                from .runs import get_run_worker

                get_run_worker().wake()
                return JSONResponse(status_code=202, content=queued_response(run, cost))
        require_sync_budget("portfolio", request)
        return await run_in_threadpool(run_study, _db(), "portfolio", request)
    except StudyError as exc:
        raise _translate(exc) from exc


# -- paper trading ------------------------------------------------------


class OpenPositionRequest(BaseModel):
    symbol: str
    side: str
    notional: float | None = Field(None, gt=0)
    qty: float | None = Field(None, gt=0)
    leverage: float = Field(1, ge=1, le=200)
    rationale: str = ""
    stop_loss: float | None = Field(None, gt=0, alias="stopLoss")
    take_profit_1: float | None = Field(None, gt=0, alias="takeProfit1")
    take_profit_2: float | None = Field(None, gt=0, alias="takeProfit2")


class ClosePositionRequest(BaseModel):
    reason: str = "manual"


class NoteRequest(BaseModel):
    notes: str = Field("", max_length=4000)


@router.get("/paper/account")
async def paper_account():
    """Mark-price valued account: unrealised PnL, margin ratio, liquidation distance."""
    engine = PaperEngine(_db(), _paper_config())
    account = await run_in_threadpool(engine.account)
    return account.as_dict()


@router.post("/paper/positions")
async def paper_open(request: OpenPositionRequest):
    try:
        spec = require_instrument(request.symbol)
    except ValueError as exc:
        raise HTTPException(422, "该合约不在固定合约池内") from exc
    engine = PaperEngine(_db(), _paper_config())
    slippage, fee = _paper_defaults(spec)

    def act():
        meta = _instrument_meta(spec)
        if meta.get("status") != "Trading" or not meta.get("qtyStep") or not meta.get("maxLeverage"):
            raise PaperError(f"暂时无法确认 {spec.venue_symbol} 的交易状态、数量步长和杠杆上限，已拒绝开仓")
        engine.config.slippage_bps = slippage
        engine.config.taker_fee_bps = fee
        return engine.open_position(
            spec.venue_symbol,
            request.side,
            notional=request.notional,
            qty=request.qty,
            leverage=request.leverage,
            rationale=request.rationale,
            tick_size=meta.get("tickSize"),
            qty_step=meta.get("qtyStep"),
            min_order_notional=float(meta.get("minNotionalValue") or 5.0),
            max_leverage=meta.get("maxLeverage"),
            stop_loss=request.stop_loss,
            take_profit_1=request.take_profit_1,
            take_profit_2=request.take_profit_2,
        )

    try:
        view = await run_in_threadpool(act)
    except PaperError as exc:
        raise HTTPException(409, str(exc)) from exc
    return view.__dict__


def run_paper_monitor_cycle() -> list[dict]:
    """One headless reconciliation pass for funding, liquidation and brackets."""
    engine = PaperEngine(_db(), _paper_config())
    rows = engine.open_positions()
    if not rows:
        return []
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["symbol"], []).append(row)

    marks: dict[str, float] = {}
    funding: dict[str, list[dict]] = {}
    ticks: dict[str, float | None] = {}
    client = BybitClient(proxy=configured_proxy(), timeout=12.0)
    try:
        for symbol, positions in grouped.items():
            ticker = client.ticker("linear", symbol)
            raw_mark = ticker.get("markPrice") or ticker.get("lastPrice")
            if not raw_mark:
                continue
            marks[symbol] = float(raw_mark)
            opened = min(int(row["updated_ts"]) for row in positions)
            funding[symbol] = client.funding_history(symbol, start_ms=opened, limit=200)
            metadata = client.instruments("linear", symbol=symbol)
            price_filter = (metadata[0] if metadata else {}).get("priceFilter", {})
            ticks[symbol] = float(price_filter["tickSize"]) if price_filter.get("tickSize") else None
    finally:
        client.close()
    return engine.reconcile(marks, funding_by_symbol=funding, tick_sizes=ticks)


@router.post("/paper/positions/{position_id}/close")
async def paper_close(position_id: int, request: ClosePositionRequest):
    spec_holder: dict = {}

    def act():
        engine = PaperEngine(_db(), _paper_config())
        rows = engine.db.query("SELECT symbol FROM positions WHERE id = ?", (position_id,))
        if not rows:
            raise PaperError(f"没有找到持仓 #{position_id}")
        spec = require_instrument(rows[0]["symbol"])
        spec_holder["spec"] = spec
        slippage, fee = _paper_defaults(spec)
        engine.config.slippage_bps = slippage
        engine.config.taker_fee_bps = fee
        meta = _instrument_meta(spec)
        return engine.close_position(position_id, tick_size=meta.get("tickSize"), exit_reason=request.reason)

    try:
        result = await run_in_threadpool(act)
    except PaperError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, "该合约不在固定合约池内") from exc
    return result


@router.put("/paper/positions/{position_id}/note")
def paper_note(position_id: int, request: NoteRequest):
    engine = PaperEngine(_db(), _paper_config())
    rows = engine.db.query("SELECT id FROM positions WHERE id = ?", (position_id,))
    if not rows:
        raise HTTPException(404, f"没有找到持仓 #{position_id}")
    engine.set_note(position_id, request.notes)
    return {"position_id": position_id, "notes": request.notes}


# -- journal ------------------------------------------------------------


@router.get("/journal")
def journal(limit: int = Query(200, ge=1, le=2000), symbol: str | None = None):
    db = _db()
    venue_symbol = None
    if symbol:
        try:
            venue_symbol = require_instrument(symbol).venue_symbol
        except ValueError as exc:
            raise HTTPException(422, "该合约不在固定合约池内") from exc
    entries = db.journal_entries(limit=limit, symbol=venue_symbol)
    return {"entries": entries, "integrity": db.journal_integrity(), "immutable": True}


@router.get("/journal/export")
def journal_export(format: str = Query("json", pattern="^(json|csv)$"), symbol: str | None = None):
    db = _db()
    venue_symbol = None
    if symbol:
        try:
            venue_symbol = require_instrument(symbol).venue_symbol
        except ValueError as exc:
            raise HTTPException(422, "该合约不在固定合约池内") from exc
    entries = db.journal_entries(limit=10_000, symbol=venue_symbol)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if format == "csv":
        columns = [
            "id", "opened_ts", "closed_ts", "symbol", "side", "qty", "entry_price", "exit_price",
            "leverage", "liq_price", "gross_pnl", "funding_paid", "fees", "net_pnl", "exit_reason",
            "rationale", "entry_hash",
        ]
        lines = [",".join(columns)]
        for row in entries:
            lines.append(",".join(_csv_cell(row.get(column)) for column in columns))
        return PlainTextResponse(
            "\n".join(lines) + "\n",
            headers={"Content-Disposition": f'attachment; filename="quantdesk-journal-{stamp}.csv"'},
            media_type="text/csv; charset=utf-8",
        )
    body = {
        "exportedAt": int(time.time() * 1000),
        "integrity": db.journal_integrity(),
        "entries": entries,
    }
    return PlainTextResponse(
        json.dumps(body, ensure_ascii=False, indent=1),
        headers={"Content-Disposition": f'attachment; filename="quantdesk-journal-{stamp}.json"'},
        media_type="application/json; charset=utf-8",
    )


def _csv_cell(value) -> str:
    if value is None:
        return ""
    text = str(value)
    return '"' + text.replace('"', '""') + '"' if any(char in text for char in ',"\n') else text


@router.post("/paper/reset")
def paper_reset():
    engine = PaperEngine(_db(), _paper_config())
    open_count = len(engine.open_positions())
    if open_count:
        raise HTTPException(409, f"还有 {open_count} 个未平仓持仓，请先平仓再重置账户")
    engine.reset()
    return {"ok": True, "cash": engine.config.initial_cash}


@router.get("/db/location")
def db_location():
    home = quantdesk_home()
    return {"home": str(home), "database": str(home / "quantdesk.db"), "uploads": str(Path(home) / "uploads")}
