"""Bybit V5 public WebSocket transport.

Protocol facts (verified against the official V5 docs, not from memory):

* Public linear endpoint: ``wss://stream.bybit.com/v5/public/linear``
* Kline topic: ``kline.{interval}.{symbol}``; the payload carries
  ``confirm: true`` once the candle has closed. Push frequency 1-60s.
* Ticker topic: ``tickers.{symbol}``.
* Heartbeat: send ``{"op": "ping"}`` roughly every 20 seconds. The server closes
  a connection after ~10 minutes without a ping or stream data.
* One public connection may hold an ``args`` array of at most 21,000 characters;
  subscriptions are therefore sharded across connections.
* Public topics need no authentication.

This module owns the transport only: connect, subscribe, heartbeat, reconnect
with exponential backoff plus jitter, and normalize each message into a typed
event. Persisting candles and deciding what a closed bar means is the market
service's job.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

import websockets

from ..config.instruments import BYBIT_INTERVALS, CORE_TIMEFRAMES, INTERVAL_MS, TIMEFRAMES

logger = logging.getLogger(__name__)

PUBLIC_LINEAR_URL = "wss://stream.bybit.com/v5/public/linear"
# Documented ceiling for the args array of one public connection.
ARGS_CHAR_LIMIT = 21_000
# The docs recommend a ping every 20s; the server drops an idle socket after 10m.
HEARTBEAT_SECONDS = 20.0
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
# A silent socket is treated as dead after this long without any frame.
RECEIVE_TIMEOUT_SECONDS = 90.0

EventKind = Literal["ticker", "candle", "connection"]


@dataclass(frozen=True)
class TickerEvent:
    """A ticker snapshot. Fields absent from the venue stay None."""

    venue_symbol: str
    last_price: float | None
    mark_price: float | None
    index_price: float | None
    funding_rate: float | None
    funding_interval_hour: float | None
    next_funding_time: int | None
    open_interest: float | None
    open_interest_value: float | None
    turnover_24h: float | None
    volume_24h: float | None
    price_24h_pct: float | None
    high_24h: float | None
    low_24h: float | None
    exchange_ts: int
    received_ts: int
    kind: EventKind = "ticker"


@dataclass(frozen=True)
class CandleEvent:
    """One kline update. ``closed`` is Bybit's own ``confirm`` flag."""

    venue_symbol: str
    interval: str
    open_ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float | None
    closed: bool
    exchange_ts: int
    received_ts: int
    kind: EventKind = "candle"

    @property
    def observation_key(self) -> str:
        """Stable identity of this bar, used for idempotent closed-candle handling."""
        return f"{self.venue_symbol}:{self.interval}:{self.open_ts}"


@dataclass(frozen=True)
class ConnectionEvent:
    state: Literal["connecting", "connected", "disconnected", "reconnecting", "stopped"]
    attempt: int
    detail: str = ""
    received_ts: int = field(default_factory=lambda: int(time.time() * 1000))
    kind: EventKind = "connection"


MarketEvent = TickerEvent | CandleEvent | ConnectionEvent
EventSink = Callable[[MarketEvent], Awaitable[None] | None]


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_message(raw: str | bytes, received_ts: int | None = None) -> MarketEvent | None:
    """Normalize one Bybit frame, or return None for control/unknown frames."""
    now = received_ts if received_ts is not None else int(time.time() * 1000)
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    topic = payload.get("topic")
    if not isinstance(topic, str):
        # Subscription acks, pongs and errors carry no data we can use here.
        return None
    data = payload.get("data")
    exchange_ts = _int(payload.get("ts")) or now

    if topic.startswith("kline."):
        parts = topic.split(".", 2)
        if len(parts) != 3:
            return None
        interval_code, venue_symbol = parts[1], parts[2]
        interval = _interval_from_code(interval_code)
        if interval is None:
            return None
        bar = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)
        if not isinstance(bar, dict):
            return None
        open_ts = _int(bar.get("start"))
        open_price, high, low, close = (_float(bar.get(key)) for key in ("open", "high", "low", "close"))
        if open_ts is None or None in (open_price, high, low, close):
            return None
        return CandleEvent(
            venue_symbol=venue_symbol,
            interval=interval,
            open_ts=open_ts,
            open=open_price,  # type: ignore[arg-type]
            high=high,  # type: ignore[arg-type]
            low=low,  # type: ignore[arg-type]
            close=close,  # type: ignore[arg-type]
            volume=_float(bar.get("volume")) or 0.0,
            turnover=_float(bar.get("turnover")),
            # Absent `confirm` must not be read as "closed".
            closed=bar.get("confirm") is True,
            exchange_ts=_int(bar.get("timestamp")) or exchange_ts,
            received_ts=now,
        )

    if topic.startswith("tickers."):
        venue_symbol = topic.split(".", 1)[1]
        if not isinstance(data, dict):
            return None
        return TickerEvent(
            venue_symbol=venue_symbol,
            last_price=_float(data.get("lastPrice")),
            mark_price=_float(data.get("markPrice")),
            index_price=_float(data.get("indexPrice")),
            funding_rate=_float(data.get("fundingRate")),
            funding_interval_hour=_float(data.get("fundingIntervalHour")),
            next_funding_time=_int(data.get("nextFundingTime")),
            open_interest=_float(data.get("openInterest")),
            open_interest_value=_float(data.get("openInterestValue")),
            turnover_24h=_float(data.get("turnover24h")),
            volume_24h=_float(data.get("volume24h")),
            # The venue spells this one `price24hPcnt`.
            price_24h_pct=_float(data.get("price24hPcnt")),
            high_24h=_float(data.get("highPrice24h")),
            low_24h=_float(data.get("lowPrice24h")),
            exchange_ts=exchange_ts,
            received_ts=now,
        )
    return None


def _interval_from_code(code: str) -> str | None:
    for canonical, venue_code in BYBIT_INTERVALS.items():
        if venue_code == code:
            return canonical
    return None


def interval_code(interval: str) -> str:
    code = BYBIT_INTERVALS.get(interval)
    if code is None:
        raise ValueError(f"unsupported interval {interval!r}")
    return code


def kline_topic(venue_symbol: str, interval: str) -> str:
    return f"kline.{interval_code(interval)}.{venue_symbol}"


def ticker_topic(venue_symbol: str) -> str:
    return f"tickers.{venue_symbol}"


def shard_topics(topics: Iterable[str], limit: int = ARGS_CHAR_LIMIT) -> list[list[str]]:
    """Split topics so each connection's args array stays inside the venue limit.

    The limit counts the serialized JSON array, so quotes and commas are included
    rather than just the sum of topic lengths.
    """
    shards: list[list[str]] = []
    current: list[str] = []
    for topic in topics:
        candidate = current + [topic]
        if current and len(json.dumps(candidate)) > limit:
            shards.append(current)
            current = [topic]
        else:
            current = candidate
    if current:
        shards.append(current)
    return shards


def backoff_delay(attempt: int, *, base: float = BACKOFF_BASE_SECONDS, cap: float = BACKOFF_MAX_SECONDS) -> float:
    """Exponential backoff with full jitter, so reconnect storms spread out."""
    ceiling = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(0.0, ceiling)


def tolerate_failed_handshake_noise() -> bool:
    """Stop websockets logging a spurious AttributeError for a failed connect.

    When a WebSocket connection dies before its handshake completes - a refused
    proxy, a venue that is unreachable, a DNS failure - `websockets` 17 still calls
    `connection_lost`, which reaches for `recv_messages`, an attribute only
    `connection_made` creates. The result is that every failed reconnect attempt
    logs an `AttributeError` instead of the real reason, and during an outage that
    buries the actual error under thousands of identical lines.

    The guard skips the teardown of a connection that never came up; the connect
    attempt itself still raises, and the stream still records `last_error`. It is
    idempotent, and it is a workaround for an upstream defect - a fixed
    `websockets` makes it a no-op.
    """
    try:
        from websockets.asyncio import connection as ws_connection
    except Exception:  # noqa: BLE001 - a different layout needs no guard
        return False
    target = getattr(ws_connection, "Connection", None)
    original = getattr(target, "connection_lost", None)
    if original is None or getattr(original, "_quantdesk_guard", False):
        return False

    def guarded(self, exc):  # type: ignore[no-untyped-def]
        if "recv_messages" not in self.__dict__:
            return None
        return original(self, exc)

    guarded._quantdesk_guard = True  # type: ignore[attr-defined]
    target.connection_lost = guarded  # type: ignore[union-attr]
    return True


class BybitStream:
    """One public WebSocket connection carrying a fixed set of topics."""

    def __init__(
        self,
        topics: list[str],
        sink: EventSink,
        *,
        url: str = PUBLIC_LINEAR_URL,
        proxy: str | None = None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        name: str = "bybit-stream",
        connect=None,
    ):
        if not topics:
            raise ValueError("a stream needs at least one topic")
        self.topics = list(topics)
        self.sink = sink
        self.url = url
        self.proxy = proxy
        self.heartbeat_seconds = heartbeat_seconds
        self.name = name
        self._connect = connect or websockets.connect
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.state: str = "stopped"
        self.attempt = 0
        self.connected_at: int | None = None
        self.last_message_at: int | None = None
        self.last_error: str | None = None
        self.messages_received = 0
        self.reconnects = 0

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=self.name)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._emit(ConnectionEvent("stopped", self.attempt))

    async def _emit(self, event: MarketEvent) -> None:
        result = self.sink(event)
        if asyncio.iscoroutine(result):
            await result

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "topics": len(self.topics),
            "attempt": self.attempt,
            "reconnects": self.reconnects,
            "messagesReceived": self.messages_received,
            "connectedAt": self.connected_at,
            "lastMessageAt": self.last_message_at,
            "lastError": self.last_error,
        }

    # -- connection loop -------------------------------------------------
    async def _run(self) -> None:
        while not self._stop.is_set():
            # A clean close still means the stream is down, so both the normal
            # return and any transport failure fall through to the backoff below.
            try:
                await self._connect_once()
                self.attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - every transport error is retryable
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("%s connection error: %s", self.name, self.last_error)
            if self._stop.is_set():
                break
            self.attempt += 1
            self.reconnects += 1
            delay = backoff_delay(self.attempt)
            self.state = "reconnecting"
            await self._emit(ConnectionEvent("reconnecting", self.attempt, self.last_error or ""))
            logger.info("%s reconnecting in %.1fs (attempt %d)", self.name, delay, self.attempt)
            try:
                # Wake early when stop is requested instead of sleeping it out.
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                continue

    async def _connect_once(self) -> None:
        self.state = "connecting"
        await self._emit(ConnectionEvent("connecting", self.attempt))
        # The proxy is resolved to a URL string at construction; websockets takes
        # it directly. Reconnects re-read it so a proxy switch is picked up.
        kwargs: dict[str, Any] = {"open_timeout": 20, "close_timeout": 10, "ping_interval": None}
        # The proxy is passed explicitly, including "none at all": websockets
        # otherwise falls back to the operating system's proxy settings, which is
        # both a way around the operator's configuration and, on a Mac with a
        # system SOCKS proxy, an outright failure ("requires python-socks"). The
        # venue must be reached through the configured proxy or directly.
        kwargs["proxy"] = self.proxy or None
        try:
            async with self._connect(self.url, **kwargs) as socket:
                self.state = "connected"
                self.connected_at = int(time.time() * 1000)
                await self._emit(ConnectionEvent("connected", self.attempt))
                await socket.send(json.dumps({"op": "subscribe", "args": self.topics}))
                logger.info("%s connected with %d topics", self.name, len(self.topics))
                heartbeat = asyncio.create_task(self._heartbeat_loop(socket), name=f"{self.name}-ping")
                try:
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=RECEIVE_TIMEOUT_SECONDS)
                        except asyncio.TimeoutError as exc:
                            raise TimeoutError(
                                f"no frame for {RECEIVE_TIMEOUT_SECONDS:.0f}s; treating the socket as dead"
                            ) from exc
                        self.messages_received += 1
                        self.last_message_at = int(time.time() * 1000)
                        event = parse_message(raw, self.last_message_at)
                        if event is not None:
                            await self._emit(event)
                finally:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
        finally:
            # Report the drop on every exit path, including a stop request and a
            # transport failure, so the reported state never lags reality.
            if self.state == "connected":
                self.state = "disconnected"
                await self._emit(ConnectionEvent("disconnected", self.attempt))

    async def _heartbeat_loop(self, socket) -> None:
        """Ping every 20s; the venue drops an idle connection after ~10 minutes."""
        while not self._stop.is_set():
            await asyncio.sleep(self.heartbeat_seconds)
            try:
                await socket.send(json.dumps({"op": "ping"}))
            except Exception as exc:  # noqa: BLE001 - a failed ping means the socket is gone
                logger.warning("%s ping failed: %s", self.name, exc)
                return


class BybitStreamManager:
    """Owns the sharded public connections for a fixed set of subscriptions."""

    def __init__(self, sink: EventSink, *, proxy: str | None = None, url: str = PUBLIC_LINEAR_URL, connect=None):
        self.sink = sink
        self.proxy = proxy
        self.url = url
        self._connect = connect
        self.streams: list[BybitStream] = []

    async def start(self, topics: list[str]) -> None:
        await self.stop()
        shards = shard_topics(topics)
        self.streams = [
            BybitStream(
                shard,
                self.sink,
                url=self.url,
                proxy=self.proxy,
                name=f"bybit-stream-{index + 1}",
                connect=self._connect,
            )
            for index, shard in enumerate(shards)
        ]
        for stream in self.streams:
            await stream.start()
        logger.info("started %d stream(s) for %d topics", len(self.streams), len(topics))

    async def stop(self) -> None:
        for stream in self.streams:
            await stream.stop()
        self.streams = []

    def status(self) -> list[dict[str, Any]]:
        return [stream.status() for stream in self.streams]

    @property
    def connected(self) -> bool:
        return any(stream.state == "connected" for stream in self.streams)

    @property
    def reconnects(self) -> int:
        return sum(stream.reconnects for stream in self.streams)

    def last_message_at(self) -> int | None:
        stamps = [stream.last_message_at for stream in self.streams if stream.last_message_at]
        return max(stamps) if stamps else None

    def last_error(self) -> str | None:
        errors = [stream.last_error for stream in self.streams if stream.last_error]
        return errors[-1] if errors else None


# Intervals that are streamed live. Weekly bars are *not* among them: a stream that
# pushes one update per contract per week is a subscription nobody reads, and the
# scheduler already refreshes weekly history. Keeping this separate from TIMEFRAMES
# is what stops a new analysis timeframe from silently becoming a new live topic.
STREAMED_INTERVALS: tuple[str, ...] = CORE_TIMEFRAMES


def default_topics(venue_symbols: Iterable[str],
                   intervals: Iterable[str] = STREAMED_INTERVALS) -> tuple[list[str], list[str]]:
    """Candle topics for every symbol/timeframe, plus ticker topics.

    Returned separately because the caller may need to shard or page the ticker
    set differently from the candle set.
    """
    candles = [kline_topic(symbol, interval) for symbol in venue_symbols for interval in intervals]
    tickers = [ticker_topic(symbol) for symbol in venue_symbols]
    return candles, tickers


def interval_ms(interval: str) -> int:
    return INTERVAL_MS[interval]
