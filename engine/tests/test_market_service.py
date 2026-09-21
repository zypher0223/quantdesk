"""Market data service: idempotency, staleness, failure containment, API surface.

The service is the single source of market state, so these tests pin the rules
that keep it honest: unconfirmed bars never persist, a replayed close is written
and evaluated once, an old quote never replaces a newer one, and a dead proxy
leaves the local snapshot API answering with the last real data.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quantdesk.api import server
from quantdesk.api.server import app
from quantdesk.config.instruments import TIMEFRAMES, VENUE_SYMBOLS
from quantdesk.datahub import realtime as rt
from quantdesk.datahub.db import Database
from quantdesk.datahub.market_service import (
    MarketDataService,
    backfill_completed_event,
    encode_event,
    get_market_service,
    reset_market_service,
)
from quantdesk.datahub.market_service import _Feed

NOW = 1_800_000_000_000
SYMBOL = "BTCUSDT"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def ticker_frame(symbol: str = SYMBOL, price: float = 100.0, ts: int = NOW) -> str:
    return json.dumps(
        {
            "topic": f"tickers.{symbol}",
            "data": {
                "symbol": symbol,
                "lastPrice": str(price),
                "markPrice": str(price),
                "indexPrice": str(price),
                "fundingRate": "0.0001",
                "fundingIntervalHour": "8",
                "openInterest": "5",
                "openInterestValue": str(price * 5),
                "turnover24h": "1000",
                "volume24h": "10",
                "price24hPcnt": "0.01",
                "highPrice24h": str(price * 1.1),
                "lowPrice24h": str(price * 0.9),
            },
            "ts": ts,
        }
    )


def kline_frame(symbol: str = SYMBOL, interval: str = "15", start: int = NOW, close: float = 101.0, confirm: bool = True) -> str:
    return json.dumps(
        {
            "topic": f"kline.{interval}.{symbol}",
            "data": [
                {
                    "start": start,
                    "end": start + 899_999,
                    "interval": interval,
                    "open": "100",
                    "close": str(close),
                    "high": str(close + 1),
                    "low": "99",
                    "volume": "12.5",
                    "turnover": "1250",
                    "confirm": confirm,
                    "timestamp": start + 800_000,
                }
            ],
            "ts": start + 800_010,
        }
    )


def venue_event(raw: str):
    """Parse one preset venue frame the way the transport does."""
    event = rt.parse_message(raw, received_ts=NOW)
    assert event is not None
    return event


def ticker_delta(symbol: str = SYMBOL, ts: int = NOW, **fields) -> str:
    """A real Bybit `type: delta` frame: only the changed fields, and few of them."""
    data = {"symbol": symbol, **{key: str(value) for key, value in fields.items()}}
    return json.dumps({"topic": f"tickers.{symbol}", "data": data, "ts": ts, "type": "delta"})


def ticker_keepalive(symbol: str = SYMBOL, ts: int = NOW) -> str:
    """A frame the venue sends with no market fields at all."""
    return json.dumps({"topic": f"tickers.{symbol}", "data": {"symbol": symbol}, "ts": ts, "type": "delta"})


def deliver(service: MarketDataService, raw: str) -> None:
    """Run one raw venue frame through the service synchronously."""
    asyncio.run(service._on_event(venue_event(raw)))


class _ServiceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.db = Database(self.home / "quantdesk.db")

    def make_service(self, **overrides) -> MarketDataService:
        overrides.setdefault("symbols", ("BTCUSDT", "ETHUSDT"))
        overrides.setdefault("db", self.db)
        overrides.setdefault("backfill", False)
        overrides.setdefault("proxy", None)
        return MarketDataService(self.home, **overrides)

    def feed_event(self, service: MarketDataService, raw: str):
        """Push a raw venue frame through the service exactly as the stream does."""
        event = rt.parse_message(raw, received_ts=NOW)
        assert event is not None
        return event

    def deliver(self, service: MarketDataService, raw: str) -> None:
        """Run one venue frame through the service synchronously."""
        deliver(service, raw)


class ClosedCandleTests(_ServiceCase):
    def test_confirmed_candle_is_persisted_once_however_often_it_replays(self):
        service = self.make_service()
        seen: list[dict] = []
        service.on_closed_candle(seen.append)
        frame = self.feed_event(service, kline_frame())

        asyncio.run(service._on_event(frame))
        asyncio.run(service._on_event(frame))
        asyncio.run(service._on_event(frame))

        rows = self.db.load_candles("bybit", SYMBOL, "15m", limit=10)
        self.assertEqual(len(rows), 1, "a replayed close must not create a second row")
        self.assertEqual(len(seen), 1, "a replayed close must not fire the handler twice")
        self.assertEqual(service.duplicate_closed, 2)
        self.assertEqual(service.closed_events, 1)

    def test_a_dropped_frame_is_never_published_to_the_browser(self):
        # The browser must see the same events the engine accepted. Re-broadcasting
        # a duplicate close would repaint the chart as if a second bar confirmed,
        # and re-broadcasting an out-of-order ticker would move the price backwards
        # on screen even though the stored quote stayed correct.
        service = self.make_service()

        async def scenario():
            queue: asyncio.Queue[str] = asyncio.Queue(maxsize=50)
            service.subscribe(queue)
            await service._on_event(venue_event(kline_frame()))
            accepted = queue.get_nowait()
            await service._on_event(venue_event(kline_frame()))
            await service._on_event(venue_event(ticker_frame(price=100, ts=NOW)))
            quote = queue.get_nowait()
            await service._on_event(venue_event(ticker_frame(price=90, ts=NOW - 5_000)))
            await service._on_event(venue_event(ticker_keepalive(ts=NOW + 60_000)))
            return accepted, quote, queue.qsize()

        accepted, quote, remaining = asyncio.run(scenario())
        self.assertEqual(json.loads(accepted)["eventType"], "CandleClosed")
        self.assertEqual(json.loads(quote)["kind"], "ticker")
        self.assertEqual(remaining, 0, "only accepted frames may reach a subscriber")

    def test_rest_backfill_of_the_same_bar_is_the_same_row(self):
        # The WS path and the REST backfill must agree on identity: symbol +
        # interval + open_ts. Driving the real backfill is the point of this test;
        # writing the row by hand would assert the schema, not the code.
        older = NOW - 900_000

        class FakeClient:
            """A window that lags one bar behind the stream, plus the older one."""

            def __init__(self):
                self.kline_calls = 0

            def kline_snapshot(self, symbol, interval, limit=None, completed_only=False):
                self.kline_calls += 1
                if interval != "15m":
                    return []
                return [
                    {"ts": older, "open": 90, "high": 95, "low": 89, "close": 94.0, "volume": 5.0, "turnover": 470},
                    {"ts": NOW, "open": 100, "high": 102, "low": 99, "close": 101.5, "volume": 12.5, "turnover": 1250},
                ]

            def ticker(self, category, symbol):
                return {"symbol": symbol, "lastPrice": "100", "markPrice": "100"}

            def close(self):
                return None

        client = FakeClient()
        service = self.make_service(symbols=(SYMBOL,), client_factory=lambda: client, backfill=True)
        seen: list[dict] = []
        service.on_closed_candle(seen.append)

        # The stream delivers the newest bar first, then REST reconciles a window
        # that also contains an older bar the database has never seen.
        self.deliver(service, kline_frame())
        before = service.snapshot(SYMBOL, "15m")["candle"]["ts"]
        first = asyncio.run(service.backfill())

        self.assertEqual(first["symbols"], 1)
        self.assertEqual(first["errors"], [])
        # The 15m window holds two bars. `inserted` now comes from the store's own
        # per-row outcome rather than a timestamp guess: the older bar had never
        # been stored, so it really was an insert, and the bar at NOW was an update.
        self.assertEqual(first["rows"], 2)
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(first["updated"], 1)
        self.assertGreaterEqual(client.kline_calls, len(TIMEFRAMES))

        rows = self.db.load_candles("bybit", SYMBOL, "15m", limit=10)
        self.assertEqual([row["ts"] for row in rows], [older, NOW], "each bar exists once")
        self.assertAlmostEqual(rows[-1]["close"], 101.5, places=4, msg="the reconciled value should win")
        self.assertEqual(len(seen), 1, "REST must not report the already-delivered bar as a new close")
        self.assertEqual(service.closed_events, 1)

        # The displayed bar is the one the stream confirmed, not the older bar the
        # REST window happened to end on.
        self.assertEqual(service.snapshot(SYMBOL, "15m")["candle"]["ts"], before)
        self.assertEqual(before, NOW)

        # The older bar is now known, so the stream replaying it is a duplicate.
        self.deliver(service, kline_frame(start=older, close=94.0))
        self.assertEqual(len(seen), 1)
        self.assertEqual(service.duplicate_closed, 1)

        # A second sweep writes the same two rows and inserts nothing.
        second = asyncio.run(service.backfill())
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(len(self.db.load_candles("bybit", SYMBOL, "15m", limit=10)), 2)

    def test_unconfirmed_bar_never_persists_or_triggers(self):
        service = self.make_service()
        seen: list[dict] = []
        service.on_closed_candle(seen.append)
        frame = self.feed_event(service, kline_frame(confirm=False))

        asyncio.run(service._on_event(frame))

        self.assertEqual(self.db.load_candles("bybit", SYMBOL, "15m", limit=10), [])
        self.assertEqual(seen, [])
        # It is still available as the forming bar, so the chart stays live.
        self.assertIsNotNone(service.snapshot(SYMBOL, "15m")["formingCandle"])

    def test_a_closed_bar_replaces_the_forming_bar_and_clears_it(self):
        service = self.make_service()

        self.deliver(service, kline_frame(confirm=False))
        self.assertIsNotNone(service.snapshot(SYMBOL, "15m")["formingCandle"])
        self.deliver(service, kline_frame(confirm=True))
        self.assertIsNone(service.snapshot(SYMBOL, "15m")["formingCandle"])
        self.assertEqual(service.snapshot(SYMBOL, "15m")["candle"]["close"], 101.0)


class TickerOrderingTests(_ServiceCase):
    def test_an_older_confirmed_bar_is_stored_without_moving_the_displayed_one(self):
        # The venue can confirm an older bar late. It must land in SQLite — it is
        # real data — but the bar the page shows stays the newest confirmed one.
        service = self.make_service()
        older = NOW - 900_000
        seen: list[dict] = []
        service.on_closed_candle(seen.append)

        self.deliver(service, kline_frame(start=NOW, close=101.0))
        self.deliver(service, kline_frame(start=older, close=94.0))

        self.assertEqual(service.snapshot(SYMBOL, "15m")["candle"]["ts"], NOW)
        rows = self.db.load_candles("bybit", SYMBOL, "15m", limit=10)
        self.assertEqual([row["ts"] for row in rows], [older, NOW])
        # Both are real closes and both are announced once.
        self.assertEqual(sorted(event["open_ts"] for event in seen), [older, NOW])
        self.assertEqual(service.closed_events, 2)
        self.assertEqual(service.duplicate_closed, 0)

    def test_a_delta_frame_never_blanks_a_field_it_did_not_carry(self):
        # Verified against the live venue: after the initial `type: snapshot`,
        # Bybit sends `type: delta` frames holding one or two fields. Replacing
        # the quote with those would empty the page's price on every tick.
        service = self.make_service()
        self.deliver(service, ticker_frame(price=100, ts=NOW))
        self.deliver(service, ticker_delta(markPrice=100.5, ts=NOW + 100))

        snapshot = service.snapshot(SYMBOL)["ticker"]
        self.assertEqual(snapshot["last_price"], 100.0, "lastPrice must survive a markPrice-only delta")
        self.assertEqual(snapshot["mark_price"], 100.5)
        self.assertEqual(snapshot["open_interest"], 5.0, "open interest must survive too")
        self.assertEqual(snapshot["funding_rate"], 0.0001)

    def test_a_keepalive_frame_does_not_refresh_the_age(self):
        # A frame with no market fields must not make a frozen quote look fresh.
        service = self.make_service()
        self.deliver(service, ticker_frame(price=100, ts=NOW))
        self.deliver(service, ticker_keepalive(ts=NOW + 30_000))

        snapshot = service.snapshot(SYMBOL)
        self.assertEqual(snapshot["exchangeTs"], NOW)
        self.assertEqual(service._feeds[SYMBOL].keepalives, 1)
        with patch("quantdesk.datahub.market_service._now_ms", return_value=NOW + 10_000_000):
            self.assertTrue(service.snapshot(SYMBOL)["stale"])

    def test_stale_transitions_are_announced_to_local_subscribers(self):
        import asyncio

        service = self.make_service()
        self.deliver(service, ticker_frame(price=100, ts=NOW))

        async def scenario():
            queue: asyncio.Queue[str] = asyncio.Queue(maxsize=50)
            service.subscribe(queue)
            with patch("quantdesk.datahub.market_service._now_ms", return_value=NOW + 10_000_000):
                service.connection_state()
                stale = json.loads(queue.get_nowait())
                service._feeds[SYMBOL].snapshot["received_ts"] = NOW + 10_000_000
                service.connection_state()
                fresh = json.loads(queue.get_nowait())
            return stale, fresh

        stale, fresh = asyncio.run(scenario())
        self.assertEqual((stale["kind"], stale["eventType"], stale["stale"]), ("stale", "QuoteStale", True))
        self.assertEqual((fresh["eventType"], fresh["stale"]), ("QuoteFresh", False))
        self.assertEqual(stale["symbol"], SYMBOL)

    def test_an_older_ticker_never_replaces_a_newer_quote(self):
        service = self.make_service()

        self.deliver(service, ticker_frame(price=100, ts=NOW))
        self.deliver(service, ticker_frame(price=90, ts=NOW - 5_000))

        self.assertEqual(service.mark_price(SYMBOL), 100.0)
        self.assertEqual(service.snapshot(SYMBOL)["ticker"]["last_price"], 100.0)

    def test_newer_ticker_wins_and_is_persisted(self):
        service = self.make_service()

        self.deliver(service, ticker_frame(price=100, ts=NOW))
        self.deliver(service, ticker_frame(price=105, ts=NOW + 1_000))

        self.assertEqual(service.mark_price(SYMBOL), 105.0)
        restored = {row["symbol"]: row for row in self.db.load_market_snapshots("bybit")}
        self.assertEqual(restored[SYMBOL]["last_price"], 105.0)


class RestoreTests(_ServiceCase):
    def test_restart_restores_the_last_real_quote_and_candle(self):
        first = self.make_service()

        self.deliver(first, ticker_frame(price=123.5))
        self.deliver(first, kline_frame())

        # A fresh process, same SQLite file: the page must paint before the
        # venue answers, so both parts come back from disk.
        restarted = MarketDataService(self.home, symbols=("BTCUSDT", "ETHUSDT"), db=Database(self.home / "quantdesk.db"), backfill=False)
        restored = restarted.restore()
        self.assertEqual(restored, 1)
        self.assertEqual(restarted.mark_price(SYMBOL), 123.5)
        snapshot = restarted.snapshot(SYMBOL, "15m")
        self.assertEqual(snapshot["candle"]["close"], 101.0)
        self.assertEqual(snapshot["source"], "websocket")
        self.assertIsInstance(snapshot["ageMs"], int)

    def test_a_restart_does_not_re_alarm_on_the_bar_it_restored(self):
        # The venue re-sends its latest confirmed bar on every subscribe, and the
        # bar restored from SQLite is exactly that bar. It must not be reported
        # as a fresh close after a restart.
        first = self.make_service()
        self.deliver(first, kline_frame())

        restarted = MarketDataService(self.home, symbols=("BTCUSDT", "ETHUSDT"), db=Database(self.home / "quantdesk.db"), backfill=False)
        restarted.restore()
        seen: list[dict] = []
        restarted.on_closed_candle(seen.append)
        deliver(restarted, kline_frame())

        self.assertEqual(seen, [], "a restored bar must not fire again after a restart")
        self.assertEqual(restarted.closed_events, 0)
        self.assertEqual(restarted.duplicate_closed, 1)
        self.assertEqual(len(self.db.load_candles("bybit", SYMBOL, "15m", limit=10)), 1)

    def test_snapshot_reports_staleness_from_the_stored_quote(self):
        service = self.make_service()

        self.deliver(service, ticker_frame())
        fresh = service.snapshot(SYMBOL)
        self.assertFalse(fresh["stale"])
        self.assertEqual(fresh["source"], "websocket")
        self.assertIn(fresh["connection"]["state"], {"stopped", "starting", "connected", "degraded"})

        with patch("quantdesk.datahub.market_service._now_ms", return_value=NOW + 10_000_000):
            stale = service.snapshot(SYMBOL)
        self.assertTrue(stale["stale"], "an old quote must be labelled stale, never refreshed silently")

    def test_a_symbol_outside_the_fixed_pool_is_rejected(self):
        service = self.make_service()
        with self.assertRaises(ValueError):
            service.snapshot("DOGEUSDT")
        with self.assertRaises(KeyError):
            service.snapshot("AAPL")  # in the fixed pool, but outside this service's feeds

    def test_mark_price_never_invents_a_value(self):
        service = self.make_service()
        self.assertIsNone(service.mark_price("ETHUSDT"))
        self.assertIsNone(service.mark_price("DOGEUSDT"))

    def test_unstamped_rest_quote_cannot_replace_a_websocket_quote(self):
        service = self.make_service()
        self.deliver(service, ticker_frame(price=100, ts=NOW))

        unstamped = service._snapshot_from_rest(
            {"symbol": SYMBOL, "lastPrice": "90", "markPrice": "90"}
        )
        self.assertIsNone(unstamped["exchange_ts"])
        service.db.upsert_market_snapshot("bybit", SYMBOL, unstamped)

        stored = service.db.load_market_snapshot("bybit", SYMBOL)
        self.assertEqual(stored["last_price"], 100.0)
        self.deliver(service, ticker_frame(price=105, ts=NOW + 1))
        self.assertEqual(service.mark_price(SYMBOL), 105.0)


class BackfillTests(_ServiceCase):
    def test_backfill_writes_closed_candles_and_the_quote_once(self):
        class FakeClient:
            def __init__(self):
                self.kline_calls = 0
                self.ticker_calls = 0

            def kline_snapshot(self, symbol, interval, limit=None, completed_only=False):
                self.kline_calls += 1
                assert completed_only, "backfill must never take the forming bar"
                return [{"ts": NOW, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 3, "turnover": 4}]

            def ticker(self, category, symbol):
                self.ticker_calls += 1
                return {
                    "symbol": symbol,
                    "lastPrice": "42.5",
                    "markPrice": "42.5",
                    "openInterestValue": "100",
                }

            def close(self):
                return None

        client = FakeClient()
        service = self.make_service(symbols=("BTCUSDT",), client_factory=lambda: client, backfill=True)

        result = asyncio.run(service.backfill())

        self.assertEqual(result["symbols"], 1)
        self.assertEqual(result["rows"], len(TIMEFRAMES))
        self.assertEqual(client.kline_calls, len(TIMEFRAMES))
        self.assertEqual(client.ticker_calls, 1)
        self.assertEqual(service.mark_price("BTCUSDT"), 42.5)
        self.assertEqual(len(self.db.load_candles("bybit", "BTCUSDT", "15m", limit=10)), 1)

        # A second sweep must not duplicate a single row.
        asyncio.run(service.backfill())
        self.assertEqual(len(self.db.load_candles("bybit", "BTCUSDT", "15m", limit=10)), 1)

    def test_backfill_failure_is_recorded_and_does_not_raise(self):
        def broken_client():
            raise OSError("proxy refused connection")

        service = self.make_service(symbols=("BTCUSDT",), client_factory=broken_client, backfill=True)

        result = asyncio.run(service.backfill())
        self.assertEqual(result["symbols"], 1)
        self.assertTrue(result["errors"])
        self.assertIn("proxy refused", result["errors"][0])
        self.assertEqual(service.backfill_errors, result["errors"])

    def test_backfill_is_skipped_when_disabled(self):
        service = self.make_service(backfill=False)

        self.assertEqual(asyncio.run(service.backfill()), {"skipped": True})


class ProxySwitchTests(_ServiceCase):
    def test_switching_nodes_keeps_the_last_real_state_and_rebuilds_the_link(self):
        # A proxy node switch is a normal event: the streams must be rebuilt on
        # the new address while the stored quote and closed bars stay untouched.
        import asyncio

        proxies: list[str | None] = []

        class FakeStreams:
            def __init__(self, sink, proxy=None, **kwargs):
                proxies.append(proxy)
                self.sink = sink
                self.proxy = proxy
                self.started_with: list[str] = []
                self.stopped = False

            async def start(self, topics):
                self.started_with = list(topics)

            async def stop(self):
                self.stopped = True

            @property
            def connected(self):
                return not self.stopped

            reconnects = 0

            def status(self):
                return []

            def last_message_at(self):
                return None

            def last_error(self):
                return None

        service = self.make_service(stream_factory=FakeStreams)

        async def scenario():
            await service.start()
            await service._on_event(venue_event(ticker_frame(price=321.5)))
            await service._on_event(venue_event(kline_frame()))
            before = service.mark_price(SYMBOL)
            await service.set_proxy("http://127.0.0.1:1")
            return before, service.mark_price(SYMBOL), service.snapshot(SYMBOL, "15m")["candle"]

        before, after, candle = asyncio.run(scenario())
        self.assertEqual(before, 321.5)
        self.assertEqual(after, before, "a node switch must not lose the last real quote")
        self.assertEqual(candle["close"], 101.0)
        self.assertEqual(proxies, [None, "http://127.0.0.1:1"])
        self.assertEqual(len(self.db.load_candles("bybit", SYMBOL, "15m", limit=10)), 1)


class FailureContainmentTests(_ServiceCase):
    def test_a_dead_proxy_leaves_the_local_snapshot_readable(self):
        # Restored data is the whole point of the snapshot: a failing upstream
        # must never blank it out or make the endpoint fail.
        service = self.make_service(client_factory=lambda: (_ for _ in ()).throw(OSError("no route")))
        service.restore()
        service._feeds[SYMBOL].snapshot = {
            "venue": "bybit",
            "symbol": SYMBOL,
            "last_price": 99.0,
            "mark_price": 99.0,
            "received_ts": NOW,
            "exchange_ts": NOW,
            "source": "sqlite",
        }

        asyncio.run(service.backfill())
        self.assertEqual(service.mark_price(SYMBOL), 99.0)
        self.assertFalse(service.snapshot(SYMBOL)["stale"])
        self.assertEqual(service.connection_state()["state"], "stopped")

    def test_wait_for_gap_consumes_a_signal_raised_before_it_is_awaited(self):
        # The startup connection sets the flag before the reconcile loop starts;
        # it must not be lost, or the first backfill would wait a full interval.
        service = self.make_service()

        async def scenario():
            service._wake.set()
            await asyncio.wait_for(service.wait_for_gap(), timeout=0.5)
            service._wake.set()
            await asyncio.wait_for(service.wait_for_gap(), timeout=0.5)

        asyncio.run(scenario())


class EventEnvelopeTests(_ServiceCase):
    def test_frames_carry_the_full_envelope(self):
        service = self.make_service()
        closed = rt.parse_message(kline_frame(), received_ts=NOW)
        assert isinstance(closed, rt.CandleEvent)
        frame = json.loads(encode_event(closed))
        self.assertEqual(frame["eventType"], "CandleClosed")
        self.assertEqual(frame["venue"], "bybit")
        self.assertEqual(frame["source"], "websocket")
        self.assertEqual(frame["observationKey"], f"{SYMBOL}:15m:{NOW}")
        self.assertEqual(frame["venueSymbol"], SYMBOL)
        self.assertEqual(frame["interval"], "15m")
        self.assertEqual(frame["receivedTs"], NOW)

        forming = rt.parse_message(kline_frame(confirm=False), received_ts=NOW)
        assert isinstance(forming, rt.CandleEvent)
        self.assertEqual(json.loads(encode_event(forming))["eventType"], "CandleUpdated")

        ticker = rt.parse_message(ticker_frame(), received_ts=NOW)
        assert isinstance(ticker, rt.TickerEvent)
        ticker_frame_payload = json.loads(encode_event(ticker))
        self.assertIn(ticker_frame_payload["eventType"], {"TickerUpdated", "DerivativesUpdated"})
        self.assertEqual(ticker_frame_payload["openInterestValue"], 500.0)
        self.assertEqual(ticker_frame_payload["observationKey"], f"{SYMBOL}:ticker:{NOW}")

        connection = json.loads(encode_event(rt.ConnectionEvent("reconnecting", 2, "boom")))
        self.assertEqual(connection["eventType"], "ConnectionChanged")
        self.assertEqual(connection["state"], "reconnecting")

        backfill = json.loads(backfill_completed_event({"symbols": 2, "rows": 8, "errors": []}))
        self.assertEqual(backfill["eventType"], "BackfillCompleted")
        self.assertEqual(backfill["rows"], 8)

    def test_subscribe_and_unsubscribe_control_delivery(self):
        service = self.make_service()

        async def scenario():
            queue: asyncio.Queue[str] = asyncio.Queue(maxsize=10)
            unsubscribe = service.subscribe(queue)
            await service._on_event(self.feed_event(service, ticker_frame()))
            delivered = queue.get_nowait()
            unsubscribe()
            await service._on_event(self.feed_event(service, ticker_frame(price=101, ts=NOW + 1_000)))
            self.assertEqual(queue.qsize(), 0)
            return json.loads(delivered)

        frame = asyncio.run(scenario())
        self.assertEqual(frame["kind"], "ticker")

    def test_a_full_subscriber_queue_drops_the_oldest_frame(self):
        service = self.make_service()

        async def scenario():
            queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
            service.subscribe(queue)
            for price, ts in ((100, NOW), (101, NOW + 1_000), (102, NOW + 2_000)):
                await service._on_event(self.feed_event(service, ticker_frame(price=price, ts=ts)))
            return json.loads(queue.get_nowait())

        # A slow browser must never stall the venue reader.
        self.assertEqual(asyncio.run(scenario())["lastPrice"], 102.0)


class MarketApiTests(unittest.IsolatedAsyncioTestCase):
    """The local snapshot API and stream, served from the in-process service."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = patch.dict(os.environ, {"QUANTDESK_HOME": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        reset_market_service()
        self.service = get_market_service()
        self.service.symbols = tuple(VENUE_SYMBOLS)
        self.service._feeds = {symbol: type(self.service._feeds[SYMBOL])() for symbol in VENUE_SYMBOLS}
        self.service._backfill_enabled = False

        await self.service._on_event(venue_event(ticker_frame(price=77785.9)))
        await self.service._on_event(venue_event(kline_frame()))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addCleanup(lambda: reset_market_service())

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_snapshot_returns_closed_candle_quote_and_freshness(self):
        response = await self.client.get("/api/market/snapshot", params={"symbol": "BTCUSDT", "interval": "15m"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["symbol"], SYMBOL)
        self.assertEqual(body["displaySymbol"], "BTC")
        self.assertEqual(body["candle"]["close"], 101.0)
        self.assertEqual(body["ticker"]["last_price"], 77785.9)
        self.assertFalse(body["stale"])
        self.assertEqual(body["source"], "websocket")
        self.assertIsInstance(body["ageMs"], int)
        self.assertIn("connection", body)
        self.assertIn("lastSuccessAt", body["connection"])

    async def test_display_symbol_is_mapped_to_the_venue_symbol(self):
        response = await self.client.get("/api/market/snapshot", params={"symbol": "AMD", "interval": "1h"})
        # AMD must resolve to its venue symbol. This feed was never fed, so the
        # reply carries no quote at all rather than an invented one.
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["symbol"], "AMDSTOCKUSDT")
        self.assertEqual(body["displaySymbol"], "AMD")
        self.assertIsNone(body["candle"])
        self.assertIsNone(body["ticker"].get("last_price"))

    async def test_out_of_pool_symbol_and_bad_interval_are_refused(self):
        for params in ({"symbol": "DOGEUSDT"}, {"symbol": "BTCUSDT", "interval": "5m"}, {"symbol": "BTCUSDT", "interval": "1"}):
            response = await self.client.get("/api/market/snapshot", params=params)
            self.assertEqual(response.status_code, 422, params)

    async def test_out_of_pool_symbol_is_validation_error_across_data_routes(self):
        for path in ("/api/data/coverage", "/api/data/replay", "/bybit/v5/market/funding/history"):
            response = await self.client.get(path, params={"symbol": "DOGEUSDT"})
            self.assertEqual(response.status_code, 422, path)
            self.assertIn("固定合约池", response.json()["detail"])

    async def test_state_endpoint_reports_the_link(self):
        response = await self.client.get("/api/market/state")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("connection", body)
        self.assertIn("duplicateClosed", body)
        self.assertEqual(body["intervals"], list(TIMEFRAMES))

    async def test_proxy_failure_does_not_stop_the_api(self):
        # Point the service at a proxy that cannot resolve, then confirm the
        # local read path is unaffected while the failure is recorded.
        self.service.proxy = "http://127.0.0.1:1"
        self.service._client_factory = lambda: (_ for _ in ()).throw(OSError("proxy refused"))
        self.service._backfill_enabled = True
        result = await self.service.backfill()

        self.assertTrue(result["errors"], "a failed sweep must be reported, not swallowed")
        self.assertIn("proxy refused", result["errors"][0])
        # The retained error list is capped on purpose: it is a diagnostic tail,
        # not an unbounded log, and the full per-symbol list is in the response.
        self.assertEqual(self.service.backfill_errors, result["errors"][-10:])
        self.assertEqual(len(self.service.backfill_errors), min(10, len(result["errors"])))
        self.assertEqual(self.service.connection_state()["state"], "stopped")

        response = await self.client.get("/api/market/snapshot", params={"symbol": "BTCUSDT"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ticker"]["last_price"], 77785.9)
        self.assertFalse(response.json()["stale"], "the last real quote is still the current one")


class MarketStreamTests(unittest.IsolatedAsyncioTestCase):
    """`WS /api/market/stream` — the browser's push channel.

    A real uvicorn socket is used rather than an in-process shim, because the
    point of these tests is that the endpoint and the service share one loop:
    a venue event must reach the browser without a poll in between.
    """

    async def asyncSetUp(self):
        import uvicorn

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = patch.dict(os.environ, {"QUANTDESK_HOME": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        reset_market_service()
        self.addCleanup(reset_market_service)
        self.service = get_market_service()
        self.service._backfill_enabled = False
        self.service.symbols = tuple(VENUE_SYMBOLS)
        self.service._feeds = {symbol: _Feed() for symbol in VENUE_SYMBOLS}

        self.port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error", lifespan="off")
        self.server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(self.server.serve())
        for _ in range(200):
            if self.server.started:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(self.server.started, "the test gateway did not start")

    async def asyncTearDown(self):
        self.server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(self.server_task, timeout=10)

    def url(self, path: str) -> str:
        return f"ws://127.0.0.1:{self.port}{path}"

    async def test_stream_announces_connection_then_pushes_events(self):
        import websockets

        async with websockets.connect(self.url("/api/market/stream"), proxy=None) as socket:
            first = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
            self.assertEqual(first["kind"], "connection")
            self.assertIn(first["state"], {"stopped", "starting", "connected", "degraded"})

            await self.service._on_event(venue_event(ticker_frame(price=50.0)))
            pushed = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
            self.assertEqual(pushed["kind"], "ticker")
            self.assertEqual(pushed["venueSymbol"], SYMBOL)
            self.assertEqual(pushed["lastPrice"], 50.0)

    async def test_a_browser_cannot_widen_the_subscription(self):
        # The socket carries normalized frames only; the venue subscription is
        # fixed at start, so nothing a browser sends can add a symbol.
        import websockets

        async with websockets.connect(self.url("/api/market/stream"), proxy=None) as socket:
            await asyncio.wait_for(socket.recv(), timeout=5)
            await socket.send(json.dumps({"op": "subscribe", "args": ["kline.1.DOGEUSDT"]}))
            await asyncio.sleep(0.2)
            self.assertEqual(set(self.service.symbols), set(VENUE_SYMBOLS))
            self.assertEqual(self.service.connection_state()["reconnects"], 0)

    async def test_closing_the_browser_releases_its_subscription(self):
        import websockets

        async with websockets.connect(self.url("/api/market/stream"), proxy=None) as socket:
            await asyncio.wait_for(socket.recv(), timeout=5)
            self.assertEqual(len(self.service._subscribers), 1)
        for _ in range(200):
            if not self.service._subscribers:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(len(self.service._subscribers), 0, "a closed tab must not leave a subscription behind")

    async def test_stream_is_registered_before_the_spa_catch_all(self):
        paths = [getattr(route, "path", "") for route in app.routes]
        self.assertIn("/api/market/stream", paths)


class ServerWiringTests(unittest.IsolatedAsyncioTestCase):
    """Alert and paper wiring: closed bars trigger exactly the right consumers."""

    async def test_alert_evaluation_is_scoped_to_the_symbol_and_interval(self):
        calls: list[str] = []

        class FakeEngine:
            def list_rules(self):
                return [
                    {"enabled": True, "venueSymbol": "BTCUSDT", "conditions": [{"timeframe": "15m"}]},
                    {"enabled": False, "venueSymbol": "BTCUSDT", "conditions": [{"timeframe": "15m"}]},
                ]

            def evaluate_symbol(self, symbol):
                calls.append(symbol)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"QUANTDESK_HOME": tmp}):
                with patch.object(server, "AlertEngine", return_value=FakeEngine()):
                    server._evaluate_alerts_for_closed_candle("BTCUSDT", "15m")
                    self.assertEqual(calls, ["BTCUSDT"])

                    calls.clear()
                    server._evaluate_alerts_for_closed_candle("BTCUSDT", "4h")
                    self.assertEqual(calls, [], "a bar no rule depends on must not sweep the book")

                    calls.clear()
                    server._evaluate_alerts_for_closed_candle("ETHUSDT", "15m")
                    self.assertEqual(calls, [], "another symbol's rules are not this symbol's business")

    async def test_closed_candle_handler_ignores_incomplete_events(self):
        with patch.object(server, "_evaluate_alerts_for_closed_candle") as evaluate:
            await server._on_closed_candle({})
            await server._on_closed_candle({"symbol": "BTCUSDT"})
            evaluate.assert_not_called()

    async def test_a_rule_without_any_timeframe_is_still_woken(self):
        # Funding, open interest and resonance conditions carry no timeframe, so a
        # rule built only from them has no bar of its own. It must still be
        # evaluated instead of silently never firing.
        calls: list[str] = []

        class FakeEngine:
            def list_rules(self):
                return [{"enabled": True, "venueSymbol": "BTCUSDT", "conditions": [{"condition_type": "funding_above", "timeframe": None}]}]

            def evaluate_symbol(self, symbol):
                calls.append(symbol)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"QUANTDESK_HOME": tmp}):
                with patch.object(server, "AlertEngine", return_value=FakeEngine()):
                    server._evaluate_alerts_for_closed_candle("BTCUSDT", TIMEFRAMES[0])
                    self.assertEqual(calls, ["BTCUSDT"])

                    calls.clear()
                    server._evaluate_alerts_for_closed_candle("BTCUSDT", TIMEFRAMES[-1])
                    self.assertEqual(calls, [], "the anchor frame is the only wakeup for a frame-less rule")

    async def test_health_endpoint_reports_the_market_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"QUANTDESK_HOME": tmp}):
                reset_market_service()
                self.addCleanup(reset_market_service)
                client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
                try:
                    health = await client.get("/health")
                    monitoring = await client.get("/api/monitoring")
                finally:
                    await client.aclose()
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()["ok"])
        self.assertIn("instrumentCount", health.json())
        # The link the operator watches lives on the monitoring payload, and it
        # has to describe the in-process service rather than a placeholder.
        self.assertEqual(monitoring.status_code, 200)
        market = monitoring.json()["components"]["marketData"]
        self.assertIn(market["status"], {"running", "stopped"})
        self.assertIn(market["connection"]["state"], {"stopped", "starting", "connected", "degraded"})
        self.assertIn("reconnects", market["connection"])
        self.assertEqual(market["symbols"], len(VENUE_SYMBOLS))


if __name__ == "__main__":
    unittest.main()
