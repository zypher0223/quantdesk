"""Local data layer: what history is stored, what is missing, and a replay.

Read-only. The endpoints answer the questions a result has to be able to answer
about itself: which bars were used, where each came from, and which absences are
defects rather than the market being shut.
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ..config.instruments import TIMEFRAMES, VENUE_SYMBOLS, require_instrument
from ..config.settings import quantdesk_home
from ..datahub.calendar import observed_session
from ..datahub.db import COLLECTOR_VERSION, Database
from ..datahub.bybit import BybitClient
from ..config.settings import configured_proxy
from ..datahub.snapshot import load_snapshot, repair_gaps, snapshot_summary, snapshot_version
from ..datahub.venue import INTERVAL_MS, last_closed_open_ts

router = APIRouter(prefix="/api/data", tags=["data"])

DEFAULT_WINDOW_BARS = 500


def _db() -> Database:
    home = quantdesk_home()
    try:
        return Database(home / "quantdesk.db")
    except Exception as exc:  # noqa: BLE001 - sqlite raises OperationalError
        raise HTTPException(503, f"无法打开本地数据库 {home / 'quantdesk.db'}：{exc}") from exc


def _resolve(symbols: str, symbol: str | None) -> tuple[list[str], dict[str, str]]:
    """Venue symbols plus their product type, from the fixed pool only."""
    if symbol:
        spec = require_instrument(symbol)
        return [spec.venue_symbol], {spec.venue_symbol: spec.product_type}
    if not symbols:
        specs = [require_instrument(item) for item in VENUE_SYMBOLS]
        return [spec.venue_symbol for spec in specs], {spec.venue_symbol: spec.product_type for spec in specs}
    out: list[str] = []
    types: dict[str, str] = {}
    for item in symbols.split(","):
        item = item.strip()
        if not item:
            continue
        spec = require_instrument(item)
        out.append(spec.venue_symbol)
        types[spec.venue_symbol] = spec.product_type
    if not out:
        raise HTTPException(422, "至少需要一个合约")
    return out, types


def _window(
    db: Database,
    venue_symbol: str,
    interval: str,
    bars: int,
    from_ts: int | None,
    to_ts: int | None,
) -> tuple[int, int]:
    """The range to inspect, ending at the newest bar that has actually closed.

    A window that ran to `now` would report the still-forming bar as missing, which
    is the opposite of the truth: it is absent because it is not over yet.
    """
    step = INTERVAL_MS[interval]
    newest = db.last_open_ts("bybit", venue_symbol, interval)
    if to_ts is not None:
        end = int(to_ts)
    else:
        end = last_closed_open_ts(newest, step)
    start = int(from_ts) if from_ts is not None else end - (bars - 1) * step
    return start, max(start, end)


@router.get("/backfill")
def backfill_status(symbol: str | None = Query(None, description="缺省为全部合约")):
    """How far each symbol's history walk has got, and how it last ended.

    Read-only: the walk itself is a CLI/background job, because it is a long
    operation that should not be tied to an HTTP request.
    """
    from ..datahub.backfill import FAILURE_LABELS

    db = Database(quantdesk_home() / "quantdesk.db")
    rows = db.list_backfill_state()
    if symbol:
        rows = [row for row in rows if row["symbol"] == symbol]
    return {
        "states": [
            {
                **row,
                "complete": bool(row["complete"]),
                "failureLabel": FAILURE_LABELS.get(row.get("last_error_kind") or "", ""),
            }
            for row in rows
        ],
        "snapshots": [
            {**row, "sources": json.loads(row.get("sources") or "{}")}
            for row in db.list_history_snapshots(symbol=symbol, limit=20)
        ],
        "failureKinds": FAILURE_LABELS,
    }


class SnapshotPinRequest(BaseModel):
    """记录一台已有序列的快照版本，不访问交易所。"""

    symbol: str
    dataKinds: list[str] = Field(
        default_factory=lambda: ["trade_candle", "mark_candle", "funding", "open_interest", "risk_limit"],
        max_length=8,
    )
    intervals: list[str] = Field(default_factory=lambda: ["15m", "1h", "4h", "1d"], max_length=4)


class BackfillMatrixRequest(BaseModel):
    symbols: list[str] | None = Field(None, max_length=64)
    timeframes: list[str] = Field(default_factory=lambda: ["15m", "1h", "4h", "1d"], max_length=4)
    dataKinds: list[str] = Field(
        default_factory=lambda: ["trade_candle", "mark_candle", "funding", "open_interest", "risk_limit"],
        max_length=8,
    )
    reset: bool = False


_BACKFILL_WORKER = None
_BACKFILL_HOME = None


def get_backfill_worker():
    """The queue as the API drives it: real venue client, bounded concurrency."""
    from ..config.settings import load_app_config
    from ..datahub.backfill import bybit_fetch
    from ..datahub.history import HistoryCollector
    from ..datahub.tasks import BackfillQueue, BackfillWorker

    global _BACKFILL_WORKER, _BACKFILL_HOME

    home = quantdesk_home()
    external = load_app_config(home).external or {}
    db = Database(home / "quantdesk.db")

    def factory() -> HistoryCollector:
        client = BybitClient(proxy=configured_proxy(home), timeout=25.0)
        return HistoryCollector(db, client)

    if _BACKFILL_WORKER is not None and _BACKFILL_HOME == home:
        return _BACKFILL_WORKER
    queue = BackfillQueue(
        db,
        factory,
        # The venue's own limit is well above this; the cap exists so a click
        # cannot turn into a burst of hundreds of requests.
        concurrency=int(external.get("backfill_concurrency") or 2),
        pages_per_minute=int(external.get("backfill_pages_per_minute") or 120),
    )
    _BACKFILL_WORKER = BackfillWorker(queue)
    _BACKFILL_HOME = home
    return _BACKFILL_WORKER


def _backfill_queue():
    return get_backfill_worker().queue


@router.get("/backfill/tasks")
def backfill_tasks(
    symbol: str | None = Query(None),
    status: str | None = Query(None),
    ranges: bool = Query(False, description="同时返回各数据族可用于回测的区间"),
):
    """The task board. Read-only, so a refresh shows the same picture.

    `ranges=true` also reports what each series can be backtested over. That part
    reads the bar history, so the page asks for it on a slower cadence than the
    board itself.
    """
    worker = get_backfill_worker()
    board = worker.status(symbol=symbol, include_ranges=ranges)
    if status:
        board["tasks"] = [task for task in board["tasks"] if task["status"] == status]
    return board


@router.get("/backfill/ranges")
def backfill_ranges(symbol: str | None = Query(None)):
    """What can be formally studied right now, per contract and data family."""
    return get_backfill_worker().queue.ranges(symbol=symbol)


@router.post("/backfill/tasks")
async def build_backfill_tasks(request: BackfillMatrixRequest):
    """Create the task matrix; re-running it does not duplicate work."""
    worker = get_backfill_worker()
    queue = worker.queue

    def build() -> dict:
        for symbol in request.symbols or []:
            require_instrument(symbol)
        return queue.build_matrix(
            symbols=request.symbols, timeframes=request.timeframes,
            data_kinds=request.dataKinds, reset=request.reset,
        )

    try:
        built = await run_in_threadpool(build)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    worker.wake()
    return {**built, "board": worker.status()}


@router.post("/backfill/snapshot")
async def pin_snapshot(request: SnapshotPinRequest):
    """Pin the version of what is already stored, so a study can cite it.

    A series that was walked before snapshot-on-completion existed has rows and no
    version, and the readiness gate refuses to let a formal study cite it. Pinning
    reads the local rows and hashes them - it does not fetch, so it cannot make a
    stale series look fresh, and the version it records is exactly the data that is
    there.
    """
    from ..datahub.history import HistoryCollector

    spec = require_instrument(request.symbol)
    db = _db()
    collector = HistoryCollector(db, None)
    interval_map = {
        "trade_candle": request.intervals,
        "mark_candle": request.intervals,
        "funding": [""],
        "open_interest": [""],
        "risk_limit": [""],
    }

    def pin() -> dict:
        pinned, refused = {}, {}
        for kind in request.dataKinds:
            intervals = interval_map.get(kind)
            if intervals is None:
                refused[kind] = "未知的数据族"
                continue
            for interval in intervals:
                record = collector.snapshot(spec.venue_symbol, kind, interval)
                if record.get("available"):
                    pinned[f"{kind}:{interval or '-'}"] = record["version"]
                else:
                    refused[f"{kind}:{interval or '-'}"] = record.get("reason") or "本地没有数据"
        return {"symbol": spec.venue_symbol, "pinned": pinned, "refused": refused}

    return await run_in_threadpool(pin)


@router.post("/backfill/tasks/{task_id}/{action}")
async def control_backfill_task(task_id: int, action: str):
    """Pause, resume, cancel or retry one task."""
    worker = get_backfill_worker()
    queue = worker.queue
    handlers = {
        "pause": queue.pause,
        "resume": queue.resume,
        "cancel": queue.cancel,
        "retry": queue.retry,
    }
    handler = handlers.get(action)
    if handler is None:
        raise HTTPException(422, f"不支持的操作：{action}；可用：{', '.join(handlers)}")
    try:
        result = await run_in_threadpool(handler, task_id)
        if action in ("resume", "retry"):
            worker.wake()
        return result
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/backfill/{action}")
async def control_backfill_queue(action: str):
    """Pause or resume the whole queue."""
    worker = get_backfill_worker()
    queue = worker.queue
    if action == "pause":
        await run_in_threadpool(queue.pause_all)
        return worker.status()
    if action == "resume":
        await run_in_threadpool(queue.resume_all)
        worker.wake()
        return worker.status()
    raise HTTPException(422, f"不支持的操作：{action}；可用：pause, resume")


@router.get("/coverage")
async def data_coverage(
    symbol: str | None = Query(None, description="缺省为固定合约池全部"),
    symbols: str = Query(""),
    interval: str = Query("1h"),
    bars: int = Query(DEFAULT_WINDOW_BARS, ge=20, le=5000),
    from_ts: int | None = Query(None),
    to_ts: int | None = Query(None),
    include_replay: bool = Query(False),
    repair: bool = Query(False, description="从交易所只补交易时段内的缺口"),
):
    """Coverage, provenance and the repairable gaps for a range."""
    if interval not in TIMEFRAMES:
        raise HTTPException(422, f"不支持的周期：{interval}；仅支持 {', '.join(TIMEFRAMES)}")
    targets, product_types = _resolve(symbols, symbol)

    def compute() -> dict:
        db = _db()
        first = None
        last = None
        for venue_symbol in targets:
            start, end = _window(db, venue_symbol, interval, bars, from_ts, to_ts)
            first = start if first is None else min(first, start)
            last = end if last is None else max(last, end)
        snapshot = load_snapshot(
            db,
            symbols=targets,
            interval=interval,
            from_ts=first or 0,
            to_ts=last or 0,
            product_types=product_types,
        )
        payload = snapshot_summary(snapshot)
        payload["version"] = snapshot_version(snapshot)
        payload["collectorVersion"] = COLLECTOR_VERSION
        payload["sessions"] = {
            venue_symbol: observed_session(snapshot.candles(venue_symbol), venue_symbol).as_dict()
            for venue_symbol in targets
        }
        if repair:
            repairable = sum(snapshot.repair_list(item)["bars"] for item in targets)
            if repairable:
                client = BybitClient(proxy=configured_proxy(), timeout=25.0)
                try:
                    payload["repair"] = repair_gaps(db, snapshot, client)
                finally:
                    client.close()
                # Re-read so the reported coverage reflects what was just written.
                snapshot = load_snapshot(
                    db, symbols=targets, interval=interval, from_ts=first or 0, to_ts=last or 0,
                    product_types=product_types,
                )
                fresh = snapshot_summary(snapshot)
                payload["symbols"] = fresh["symbols"]
                payload["provenance"] = fresh["provenance"]
                payload["notes"] = fresh["notes"]
                payload["version"] = snapshot_version(snapshot)
            else:
                payload["repair"] = {"symbols": 0, "ranges": 0, "bars": 0, "written": 0, "errors": [], "detail": []}
        if include_replay:
            events = list(snapshot.replay())
            payload["replay"] = {
                "events": len(events),
                "head": events[:50],
                "kinds": {kind: sum(1 for event in events if event["kind"] == kind)
                          for kind in {event["kind"] for event in events}},
            }
        return payload

    return await run_in_threadpool(compute)


@router.get("/replay")
async def data_replay(
    symbol: str = Query(..., description="如 BTCUSDT 或 BTC"),
    interval: str = Query("1h"),
    bars: int = Query(200, ge=20, le=2000),
    from_ts: int | None = Query(None),
    to_ts: int | None = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    with_funding: bool = Query(True),
    with_marks: bool = Query(False),
):
    """Walk one contract's stored history in arrival order, gaps included."""
    if interval not in TIMEFRAMES:
        raise HTTPException(422, f"不支持的周期：{interval}")
    spec = require_instrument(symbol)

    def compute() -> dict:
        db = _db()
        start, end = _window(db, spec.venue_symbol, interval, bars, from_ts, to_ts)
        snapshot = load_snapshot(
            db,
            symbols=[spec.venue_symbol],
            interval=interval,
            from_ts=start,
            to_ts=end,
            product_types={spec.venue_symbol: spec.product_type},
        )
        events = list(snapshot.replay(include_funding=with_funding, include_marks=with_marks))
        return {
            "symbol": spec.venue_symbol,
            "displaySymbol": spec.display_symbol,
            "interval": interval,
            "from": start,
            "to": end,
            "version": snapshot_version(snapshot),
            "total": len(events),
            "events": events[:limit],
            "truncated": len(events) > limit,
            "notes": snapshot.notes,
        }

    return await run_in_threadpool(compute)
