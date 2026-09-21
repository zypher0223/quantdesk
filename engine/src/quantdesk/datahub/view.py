"""The single data version every consumer reads.

The backtest, the alert engine, the strategy validators and TradingAgents research
all need history, and they need it to be the *same* history: a signal explained by
bars that a backtest cannot see is not reproducible, and a report written against
a dataset that has since been repaired is not a report anyone can check.

`read_history` is that one read. It returns the bars a consumer asked for, the
funding and mark rows that go with them, the coverage of that range, and a version
string that pins the whole thing. The version is a hash of the *content* - bars,
funding and marks - plus the range, so two consumers that asked for the same range
get the same version, and a range that gains a bar gets a new one.

The version is deliberately independent of when the call happened: `generated_at`
is reported separately, so "same data" and "same moment" are not confused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .venue import INTERVAL_MS

HISTORY_VERSION = "history/1"
# Bars a consumer gets when it does not say how much it needs.
DEFAULT_BARS = 500


@dataclass
class HistorySlice:
    """One symbol's pinned history, plus what is known about its completeness."""

    symbol: str
    display_symbol: str
    product_type: str
    interval: str
    from_ts: int
    to_ts: int
    bars: list[dict]
    funding: list[dict] = field(default_factory=list)
    marks: list[dict] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    gaps: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    version: str = ""
    generated_at: int = 0

    @property
    def sources(self) -> dict[str, int]:
        return dict(self.coverage.get("sources") or {})

    def provenance(self) -> dict[str, Any]:
        """What a result stores so it can be checked later."""
        return {
            "version": self.version,
            "historyVersion": HISTORY_VERSION,
            "symbol": self.symbol,
            "interval": self.interval,
            "from": self.from_ts,
            "to": self.to_ts,
            "bars": len(self.bars),
            "sources": self.sources,
            "collectors": dict(self.coverage.get("collectors") or {}),
            "complete": bool(self.coverage.get("complete")),
            "missingInSession": int(self.coverage.get("missing_in_session") or 0),
            "generatedAt": self.generated_at,
        }


def _digest(*payloads: list[dict]) -> str:
    digest = hashlib.sha256()
    for rows in payloads:
        for row in rows:
            digest.update(
                f"{int(row['ts'])}|{float(row['open']):.10g}|{float(row['high']):.10g}|"
                f"{float(row['low']):.10g}|{float(row['close']):.10g}|{float(row.get('volume') or 0):.10g}\n".encode()
            )
    return digest.hexdigest()[:16]


def _funding_digest(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(f"{int(row['ts'])}|{float(row['rate']):.12g}\n".encode())
    return digest.hexdigest()[:8]


def read_history(
    db,
    *,
    symbol: str,
    interval: str,
    bars: int = DEFAULT_BARS,
    venue: str = "bybit",
    product_type: str = "crypto",
    display_symbol: str | None = None,
    from_ts: int | None = None,
    to_ts: int | None = None,
    with_funding: bool = True,
    with_marks: bool = False,
    service_windows: list[tuple[int, int]] | None = None,
) -> HistorySlice:
    """Read one symbol's history over one range and pin it.

    The range ends at the newest *closed* bar unless the caller says otherwise, so
    a consumer never gets a still-forming bar handed to it as if it were history.
    """
    from .snapshot import history_version, load_snapshot
    import time as _time

    step = INTERVAL_MS.get(interval)
    if step is None:
        raise ValueError(f"unsupported interval {interval!r}")
    newest = db.last_open_ts(venue, symbol, interval)
    if to_ts is None:
        # A bar is history only once it has closed. The newest row in the store
        # can still be the bar forming right now, and handing that to a backtest,
        # a strategy or an alert is handing it a price that had not happened yet.
        from .venue import last_closed_open_ts

        to_ts = last_closed_open_ts(newest, step)
    if from_ts is None:
        from_ts = int(to_ts) - (bars - 1) * step
    snapshot = load_snapshot(
        db,
        symbols=[symbol],
        interval=interval,
        from_ts=int(from_ts),
        to_ts=int(to_ts),
        venue=venue,
        product_types={symbol: product_type},
        with_funding=with_funding,
        with_marks=with_marks,
        service_windows=service_windows,
    )
    slice_rows = snapshot.candles(symbol)
    funding_rows = snapshot.funding_rows(symbol) if with_funding else []
    mark_rows = snapshot.mark_rows(symbol) if with_marks else []
    coverage = snapshot.coverage[symbol]
    # The id is computed from the rows this caller receives, with the same
    # include/exclude choices, so a consumer that asked for funding off gets an id
    # for the data it actually read.
    version = history_version(
        venue,
        symbol,
        interval,
        int(from_ts),
        int(to_ts),
        slice_rows,
        funding=funding_rows or None,
        marks=mark_rows or None,
    )
    return HistorySlice(
        symbol=symbol,
        display_symbol=display_symbol or symbol,
        product_type=product_type,
        interval=interval,
        from_ts=int(from_ts),
        to_ts=int(to_ts),
        bars=slice_rows,
        funding=funding_rows,
        marks=mark_rows,
        coverage=coverage.as_dict(),
        gaps=[gap.as_dict() for gap in snapshot.gaps.get(symbol, [])],
        notes=list(snapshot.notes),
        version=version,
        generated_at=int(_time.time() * 1000),
    )
