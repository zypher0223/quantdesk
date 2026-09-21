"""End-to-end closed-candle pipeline over the real local WebSocket endpoint.

A fake Bybit transport replays captured frames through the real market service,
and the test reads the browser stream from a real uvicorn socket. What is covered
is the path from a venue frame to a persisted, once-only closed candle and the
delta the browser receives. The alert hook that the FastAPI lifespan registers is
not part of this test: uvicorn runs here with the lifespan disabled, and that
wiring has its own coverage in `test_market_service.ServerWiringTests`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

import uvicorn
import websockets

from quantdesk.api.server import app
from quantdesk.datahub import realtime as rt
from quantdesk.datahub.market_service import _Feed, get_market_service, reset_market_service
from tests.test_market_service import kline_frame

NOW = 1_800_000_000_000


class _ReplaySocket:
    """A venue socket that serves scripted frames and records subscriptions."""

    def __init__(self, frames: list[str]):
        self._frames = list(frames)
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)
        if json.loads(payload).get("op") == "ping":
            self._frames.append('{"success":true,"ret_msg":"pong","op":"ping"}')

    async def recv(self):
        while True:
            if self._frames:
                return self._frames.pop(0)
            await asyncio.sleep(0.02)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class ClosedCandlePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = patch.dict(os.environ, {"QUANTDESK_HOME": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        reset_market_service()
        self.addCleanup(reset_market_service)

        self.frames: list[str] = []
        self.sockets: list[_ReplaySocket] = []

        def connect(url, **kwargs):
            socket = _ReplaySocket(self.frames)
            self.sockets.append(socket)
            return socket

        self.service = get_market_service()
        self.service._backfill_enabled = False
        self.service._stream_factory = lambda sink, proxy=None: rt.BybitStreamManager(sink, proxy=proxy, connect=connect)
        self.service.symbols = ("BTCUSDT",)
        self.service.intervals = ("15m",)
        self.service._feeds = {"BTCUSDT": _Feed()}

        self.evaluated: list[tuple[str, str]] = []

        async def record_closed(event: dict) -> None:
            self.evaluated.append((event["symbol"], event["interval"]))

        # The handler under test is the one the service itself calls; register it
        # the same way a consumer would.
        self.service.on_closed_candle(record_closed)

        self.port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error", lifespan="off")
        self.server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(self.server.serve())
        for _ in range(250):
            if self.server.started:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(self.server.started)

        await self.service.start()
        for _ in range(250):
            if self.service.connection_state()["connected"]:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(self.service.connection_state()["connected"], "the replay stream did not connect")

    async def asyncTearDown(self):
        await self.service.stop()
        self.server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(self.server_task, timeout=10)

    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/market/stream"

    async def _push(self, frame: str) -> None:
        """Deliver a venue frame through the replay socket the service reads."""
        # The open socket holds its own copy of the queue, so the frame goes
        # straight into it rather than into a list captured at construction.
        self.assertTrue(self.sockets, "the replay transport never connected")
        target = self.sockets[-1]
        target._frames.append(frame)
        for _ in range(250):
            await asyncio.sleep(0.02)
            if frame not in target._frames:
                return
        self.fail("the replay frame was not consumed by the stream")

    async def _drain(self, socket, seconds: float) -> list[dict]:
        """Collect every frame that arrives inside a short window."""
        frames: list[dict] = []
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=max(0.05, deadline - time.perf_counter()))
            except asyncio.TimeoutError:
                break
            frames.append(json.loads(raw))
        return frames

    async def test_a_closed_bar_reaches_the_browser_once_and_only_once(self):
        closed = kline_frame(start=NOW, close=101.0, confirm=True)
        forming = kline_frame(start=NOW + 900_000, close=102.0, confirm=False)

        async with websockets.connect(self.url(), proxy=None) as socket:
            hello = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
            self.assertEqual(hello["kind"], "connection")

            started = time.perf_counter()
            await self._push(forming)
            unconfirmed = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
            self.assertEqual(unconfirmed["kind"], "candle")
            self.assertFalse(unconfirmed["closed"], "a forming bar must be marked as such on the wire")
            self.assertEqual(unconfirmed["eventType"], "CandleUpdated")

            await self._push(closed)
            confirmed = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
            latency_ms = (time.perf_counter() - started) * 1000
            self.assertEqual(confirmed["eventType"], "CandleClosed")
            self.assertTrue(confirmed["closed"])
            self.assertEqual(confirmed["observationKey"], f"BTCUSDT:15m:{NOW}")
            self.assertEqual(confirmed["source"], "websocket")
            self.assertLess(latency_ms, 15_000)

            # Replaying the same close, as a reconnect or a REST sweep would.
            # Nothing at all may arrive after it: not a second CandleClosed, and
            # not a duplicate ticker frame either.
            await self._push(closed)
            replayed = await self._drain(socket, 1.0)
            self.assertEqual(
                [(frame.get("kind"), frame.get("eventType")) for frame in replayed],
                [],
                "a replayed close must not be re-published to the browser",
            )

        self.assertEqual(self.evaluated, [("BTCUSDT", "15m")], "the closed bar must be evaluated exactly once")
        self.assertEqual(self.service.closed_events, 1)
        self.assertEqual(self.service.duplicate_closed, 1)
        rows = self.service.db.load_candles("bybit", "BTCUSDT", "15m", limit=10)
        self.assertEqual(len(rows), 1)

    async def test_the_forming_bar_never_becomes_a_closed_row(self):
        async with websockets.connect(self.url(), proxy=None) as socket:
            await asyncio.wait_for(socket.recv(), timeout=5)
            await self._push(kline_frame(start=NOW, close=101.0, confirm=False))
            await asyncio.wait_for(socket.recv(), timeout=5)
        self.assertEqual(self.service.db.load_candles("bybit", "BTCUSDT", "15m", limit=10), [])
        self.assertEqual(self.evaluated, [])
        self.assertIsNotNone(self.service.snapshot("BTCUSDT", "15m")["formingCandle"])


if __name__ == "__main__":
    unittest.main()


class WeeklyIntervalTests(unittest.TestCase):
    """The weekly series: fetchable, storable, analysable - and not a signal gate."""

    def test_the_venue_maps_the_canonical_interval_to_its_code(self):
        from quantdesk.config.instruments import BYBIT_INTERVALS, INTERVAL_MS, TIMEFRAMES
        from quantdesk.datahub.bybit import INTERVAL_MAP

        self.assertIn("1w", TIMEFRAMES, "周线是引擎的能力之一")
        self.assertEqual(BYBIT_INTERVALS["1w"], "W")
        self.assertEqual(INTERVAL_MAP["1w"], "W")
        self.assertEqual(INTERVAL_MS["1w"], 604_800_000)

    def test_the_backtest_step_lookup_is_not_a_second_hard_coded_table(self):
        """A new interval must not need an edit in the venue client as well.

        The first weekly fetch failed with `KeyError: 'W'` because `bybit.py` kept its
        own code->milliseconds dict next to the canonical one.
        """
        import inspect

        from quantdesk.datahub import bybit

        source = inspect.getsource(bybit)
        self.assertNotIn('{"15": 900_000', source, "周期步长只能有一个来源")
        self.assertIn("INTERVAL_MS[interval]", source)

    def test_signal_eligibility_and_resonance_use_the_core_timeframes(self):
        from quantdesk.config.instruments import CORE_TIMEFRAMES, TIMEFRAMES

        self.assertEqual(CORE_TIMEFRAMES, ("15m", "1h", "4h", "1d"))
        self.assertNotIn("1w", CORE_TIMEFRAMES, "周线是分析数据，不是信号前置条件")
        self.assertIn("1w", TIMEFRAMES)

    def test_the_gateway_forwards_the_weekly_code_but_nothing_arbitrary(self):
        from quantdesk.api.server import IntervalCode

        allowed = IntervalCode.__args__
        self.assertIn("W", allowed)
        self.assertEqual(set(allowed), {"15", "60", "240", "D", "W"})
