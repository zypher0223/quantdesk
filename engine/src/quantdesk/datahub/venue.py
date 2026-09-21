"""Venue adapter interface.

Every adapter returns normalized candle dicts:
    {ts: int(ms bar open), open, high, low, close, volume, trades: int|None}
ordered oldest-first. Adapters must be read-only and keyless (public data).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

CANONICAL_INTERVALS = ("15m", "1h", "4h", "1d", "1w")

# Canonical interval -> milliseconds
INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}


def last_closed_open_ts(newest: int | None, step_ms: int, *, now_ms: int | None = None) -> int:
    """The newest bar open time whose bar has already closed.

    The store can hold the bar that is forming right now - the venue stream writes
    it as it moves - and every reader that means "history" has to end before it.
    One rule in one place: backtests, alerts, coverage windows and research
    provenance ask this function instead of each re-deriving the boundary.
    """
    import time as _time

    now = int(now_ms if now_ms is not None else _time.time() * 1000)
    last_closed = now // step_ms * step_ms - step_ms
    return last_closed if newest is None else min(int(newest), last_closed)


@dataclass(frozen=True)
class VenueInfo:
    name: str
    supports_perp: bool
    supports_spot: bool


class Venue(Protocol):
    name: str

    def fetch_candles(
        self, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> list[dict]: ...


def validate_interval(interval: str) -> str:
    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported interval {interval!r}; choose from {list(INTERVAL_MS)}")
    return interval
