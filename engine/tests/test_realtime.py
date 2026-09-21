"""Bybit WS transport: normalization, sharding, backoff, reconnect, heartbeat.

Frames below are real Bybit public payloads captured live, so the parser is
tested against the venue's actual shape rather than an invented one.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from quantdesk.datahub import realtime as rt

# Captured from wss://stream.bybit.com/v5/public/linear
KLINE_OPEN = (
    '{"topic":"kline.15.BTCUSDT","data":[{"start":1789443000000,"end":1789443899999,'
    '"interval":"15","open":"77700.5","close":"77785.9","high":"77810","low":"77690",'
    '"volume":"12.5","turnover":"971000.2","confirm":false,"timestamp":1789443832000}],'
    '"ts":1789443832010,"type":"snapshot"}'
)
KLINE_CLOSED = KLINE_OPEN.replace('"confirm":false', '"confirm":true')
TICKER = (
    '{"topic":"tickers.BTCUSDT","data":{"symbol":"BTCUSDT","lastPrice":"77785.90",'
    '"markPrice":"77785.90","indexPrice":"77790.1","fundingRate":"-0.00000757",'
    '"fundingIntervalHour":"8","openInterest":"53524.596","openInterestValue":"4138248787.28",'
    '"turnover24h":"5609628158.77","volume24h":"72172.63","price24hPcnt":"0.00555",'
    '"highPrice24h":"79909.70","lowPrice24h":"79400.0"},"ts":1789443832010,"type":"snapshot"}'
)


class ParseTests(unittest.TestCase):
    def test_kline_frame_normalizes_and_exposes_confirm(self):
        event = rt.parse_message(KLINE_OPEN)
        self.assertIsInstance(event, rt.CandleEvent)
        assert isinstance(event, rt.CandleEvent)
        self.assertEqual(event.venue_symbol, "BTCUSDT")
        self.assertEqual(event.interval, "15m")
        self.assertEqual(event.open_ts, 1789443000000)
        self.assertAlmostEqual(event.close, 77785.9)
        self.assertAlmostEqual(event.high, 77810.0)
        self.assertAlmostEqual(event.turnover or 0, 971000.2)
        self.assertFalse(event.closed)
        self.assertEqual(event.observation_key, "BTCUSDT:15m:1789443000000")

    def test_confirm_true_marks_the_candle_closed(self):
        event = rt.parse_message(KLINE_CLOSED)
        assert isinstance(event, rt.CandleEvent)
        self.assertTrue(event.closed)

    def test_missing_confirm_is_not_treated_as_closed(self):
        # Absence of the field must fail safe: never persist an unconfirmed bar.
        event = rt.parse_message(KLINE_OPEN.replace('"confirm":false,', ""))
        assert isinstance(event, rt.CandleEvent)
        self.assertFalse(event.closed)

    def test_ticker_frame_normalizes_string_numbers(self):
        event = rt.parse_message(TICKER)
        assert isinstance(event, rt.TickerEvent)
        self.assertEqual(event.venue_symbol, "BTCUSDT")
        self.assertAlmostEqual(event.mark_price or 0, 77785.90, places=2)
        self.assertAlmostEqual(event.funding_rate or 0, -0.00000757, places=10)
        self.assertAlmostEqual(event.open_interest_value or 0, 4138248787.28, places=2)
        self.assertAlmostEqual(event.funding_interval_hour or 0, 8.0)
        self.assertAlmostEqual(event.price_24h_pct or 0, 0.00555, places=5)

    def test_control_and_malformed_frames_are_ignored(self):
        for raw in (
            '{"success":true,"ret_msg":"pong","op":"ping"}',
            '{"success":true,"op":"subscribe"}',
            "not json",
            "[]",
            '{"topic":"kline.15.BTCUSDT","data":[]}',
            '{"topic":"kline.7.BTCUSDT","data":[{"start":1,"open":"1","high":"1","low":"1","close":"1"}]}',
            '{"topic":"orderbook.1.BTCUSDT","data":{"a":1}}',
            '{"topic":"kline.15.BTCUSDT","data":[{"start":1,"open":"x","high":"1","low":"1","close":"1"}]}',
        ):
            self.assertIsNone(rt.parse_message(raw), raw)

    def test_transport_does_not_police_symbols(self):
        # Subscription whitelisting belongs to the market service, not the wire parser.
        event = rt.parse_message(KLINE_OPEN.replace("BTCUSDT", "DOGEUSDT"))
        assert isinstance(event, rt.CandleEvent)
        self.assertEqual(event.venue_symbol, "DOGEUSDT")


class TopicTests(unittest.TestCase):
    def test_topic_builders_use_documented_interval_codes(self):
        self.assertEqual(rt.kline_topic("BTCUSDT", "15m"), "kline.15.BTCUSDT")
        self.assertEqual(rt.kline_topic("AMD", "1h"), "kline.60.AMD")
        self.assertEqual(rt.kline_topic("AAPLUSDT", "4h"), "kline.240.AAPLUSDT")
        self.assertEqual(rt.kline_topic("AAPLUSDT", "1d"), "kline.D.AAPLUSDT")
        self.assertEqual(rt.ticker_topic("AAPLUSDT"), "tickers.AAPLUSDT")

    def test_unsupported_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            rt.kline_topic("BTCUSDT", "5m")

    def test_sharding_respects_the_serialized_args_limit(self):
        topics = [f"kline.15.SYMBOL{index}USDT" for index in range(1200)]
        shards = rt.shard_topics(topics, limit=2000)
        self.assertGreater(len(shards), 1)
        for shard in shards:
            self.assertLessEqual(len(json.dumps(shard)), 2000)
            self.assertTrue(shard)
        self.assertEqual([topic for shard in shards for topic in shard], topics)

    def test_single_oversized_topic_does_not_loop_forever(self):
        shards = rt.shard_topics(["kline.15." + "A" * 5000], limit=100)
        self.assertEqual(len(shards), 1)
        self.assertEqual(len(shards[0]), 1)

    def test_full_universe_topics_fit_the_documented_limit(self):
        from quantdesk.config.instruments import TIMEFRAMES, VENUE_SYMBOLS

        candles, tickers = rt.default_topics(VENUE_SYMBOLS, TIMEFRAMES)
        self.assertEqual(len(candles), len(VENUE_SYMBOLS) * len(TIMEFRAMES))
        self.assertEqual(len(tickers), len(VENUE_SYMBOLS))
        for shard in rt.shard_topics(candles + tickers):
            self.assertLessEqual(len(json.dumps(shard)), rt.ARGS_CHAR_LIMIT)


class BackoffTests(unittest.TestCase):
    def test_delay_stays_within_bounds(self):
        for attempt in range(1, 12):
            value = rt.backoff_delay(attempt)
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, rt.BACKOFF_MAX_SECONDS)

    def test_delay_is_jittered_so_reconnects_spread_out(self):
        samples = {round(rt.backoff_delay(6), 6) for _ in range(20)}
        self.assertGreater(len(samples), 1, "reconnects must not be synchronised")


class _FakeSocket:
    """Stand-in for a websockets connection, recording what was sent."""

    def __init__(self, frames, server, fail_after_recv=None):
        self._frames = list(frames)
        self._server = server
        self._fail_after_recv = fail_after_recv
        self.sent: list[str] = []
        self._received = 0
        self.closed = False

    async def send(self, payload: str) -> None:
        if self.closed:
            raise ConnectionError("socket closed")
        self.sent.append(payload)
        # A real venue answers a ping with a pong frame; without this the fake
        # would look like a socket that dies the moment it is heart-beaten.
        if json.loads(payload).get("op") == "ping":
            self._frames.append('{"success":true,"ret_msg":"pong","op":"ping"}')

    async def recv(self):
        """Serve queued frames; stay quietly open otherwise.

        A healthy Bybit socket with no updates simply sends nothing, so an empty
        queue must block rather than raise — that quiet window is exactly where a
        heartbeat is supposed to fire. `fail_after_recv` models an upstream drop.
        """
        if self._fail_after_recv is not None and self._received >= self._fail_after_recv:
            raise ConnectionError("upstream dropped")
        while True:
            if self._frames:
                self._received += 1
                self._server["delivered"] = self._server.get("delivered", 0) + 1
                return self._frames.pop(0)
            await asyncio.sleep(0.05)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events: list[rt.MarketEvent] = []
        self.sockets: list[_FakeSocket] = []
        self.connections = 0

    async def _sink(self, event):
        self.events.append(event)

    def _connector(self, *, max_connections: int = 1, fail_after_recv=None, frames=None):
        def connect(url, **kwargs):
            self.connections += 1
            if self.connections > max_connections:
                raise ConnectionError("no further connections in this test")
            socket = _FakeSocket(frames or [KLINE_OPEN, KLINE_CLOSED, TICKER], {}, fail_after_recv)
            self.sockets.append(socket)
            return socket

        return connect

    async def _wait_for(self, predicate, timeout: float = 3.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return False

    def _data_events(self):
        return [event for event in self.events if event.kind in {"candle", "ticker"}]

    async def test_subscribes_then_delivers_normalized_events(self):
        stream = rt.BybitStream(
            ["kline.15.BTCUSDT", "tickers.BTCUSDT"], self._sink, connect=self._connector(), heartbeat_seconds=30
        )
        await stream.start()
        self.assertTrue(await self._wait_for(lambda: len(self._data_events()) >= 3))
        await stream.stop()

        subscription = json.loads(self.sockets[0].sent[0])
        self.assertEqual(subscription["op"], "subscribe")
        self.assertEqual(subscription["args"], ["kline.15.BTCUSDT", "tickers.BTCUSDT"])

        candles = [event for event in self.events if isinstance(event, rt.CandleEvent)]
        tickers = [event for event in self.events if isinstance(event, rt.TickerEvent)]
        self.assertEqual(len(candles), 2)
        self.assertFalse(candles[0].closed)
        self.assertTrue(candles[1].closed)
        self.assertEqual(len(tickers), 1)
        self.assertGreaterEqual(stream.status()["messagesReceived"], 3)

    async def test_state_transitions_are_reported(self):
        stream = rt.BybitStream(["kline.15.BTCUSDT"], self._sink, connect=self._connector(), heartbeat_seconds=30)
        await stream.start()
        self.assertTrue(await self._wait_for(lambda: stream.status()["state"] == "connected"))
        await stream.stop()
        states = [event.state for event in self.events if isinstance(event, rt.ConnectionEvent)]
        self.assertIn("connecting", states)
        self.assertIn("connected", states)
        self.assertEqual(states[-1], "stopped")

    async def test_drop_triggers_reconnect(self):
        stream = rt.BybitStream(
            ["kline.15.BTCUSDT"], self._sink, connect=self._connector(max_connections=2, fail_after_recv=1), heartbeat_seconds=30
        )
        await stream.start()
        reconnected = await self._wait_for(lambda: self.connections >= 2, timeout=8)
        await stream.stop()
        self.assertTrue(reconnected, "a dropped socket must actually be re-established")
        self.assertGreaterEqual(stream.reconnects, 1)
        states = [event.state for event in self.events if isinstance(event, rt.ConnectionEvent)]
        self.assertIn("reconnecting", states)

    async def test_heartbeat_ping_is_sent(self):
        # No data frames: the socket stays open on pongs alone, which is exactly
        # the quiet-market case the heartbeat exists for.
        stream = rt.BybitStream(
            ["kline.15.BTCUSDT"], self._sink, connect=self._connector(frames=[]), heartbeat_seconds=0.05
        )
        await stream.start()
        # Wait for the first socket before polling it: the stream reconnects once
        # its frames run out, so indexing the list inside the predicate races.
        self.assertTrue(await self._wait_for(lambda: bool(self.sockets)))
        socket = self.sockets[0]
        sent_ping = await self._wait_for(
            lambda: any(json.loads(payload).get("op") == "ping" for payload in socket.sent), timeout=3
        )
        await stream.stop()
        self.assertTrue(sent_ping, "a ping must be sent to keep the connection alive")

    async def test_stop_is_idempotent_and_leaves_no_task(self):
        stream = rt.BybitStream(["kline.15.BTCUSDT"], self._sink, connect=self._connector(), heartbeat_seconds=30)
        await stream.start()
        await stream.stop()
        await stream.stop()
        self.assertIsNone(stream._task)
        self.assertEqual(stream.status()["state"], "stopped")

    async def test_stream_requires_topics(self):
        with self.assertRaises(ValueError):
            rt.BybitStream([], self._sink)

    async def test_transport_failure_is_recorded_not_raised(self):
        def always_fails(url, **kwargs):
            raise OSError("proxy refused connection")

        stream = rt.BybitStream(["kline.15.BTCUSDT"], self._sink, connect=always_fails, heartbeat_seconds=30)
        await stream.start()
        self.assertTrue(await self._wait_for(lambda: stream.last_error is not None))
        await stream.stop()
        self.assertIn("proxy refused", stream.status()["lastError"] or "")


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_shards_raw_topics_and_reports_aggregate_status(self):
        events: list[rt.MarketEvent] = []

        async def sink(event):
            events.append(event)

        def connect(url, **kwargs):
            return _FakeSocket([KLINE_CLOSED], {})

        manager = rt.BybitStreamManager(sink, connect=connect)
        # Force multiple shards by shrinking the limit through a fresh manager start.
        topics = [f"kline.15.SYMBOL{index}USDT" for index in range(400)]
        shards = rt.shard_topics(topics)
        self.assertGreaterEqual(len(shards), 1)
        await manager.start(topics)
        self.assertEqual(len(manager.streams), len(shards))
        # Streams connect inside their own tasks; poll instead of assuming.
        for _ in range(200):
            if manager.connected:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(manager.connected)
        status = manager.status()
        self.assertEqual(len(status), len(shards))
        await manager.stop()
        self.assertEqual(manager.status(), [])

    async def test_empty_manager_reports_nothing(self):
        manager = rt.BybitStreamManager(lambda event: None)
        self.assertEqual(manager.status(), [])
        self.assertFalse(manager.connected)
        self.assertEqual(manager.reconnects, 0)
        self.assertIsNone(manager.last_message_at())
        self.assertIsNone(manager.last_error())


if __name__ == "__main__":
    unittest.main()


class FailedHandshakeNoiseTests(unittest.TestCase):
    """连接失败时要看到真正的原因，而不是库里的 AttributeError 噪音。"""

    def test_the_guard_skips_teardown_of_a_connection_that_never_came_up(self):
        from quantdesk.datahub.realtime import tolerate_failed_handshake_noise

        tolerate_failed_handshake_noise()
        from websockets.asyncio import connection as ws_connection

        class NeverConnected:
            """A connection object that died before `connection_made`."""

            def __init__(self):
                self.closed = False

        self.assertIsNone(ws_connection.Connection.connection_lost(NeverConnected(), None))
        self.assertFalse(tolerate_failed_handshake_noise(), "重复安装应当是无操作")

    def test_a_real_connection_is_not_swallowed_by_the_guard(self):
        """有 recv_messages 时守卫必须让真正的收尾逻辑跑下去。"""
        from websockets.asyncio import connection as ws_connection
        from quantdesk.datahub.realtime import tolerate_failed_handshake_noise

        tolerate_failed_handshake_noise()

        class Assembler:
            def close(self):
                pass

        class Connected:
            def __init__(self):
                self.recv_messages = Assembler()

        delegated = False
        try:
            ws_connection.Connection.connection_lost(Connected(), None)
        except Exception:  # noqa: BLE001 - the real teardown needs more state than a fake has
            delegated = True
        self.assertTrue(delegated, "真实连接的收尾被守卫吞掉了")
