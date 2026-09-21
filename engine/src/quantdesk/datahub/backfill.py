"""Full-history backfill: page back to the listing, resume, snapshot, classify.

Three requirements shape this module.

**Full history.** The venue serves at most 1000 bars per call, so "since listing"
means walking backwards page by page until the venue stops returning rows. The
walk is bounded by a page budget so one call cannot run forever, and a budget that
runs out is reported as *incomplete* rather than as an error.

**Resumable.** Progress is written after every page: the earliest bar reached, the
page count, and how the last attempt ended. An interrupted walk - a crash, a
closed laptop, a rate limit - continues from its own frontier next time instead of
re-downloading everything it already has.

**Classified failures.** "It failed" is not an operator message. Every failure is
recorded with a kind (`rate_limited`, `timeout`, `network`, `invalid_symbol`,
`no_data`, …) and the sentence that produced it, so the next run can be reasoned
about instead of retried blindly.

Nothing here computes prices, returns or funding: it moves bars from the venue
into the store and records where they came from.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .venue import INTERVAL_MS

# Failure kinds. Stable strings because they are stored and displayed.
RATE_LIMITED = "rate_limited"
TIMEOUT = "timeout"
NETWORK = "network"
UPSTREAM_ERROR = "upstream_error"
INVALID_SYMBOL = "invalid_symbol"
INVALID_INTERVAL = "invalid_interval"
NO_DATA = "no_data"
BUDGET_EXHAUSTED = "budget_exhausted"
UNKNOWN = "unknown"

FAILURE_LABELS = {
    RATE_LIMITED: "被交易所限速（429），可稍后从断点继续",
    TIMEOUT: "请求超时，可从断点继续",
    NETWORK: "网络不可达，可从断点继续",
    UPSTREAM_ERROR: "交易所 5xx，可从断点继续",
    INVALID_SYMBOL: "合约代码不被交易所接受",
    INVALID_INTERVAL: "周期不被支持",
    NO_DATA: "交易所没有返回数据（可能已到上线日或该周期无成交）",
    BUDGET_EXHAUSTED: "达到本次页数预算，尚未回溯到上线；可再次运行继续",
    UNKNOWN: "未分类失败",
}

# The venue's per-call cap.
PAGE_BARS = 1000
# Default page budget for one run: enough for ~1 year of 15m bars, and a bound on
# how long a single command may hold the process.
DEFAULT_MAX_PAGES = 40


def classify_failure(exc: BaseException | None, *, http_status: int | None = None, ret_code: int | None = None) -> str:
    """Turn whatever went wrong into a kind an operator can act on."""
    if ret_code == 10006:
        return RATE_LIMITED
    if http_status == 429:
        return RATE_LIMITED
    if http_status is not None and http_status >= 500:
        return UPSTREAM_ERROR
    if http_status is not None and http_status in (400, 401, 403, 404):
        return INVALID_SYMBOL

    text = f"{type(exc).__name__}: {exc}" if exc is not None else ""
    lowered = text.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return TIMEOUT
    if "429" in text or "rate limit" in lowered or "too many" in lowered:
        return RATE_LIMITED
    if "10006" in text:
        return RATE_LIMITED
    if any(token in lowered for token in ("connect", "network", "unreachable", "dns", "socket", "proxy")):
        return NETWORK
    if "unsupported bybit interval" in lowered or "unsupported interval" in lowered:
        return INVALID_INTERVAL
    if any(token in lowered for token in ("retcode=10001", "param", "symbol", "instrument")):
        return INVALID_SYMBOL
    if isinstance(exc, ValueError):
        return INVALID_INTERVAL
    return UNKNOWN


@dataclass
class BackfillOutcome:
    """What one backfill run did, and what it could not do."""

    venue: str
    symbol: str
    interval: str
    pages: int = 0            # pages fetched by this run
    pages_total: int = 0      # pages this walk has fetched across all its runs
    bars_fetched: int = 0
    # Bars this run added or corrected. Re-running the same pages adds nothing,
    # which is the number that proves a backfill is idempotent.
    bars_stored: int = 0
    bars_unchanged: int = 0
    # Accumulated per-row write outcomes for this run.
    report: object = None
    oldest_ts: int | None = None
    newest_ts: int | None = None
    complete: bool = False
    # Distinct rows in the store for this series, counted from the database rather
    # than accumulated from this run's fetches: overlapping pages must not inflate it.
    bars_available: int = 0
    resumed_from: int | None = None
    stopped_because: str = ""
    failure_kind: str = ""
    failure: str = ""
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "interval": self.interval,
            "pages": self.pages,
            "pagesTotal": self.pages_total,
            "barsFetched": self.bars_fetched,
            "barsStored": self.bars_stored,
            "barsUnchanged": self.bars_unchanged,
            "barsAvailable": self.bars_available,
            "oldestTs": self.oldest_ts,
            "newestTs": self.newest_ts,
            "complete": self.complete,
            "resumedFrom": self.resumed_from,
            "stoppedBecause": self.stopped_because,
            "failureKind": self.failure_kind,
            "failure": self.failure,
            "failureLabel": FAILURE_LABELS.get(self.failure_kind, ""),
            "warnings": self.warnings,
            "durationMs": self.duration_ms,
        }


class HistoryBackfill:
    """Walk a symbol's history back to the listing, resumably and audibly."""

    def __init__(self, db, fetch, *, venue: str = "bybit", now: Callable[[], int] | None = None,
                 sleep: Callable[[float], None] | None = None, page_bars: int = PAGE_BARS,
                 data_kind: str = "trade_candle", store: Callable[..., Any] | None = None,
                 counter: Callable[[], int] | None = None,
                 bounds: Callable[[], tuple[int | None, int | None]] | None = None):
        # `fetch(symbol, interval, start_ms, end_ms)` returns normalized bars.
        self.db = db
        self.fetch = fetch
        self.venue = venue
        # Which series this walk fills. Part of the state key, so a mark-price
        # walk and a trade-candle walk over one symbol keep separate progress.
        self.data_kind = data_kind
        # Where the bars go. A mark-price walk writes mark_candles; the default is
        # the trade-candle store, because that is what a candle walk means.
        self._store = store
        self._counter = counter
        self._bounds = bounds
        self._now = now or (lambda: int(time.time() * 1000))
        self._sleep = sleep or time.sleep
        self.page_bars = int(page_bars)

    # -- state -----------------------------------------------------------
    def state(self, symbol: str, interval: str) -> dict | None:
        return self.db.load_backfill_state(self.venue, symbol, interval, self.data_kind)

    def _save_state(self, *, symbol: str, interval: str, outcome: BackfillOutcome, attempts: int,
                    error: str | None, error_kind: str | None) -> None:
        self.db.upsert_backfill_state(
            {
                "venue": self.venue,
                "symbol": symbol,
                "interval": interval,
                "data_kind": self.data_kind,
                "oldest_ts": outcome.oldest_ts,
                "newest_ts": outcome.newest_ts,
                "complete": outcome.complete,
                "pages": outcome.pages_total,
                "rows_available": outcome.bars_available,
                "attempts": attempts,
                "status": "ok",
                "last_error": error,
                "last_error_kind": error_kind,
                "last_run_ts": self._now(),
            }
        )

    # -- run -------------------------------------------------------------
    def run(
        self,
        symbol: str,
        interval: str,
        *,
        max_pages: int = DEFAULT_MAX_PAGES,
        restart: bool = False,
        start_ms: int | None = None,
        interval_ms: int | None = None,
        before_page: Callable[[], bool] | None = None,
        on_page: Callable[[int, int], bool] | None = None,
    ) -> BackfillOutcome:
        """Page backwards from the newest bar until the venue stops answering."""
        step = interval_ms or INTERVAL_MS.get(interval)
        if step is None:
            outcome = BackfillOutcome(
                self.venue, symbol, interval, stopped_because="invalid_interval",
                failure_kind=INVALID_INTERVAL, failure=f"不支持的周期：{interval}",
            )
            return outcome

        previous = None if restart else self.state(symbol, interval)
        attempts = int((previous or {}).get("attempts") or 0) + 1
        outcome = BackfillOutcome(self.venue, symbol, interval)
        started = time.monotonic()

        cursor = int(start_ms) if start_ms is not None else None
        if cursor is None:
            if previous and previous.get("oldest_ts") and not previous.get("complete"):
                # Resume from the frontier rather than from now: everything newer
                # is already stored. The running totals carry over so the state
                # describes the whole walk, not just its latest run.
                cursor = int(previous["oldest_ts"])
                outcome.resumed_from = cursor
                outcome.bars_stored = int(previous.get("bars") or 0)
                outcome.pages_total = int(previous.get("pages") or 0)
            else:
                cursor = self._now()

        stored_total = int(outcome.bars_stored)
        newest_seen: int | None = None
        oldest_seen: int | None = None
        failure_kind = ""
        failure = ""

        for page in range(max_pages):
            if before_page is not None and not before_page():
                outcome.stopped_because = "stopped_by_caller"
                break
            window_end = cursor - 1 if page > 0 or outcome.resumed_from else cursor
            window_start = window_end - self.page_bars * step
            if window_end <= 0:
                outcome.complete = True
                outcome.stopped_because = "reached_epoch"
                break
            try:
                rows = self.fetch(symbol, interval, window_start, window_end)
            except Exception as exc:  # noqa: BLE001 - classified, then reported
                failure_kind = classify_failure(exc)
                failure = f"{type(exc).__name__}: {exc}"
                outcome.stopped_because = failure_kind
                break

            if not rows:
                # The venue has nothing before this point: either the listing date
                # or a genuinely empty stretch. Both end the walk.
                outcome.complete = True
                outcome.stopped_because = "no_more_data"
                break

            page_oldest = min(int(row["ts"]) for row in rows)
            page_newest = max(int(row["ts"]) for row in rows)
            oldest_seen = page_oldest if oldest_seen is None else min(oldest_seen, page_oldest)
            newest_seen = page_newest if newest_seen is None else max(newest_seen, page_newest)

            # Provenance is the venue REST read; *how* it arrived (a history walk)
            # is recorded separately, so this series is never an unknown source.
            if self._store is not None:
                report = self._store(symbol, interval, rows)
            else:
                report = self.db.upsert_candles(
                    self.venue, symbol, interval, rows,
                    source="venue_rest", ingestion_mode="backfill",
                )
            stored_total += report.written
            outcome.report = report if outcome.report is None else outcome.report + report
            outcome.pages += 1
            outcome.pages_total = outcome.pages_total + 1
            outcome.bars_fetched += len(rows)
            outcome.bars_stored = stored_total
            outcome.oldest_ts = oldest_seen
            outcome.newest_ts = newest_seen
            # What the store holds right now, not after the walk finishes: a long
            # walk must not report zero rows while it is filling the series.
            outcome.bars_available = (
                self._counter() if self._counter is not None
                else self.db.count_candles(self.venue, symbol, interval)
            )

            # Persist after every page: this is what makes the walk resumable.
            self._save_state(
                symbol=symbol, interval=interval, outcome=outcome, attempts=attempts,
                error=None, error_kind=None,
            )
            if on_page is not None and not on_page(outcome.pages, outcome.bars_available):
                # The caller asked to stop: a pause or a cancellation. The frontier
                # is already saved, so the walk can continue from exactly here.
                outcome.stopped_because = "stopped_by_caller"
                break

            # A short page is *not* proof that the history ended: the venue omits
            # the still-forming bar, so a window holding exactly one page comes
            # back one row short. Only an empty window ends the walk.
            cursor = page_oldest
        else:
            outcome.stopped_because = "budget_exhausted"
            failure_kind = BUDGET_EXHAUSTED
            failure = FAILURE_LABELS[BUDGET_EXHAUSTED]

        # What the store now holds, which is a superset of what this run fetched:
        # the snapshot describes the data, not the run.
        if self._bounds is not None:
            first, last = self._bounds()
        else:
            first = self.db.first_open_ts(self.venue, symbol, interval)
            last = self.db.last_open_ts(self.venue, symbol, interval)
        if first is not None:
            outcome.oldest_ts = min(outcome.oldest_ts, first) if outcome.oldest_ts else first
        if last is not None:
            outcome.newest_ts = max(outcome.newest_ts, last) if outcome.newest_ts else last
        # The store's own count, not this run's arithmetic.
        outcome.bars_available = (
            self._counter() if self._counter is not None
            else self.db.count_candles(self.venue, symbol, interval)
        )
        if outcome.report is not None:
            outcome.bars_unchanged = int(getattr(outcome.report, "unchanged", 0))
        outcome.failure_kind = failure_kind
        outcome.failure = failure
        if outcome.pages_total == 0:
            outcome.pages_total = outcome.pages
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        if outcome.stopped_because == "short_page" or outcome.complete:
            outcome.complete = True
        self._save_state(
            symbol=symbol, interval=interval, outcome=outcome, attempts=attempts,
            error=failure or None, error_kind=failure_kind or None,
        )
        return outcome

    # -- snapshot --------------------------------------------------------
    def snapshot(self, symbol: str, interval: str, *, session_windows=None) -> dict:
        """Pin the stored history and record it as a reproducible snapshot.

        The version comes from the bars themselves, so the same data always
        produces the same version and a backtest can state which history it read.
        """
        from .snapshot import history_version, load_snapshot

        first = self.db.first_open_ts(self.venue, symbol, interval)
        last = self.db.last_open_ts(self.venue, symbol, interval)
        if first is None or last is None:
            record = {
                "venue": self.venue, "symbol": symbol, "interval": interval,
                "version": "", "from_ts": 0, "to_ts": 0, "bars": 0,
                "complete": False, "missing_in_session": 0, "sources": {},
                "available": False, "reason": "本地没有该周期的K线",
            }
            return record
        snapshot = load_snapshot(
            self.db, symbols=[symbol], interval=interval, from_ts=first, to_ts=last,
            venue=self.venue, service_windows=session_windows,
        )
        rows = snapshot.candles(symbol)
        coverage = snapshot.coverage[symbol]
        version = history_version(self.venue, symbol, interval, first, last, rows)
        record = {
            "venue": self.venue,
            "symbol": symbol,
            "interval": interval,
            "data_kind": self.data_kind,
            "version": version,
            "from_ts": first,
            "to_ts": last,
            "bars": len(rows),
            # Same number, read from the store: the count a reader can verify.
            "barsAvailable": self.db.count_candles(self.venue, symbol, interval),
            "complete": bool(coverage.complete),
            "missing_in_session": int(coverage.missing_in_session),
            "sources": dict(coverage.sources),
            "available": True,
            "created_ts": self._now(),
        }
        self.db.record_history_snapshot(record)
        return record

    def backfill_and_snapshot(self, symbol: str, interval: str, **kwargs) -> dict:
        outcome = self.run(symbol, interval, **kwargs)
        record = self.snapshot(symbol, interval, session_windows=kwargs.get("session_windows"))
        return {"outcome": outcome.as_dict(), "snapshot": record}


def bybit_fetch(client, category: str):
    """A fetch function for `HistoryBackfill` backed by the venue client."""

    def fetch(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
        return client.kline(category, symbol, interval, start_ms, end_ms, max_bars=PAGE_BARS)

    return fetch
