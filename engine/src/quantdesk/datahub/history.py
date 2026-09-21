"""Every historical series QuantDesk needs, walked the same resumable way.

Trade candles are not the only history a backtest reads: liquidation and funding
settle against the venue's mark price, funding is charged at the venue's own
timestamps, open interest is a factor input, and the risk ladder decides margin.
Each of those is a separate walk over the same symbol with its own progress, its
own snapshot and its own failure history - which is why the state key carries a
`data_kind`.

Two rules are enforced here rather than left to callers:

* **A missing series is named, never zero-filled.** When the venue does not
  publish a family for a contract, the state says `unsupported` with the reason.
  Writing zeros would make the data look present and every downstream number
  quietly wrong.
* **A walk resumes from its own frontier.** Marks, funding and open interest each
  page backwards and save progress per page, exactly like candles.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .backfill import (
    BUDGET_EXHAUSTED,
    FAILURE_LABELS,
    INVALID_INTERVAL,
    NO_DATA,
    HistoryBackfill,
    classify_failure,
)
from .venue import INTERVAL_MS

# The data families a contract can have. Stable strings: they are stored.
TRADE_CANDLE = "trade_candle"
MARK_CANDLE = "mark_candle"
FUNDING = "funding"
OPEN_INTEREST = "open_interest"
RISK_LIMIT = "risk_limit"

DATA_KINDS = (TRADE_CANDLE, MARK_CANDLE, FUNDING, OPEN_INTEREST, RISK_LIMIT)

# Kinds that are indexed by a timeframe; the others are symbol-level series.
TIMEFRAMED_KINDS = (TRADE_CANDLE, MARK_CANDLE)

# Kinds the venue publishes only as a current snapshot rather than a history.
SNAPSHOT_KINDS = (RISK_LIMIT,)

KIND_LABELS = {
    TRADE_CANDLE: "成交K线",
    MARK_CANDLE: "标记价格K线",
    FUNDING: "资金费率",
    OPEN_INTEREST: "持仓量",
    RISK_LIMIT: "风险档位",
}

SERIES_VERSION = "series/1"

# The venue caps these endpoints below the 1000-row kline cap.
FUNDING_PAGE = 200
OI_PAGE = 200
MARK_PAGE = 1000

# Open interest is published on a grid, not per candle.
OI_INTERVALS = ("5min", "15min", "30min", "1h", "4h", "1d")
DEFAULT_OI_INTERVAL = "1h"


def series_version(kind: str, venue: str, symbol: str, rows: list[dict], *, interval: str = "") -> str:
    """A stable id for a non-candle series, from its own contents."""
    material = json.dumps(
        {
            "kind": kind,
            "venue": venue,
            "symbol": symbol,
            "interval": interval,
            "rows": rows,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return f"{SERIES_VERSION}:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


@dataclass
class KindOutcome:
    """What one series' walk did, in the same shape for every kind."""

    venue: str
    symbol: str
    data_kind: str
    interval: str = ""
    pages: int = 0
    pages_total: int = 0
    rows_fetched: int = 0
    rows_stored: int = 0
    rows_available: int = 0
    oldest_ts: int | None = None
    newest_ts: int | None = None
    complete: bool = False
    status: str = "ok"                 # ok | unsupported
    reason: str = ""
    failure_kind: str = ""
    failure: str = ""
    resumed_from: int | None = None
    stopped_because: str = ""
    duration_ms: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dataKind": self.data_kind,
            "kindLabel": KIND_LABELS.get(self.data_kind, self.data_kind),
            "symbol": self.symbol,
            "interval": self.interval,
            "pages": self.pages,
            "pagesTotal": self.pages_total,
            "rowsFetched": self.rows_fetched,
            "rowsStored": self.rows_stored,
            "rowsAvailable": self.rows_available,
            "oldestTs": self.oldest_ts,
            "newestTs": self.newest_ts,
            "complete": self.complete,
            "status": self.status,
            "reason": self.reason,
            "failureKind": self.failure_kind,
            "failure": self.failure,
            "failureLabel": FAILURE_LABELS.get(self.failure_kind, ""),
            "resumedFrom": self.resumed_from,
            "stoppedBecause": self.stopped_because,
            "durationMs": self.duration_ms,
            "warnings": self.warnings,
        }


class HistoryCollector:
    """Walks every historical series a contract has, one kind at a time."""

    def __init__(self, db, client, *, venue: str = "bybit", sleep: Callable[[float], None] | None = None,
                 now: Callable[[], int] | None = None):
        self.db = db
        self.client = client
        self.venue = venue
        self._sleep = sleep or time.sleep
        self._now = now or (lambda: int(time.time() * 1000))

    # -- instrument metadata ---------------------------------------------
    def collect_instrument_meta(self, symbol: str, category: str = "linear") -> dict:
        """Store what the venue says the contract is, including its launch time.

        The launch time is what tells a reader whether a short history is a
        backfill failure or simply the whole life of the contract.
        """
        rows = self.client.instruments(category, symbol=symbol)
        if not rows:
            return {"symbol": symbol, "available": False, "reason": "交易所没有返回该合约的元数据"}
        raw = rows[0]
        entry = {
            "venue": self.venue,
            "symbol": symbol,
            "display_symbol": raw.get("symbol"),
            "contract_type": raw.get("contractType") or raw.get("symbolType"),
            "status": raw.get("status"),
            "launch_ts": _int_or_none(raw.get("launchTime")),
            "tick_size": _float_or_none(raw.get("priceFilter", {}).get("tickSize") if isinstance(raw.get("priceFilter"), dict) else None),
            "qty_step": _float_or_none(raw.get("lotSizeFilter", {}).get("qtyStep") if isinstance(raw.get("lotSizeFilter"), dict) else None),
            "min_notional": _float_or_none(raw.get("lotSizeFilter", {}).get("minNotionalValue") if isinstance(raw.get("lotSizeFilter"), dict) else None),
            # Bybit publishes this field in minutes (480 means eight hours).
            "funding_interval_hours": (
                _float_or_none(raw.get("fundingInterval")) / 60
                if _float_or_none(raw.get("fundingInterval")) is not None else None
            ),
            "raw": raw,
            "collected_ts": self._now(),
        }
        self.db.upsert_instrument_meta(entry)
        return {**entry, "available": True}

    def _supports(self, symbol: str, data_kind: str) -> tuple[bool, str]:
        """Whether the venue publishes this family for this contract.

        Only the cases the venue actually states are refused here; anything else
        is attempted, because a guess either way would be worse than a fetch.
        """
        meta = self.db.load_instrument_meta(self.venue, symbol)
        if meta is None:
            return True, ""
        if data_kind == FUNDING:
            interval = meta.get("funding_interval_hours")
            if interval is None:
                return False, "交易所元数据未给出资金费结算周期，该合约不提供资金费率历史"
        return True, ""

    # -- dispatch --------------------------------------------------------
    def run(self, symbol: str, data_kind: str, interval: str = "", *, max_pages: int = 40,
            restart: bool = False, category: str = "linear",
            oi_interval: str = DEFAULT_OI_INTERVAL,
            before_page: Callable[[], bool] | None = None,
            on_page: Callable[[int, int], bool] | None = None) -> KindOutcome:
        if data_kind not in DATA_KINDS:
            return KindOutcome(
                self.venue, symbol, data_kind, interval, status="unsupported",
                reason=f"未知的数据类型：{data_kind}", failure_kind=INVALID_INTERVAL,
            )
        if data_kind in TIMEFRAMED_KINDS and interval not in INTERVAL_MS:
            return KindOutcome(
                self.venue, symbol, data_kind, interval, status="unsupported",
                reason=f"不支持的时间周期：{interval}", failure_kind=INVALID_INTERVAL,
            )
        supported, reason = self._supports(symbol, data_kind)
        if not supported:
            outcome = KindOutcome(
                self.venue, symbol, data_kind, interval, complete=True,
                status="unsupported", reason=reason,
            )
            self._save(outcome, attempts=self._attempts(symbol, data_kind, interval) + 1,
                       error=None, error_kind=None)
            return outcome

        if data_kind == TRADE_CANDLE:
            return self._run_candles(symbol, interval, max_pages, restart, category, TRADE_CANDLE,
                                     before_page, on_page)
        if data_kind == MARK_CANDLE:
            return self._run_candles(symbol, interval, max_pages, restart, category, MARK_CANDLE,
                                     before_page, on_page)
        if data_kind == FUNDING:
            return self._run_funding(symbol, max_pages, restart, category, before_page, on_page)
        if data_kind == OPEN_INTEREST:
            return self._run_open_interest(symbol, max_pages, restart, category, oi_interval,
                                           before_page, on_page)
        return self._run_risk_limit(symbol, category)

    # -- per-kind walks --------------------------------------------------
    def _run_candles(self, symbol: str, interval: str, max_pages: int, restart: bool,
                     category: str, data_kind: str,
                     before_page: Callable[[], bool] | None = None,
                     on_page: Callable[[int, int], bool] | None = None) -> KindOutcome:
        if data_kind == MARK_CANDLE:
            def fetch(sym: str, itv: str, start_ms: int, end_ms: int) -> list[dict]:
                return self.client.mark_price_kline(
                    sym, itv, limit=MARK_PAGE, category=category, start_ms=start_ms, end_ms=end_ms,
                )
        else:
            def fetch(sym: str, itv: str, start_ms: int, end_ms: int) -> list[dict]:
                return self.client.kline(category, sym, itv, start_ms, end_ms)

        if data_kind == MARK_CANDLE:
            # Mark prices are their own series and their own table: mixing them
            # into the trade-candle store would silently rewrite the price a
            # backtest matches against.
            store = lambda sym, itv, rows: self._store_marks(sym, itv, rows)  # noqa: E731
            counter = lambda: self.db.count_mark_candles(self.venue, symbol, interval)  # noqa: E731
            bounds = lambda: self.db.mark_bounds(self.venue, symbol, interval)  # noqa: E731
        else:
            store = counter = bounds = None
        walk = HistoryBackfill(
            self.db, fetch, venue=self.venue, now=self._now, sleep=self._sleep,
            page_bars=MARK_PAGE, data_kind=data_kind,
            store=store, counter=counter, bounds=bounds,
        )
        result = walk.run(symbol, interval, max_pages=max_pages, restart=restart,
                          before_page=before_page, on_page=on_page)
        payload = result.as_dict()
        outcome = KindOutcome(
            self.venue, symbol, data_kind, interval,
            pages=payload["pages"], pages_total=payload["pagesTotal"],
            rows_fetched=payload["barsFetched"],
            rows_stored=payload["barsStored"], rows_available=payload["barsAvailable"],
            oldest_ts=payload["oldestTs"], newest_ts=payload["newestTs"],
            complete=payload["complete"], failure_kind=payload["failureKind"],
            failure=payload["failure"], resumed_from=payload["resumedFrom"],
            duration_ms=payload["durationMs"], warnings=list(payload["warnings"]),
        )
        self._save(outcome, attempts=self._attempts(symbol, data_kind, interval),
                   error=outcome.failure or None, error_kind=outcome.failure_kind or None)
        return outcome

    def _walk(self, *, symbol: str, data_kind: str, interval: str, page: int, max_pages: int,
              restart: bool, fetch_window: Callable[[int, int], list[dict]],
              store: Callable[[list[dict]], int], count: Callable[[], int],
              bounds: Callable[[], tuple[int | None, int | None]],
              row_spacing_ms: int = 3_600_000,
              before_page: Callable[[], bool] | None = None,
              on_page: Callable[[int, int], bool] | None = None) -> KindOutcome:
        """Shared backward walk for the symbol-level series.

        The window has to be wide enough to actually contain a full page: a
        one-day window returns three funding settlements, not two hundred, and a
        short window would look like "the venue has no more data" while the real
        history is still further back.
        """
        window_ms = max(row_spacing_ms, page * row_spacing_ms)
        previous = None if restart else self.db.load_backfill_state(self.venue, symbol, interval, data_kind)
        attempts = int((previous or {}).get("attempts") or 0) + 1
        outcome = KindOutcome(self.venue, symbol, data_kind, interval)
        started = time.monotonic()

        if previous and previous.get("oldest_ts") and not previous.get("complete"):
            cursor = int(previous["oldest_ts"])
            outcome.resumed_from = cursor
            outcome.pages_total = int(previous.get("pages") or 0)
        else:
            cursor = self._now()

        oldest_seen: int | None = None
        newest_seen: int | None = None
        total_stored = 0
        failure_kind = ""
        failure = ""

        for _ in range(max_pages):
            if before_page is not None and not before_page():
                outcome.stopped_because = "stopped_by_caller"
                break
            window_end = cursor - 1
            try:
                rows = fetch_window(window_end - window_ms, window_end)
            except Exception as exc:  # noqa: BLE001 - classified, then reported
                failure_kind = classify_failure(exc)
                failure = f"{type(exc).__name__}: {exc}"
                break
            if not rows:
                outcome.complete = True
                break
            page_oldest = min(int(row["ts"]) for row in rows)
            page_newest = max(int(row["ts"]) for row in rows)
            oldest_seen = page_oldest if oldest_seen is None else min(oldest_seen, page_oldest)
            newest_seen = page_newest if newest_seen is None else max(newest_seen, page_newest)
            total_stored += store(rows)
            outcome.pages += 1
            outcome.pages_total += 1
            outcome.rows_fetched += len(rows)
            outcome.rows_stored = total_stored
            outcome.oldest_ts = oldest_seen
            outcome.newest_ts = newest_seen
            outcome.rows_available = count()
            self._save(outcome, attempts=attempts, error=None, error_kind=None)
            if on_page is not None and not on_page(outcome.pages, outcome.rows_available):
                outcome.stopped_because = "stopped_by_caller"
                break
            # Same rule as the candle walk: only an empty window proves the venue
            # has nothing older. A short page can simply be a window that holds
            # fewer rows than the page size.
            cursor = page_oldest
        else:
            failure_kind = BUDGET_EXHAUSTED
            failure = FAILURE_LABELS[BUDGET_EXHAUSTED]

        lo, hi = bounds()
        if lo is not None:
            outcome.oldest_ts = min(outcome.oldest_ts, lo) if outcome.oldest_ts else lo
        if hi is not None:
            outcome.newest_ts = max(outcome.newest_ts, hi) if outcome.newest_ts else hi
        outcome.rows_available = count()
        outcome.failure_kind = failure_kind
        outcome.failure = failure
        if not outcome.rows_available and not failure:
            # Nothing at all and no error: the venue simply has no such history
            # for this contract. That is a named state, not a zero.
            outcome.status = "unsupported"
            outcome.reason = "交易所未返回该合约的任何数据，且元数据未表明支持该数据族"
            outcome.failure_kind = NO_DATA
            outcome.complete = True
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        self._save(outcome, attempts=attempts, error=failure or None, error_kind=failure_kind or None)
        return outcome

    def _run_funding(self, symbol: str, max_pages: int, restart: bool, category: str,
                     before_page: Callable[[], bool] | None = None,
                     on_page: Callable[[int, int], bool] | None = None) -> KindOutcome:
        def fetch_window(start_ms: int, end_ms: int) -> list[dict]:
            return self.client.funding_history_window(symbol, start_ms, end_ms, limit=FUNDING_PAGE)

        def store(rows: list[dict]) -> int:
            return self._store_funding(symbol, rows)

        return self._walk(
            symbol=symbol, data_kind=FUNDING, interval="", page=FUNDING_PAGE, max_pages=max_pages,
            restart=restart, fetch_window=fetch_window, store=store,
            count=lambda: self._count_series(FUNDING, symbol, ""),
            bounds=lambda: self._bounds(FUNDING, symbol, ""),
            # Funding settles every 1–8 hours depending on the contract.
            row_spacing_ms=8 * 3_600_000,
            before_page=before_page,
            on_page=on_page,
        )

    def _run_open_interest(self, symbol: str, max_pages: int, restart: bool, category: str,
                           oi_interval: str,
                           before_page: Callable[[], bool] | None = None,
                           on_page: Callable[[int, int], bool] | None = None) -> KindOutcome:
        if oi_interval not in OI_INTERVALS:
            oi_interval = DEFAULT_OI_INTERVAL

        def fetch_window(start_ms: int, end_ms: int) -> list[dict]:
            return self.client.open_interest_window(
                symbol, interval_time=oi_interval, start_ms=start_ms, end_ms=end_ms, limit=OI_PAGE,
            )

        def store(rows: list[dict]) -> int:
            return self._store_oi(symbol, rows, oi_interval)

        return self._walk(
            symbol=symbol, data_kind=OPEN_INTEREST, interval=oi_interval, page=OI_PAGE,
            max_pages=max_pages, restart=restart, fetch_window=fetch_window, store=store,
            count=lambda: self._count_series(OPEN_INTEREST, symbol, oi_interval),
            bounds=lambda: self._bounds(OPEN_INTEREST, symbol, oi_interval),
            row_spacing_ms=INTERVAL_MS.get(oi_interval, 3_600_000),
            before_page=before_page,
            on_page=on_page,
        )

    def _run_risk_limit(self, symbol: str, category: str) -> KindOutcome:
        """The ladder is a current snapshot, not a history: it is stored as one."""
        started = time.monotonic()
        outcome = KindOutcome(self.venue, symbol, RISK_LIMIT)
        attempts = self._attempts(symbol, RISK_LIMIT, "") + 1
        try:
            tiers = self.client.risk_limit(symbol, category=category) or []
        except Exception as exc:  # noqa: BLE001 - classified like any other walk
            outcome.failure_kind = classify_failure(exc)
            outcome.failure = f"{type(exc).__name__}: {exc}"
            outcome.duration_ms = int((time.monotonic() - started) * 1000)
            self._save(outcome, attempts=attempts, error=outcome.failure,
                       error_kind=outcome.failure_kind)
            return outcome
        if tiers:
            # The venue returns its own field names; the risk layer owns the
            # translation, and the ladder is stored as a snapshot with the moment
            # it was collected - it is a current state, not a history.
            from ..risk import RiskProfile, tier_rows_for_db

            profile = RiskProfile.from_rows(symbol, tiers, synced_at=self._now())
            if profile.tiers:
                self.db.upsert_risk_tiers(
                    self.venue, symbol, tier_rows_for_db(profile),
                    source=profile.source, synced_at=self._now(),
                )
                outcome.status = "ok"
                outcome.rows_available = len(profile.tiers)
            else:
                outcome.status = "unsupported"
                outcome.reason = "交易所返回了档位但无法解析，已保留本地缓存"
                outcome.failure_kind = NO_DATA
        else:
            outcome.status = "unsupported"
            outcome.reason = "交易所未返回该合约的风险档位"
            outcome.failure_kind = NO_DATA
        outcome.complete = True
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        self._save(outcome, attempts=attempts, error=None, error_kind=None)
        return outcome

    # -- storage helpers -------------------------------------------------
    def _store_marks(self, symbol: str, interval: str, rows: list[dict]):
        return self.db.upsert_mark_candles(self.venue, symbol, interval, rows, source="venue_rest")

    def _store_funding(self, symbol: str, rows: list[dict]) -> int:
        before = self._count_series(FUNDING, symbol, "")
        self.db.upsert_funding(self.venue, symbol, rows)
        return max(0, self._count_series(FUNDING, symbol, "") - before)

    def _store_oi(self, symbol: str, rows: list[dict], interval: str) -> int:
        before = self._count_series(OPEN_INTEREST, symbol, interval)
        self.db.upsert_oi(self.venue, symbol, rows, interval=interval)
        return max(0, self._count_series(OPEN_INTEREST, symbol, interval) - before)

    def _count_series(self, data_kind: str, symbol: str, interval: str) -> int:
        if data_kind == FUNDING:
            return self.db.count_funding(self.venue, symbol)
        if data_kind == OPEN_INTEREST:
            return self.db.count_oi(self.venue, symbol, interval or DEFAULT_OI_INTERVAL)
        if data_kind == MARK_CANDLE:
            return self.db.count_mark_candles(self.venue, symbol, interval)
        return 0

    def _bounds(self, data_kind: str, symbol: str, interval: str) -> tuple[int | None, int | None]:
        if data_kind == FUNDING:
            return self.db.funding_bounds(self.venue, symbol)
        if data_kind == OPEN_INTEREST:
            return self.db.oi_bounds(self.venue, symbol, interval or DEFAULT_OI_INTERVAL)
        if data_kind == MARK_CANDLE:
            return self.db.mark_bounds(self.venue, symbol, interval)
        return None, None

    def _attempts(self, symbol: str, data_kind: str, interval: str) -> int:
        state = self.db.load_backfill_state(self.venue, symbol, interval, data_kind)
        return int((state or {}).get("attempts") or 0)

    def _save(self, outcome: KindOutcome, *, attempts: int, error: str | None,
              error_kind: str | None) -> None:
        self.db.upsert_backfill_state(
            {
                "venue": self.venue,
                "symbol": outcome.symbol,
                "interval": outcome.interval,
                "data_kind": outcome.data_kind,
                "oldest_ts": outcome.oldest_ts,
                "newest_ts": outcome.newest_ts,
                "complete": outcome.complete,
                "pages": outcome.pages_total,
                "rows_available": outcome.rows_available,
                "attempts": attempts,
                "status": outcome.status,
                "reason": outcome.reason or None,
                "last_error": error,
                "last_error_kind": error_kind,
                "last_run_ts": self._now(),
            }
        )

    # -- snapshots -------------------------------------------------------
    def snapshot(self, symbol: str, data_kind: str, interval: str = "") -> dict:
        """Pin one series and record its version, independently of the others."""
        if data_kind == TRADE_CANDLE:
            walk = HistoryBackfill(self.db, lambda *a: [], venue=self.venue, now=self._now,
                                   data_kind=TRADE_CANDLE)
            return walk.snapshot(symbol, interval)
        if data_kind == MARK_CANDLE:
            rows = self.db.load_mark_candles(self.venue, symbol, interval)
            first, last = self.db.mark_bounds(self.venue, symbol, interval)
            record = self._snapshot_record(symbol, data_kind, interval, rows, first, last)
        elif data_kind == FUNDING:
            rows = self.db.load_funding(self.venue, symbol)
            first, last = self.db.funding_bounds(self.venue, symbol)
            record = self._snapshot_record(symbol, data_kind, "", rows, first, last)
        elif data_kind == OPEN_INTEREST:
            oi_interval = interval or DEFAULT_OI_INTERVAL
            rows = self.db.load_oi(self.venue, symbol, interval=oi_interval)
            first, last = self.db.oi_bounds(self.venue, symbol, oi_interval)
            record = self._snapshot_record(symbol, data_kind, oi_interval, rows, first, last)
        elif data_kind == RISK_LIMIT:
            rows = self.db.load_risk_tiers(self.venue, symbol) or []
            record = self._snapshot_record(symbol, data_kind, "", rows, None, None)
        else:
            return {"available": False, "reason": f"未知的数据类型：{data_kind}"}
        if not record.get("available"):
            # A series with nothing in it has no version to pin. Recording an empty
            # one would make "is this family citable?" answer yes with a version
            # that names no data, so the refusal is returned and not stored.
            return record
        self.db.record_history_snapshot(record)
        return record

    def _snapshot_record(self, symbol: str, data_kind: str, interval: str, rows: list[dict],
                         first: int | None, last: int | None) -> dict:
        if not rows:
            return {
                "venue": self.venue, "symbol": symbol, "interval": interval,
                "data_kind": data_kind, "version": "", "from_ts": 0, "to_ts": 0,
                "bars": 0, "barsAvailable": 0, "complete": False, "missing_in_session": 0,
                "sources": {}, "available": False,
                "reason": f"本地没有{KIND_LABELS.get(data_kind, data_kind)}数据",
            }
        version = series_version(data_kind, self.venue, symbol, rows, interval=interval)
        return {
            "venue": self.venue,
            "symbol": symbol,
            "interval": interval,
            "data_kind": data_kind,
            "version": version,
            "from_ts": int(first) if first else 0,
            "to_ts": int(last) if last else 0,
            "bars": len(rows),
            "barsAvailable": len(rows),
            # A series that exists is not "gapped" the way a bar grid is; coverage
            # of a settlement or publication schedule is the readiness gate's job.
            "complete": True,
            "missing_in_session": 0,
            "sources": {f"venue_rest:{data_kind}": len(rows)},
            "available": True,
            "created_ts": self._now(),
        }

    def snapshot_all(self, symbol: str, intervals: list[str] | None = None) -> dict:
        """Every series this symbol has, each with its own version."""
        versions: dict[str, Any] = {}
        for interval in intervals or []:
            versions[f"{TRADE_CANDLE}:{interval}"] = self.snapshot(symbol, TRADE_CANDLE, interval)
            versions[f"{MARK_CANDLE}:{interval}"] = self.snapshot(symbol, MARK_CANDLE, interval)
        versions[FUNDING] = self.snapshot(symbol, FUNDING)
        versions[OPEN_INTEREST] = self.snapshot(symbol, OPEN_INTEREST, DEFAULT_OI_INTERVAL)
        versions[RISK_LIMIT] = self.snapshot(symbol, RISK_LIMIT)
        return versions


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
