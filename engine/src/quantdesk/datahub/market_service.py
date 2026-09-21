"""The single in-process market state.

Every consumer — the browser, resonance, alerts, paper trading and research —
reads market data through this service instead of opening its own venue
connection. That is what stops the request bursts, duplicate connections and
disagreeing numbers the previous architecture accumulated.

Responsibilities:

* restore the last real snapshot from SQLite at startup, so the page paints
  before any public round trip;
* hold one WebSocket per shard and fold its events into memory + SQLite;
* backfill gaps over REST when a connection is (re)established;
* emit ``CandleClosed`` exactly once per (symbol, interval, open_ts);
* never substitute generated data for a real quote — a disconnected feed keeps
  serving the last real snapshot, flagged stale, and `/health` still answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..config.instruments import (
    TIMEFRAMES,
    VENUE_SYMBOLS,
    require_instrument,
)
from ..config.settings import configured_proxy, quantdesk_home
from . import realtime as rt
from .bybit import BybitClient
from .db import Database

logger = logging.getLogger(__name__)

VENUE = "bybit"
# A quote older than this is reported as stale rather than presented as live.
STALE_AFTER_MS = 120_000
# Closed candles kept in memory per (symbol, interval) for instant snapshot reads.
MEMORY_CANDLES = 400

ClosedCandleHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _finite(value: Any) -> float | None:
    """Coerce to float, rejecting NaN/Infinity which JSON cannot carry."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _stale_window(default: int) -> int:
    """Freshness window, overridable so a drill can watch it elapse."""
    raw = os.environ.get("QUANTDESK_STALE_AFTER_MS", "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _snapshot_fields(event: rt.TickerEvent) -> dict[str, Any]:
    """The stored quote columns carried by one ticker frame."""
    return {
        "last_price": event.last_price,
        "mark_price": event.mark_price,
        "index_price": event.index_price,
        "funding_rate": event.funding_rate,
        "funding_interval_hour": event.funding_interval_hour,
        "next_funding_time": event.next_funding_time,
        "open_interest": event.open_interest,
        "open_interest_value": event.open_interest_value,
        "turnover_24h": event.turnover_24h,
        "volume_24h": event.volume_24h,
        "price_24h_pct": event.price_24h_pct,
        "high_24h": event.high_24h,
        "low_24h": event.low_24h,
    }


@dataclass
class _Feed:
    """Per-symbol-state: latest snapshot plus the newest closed candle per frame."""

    snapshot: dict[str, Any] = field(default_factory=dict)
    closed: dict[str, dict[str, Any]] = field(default_factory=dict)
    watching: bool = False
    keepalives: int = 0
    stale: bool = False


class MarketDataService:
    """Owns subscriptions, in-memory state, persistence and closed-candle events."""

    def __init__(
        self,
        home=None,
        *,
        symbols: tuple[str, ...] | None = None,
        intervals: tuple[str, ...] = TIMEFRAMES,
        db: Database | None = None,
        proxy: str | None = None,
        stream_factory=None,
        client_factory=None,
        backfill: bool = True,
        stale_after_ms: int = STALE_AFTER_MS,
    ):
        self.home = home or quantdesk_home()
        self.symbols = tuple(symbols or VENUE_SYMBOLS)
        self.intervals = tuple(intervals)
        self.db = db or Database(self.home / "quantdesk.db")
        self.proxy = proxy if proxy is not None else configured_proxy(self.home)
        self._stream_factory = stream_factory or rt.BybitStreamManager
        self._client_factory = client_factory or (lambda: BybitClient(proxy=self.proxy, timeout=20.0))
        self._backfill_enabled = backfill
        self.stale_after_ms = _stale_window(stale_after_ms)

        self._feeds: dict[str, _Feed] = {symbol: _Feed() for symbol in self.symbols}
        # One short string per closed bar, for the life of the process: at this
        # universe size it costs kilobytes even after months of uptime, and it is
        # what makes a replayed close idempotent across reconnects and restarts.
        self._closed_seen: set[str] = set()
        self._closed_handlers: list[ClosedCandleHandler] = []
        self._ticker_handlers: list[Callable[[dict[str, Any]], Awaitable[None] | None]] = []
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._lock = threading.Lock()
        self._wake = asyncio.Event()

        self.streams: Any = None
        self.started_at: int | None = None
        self.stopped_at: int | None = None
        self.closed_events = 0
        self.duplicate_closed = 0
        self.restored_snapshots = 0
        self.backfills = 0
        self.backfill_errors: list[str] = []
        self.last_error: str | None = None
        self.last_backfill_at: int | None = None
        # Last time any real upstream data arrived (WebSocket frame or REST read).
        self.last_success_at: int | None = None

    # -- lifecycle -------------------------------------------------------
    def restore(self) -> int:
        """Load the last real snapshots and recent closed candles from SQLite."""
        restored = 0
        for row in self.db.load_market_snapshots(VENUE):
            symbol = row.get("symbol")
            if symbol not in self._feeds:
                continue
            self._feeds[symbol].snapshot = dict(row)
            restored += 1
        for symbol in self.symbols:
            for interval in self.intervals:
                rows = self.db.load_candles(VENUE, symbol, interval, limit=MEMORY_CANDLES)
                if rows:
                    self._feeds[symbol].closed[interval] = rows[-1]
                    # The newest stored bar may be re-sent by the venue after a
                    # reconnect or a restart. Marking it here keeps that replay
                    # from being reported as a new close.
                    self._closed_seen.add(f"{symbol}:{interval}:{rows[-1]['ts']}")
        self.restored_snapshots = restored
        logger.info("restored %d snapshots and candle tails from SQLite", restored)
        return restored

    async def start(self) -> None:
        if self.started_at is not None:
            return
        self.restore()
        self.started_at = _now_ms()
        self.stopped_at = None
        candles, tickers = rt.default_topics(self.symbols, self.intervals)
        # A failed reconnect must log its real reason, not the library's
        # AttributeError for a connection that never completed its handshake.
        rt.tolerate_failed_handshake_noise()
        self.streams = self._stream_factory(self._on_event, proxy=self.proxy)
        self._wake.clear()
        await self.streams.start(candles + tickers)
        _ = self._wake

    async def stop(self) -> None:
        if self.streams is not None:
            await self.streams.stop()
        self.stopped_at = _now_ms()

    async def set_proxy(self, proxy: str | None) -> None:
        """Adopt a new proxy address and rebuild the streams on it.

        A running stream holds the proxy it was constructed with, so a node
        switch only takes effect once the streams are rebuilt. Stored quotes and
        closed candles are untouched: the page keeps the last real data while
        the new link comes up.
        """
        self.proxy = proxy
        if self.streams is None:
            return
        candles, tickers = rt.default_topics(self.symbols, self.intervals)
        streams = self._stream_factory(self._on_event, proxy=self.proxy)
        await self.streams.stop()
        self.streams = streams
        await self.streams.start(candles + tickers)

    @property
    def running(self) -> bool:
        return self.started_at is not None and self.stopped_at is None

    async def wait_for_gap(self) -> None:
        """Resolve on the next (re)connect, i.e. when a REST gap may exist.

        A flag raised before this is awaited is consumed immediately, so the
        startup connection still triggers the first reconciliation.
        """
        event = self._wake
        if event.is_set():
            event.clear()
            return
        await event.wait()
        event.clear()

    def subscribe(self, queue: "asyncio.Queue[str]") -> Callable[[], None]:
        """Register a local event sink (the browser stream). Returns an unsubscribe."""
        self._subscribers.add(queue)

        def unsubscribe() -> None:
            self._subscribers.discard(queue)

        return unsubscribe

    async def _broadcast(self, event: rt.MarketEvent) -> None:
        if not self._subscribers:
            return
        try:
            payload = encode_event(event)
        except Exception:  # noqa: BLE001 - a serialization slip must not kill the feed
            return
        self.publish(payload)

    def publish(self, payload: str) -> None:
        """Push a pre-encoded frame to local subscribers (never blocks the feed)."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # A slow browser must never stall the venue reader.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(payload)

    # -- handlers --------------------------------------------------------
    def on_closed_candle(self, handler: ClosedCandleHandler) -> None:
        """Register a consumer of confirmed-closed candles (alerts, strategies)."""
        self._closed_handlers.append(handler)

    def on_ticker(self, handler: Callable[[dict[str, Any]], Awaitable[None] | None]) -> None:
        self._ticker_handlers.append(handler)

    # -- ingestion -------------------------------------------------------
    async def _on_event(self, event: rt.MarketEvent) -> None:
        """Apply one venue event, then publish it only if it changed anything.

        A frame that was dropped as a duplicate, out of order or empty must not
        reach the browser either: otherwise a replayed close would repaint the
        chart and look like a second confirmation even though nothing was stored.
        """
        if isinstance(event, rt.CandleEvent):
            applied = await self._on_candle(event)
        elif isinstance(event, rt.TickerEvent):
            applied = await self._on_ticker(event)
        else:
            applied = True
            if isinstance(event, rt.ConnectionEvent) and event.state in {"connected", "reconnecting"}:
                # A (re)established connection may have missed bars; reconcile soon.
                self._wake.set()
        if applied:
            await self._broadcast(event)

    async def _on_ticker(self, event: rt.TickerEvent) -> bool:
        feed = self._feeds.get(event.venue_symbol)
        if feed is None:
            return False
        # Bybit sends a full snapshot type and then small delta frames that carry
        # only one or two fields. The ticker fields are therefore merged into the
        # stored quote: a delta that mentions only markPrice must never blank the
        # lastPrice the page is showing.
        fields = {key: value for key, value in _snapshot_fields(event).items() if value is not None}
        with self._lock:
            current = feed.snapshot
            current_ts = current.get("exchange_ts")
            if not fields:
                # Sometimes a frame is only a keep-alive (`type: delta` with pong
                # fields). It carries no market state, so it must not refresh the
                # age either -- otherwise a frozen feed would look live forever.
                feed.keepalives += 1
                return False
            if current_ts is not None and event.exchange_ts < current_ts:
                # Out-of-order frame: never move the displayed quote backwards.
                return False
            merged = dict(current)
            merged.update(fields)
            merged["venue"] = VENUE
            merged["symbol"] = event.venue_symbol
            merged["exchange_ts"] = event.exchange_ts
            merged["received_ts"] = event.received_ts
            merged["source"] = "websocket"
        self.db.upsert_market_snapshot(VENUE, event.venue_symbol, merged)
        with self._lock:
            feed.snapshot = merged
        self.last_success_at = event.received_ts
        for handler in self._ticker_handlers:
            result = handler(dict(merged))
            if asyncio.iscoroutine(result):
                await result
        return True

    async def _on_candle(self, event: rt.CandleEvent) -> bool:
        feed = self._feeds.get(event.venue_symbol)
        if feed is None:
            return False
        row = {
            "ts": event.open_ts,
            "open": event.open,
            "high": event.high,
            "low": event.low,
            "close": event.close,
            "volume": event.volume,
            "turnover": event.turnover,
        }
        if not event.closed:
            # An unconfirmed bar may update the live chart but must never enter
            # persistence, resonance, backtests or alerts.
            with self._lock:
                forming = feed.snapshot.setdefault("forming", {})
                if isinstance(forming, dict):
                    # Only the configured frames belong here; the venue could send
                    # anything on the topic, and this dict must not grow with it.
                    if event.interval in self.intervals or not forming:
                        forming[event.interval] = row
                        for stale_frame in [frame for frame in forming if frame not in self.intervals]:
                            forming.pop(stale_frame, None)
            self.last_success_at = event.received_ts
            return True

        key = event.observation_key
        with self._lock:
            if key in self._closed_seen:
                self.duplicate_closed += 1
                return False
            self._closed_seen.add(key)
            # The confirmed bar supersedes whatever was forming for that frame,
            # but an out-of-order confirmation for an unseen older bar must not
            # move the displayed bar backwards. It is still persisted below.
            current = feed.closed.get(event.interval)
            if current is None or event.open_ts >= current["ts"]:
                feed.closed[event.interval] = row
            forming = feed.snapshot.get("forming")
            if isinstance(forming, dict):
                forming.pop(event.interval, None)
        self.db.upsert_candles(
            VENUE,
            event.venue_symbol,
            event.interval,
            [{**row, "source": "venue_ws", "exchange_ts": event.exchange_ts, "received_ts": event.received_ts}],
            source="venue_ws",
        )
        self.closed_events += 1
        self.last_success_at = event.received_ts
        payload = {
            "venue": VENUE,
            "symbol": event.venue_symbol,
            "interval": event.interval,
            "open_ts": event.open_ts,
            "observation_key": key,
            "candle": row,
            "exchange_ts": event.exchange_ts,
            "received_ts": event.received_ts,
            "source": "websocket",
        }
        for handler in self._closed_handlers:
            result = handler(dict(payload))
            if asyncio.iscoroutine(result):
                await result
        return True

    # -- REST reconciliation --------------------------------------------
    async def backfill(self, symbols: list[str] | None = None) -> dict[str, Any]:
        """Close gaps over REST after a (re)connect; safe to run repeatedly."""
        if not self._backfill_enabled:
            return {"skipped": True}
        targets = [s for s in (symbols or list(self.symbols)) if s in self._feeds]
        synced = 0
        inserted = 0
        errors: list[str] = []
        updated = 0
        for symbol in targets:
            try:
                result = await asyncio.to_thread(self._backfill_symbol, symbol)
            except Exception as exc:  # noqa: BLE001 - one symbol must not stop the rest
                message = f"{symbol}: {type(exc).__name__}: {exc}"
                errors.append(message)
                logger.warning("backfill failed for %s: %s", symbol, exc)
                continue
            synced += result["synced"]
            inserted += result["inserted"]
            updated += result["updated"]
        self.backfills += 1
        self.last_backfill_at = _now_ms()
        if errors:
            self.backfill_errors = errors[-10:]
            self.last_error = errors[-1]
        # `rows` counts every bar the sweep confirmed; `inserted` counts the ones
        # the database did not already have. Both are reported, because only the
        # second says whether the sweep actually closed a gap.
        return {
            "symbols": len(targets), "rows": synced, "inserted": inserted,
            "updated": updated, "errors": errors,
        }

    def _backfill_symbol(self, symbol: str) -> dict[str, int]:
        client = self._client_factory()
        # What the store did, per row: `synced` = added or corrected,
        # `inserted` = genuinely new, `updated` = existing bar whose values moved.
        synced = 0
        inserted = 0
        updated = 0
        try:
            for interval in self.intervals:
                rows = client.kline_snapshot(symbol, interval, limit=MEMORY_CANDLES, completed_only=True)
                if not rows:
                    continue
                # The store now reports per-row outcomes, so "synced" and
                # "inserted" are what actually happened rather than a guess from
                # timestamps.
                outcome = self.db.upsert_candles(VENUE, symbol, interval, rows, source="venue_rest")
                synced += outcome.written
                inserted += outcome.inserted
                updated += outcome.updated
                with self._lock:
                    newest = rows[-1]["ts"]
                    # A read whose window closed just before the newest bar was
                    # confirmed must not move the displayed bar backwards.
                    current = self._feeds[symbol].closed.get(interval)
                    if current is None or newest >= current["ts"]:
                        self._feeds[symbol].closed[interval] = rows[-1]
                    # Bars older than the newest are ones the stream cannot newly
                    # deliver, so their identity is recorded and a replay of them
                    # is dropped. The newest bar is left unrecorded on purpose:
                    # whichever path reports it first gets to persist it, and if
                    # REST stored it first the stream's confirmation still writes
                    # the same primary key once more with the venue's own values.
                    for row in rows[:-1]:
                        self._closed_seen.add(f"{symbol}:{interval}:{row['ts']}")
            ticker = client.ticker("linear", symbol)
            snapshot = self._snapshot_from_rest(ticker)
            with self._lock:
                current_snapshot = self._feeds[symbol].snapshot
                current = current_snapshot.get("exchange_ts")
                incoming = snapshot.get("exchange_ts")
                if not current_snapshot or current is None or (incoming is not None and incoming >= current):
                    self._feeds[symbol].snapshot = snapshot
            self.db.upsert_market_snapshot(VENUE, symbol, snapshot)
            self.last_success_at = snapshot.get("received_ts") or self.last_success_at
        finally:
            client.close()
        return {"synced": synced, "inserted": inserted, "updated": updated}

    def _snapshot_from_rest(self, ticker: dict[str, Any]) -> dict[str, Any]:
        # `exchange_ts` is the venue's own clock when the response carries it, so
        # a REST quote can be ordered against WebSocket frames. `received_ts` is
        # always local: it is what the staleness window is measured from.
        venue_ts = ticker.get("exchangeTs")
        try:
            exchange_ts = int(venue_ts) if venue_ts is not None else None
        except (TypeError, ValueError):
            exchange_ts = None
        received = _now_ms()
        return {
            "venue": VENUE,
            "symbol": ticker.get("symbol"),
            "last_price": _finite(ticker.get("lastPrice")),
            "mark_price": _finite(ticker.get("markPrice")),
            "index_price": _finite(ticker.get("indexPrice")),
            "funding_rate": _finite(ticker.get("fundingRate")),
            "funding_interval_hour": _finite(ticker.get("fundingIntervalHour")),
            "next_funding_time": ticker.get("nextFundingTime") or None,
            "open_interest": _finite(ticker.get("openInterest")),
            "open_interest_value": _finite(ticker.get("openInterestValue")),
            "turnover_24h": _finite(ticker.get("turnover24h")),
            "volume_24h": _finite(ticker.get("volume24h")),
            "price_24h_pct": _finite(ticker.get("price24hPcnt")),
            "high_24h": _finite(ticker.get("highPrice24h")),
            "low_24h": _finite(ticker.get("lowPrice24h")),
            # Missing venue time stays missing; received_ts measures freshness but
            # must never masquerade as an exchange ordering key.
            "exchange_ts": exchange_ts,
            "received_ts": received,
            "source": "rest_backfill",
        }

    # -- reads -----------------------------------------------------------
    def mark_price(self, symbol: str) -> float | None:
        feed = self._feeds.get(symbol)
        if feed is None:
            return None
        snapshot = feed.snapshot
        return _finite(snapshot.get("mark_price")) or _finite(snapshot.get("last_price"))

    def snapshot(self, symbol: str, interval: str | None = None) -> dict[str, Any]:
        """Everything the page needs, with per-part provenance and freshness."""
        spec = require_instrument(symbol)
        venue_symbol = spec.venue_symbol
        feed = self._feeds.get(venue_symbol)
        if feed is None:
            raise KeyError(venue_symbol)
        now = _now_ms()
        with self._lock:
            raw = dict(feed.snapshot)
            forming = (raw.get("forming") or {}).get(interval) if interval else None
            closed_row = feed.closed.get(interval) if interval else None
        received = raw.get("received_ts")
        age = (now - int(received)) if received else None
        stale = self.stale(venue_symbol, now) if received else True
        return {
            "venue": VENUE,
            "symbol": venue_symbol,
            "displaySymbol": spec.display_symbol,
            "interval": interval,
            "candle": closed_row,
            "formingCandle": forming,
            "ticker": {key: value for key, value in raw.items() if key not in {"forming"}},
            "source": raw.get("source"),
            "exchangeTs": raw.get("exchange_ts"),
            "receivedTs": received,
            "ageMs": age,
            "stale": stale,
            "connection": self.connection_state(),
        }

    def _stale_symbols(self) -> list[str]:
        """Symbols whose stored quote has aged out, and the transition itself.

        Evaluating freshness here rather than only in `snapshot` means a browser
        that is idle between requests still receives the moment a quote goes
        stale, instead of discovering it on its next click.
        """
        now = _now_ms()
        stale: list[str] = []
        for symbol, feed in self._feeds.items():
            if not feed.snapshot:
                continue
            age = self.quote_age_ms(symbol, now)
            current = age is None or age > self.stale_after_ms
            if current:
                stale.append(symbol)
            if current != feed.stale:
                feed.stale = current
                self._announce_stale(feed, symbol, current)
        return stale

    def _announce_stale(self, feed: _Feed, symbol: str | None, stale: bool) -> None:
        """Tell local subscribers that one symbol's quote went stale or recovered."""
        if not symbol:
            return
        try:
            self.publish(
                json.dumps(
                    {
                        "kind": "stale",
                        "eventType": "QuoteStale" if stale else "QuoteFresh",
                        "venue": VENUE,
                        "symbol": symbol,
                        "venueSymbol": symbol,
                        "interval": None,
                        "stale": stale,
                        "source": feed.snapshot.get("source") or "sqlite",
                        "sentTs": _now_ms(),
                    },
                    ensure_ascii=False,
                )
            )
        except Exception:  # noqa: BLE001 - an announcement must never break a read
            logger.debug("stale announcement failed for %s", symbol)

    def stale(self, symbol: str, now: int | None = None) -> bool:
        """Whether one symbol's stored quote is older than the freshness window."""
        age = self.quote_age_ms(symbol, now)
        return age is None or age > self.stale_after_ms

    def quote_age_ms(self, symbol: str, now: int | None = None) -> int | None:
        feed = self._feeds.get(symbol)
        if feed is None:
            return None
        received = feed.snapshot.get("received_ts")
        if not received:
            return None
        return (now if now is not None else _now_ms()) - int(received)

    def connection_state(self) -> dict[str, Any]:
        streams = self.streams
        connected = bool(streams and streams.connected)
        last_message = streams.last_message_at() if streams else None
        age = (_now_ms() - last_message) if last_message else None
        if not self.running:
            state = "stopped"
        elif connected:
            state = "connected"
        elif streams:
            state = "degraded"
        else:
            state = "starting"
        last_success = self.last_success_at
        return {
            "state": state,
            "connected": connected,
            "staleSymbols": self._stale_symbols(),
            "reconnects": streams.reconnects if streams else 0,
            "lastMessageAt": last_message,
            "lastMessageAgeMs": age,
            "lastSuccessAt": last_success,
            "lastSuccessAgeMs": (_now_ms() - last_success) if last_success else None,
            "lastBackfillAt": self.last_backfill_at,
            "lastError": streams.last_error() if streams else self.last_error,
            "proxyConfigured": bool(self.proxy),
            # Never expose the proxy address (it may carry credentials).
            "streams": streams.status() if streams else [],
        }

    def health(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "startedAt": self.started_at,
            "stoppedAt": self.stopped_at,
            "symbols": len(self.symbols),
            "intervals": list(self.intervals),
            "restoredSnapshots": self.restored_snapshots,
            "closedEvents": self.closed_events,
            "duplicateClosed": self.duplicate_closed,
            "backfills": self.backfills,
            "lastBackfillAt": self.last_backfill_at,
            "backfillErrors": self.backfill_errors[-3:],
            "staleAfterMs": self.stale_after_ms,
            "connection": self.connection_state(),
        }


_SERVICE: MarketDataService | None = None
_SERVICE_LOCK = threading.Lock()


def get_market_service(home=None, **kwargs) -> MarketDataService:
    """Process-wide singleton: one market state, one set of connections."""
    global _SERVICE
    target_home = home or quantdesk_home()
    with _SERVICE_LOCK:
        if _SERVICE is None or _SERVICE.home != target_home:
            _SERVICE = MarketDataService(target_home, **kwargs)
        return _SERVICE


def reset_market_service() -> None:
    """Drop the singleton (used by tests and by a home switch)."""
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = None


def encode_event(event: rt.MarketEvent, source: str = "websocket") -> str:
    """Serialize a market event for the local browser stream.

    The frame carries the same envelope the internal events do -- venue, symbol,
    interval, both timestamps, a stable observation key and the provenance --
    so the browser can deduplicate and label a delta without asking again.
    """
    base: dict[str, Any] = {
        "venue": VENUE,
        "source": source,
        "sentTs": _now_ms(),
    }
    if isinstance(event, rt.CandleEvent):
        payload = {
            **base,
            "kind": "candle",
            "eventType": "CandleClosed" if event.closed else "CandleUpdated",
            "symbol": event.venue_symbol,
            "venueSymbol": event.venue_symbol,
            "interval": event.interval,
            "openTs": event.open_ts,
            "observationKey": event.observation_key,
            "closed": event.closed,
            "open": event.open,
            "high": event.high,
            "low": event.low,
            "close": event.close,
            "volume": event.volume,
            "exchangeTs": event.exchange_ts,
            "receivedTs": event.received_ts,
        }
    elif isinstance(event, rt.TickerEvent):
        payload = {
            **base,
            "kind": "ticker",
            "eventType": "DerivativesUpdated" if event.open_interest_value is not None else "TickerUpdated",
            "symbol": event.venue_symbol,
            "venueSymbol": event.venue_symbol,
            "interval": None,
            "observationKey": f"{event.venue_symbol}:ticker:{event.exchange_ts}",
            "lastPrice": event.last_price,
            "markPrice": event.mark_price,
            "indexPrice": event.index_price,
            "fundingRate": event.funding_rate,
            "fundingIntervalHour": event.funding_interval_hour,
            "nextFundingTime": event.next_funding_time,
            "openInterest": event.open_interest,
            "openInterestValue": event.open_interest_value,
            "volume24h": event.volume_24h,
            "turnover24h": event.turnover_24h,
            "price24hPct": event.price_24h_pct,
            "high24h": event.high_24h,
            "low24h": event.low_24h,
            "exchangeTs": event.exchange_ts,
            "receivedTs": event.received_ts,
        }
    else:
        payload = {
            **base,
            "kind": "connection",
            "eventType": "ConnectionChanged",
            "state": event.state,
            "attempt": event.attempt,
            "detail": event.detail,
            "receivedTs": event.received_ts,
        }
    return json.dumps(payload, ensure_ascii=False)


def backfill_completed_event(result: dict[str, Any]) -> str:
    """Announce a finished REST reconciliation round to local subscribers."""
    return json.dumps(
        {
            "kind": "backfill",
            "eventType": "BackfillCompleted",
            "venue": VENUE,
            "symbol": None,
            "interval": None,
            "source": "rest_backfill",
            "symbols": result.get("symbols", 0),
            "rows": result.get("rows", 0),
            "inserted": result.get("inserted", 0),
            "updated": result.get("updated", 0),
            "errors": result.get("errors", []),
            "sentTs": _now_ms(),
        },
        ensure_ascii=False,
    )
