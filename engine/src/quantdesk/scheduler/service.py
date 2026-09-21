"""Persistent background collection and timed job orchestration."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..alerts import AlertEngine
from ..config.instruments import TIMEFRAMES, VENUE_SYMBOLS, require_instrument
from ..config.settings import configured_proxy, load_app_config
from ..datahub.bybit import BybitClient
from ..datahub.db import Database
from ..plugins import NotificationEvent, PluginManager, PluginRegistry

EnqueueTradingAgents = Callable[[str, str], Awaitable[str]]

# Run history is kept two years; the rotation writes one row per tick, so without
# a retention window this table is the one that grows without bound.
RUN_RETENTION_MS = 2 * 365 * 86_400_000


class MarketCollector:
    """Collect one fixed-universe symbol per scheduler tick to avoid API bursts."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self.db = Database(self.home / "quantdesk.db")
        self._last_telemetry_prune = 0
        self._last_run_prune = 0

    def _record_request(self, event: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO market_requests "
            "(provider,operation,symbol,status,http_status,attempt,duration_ms,error,created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event["provider"], event["operation"], event.get("symbol"), event["status"],
                event.get("http_status"), event["attempt"], event["duration_ms"],
                (event.get("error") or "")[:1000] or None, event["created_ts"],
            ),
        )
        if event["created_ts"] - self._last_telemetry_prune >= 3_600_000:
            self.db.execute("DELETE FROM market_requests WHERE created_ts<?", (event["created_ts"] - 7 * 86_400_000,))
            self._last_telemetry_prune = event["created_ts"]

    def prune_runs(self, now: int | None = None) -> int:
        """Keep two years of run history; the rotation writes a row every tick.

        The same daily pass applies the external-evidence retention window, so the
        cache cannot grow without bound while nobody is looking at it.
        """
        stamp = int(now if now is not None else time.time() * 1000)
        if self._last_run_prune and stamp - self._last_run_prune < 86_400_000:
            return 0
        self._last_run_prune = stamp
        try:
            from ..config.settings import load_app_config

            retention = (load_app_config(self.home).external.get("retention_days") or {})
            self.db.prune_external_evidence(
                max_age_days=int(retention.get("evidence") or 400), now_ms=stamp
            )
            self.db.prune_external_analytics(
                max_age_days=int(retention.get("analytics") or 400), now_ms=stamp
            )
        except Exception:  # noqa: BLE001 - retention must never stop the rotation
            pass
        return self.db.prune_scheduler_runs(stamp - RUN_RETENTION_MS)

    def collect_symbol(self, symbol: str, bars: int = 400, include_candles: bool = True) -> dict[str, Any]:
        """Refresh one symbol's venue history.

        With the WebSocket service running, candles arrive by themselves, so the
        rotation only has to keep the funding and open-interest history fresh:
        those have no realtime channel and still feed alerts and quality checks.
        """
        spec = require_instrument(symbol)
        client = BybitClient(proxy=configured_proxy(self.home), timeout=25.0, telemetry=self._record_request)
        candle_counts: dict[str, int] = {}
        try:
            if include_candles:
                for timeframe in TIMEFRAMES:
                    rows = client.kline_snapshot(
                        spec.venue_symbol, timeframe, limit=max(30, min(int(bars), 1000)), completed_only=True
                    )
                    candle_counts[timeframe] = self.db.upsert_candles(
                        "bybit", spec.venue_symbol, timeframe, rows, source="venue_rest"
                    ).written
            funding = client.funding_history(spec.venue_symbol, limit=200)
            interest = client.open_interest(spec.venue_symbol, interval_time="1h", limit=200)
            funding_count = self.db.upsert_funding("bybit", spec.venue_symbol, funding)
            oi_count = self.db.upsert_oi("bybit", spec.venue_symbol, interest)
        finally:
            client.close()
        return {
            "symbol": spec.venue_symbol,
            "mode": "full" if include_candles else "derivatives",
            "candles": candle_counts,
            "funding": funding_count,
            "openInterest": oi_count,
            "collectedAt": int(time.time() * 1000),
        }


class BackgroundTaskScheduler:
    def __init__(self, home: Path, enqueue_tradingagents: EnqueueTradingAgents | None = None):
        self.home = Path(home)
        self.collector = MarketCollector(self.home)
        self.alerts = AlertEngine(self.home)
        self.enqueue_tradingagents = enqueue_tradingagents
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._collection_lock = asyncio.Lock()
        self._next_symbol = 0
        self._last_daily_key: str | None = None
        self.state: dict[str, Any] = {
            "running": False,
            "market": {"status": "idle", "lastRunAt": None, "lastSuccessAt": None, "lastError": None, "lastResult": None},
            "dailyTradingAgents": {"status": "idle", "lastRunAt": None, "lastError": None, "queued": []},
            "alerts": {"status": "idle", "lastRunAt": None, "lastError": None, "lastResult": None},
        }

    async def start(self) -> None:
        if self.state["running"]:
            return
        self._stop.clear()
        self.state["running"] = True
        self._tasks = [
            asyncio.create_task(self._market_loop(), name="quantdesk-market-collector"),
            asyncio.create_task(self._daily_tradingagents_loop(), name="quantdesk-daily-tradingagents"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self.state["running"] = False

    def status(self) -> dict[str, Any]:
        config = load_app_config(self.home).scheduler
        return {
            **self.state,
            "config": {
                "marketCollectionEnabled": bool(config.get("market_collection_enabled", True)),
                "marketSymbolIntervalSec": float(config.get("market_symbol_interval_sec", 20)),
                "marketBackfillBars": int(config.get("market_backfill_bars", 400)),
                "dailyTradingAgentsEnabled": bool(config.get("daily_ta_enabled", False)),
                "dailyTradingAgentsTime": str(config.get("daily_ta_time", "16:05")),
                "dailyTradingAgentsSymbols": list(config.get("daily_ta_symbols", ["BTCUSDT", "ETHUSDT"])),
            },
            "nextMarketSymbol": VENUE_SYMBOLS[self._next_symbol % len(VENUE_SYMBOLS)],
        }

    def _record(self, job: str, subject: str, status: str, started: int, summary=None, error=None) -> None:
        try:
            self.collector.db.execute(
                "INSERT INTO scheduler_runs (job, subject, status, summary, error, started_ts, finished_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    job, subject, status,
                    json.dumps(summary, ensure_ascii=False) if summary is not None else None,
                    error, started, int(time.time() * 1000),
                ),
            )
        except Exception:
            pass

    async def run_market_once(self, symbol: str | None = None, rotate: bool = True) -> dict[str, Any]:
        """One full collection pass for a symbol.

        `rotate` advances the pool pointer, which is what the background loop
        wants and what a manual trigger does not: clicking "run now" must not
        skip a symbol in the rotation.
        """
        async with self._collection_lock:
            target = require_instrument(symbol).venue_symbol if symbol else VENUE_SYMBOLS[self._next_symbol % len(VENUE_SYMBOLS)]
            if symbol is None and rotate:
                self._next_symbol = (self._next_symbol + 1) % len(VENUE_SYMBOLS)
            started = int(time.time() * 1000)
            self.state["market"].update(status="running", lastRunAt=started, lastError=None)
            bars = int(load_app_config(self.home).scheduler.get("market_backfill_bars", 400))
            try:
                result = await asyncio.to_thread(self.collector.collect_symbol, target, bars)
                try:
                    alert_result = await asyncio.to_thread(self.alerts.evaluate_symbol, target)
                    result["alerts"] = {
                        "evaluated": alert_result["evaluated"],
                        "triggered": len(alert_result["triggered"]),
                        "unavailable": len(alert_result["unavailable"]),
                        "blocked": len(alert_result.get("blocked", [])),
                    }
                    self.state["alerts"].update(
                        status="idle",
                        lastRunAt=alert_result.get("evaluatedAt"),
                        lastError=None,
                        lastResult={"symbol": target, **result["alerts"]},
                    )
                except Exception as alert_exc:  # market data remains a successful collection
                    self.state["alerts"].update(
                        status="error", lastRunAt=int(time.time() * 1000), lastError=str(alert_exc)
                    )
                self.state["market"].update(status="idle", lastSuccessAt=int(time.time() * 1000), lastResult=result)
                self._record("market_collection", target, "succeeded", started, summary=result)
                return result
            except Exception as exc:
                message = str(exc)
                self.state["market"].update(status="error", lastError=message)
                self._record("market_collection", target, "failed", started, error=message)
                await asyncio.to_thread(self._notify_failure, target, message)
                raise

    def _notify_failure(self, symbol: str, message: str) -> None:
        event = NotificationEvent(
            id=f"market-{symbol}-{uuid.uuid4().hex[:10]}",
            type="scheduler.market_failed",
            severity="warning",
            title=f"{symbol} 行情采集失败",
            message=message[:4000],
            occurredAt=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
        )
        PluginRegistry(PluginManager(self.home)).notify_all(event)

    async def _market_loop(self) -> None:
        await asyncio.sleep(1)
        while not self._stop.is_set():
            config = load_app_config(self.home).scheduler
            # The interval paces the rotation over the pool, not the work per
            # symbol: one symbol is refreshed per tick either way.
            interval = max(5.0, float(config.get("market_symbol_interval_sec", 20)))
            try:
                if bool(config.get("market_collection_enabled", True)):
                    await self.run_market_once()
                else:
                    await self._collect_derivatives_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.to_thread(self.collector.prune_runs)
            except Exception:  # noqa: BLE001 - housekeeping must never stop the rotation
                pass
            await asyncio.sleep(interval)

    async def _collect_derivatives_once(self) -> None:
        """Keep funding and open-interest history moving while candles come from WS.

        Those two series have no realtime channel, and the fixed rotation means a
        single symbol is refreshed every ``len(universe) * interval`` seconds, so
        the venue sees two requests per tick at most.
        """
        async with self._collection_lock:
            target = VENUE_SYMBOLS[self._next_symbol % len(VENUE_SYMBOLS)]
            self._next_symbol = (self._next_symbol + 1) % len(VENUE_SYMBOLS)
            # `bars` is irrelevant without candles; the history depth is fixed by
            # the collector's own funding/open-interest limits.
            result = await asyncio.to_thread(self.collector.collect_symbol, target, 1, False)
            finished = int(time.time() * 1000)
            self._record("market_derivatives", target, "succeeded", finished, summary=result)
            # The heartbeat of the rotation is separate from the last full
            # collection, so the status page can show both without conflating them.
            self.state["market"].update(status="idle", lastError=None, lastDerivativesAt=finished, lastDerivativesSymbol=target)

    async def _daily_tradingagents_loop(self) -> None:
        while not self._stop.is_set():
            config = load_app_config(self.home).scheduler
            if bool(config.get("daily_ta_enabled", False)) and self.enqueue_tradingagents:
                now = datetime.now(timezone.utc)
                target_time = str(config.get("daily_ta_time", "16:05"))
                key = f"{now.date().isoformat()}@{target_time}"
                if now.strftime("%H:%M") == target_time and self._last_daily_key != key:
                    self._last_daily_key = key
                    queued: list[str] = []
                    try:
                        for symbol in config.get("daily_ta_symbols", ["BTCUSDT", "ETHUSDT"]):
                            require_instrument(str(symbol))
                            queued.append(await self.enqueue_tradingagents(str(symbol), now.date().isoformat()))
                        self.state["dailyTradingAgents"].update(
                            status="idle", lastRunAt=int(time.time() * 1000), lastError=None, queued=queued
                        )
                    except Exception as exc:
                        self.state["dailyTradingAgents"].update(status="error", lastError=str(exc))
            await asyncio.sleep(20)


_SCHEDULER: BackgroundTaskScheduler | None = None


def get_scheduler(home: Path, enqueue_tradingagents: EnqueueTradingAgents | None = None) -> BackgroundTaskScheduler:
    global _SCHEDULER
    if _SCHEDULER is None or _SCHEDULER.home != Path(home):
        _SCHEDULER = BackgroundTaskScheduler(home, enqueue_tradingagents)
    elif enqueue_tradingagents is not None:
        _SCHEDULER.enqueue_tradingagents = enqueue_tradingagents
    return _SCHEDULER
