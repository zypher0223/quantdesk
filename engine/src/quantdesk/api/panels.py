"""Shared panel computations.

Both the HTTP panel endpoints and the research evidence builder need the same
multi-timeframe resonance; keeping it here means neither has to import the
other, and there is exactly one implementation.
"""

from __future__ import annotations

import asyncio
import math

import pandas as pd
from fastapi.concurrency import run_in_threadpool

from ..config.instruments import CORE_TIMEFRAMES, RESONANCE_MIN_BARS, TIMEFRAMES
from ..config.settings import configured_proxy, quantdesk_home
from ..datahub.bybit import BybitClient
from ..datahub.db import Database
from ..features.indicators import add_indicators
from ..features.resonance import DEFAULT_WEIGHTS, resonance, stance_for_interval

KLINE_BARS = 400
CACHE_TTL_SECONDS = 30.0
# The collector always requests at least 30 bars. Once that batch exists it is
# more useful than opening four fresh proxy tunnels; indicator sufficiency is
# still reported separately through RESONANCE_MIN_BARS.
LOCAL_MIN_BARS = 30


# One cache for the whole gateway. It lives here rather than on the app object
# so the panel endpoints and the research layer can share it without importing
# each other.
_CACHE: "TTLCache | None" = None


def init_cache(ttl: float = CACHE_TTL_SECONDS) -> "TTLCache":
    global _CACHE
    _CACHE = TTLCache(ttl)
    return _CACHE


def get_cache() -> "TTLCache":
    global _CACHE
    if _CACHE is None:
        _CACHE = TTLCache()
    return _CACHE


class TTLCache:
    """Tiny async TTL cache with single-flight per key."""

    def __init__(self, ttl: float = CACHE_TTL_SECONDS):
        self.ttl = ttl
        self._values: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get_or_set(self, key: str, factory):
        import time

        hit = self._values.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._values.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl:
                return hit[1]
            value = await factory()
            self._values[key] = (time.monotonic(), value)
            return value


def clean(value):
    """JSON has no NaN/Infinity — convert to null rather than emit invalid JSON."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def candles_frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame = frame.rename(columns={"open_ts": "ts"})
    return frame[["ts", "open", "high", "low", "close", "volume"]]


def _load_local_intervals(venue_symbol: str, intervals: list[str], bars: int) -> dict[str, list[dict]]:
    """Read the scheduler's completed-candle cache in one SQLite connection."""
    db: Database | None = None
    try:
        db = Database(quantdesk_home() / "quantdesk.db")
        return {
            interval: db.load_candles("bybit", venue_symbol, interval, limit=bars)
            for interval in intervals
        }
    except Exception:
        # The local cache is an acceleration layer. A locked, missing, or
        # damaged cache must not prevent the live Bybit fallback from running.
        return {}
    finally:
        if db is not None:
            db.close()


async def fetch_interval(
    cache: TTLCache,
    venue_symbol: str,
    interval: str,
    bars: int = KLINE_BARS,
    local_rows: list[dict] | None = None,
) -> tuple[list[dict], str]:
    """Closed candles for one timeframe, briefly cached.

    Bybit public endpoints are rate limited and the fastest analysis timeframe
    closes every 15 minutes, so a short TTL removes the repeat cost of switching
    symbols without hiding fresh bars.
    """

    async def factory():
        if local_rows is not None and len(local_rows) >= LOCAL_MIN_BARS:
            return {"rows": local_rows, "source": "local"}
        client = BybitClient(proxy=configured_proxy(), timeout=20.0)
        try:
            rows = await run_in_threadpool(lambda: client.kline_snapshot(venue_symbol, interval, limit=bars))
            return {"rows": rows, "source": "bybit"}
        finally:
            client.close()

    snapshot = await cache.get_or_set(f"kline:v2:{venue_symbol}:{interval}:{bars}", factory)
    return snapshot["rows"], snapshot["source"]  # type: ignore[index,return-value]


async def compute_resonance(
    cache: TTLCache,
    venue_symbol: str,
    intervals: list[str] | None = None,
    bars: int = KLINE_BARS,
) -> dict:
    """Multi-timeframe resonance computed by the engine, not by the browser."""
    # Default to the core timeframes: adding an analysis timeframe (weekly) must not
    # change what the resonance panel means. Pass `intervals` to include more.
    wanted = list(intervals or CORE_TIMEFRAMES)
    local = await run_in_threadpool(_load_local_intervals, venue_symbol, wanted, bars)
    snapshots = await asyncio.gather(
        *(fetch_interval(cache, venue_symbol, item, bars, local.get(item)) for item in wanted)
    )
    fetched = [snapshot[0] for snapshot in snapshots]
    sources = {interval: snapshot[1] for interval, snapshot in zip(wanted, snapshots)}

    def compute() -> dict:
        stances = []
        unavailable = []
        for interval, rows in zip(wanted, fetched):
            frame = candles_frame(rows)
            enough = len(frame) >= RESONANCE_MIN_BARS
            if not enough:
                unavailable.append({"interval": interval, "bars": len(frame), "required": RESONANCE_MIN_BARS})
            if frame.empty:
                continue
            frame.attrs["interval"] = interval
            score = stance_for_interval(add_indicators(frame))
            if not enough:
                score.notes["insufficient_bars"] = True
            stances.append(score)
        combined = resonance(stances, DEFAULT_WEIGHTS)
        # Carry the weights that were actually applied, so every consumer — the
        # panel and the research bundle alike — states the same thing.
        combined["weights"] = dict(DEFAULT_WEIGHTS)
        combined["unavailable"] = unavailable
        combined["barsByInterval"] = {interval: len(rows) for interval, rows in zip(wanted, fetched)}
        combined["dataSources"] = sources
        return combined

    return await run_in_threadpool(compute)


def resonance_frames(result: dict) -> dict[str, dict]:
    """Flatten a resonance result into citable per-timeframe evidence rows."""
    frames: dict[str, dict] = {}
    for frame in result.get("timeframes", []):
        notes = frame.get("notes") or {}
        frames[frame["interval"]] = {
            "close": frame.get("close"),
            "stance": frame.get("stance"),
            "score": frame.get("score"),
            "trend": frame.get("trend"),
            "momentum": frame.get("momentum"),
            "volume": frame.get("volume"),
            "adx": notes.get("adx"),
            "rsi": notes.get("rsi"),
            "session_thin": notes.get("session_thin"),
            "activity_ratio": notes.get("activity_ratio"),
            "bars": (result.get("barsByInterval") or {}).get(frame["interval"]),
        }
    return frames
