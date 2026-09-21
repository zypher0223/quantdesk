"""The batch backfill: one task per series, run under a rate limit and a queue.

Seventeen contracts across four timeframes is sixty-eight candle walks before the
mark, funding and open-interest series are counted, so the work has to be a queue
rather than a loop:

* **Bounded concurrency and rate.** A semaphore caps how many walks run at once,
  and a token bucket caps how many venue pages per minute leave the process.
* **Pausable, cancellable, retryable.** The state lives in the database, so a
  browser refresh shows the same picture, and a decision made on one page holds on
  the next.
* **Progress that means something.** Pages done against an estimated total, the
  rows actually available, and an estimated remainder - all read from the store.
* **Failure is per task.** One contract's funding history failing does not stop
  the other sixty-seven, and it is classified rather than merely logged.
"""

from __future__ import annotations

import asyncio
import math
import time
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..config.instruments import VENUE_SYMBOLS
from .backfill import FAILURE_LABELS, HistoryBackfill
from .history import (
    DATA_KINDS,
    DEFAULT_OI_INTERVAL,
    FUNDING,
    KIND_LABELS,
    MARK_CANDLE,
    OPEN_INTEREST,
    RISK_LIMIT,
    TIMEFRAMED_KINDS,
    TRADE_CANDLE,
    HistoryCollector,
)
from .venue import INTERVAL_MS

# Timeframes the report's matrix asks for.
# The default matrix stays at four timeframes; `1w` is available on request (see
# `config.settings.DEFAULT_INTERVALS` for why the default excludes it).
DEFAULT_TIMEFRAMES = ("15m", "1h", "4h", "1d")

# Retryable failures are the ones worth trying again; a refused symbol is not.
RETRYABLE_KINDS = ("rate_limited", "timeout", "network", "upstream_error", "budget_exhausted", "unknown")

# A retryable failure waits before the next attempt: a flapping link must not
# burn the whole attempt budget inside a second. Indexed by attempts already made.
RETRY_BACKOFF_SECONDS = (5.0, 20.0)

# How many pages one task may run before it yields and is resumed later.
DEFAULT_PAGES_PER_RUN = 20


class TokenBucket:
    """A per-minute page allowance, shared by every walk in the queue."""

    def __init__(self, per_minute: int, *, now: Callable[[], float] | None = None):
        self.capacity = max(1, int(per_minute))
        self.tokens = float(self.capacity)
        self._now = now or time.monotonic
        self._updated = self._now()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        moment = self._now()
        elapsed = max(0.0, moment - self._updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * (self.capacity / 60.0))
        self._updated = moment

    @property
    def tokens_available(self) -> float:
        """Tokens left right now, refilled to this instant.

        Reading the raw counter would report a stale allowance: the bucket only
        fills when it is touched, so a status view has to ask for a refill.
        """
        with self._lock:
            self._refill()
            return self.tokens

    def take(self, count: int = 1) -> float:
        """Spend tokens for `count` pages; returns how long to wait first."""
        with self._lock:
            self._refill()
            self.tokens -= count
            if self.tokens >= 0:
                return 0.0
            deficit = -self.tokens
            # Keep the debt. Each concurrent caller reserves a different future
            # token instead of all waking at the same instant after one wait.
            return deficit * 60.0 / self.capacity


def pages_estimate(interval: str, *, launch_ts: int | None, now_ms: int, page: int = 1000,
                   pages_done: int = 0) -> int:
    """How many pages this series should take, from its listing date.

    The launch time is the honest denominator: a contract listed three months ago
    has three months of history, and reporting "1000 pages remaining" for it would
    be a number nobody could act on.
    """
    step = INTERVAL_MS.get(interval)
    if not step or not launch_ts or now_ms <= launch_ts:
        return 0
    bars = max(0, (now_ms - launch_ts) // step)
    total = int(math.ceil(bars / page)) if bars else 0
    return max(0, total - pages_done)


@dataclass
class TaskResult:
    task_id: int
    status: str
    pages: int
    rows_available: int
    failure_kind: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "taskId": self.task_id,
            "status": self.status,
            "pages": self.pages,
            "rowsAvailable": self.rows_available,
            "failureKind": self.failure_kind,
            "failureLabel": FAILURE_LABELS.get(self.failure_kind, ""),
            "reason": self.reason,
        }


class BackfillQueue:
    """A persistent queue of backfill tasks, run with limits and controls."""

    def __init__(
        self,
        db,
        collector_factory: Callable[[], Any],
        *,
        venue: str = "bybit",
        concurrency: int = 2,
        pages_per_minute: int = 120,
        pages_per_run: int = DEFAULT_PAGES_PER_RUN,
        sleep: Callable[[float], Any] | None = None,
        now: Callable[[], int] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.db = db
        self.venue = venue
        self._collector_factory = collector_factory
        self.concurrency = max(1, int(concurrency))
        self.bucket = TokenBucket(pages_per_minute)
        self.pages_per_run = max(1, int(pages_per_run))
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._now = now or (lambda: int(time.time() * 1000))
        self._paused = self.db.kv_get("backfill.queue.paused") == "1"
        self._retry_after: dict[int, float] = {}

    # -- retry policy ----------------------------------------------------
    def retry_delay(self, failures: int) -> float:
        """How long a retryable failure waits before its next attempt."""
        index = max(0, min(int(failures) - 1, len(RETRY_BACKOFF_SECONDS) - 1))
        return RETRY_BACKOFF_SECONDS[index]

    def _schedule_retry(self, task_id: int, failures: int) -> float:
        deadline = self._clock() + self.retry_delay(failures)
        self._retry_after[task_id] = deadline
        return deadline

    def cooling_down(self, task_id: int) -> bool:
        deadline = self._retry_after.get(int(task_id))
        if deadline is None:
            return False
        if self._clock() >= deadline:
            self._retry_after.pop(int(task_id), None)
            return False
        return True

    def _requeue_or_fail(self, task: dict, kind: str,
                         *, message: str | None = None) -> tuple[str, int, str | None]:
        """Decide between another attempt and a recorded failure, and say why."""
        task_id = int(task["id"])
        if kind == "budget_exhausted":
            # Running out of the per-run page budget is not a failure, so it must
            # not consume the attempt budget either.
            self._retry_after.pop(task_id, None)
            return "pending", 0, message
        failures = int(task.get("failure_attempts") or 0) + 1
        retryable = kind in RETRYABLE_KINDS
        if retryable and failures < int(task.get("max_attempts") or 3):
            deadline = self._schedule_retry(task_id, failures)
            if message:
                wait = max(0.0, deadline - self._clock())
                message = f"{message}（{wait:.0f}s 后重试，第 {failures + 1} 次尝试）"
            return "pending", failures, message
        self._retry_after.pop(task_id, None)
        return "failed", failures, message

    # -- matrix ----------------------------------------------------------
    def build_matrix(
        self,
        *,
        symbols: Iterable[str] | None = None,
        timeframes: Iterable[str] = DEFAULT_TIMEFRAMES,
        data_kinds: Iterable[str] = (TRADE_CANDLE, MARK_CANDLE, FUNDING, OPEN_INTEREST, RISK_LIMIT),
        reset: bool = False,
    ) -> dict:
        """Create one task per (contract, series). Re-running it adds nothing."""
        selected = list(symbols or VENUE_SYMBOLS)
        intervals = list(timeframes)
        created = 0
        for symbol in selected:
            meta = self.db.load_instrument_meta(self.venue, symbol)
            launch_ts = int(meta["launch_ts"]) if meta and meta.get("launch_ts") else None
            for kind in data_kinds:
                if kind in TIMEFRAMED_KINDS:
                    for interval in intervals:
                        created += self._create(symbol, kind, interval, launch_ts, reset)
                elif kind == OPEN_INTEREST:
                    created += self._create(symbol, kind, DEFAULT_OI_INTERVAL, launch_ts, reset)
                else:
                    created += self._create(symbol, kind, "", launch_ts, reset)
        return {"tasks": created, "summary": self.db.task_summary()}

    def _create(self, symbol: str, data_kind: str, interval: str, launch_ts: int | None,
                reset: bool) -> int:
        estimate = 0
        if data_kind in TIMEFRAMED_KINDS:
            estimate = pages_estimate(interval, launch_ts=launch_ts, now_ms=self._now())
        self.db.upsert_backfill_task(
            {
                "venue": self.venue, "symbol": symbol, "interval": interval,
                "data_kind": data_kind, "status": "pending",
                "pages_estimate": estimate, "max_attempts": 3,
                "created_ts": self._now(), "updated_ts": self._now(),
            },
            reset=reset,
        )
        return 1

    # -- controls --------------------------------------------------------
    def pause_all(self) -> dict:
        self._paused = True
        self.db.kv_set("backfill.queue.paused", "1")
        for task in self.db.list_backfill_tasks():
            if task["status"] in ("pending", "running"):
                self.db.update_backfill_task(task["id"], status="paused")
        return self.status()

    def resume_all(self) -> dict:
        self._paused = False
        self.db.kv_set("backfill.queue.paused", "0")
        for task in self.db.list_backfill_tasks():
            if task["status"] == "paused":
                self.db.update_backfill_task(task["id"], status="pending")
        return self.status()

    def pause(self, task_id: int) -> dict:
        task = self._require(task_id)
        if task["status"] in ("pending", "running"):
            self.db.update_backfill_task(task_id, status="paused")
        return self._task_payload(self._require(task_id))

    def resume(self, task_id: int) -> dict:
        task = self._require(task_id)
        if task["status"] == "paused":
            self.db.update_backfill_task(task_id, status="pending")
        return self._task_payload(self._require(task_id))

    def retry(self, task_id: int) -> dict:
        """Requeue one task, clearing the reason it stopped."""
        task = self._require(task_id)
        if task["status"] in ("done", "unsupported") and not task.get("last_error"):
            return self._task_payload(task)
        self.db.update_backfill_task(
            task_id, status="pending", cancel_requested=0, last_error=None,
            last_error_kind=None, reason=None, finished_ts=None, failure_attempts=0,
        )
        return self._task_payload(self._require(task_id))

    def cancel(self, task_id: int) -> dict:
        task = self._require(task_id)
        if task["status"] in ("done", "unsupported"):
            return self._task_payload(task)
        self.db.update_backfill_task(task_id, status="cancelled", cancel_requested=1,
                                     finished_ts=self._now())
        return self._task_payload(self._require(task_id))

    def _require(self, task_id: int) -> dict:
        task = self.db.get_backfill_task(task_id)
        if task is None:
            raise KeyError(f"没有找到回填任务 {task_id}")
        return task

    def is_cancelled(self, task_id: int) -> bool:
        task = self.db.get_backfill_task(task_id)
        return bool(task and (task["cancel_requested"] or task["status"] == "cancelled"))

    def is_paused(self, task_id: int) -> bool:
        task = self.db.get_backfill_task(task_id)
        return bool(task and task["status"] == "paused")

    # -- execution -------------------------------------------------------
    def run_task(self, task_id: int) -> TaskResult:
        """Run one task to completion, or to its per-run page budget."""
        task = self._require(task_id)
        if task["status"] in ("done", "unsupported", "cancelled"):
            return TaskResult(task_id, task["status"], task["pages"], task["rows_available"])
        if task["status"] == "paused":
            return TaskResult(task_id, "paused", task["pages"], task["rows_available"])

        collector = self._collector_factory()
        pages_before = int(task["pages"] or 0)

        def before_page() -> bool:
            if self.is_cancelled(task_id) or self.is_paused(task_id):
                return False
            wait = self.bucket.take(1)
            if wait > 0:
                result = self._sleep(wait)
                if asyncio.iscoroutine(result):  # pragma: no cover - async caller
                    asyncio.get_event_loop().run_until_complete(result)
            return not self.is_cancelled(task_id) and not self.is_paused(task_id)

        def on_page(pages: int, rows_available: int) -> bool:
            """Keep the task row honest after every page; return False to stop."""
            self.db.update_backfill_task(
                task_id, pages=pages_before + pages, rows_available=rows_available,
                pages_estimate=max(0, int(task["pages_estimate"] or 0) - pages),
            )
            if self.is_cancelled(task_id) or self.is_paused(task_id):
                return False
            return True

        try:
            outcome = collector.run(
                task["symbol"], task["data_kind"], task["interval"],
                max_pages=self.pages_per_run,
                oi_interval=task["interval"] or DEFAULT_OI_INTERVAL,
                before_page=before_page, on_page=on_page,
            )
        except Exception as exc:  # noqa: BLE001 - a task failure stays a task failure
            from .backfill import classify_failure

            kind = classify_failure(exc)
            # An exception escaping the walk is classified like any other failure:
            # a transient network error waits and retries instead of parking the
            # series as failed on its first blip.
            message = f"{type(exc).__name__}: {exc}"
            status, failures, message = self._requeue_or_fail(task, kind, message=message)
            self.db.update_backfill_task(
                task_id, status=status, last_error=message,
                last_error_kind=kind, failure_attempts=failures,
                finished_ts=None if status == "pending" else self._now(),
            )
            return TaskResult(task_id, status, pages_before, int(task["rows_available"] or 0), kind)
        finally:
            client = getattr(collector, "client", None)
            close = getattr(client, "close", None)
            if callable(close):
                close()

        payload = outcome.as_dict() if hasattr(outcome, "as_dict") else dict(outcome)
        pages_now = int(task["pages"] or 0) + int(payload.get("pages") or 0)
        rows_now = int(payload.get("rowsAvailable") or 0)
        # The vocabulary is the one the acceptance criteria name: done, in
        # progress, failed, unsupported. A walk that stopped at its page budget
        # with history still to fetch is *in progress*, and it goes back on the
        # queue rather than being reported as a finished series.
        status = "done"
        failures = 0
        failure_text = payload.get("failure")
        if payload.get("status") == "unsupported":
            status = "unsupported"
        elif payload.get("failureKind"):
            status, failures, failure_text = self._requeue_or_fail(
                task, payload["failureKind"], message=failure_text)
        if payload.get("failureKind") == "budget_exhausted" \
                and pages_now == int(task["pages"] or 0) and rows_now == int(task["rows_available"] or 0):
            # Running out of budget is not a failure, so it is normally requeued
            # without consuming an attempt; a pass that stored nothing at all
            # would spin forever, and that alone is worth reporting.
            status = "failed"
            self._retry_after.pop(task_id, None)
            failure_text = failure_text or "本次运行没有取得任何进展"
            payload = {**payload, "failureKind": "no_progress"}
        if self.is_cancelled(task_id):
            status = "cancelled"
        elif self.is_paused(task_id):
            status = "paused"

        self.db.update_backfill_task(
            task_id,
            status=status,
            pages=pages_now,
            # Once the venue says the walk reached listing/no-data, the estimate
            # is no longer work remaining. Leaving an old estimate here produced
            # contradictory rows such as "complete, 100%, 3 pages remaining".
            **({"pages_estimate": 0} if status in ("done", "unsupported") else {}),
            rows_available=rows_now,
            complete=1 if status in ("done", "unsupported") else 0,
            failure_attempts=failures,
            last_error=failure_text or None,
            last_error_kind=payload.get("failureKind") or None,
            reason=payload.get("reason") or None,
            finished_ts=None if status in ("pending", "paused") else self._now(),
        )
        if status == "done":
            # A finished series gets its version pinned, so a study that reads it
            # can cite exactly what it read. A failure here only leaves the series
            # uncitable - which the readiness gate reports - so it must not fail
            # the task that just succeeded.
            try:
                collector.snapshot(task["symbol"], task["data_kind"], task["interval"])
            except Exception:  # noqa: BLE001 - the data is stored either way
                pass
        return TaskResult(task_id, status, pages_now, rows_now,
                          payload.get("failureKind") or "", payload.get("reason") or "")

    async def run_pending(self, *, limit: int | None = None) -> dict:
        """Drain the queue with bounded concurrency, respecting pause and cancel."""
        if self._paused:
            return {"ran": 0, "summary": self.db.task_summary(), "paused": True}
        semaphore = asyncio.Semaphore(self.concurrency)
        ran = 0

        # A deep series needs several passes, so the pass budget is derived from
        # the work left rather than guessed: every task's remaining pages, divided
        # by the per-run page budget, plus a pass for the ones that finish early.
        # The factor of two leaves room for the resumed frontends that come back
        # with a little more history than the estimate assumed.
        if limit is not None:
            budget = limit
        else:
            tasks = self.db.list_backfill_tasks()
            passes = sum(
                max(1, math.ceil(int(task["pages_estimate"] or 0) / self.pages_per_run)) + 1
                for task in tasks
            )
            budget = max(20, passes * 2)

        async def worker() -> None:
            nonlocal ran
            cooling = 0
            while (limit is None or ran < limit) and ran < budget:
                if self._paused:
                    return
                task = self.db.claim_next_backfill_task()
                if task is None:
                    return
                if self.cooling_down(int(task["id"])):
                    # A task waiting out its retry delay goes back on the queue;
                    # the loop re-enters after its idle wait, so waiting here
                    # would only occupy a slot that another series could use.
                    self.db.update_backfill_task(int(task["id"]), status="pending")
                    cooling += 1
                    if cooling > self.concurrency:
                        return
                    continue
                cooling = 0
                async with semaphore:
                    await asyncio.to_thread(self.run_task, int(task["id"]))
                ran += 1

        await asyncio.gather(*[worker() for _ in range(self.concurrency)])
        return {"ran": ran, "summary": self.db.task_summary(), "paused": self._paused}

    # -- reporting -------------------------------------------------------
    def _rows_available(self, task: dict) -> int:
        """Records this series has in the store - counted, not remembered.

        The live stream keeps appending bars after a walk is done, and the risk
        book can sync a ladder with no backfill task behind it, so the counter a
        walk stopped at is not the answer to "how many records are there". The
        panel and the board must not disagree about one series.
        """
        from .readiness import stored_count

        counted = stored_count(self.db, self.venue, task["symbol"], task["data_kind"],
                               task["interval"] or "")
        if counted is None:
            return int(task["rows_available"] or 0)
        return int(counted)

    def _task_payload(self, task: dict) -> dict:
        pages = int(task["pages"] or 0)
        estimate = int(task["pages_estimate"] or 0)
        complete = bool(task.get("complete"))
        done_estimate = pages + estimate
        if complete or task["status"] in ("done", "unsupported"):
            progress = 100.0
        elif done_estimate:
            # Never 100% while there is still history to fetch: a countdown that
            # runs out early is not a finished walk.
            progress = min(99.0, round(pages / done_estimate * 100, 2))
        else:
            progress = 0.0
        return {
            "id": int(task["id"]),
            "symbol": task["symbol"],
            "interval": task["interval"],
            "dataKind": task["data_kind"],
            "kindLabel": KIND_LABELS.get(task["data_kind"], task["data_kind"]),
            "status": task["status"],
            "pages": pages,
            "pagesRemaining": estimate,
            "pagesEstimateTotal": done_estimate,
            "progressPct": progress,
            "complete": complete,
            "estimateExhausted": bool(estimate == 0 and not complete
                                      and task["status"] not in ("done", "unsupported")),
            "rowsAvailable": self._rows_available(task),
            "attempts": int(task["attempts"] or 0),
            "failureAttempts": int(task.get("failure_attempts") or 0),
            "maxAttempts": int(task["max_attempts"] or 3),
            "failureKind": task["last_error_kind"] or "",
            "failureLabel": FAILURE_LABELS.get(task["last_error_kind"] or "", ""),
            "failure": task["last_error"],
            "reason": task["reason"],
            "createdTs": task["created_ts"],
            "startedTs": task["started_ts"],
            "finishedTs": task["finished_ts"],
            "updatedTs": task["updated_ts"],
        }

    def status(self, *, symbol: str | None = None, include_ranges: bool = False,
               scan_limit: int | None = None) -> dict:
        """The whole board, as a page refresh needs to see it.

        `include_ranges` adds what each series can actually be backtested over.
        It reads the store rather than the task counters, so it is asked for on a
        slower cadence than the board itself: a five-second poll should not scan
        every series' bar history on each tick.
        """
        tasks = [self._task_payload(task) for task in self.db.list_backfill_tasks(symbol=symbol)]
        summary = self.db.task_summary()
        by_kind: dict[str, dict[str, int]] = {}
        for task in tasks:
            bucket = by_kind.setdefault(task["dataKind"], {})
            bucket[task["status"]] = bucket.get(task["status"], 0) + 1
        payload = {
            "paused": self._paused,
            "concurrency": self.concurrency,
            "pagesPerMinute": self.bucket.capacity,
            "summary": summary,
            "byKind": by_kind,
            "tasks": tasks,
        }
        if include_ranges:
            payload["ranges"] = self.ranges(symbol=symbol, scan_limit=scan_limit)
        return payload

    def ranges(self, *, symbol: str | None = None, scan_limit: int | None = None) -> dict:
        """Per contract and family: the window a formal study may use, and the gaps."""
        from .readiness import CONTIGUITY_SCAN_LIMIT, backtestable_ranges

        intervals = tuple(dict.fromkeys(
            task["interval"] for task in self.db.list_backfill_tasks(symbol=symbol)
            if task["interval"]
        )) or ("15m", "1h", "4h", "1d")
        kinds = tuple(dict.fromkeys(
            task["data_kind"] for task in self.db.list_backfill_tasks(symbol=symbol)
        )) or ("trade_candle", "mark_candle", "funding", "open_interest", "risk_limit")
        symbols = [symbol] if symbol else None
        return backtestable_ranges(
            self.db, venue=self.venue, symbols=symbols, intervals=intervals, kinds=kinds,
            scan_limit=scan_limit or CONTIGUITY_SCAN_LIMIT,
        )


class BackfillWorker:
    """One process-wide consumer for the persistent backfill queue."""

    def __init__(self, queue: BackfillQueue, *, idle_seconds: float = 5.0):
        self.queue = queue
        self.idle_seconds = max(0.25, float(idle_seconds))
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def start(self) -> None:
        if self.running:
            return
        # A process can die between claim and completion. SQLite is the lease:
        # any running row left by the old process becomes pending on startup.
        self.queue.db.execute(
            "UPDATE backfill_tasks SET status='pending', updated_ts=? WHERE status='running'",
            (int(time.time() * 1000),),
        )
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="quantdesk-backfill-worker")
        self._wake.set()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.queue.status()["paused"]:
                    await self.queue.run_pending()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one failed drain cannot kill the service
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.idle_seconds)
            except asyncio.TimeoutError:
                pass

    def status(self, *, symbol: str | None = None, include_ranges: bool = False) -> dict:
        return {
            **self.queue.status(symbol=symbol, include_ranges=include_ranges),
            "workerRunning": self.running,
            "workerError": self.last_error,
        }


def collector_factory(db, client_factory, *, venue: str = "bybit", now=None) -> Callable[[], HistoryCollector]:
    """A fresh collector per task: one client per walk, closed by its owner."""

    def build() -> HistoryCollector:
        return HistoryCollector(db, client_factory(), venue=venue, now=now)

    return build
