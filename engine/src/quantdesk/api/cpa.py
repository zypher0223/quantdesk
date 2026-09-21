"""CPA analysis endpoints: the phase reading, its catalogue, and its data readiness.

Read-only by construction. Nothing here places an order, changes configuration or
writes to the database; a phase series is computed from the engine's own stored,
closed candles and returned with the versions that produced it.

Three endpoints:

* `GET /api/cpa/phases`   - the whole series (or its tail) with per-bar evidence;
* `GET /api/cpa/summary`  - the current phase and the distribution, for a card;
* `GET /api/cpa/catalog`  - phases, parameter schema with defaults per asset class,
                            the higher-timeframe map, and the attribution notice.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from ..config.instruments import INTERVAL_MS, require_instrument
from ..datahub.db import Database
from ..datahub.view import read_history
from ..strategy import cpa
from ..config.settings import quantdesk_home

router = APIRouter(prefix="/api/cpa", tags=["cpa"])

# How far back the higher-timeframe series is read. Enough for the slowest default
# indicator plus the pivot window, without pulling a decade of 15m bars.
HIGHER_BARS = 600


def _db() -> Database:
    return Database(quantdesk_home() / "quantdesk.db")


def _bars(db: Database, spec, interval: str, bars: int) -> list[dict]:
    slice_ = read_history(
        db, symbol=spec.venue_symbol, interval=interval, bars=bars,
        display_symbol=spec.display_symbol, product_type=spec.product_type,
        with_funding=False, with_marks=False,
    )
    return [
        {
            "ts": int(row["ts"]), "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "volume": float(row.get("volume") or 0.0),
            "turnover": row.get("turnover"),
        }
        for row in slice_.bars
    ], slice_


@router.post("/ablation", status_code=202)
async def cpa_ablation(body: dict):
    """Queue one ablation sweep. Read-only about the market; it only runs studies."""
    group = str(body.get("group") or "stock")
    interval = str(body.get("interval") or "1h")
    bars = int(body.get("bars") or 1200)
    if interval not in INTERVAL_MS:
        raise HTTPException(422, f"不支持的周期：{interval}")
    if group not in ("stock", "leveraged_etf", "crypto"):
        raise HTTPException(422, f"未知分组：{group}")

    def submit() -> dict:
        from .runs import get_run_worker

        worker = get_run_worker()
        queued = worker.queue.submit("cpa_ablation", {
            "group": group, "interval": interval, "bars": bars,
            "allowDegraded": bool(body.get("allowDegraded")),
        })
        worker.wake()
        return queued

    try:
        queued = await run_in_threadpool(submit)
    except Exception as exc:  # the queue's own message is the useful one
        raise HTTPException(409, f"{type(exc).__name__}: {exc}") from exc
    return {"queued": True, "run": queued}


@router.get("/catalog")
async def cpa_catalog():
    """The rule set, its parameters, and what it deliberately is not."""
    described = cpa.describe()
    return {
        **described,
        "phases": [
            {
                "id": phase,
                "label": cpa.PHASE_LABELS[phase],
                "observation": phase in cpa.defaults.OBSERVATION_PHASES,
            }
            for phase in cpa.PHASES
        ],
        "phaseLabels": cpa.PHASE_LABELS,
        "parameters": list(cpa.PARAMETER_SPECS),
        "defaults": {
            product: {interval: cpa.defaults_for(product, interval)
                      for interval in ("15m", "1h", "4h", "1d", "1w")}
            for product in ("stock", "etf", "crypto")
        },
        "optionalFactorLayer": cpa.defaults.OPTIONAL_FACTOR_LAYER,
        "supportedIntervals": ["15m", "1h", "4h", "1d", "1w"],
        "higherTimeframeNotice": (
            "低周期只读取当时已经收盘的高周期K线；周线未收盘时不参与判断，"
            "高周期数据不足时显示背景不足而不是用最终状态回填历史。"
        ),
    }


@router.get("/phases")
async def cpa_phases(
    symbol: str = Query(..., min_length=1, max_length=32),
    interval: str = Query("1h", max_length=8),
    bars: int = Query(600, ge=60, le=5000),
    limit: int = Query(400, ge=1, le=5000),
    withBackground: bool = Query(True, alias="withBackground"),
):
    """The phase series for one contract and interval."""
    try:
        spec = require_instrument(symbol)
    except ValueError as exc:
        raise HTTPException(422, "该合约不在固定合约池内") from exc
    if interval not in INTERVAL_MS:
        raise HTTPException(422, f"不支持的周期：{interval}")

    def work() -> dict:
        db = _db()
        base, slice_ = _bars(db, spec, interval, bars)
        management_interval, background_interval = cpa.higher_intervals_for(interval)
        management = background = None
        if withBackground and management_interval:
            management, _ = _bars(db, spec, management_interval, HIGHER_BARS)
            if background_interval and background_interval != management_interval:
                background, _ = _bars(db, spec, background_interval, HIGHER_BARS)
            else:
                background = management
        series = cpa.analyze(
            symbol=spec.venue_symbol, display_symbol=spec.display_symbol,
            interval=interval, product_type=spec.product_type, bars=base,
            management_bars=management, background_bars=background,
            snapshot_hash=str(slice_.version or ""), data_version=str(slice_.version or ""),
        )
        payload = series.as_dict(limit=limit)
        payload["requestedBars"] = bars
        payload["storedBars"] = len(base)
        return payload

    return await run_in_threadpool(work)


@router.get("/summary")
async def cpa_summary(
    symbol: str = Query(..., min_length=1, max_length=32),
    interval: str = Query("1h", max_length=8),
    bars: int = Query(600, ge=60, le=5000),
):
    """The current phase and the distribution - what a card shows."""
    payload = await cpa_phases(symbol=symbol, interval=interval, bars=bars, limit=1)
    return {
        "symbol": payload["symbol"],
        "displaySymbol": payload["displaySymbol"],
        "interval": payload["interval"],
        "current": payload["current"],
        "counts": payload["counts"],
        "insufficient": payload["insufficient"],
        "insufficientReason": payload["insufficientReason"],
        "parameterVersion": payload["parameterVersion"],
        "snapshotHash": payload["snapshotHash"],
        "higherIntervals": payload["higherIntervals"],
        "warnings": payload["warnings"],
        "attribution": payload["attribution"],
    }
