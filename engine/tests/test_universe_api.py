"""Fixed-universe contracts, pool split, and the engine-computed resonance panel."""

import math
import unittest
from unittest.mock import patch

import httpx
import pandas as pd

from quantdesk.api.server import app
from quantdesk.config.instruments import (
    CRYPTO_SYMBOLS,
    INSTRUMENTS,
    INTERVAL_MS,
    RESONANCE_MIN_BARS,
    STOCK_CLASS_SYMBOLS,
    instrument_payload,
    require_instrument,
)
from quantdesk.features.resonance import (
    THIN_SESSION_MIN_NOTIONAL,
    is_thin_session,
    resonance,
    stance_for_interval,
)
from quantdesk.features.indicators import add_indicators


def make_rows(count: int = 320, step_ms: int = 3_600_000, start: int = 1_700_000_000_000, volume: float = 1_000.0) -> list[dict]:
    rows = []
    price = 100.0
    for index in range(count):
        price *= 1.0005
        rows.append(
            {
                "ts": start + index * step_ms,
                "open": price * 0.999,
                "high": price * 1.002,
                "low": price * 0.998,
                "close": price,
                "volume": volume,
                "trades": None,
            }
        )
    return rows


def make_frame(count: int = 320, volume: float = 1_000.0) -> pd.DataFrame:
    return pd.DataFrame(make_rows(count=count, volume=volume))


class UniverseRegistryTests(unittest.TestCase):
    def test_pool_split_is_15_stock_class_plus_2_crypto(self):
        self.assertEqual(len(STOCK_CLASS_SYMBOLS), 15)
        self.assertEqual(len(CRYPTO_SYMBOLS), 2)
        self.assertEqual(len(INSTRUMENTS), 17)
        self.assertEqual(set(STOCK_CLASS_SYMBOLS) | set(CRYPTO_SYMBOLS), {item.venue_symbol for item in INSTRUMENTS})

    def test_leveraged_etfs_are_not_labelled_stock(self):
        # Bybit reports SOXL/SOXS with symbolType=ETF; the UI must not call them stocks.
        for display in ("SOXL", "SOXS"):
            item = require_instrument(display)
            self.assertEqual(item.product_type, "etf", display)
            self.assertEqual(item.risk_class, "leveraged_etf", display)

        payload = {row["displaySymbol"]: row for row in instrument_payload()["instruments"]}
        self.assertEqual(payload["SOXL"]["productLabel"], "ETF")
        self.assertEqual(payload["SOXL"]["pool"], "stock")
        self.assertEqual(payload["BTC"]["pool"], "crypto")
        self.assertTrue(payload["AMD"]["symbolMapped"])

    def test_every_instrument_declares_a_supported_chart_interval(self):
        payload = instrument_payload()
        timeframes = set(payload["timeframes"])
        for row in payload["instruments"]:
            self.assertIn(row["chartInterval"], timeframes, row["displaySymbol"])
            self.assertIn(row["productType"], {"stock", "etf", "crypto"})

    def test_unknown_symbol_is_rejected(self):
        with self.assertRaises(ValueError):
            require_instrument("DOGEUSDT")


class ThinSessionTests(unittest.TestCase):
    def test_negligible_notional_bar_is_thin(self):
        frame = make_frame(count=40, volume=1_000.0)
        frame.loc[frame.index[-1], "volume"] = 4.0  # ~400 USDT notional
        thin, ratio = is_thin_session(frame)
        self.assertTrue(thin)
        self.assertLess(ratio, 0.1)

    def test_absolute_floor_flags_quiet_bar_even_when_ratio_is_high(self):
        # Whole window is quiet, so the ratio alone would look "normal".
        frame = make_frame(count=40, volume=1.0)  # ~100 USDT notional per bar
        thin, ratio = is_thin_session(frame)
        self.assertTrue(thin)
        self.assertGreater(ratio, 0.5)
        self.assertLess(frame["close"].iloc[-1] * frame["volume"].iloc[-1], THIN_SESSION_MIN_NOTIONAL)

    def test_active_bar_is_not_thin(self):
        frame = make_frame(count=40, volume=1_000.0)
        frame.loc[frame.index[-1], "volume"] = 3_000.0
        thin, _ = is_thin_session(frame)
        self.assertFalse(thin)

    def test_thin_bar_drops_the_volume_vote(self):
        frame = make_frame(count=RESONANCE_MIN_BARS + 10)
        # A genuine expansion: the last bar trades 3x the recent pace.
        frame.loc[frame.index[-1], "volume"] = 3_000.0
        frame.attrs["interval"] = "15m"
        active = stance_for_interval(add_indicators(frame))
        self.assertFalse(active.notes["session_thin"])
        self.assertEqual(active.volume, 1)

        quiet = make_frame(count=RESONANCE_MIN_BARS + 10)
        quiet.loc[quiet.index[-1], "volume"] = 1.0  # off-hours print
        quiet.attrs["interval"] = "15m"
        suppressed = stance_for_interval(add_indicators(quiet))
        self.assertTrue(suppressed.notes["session_thin"])
        self.assertEqual(suppressed.volume, 0)

    def test_resonance_weights_favour_higher_timeframes(self):
        stances = []
        for interval, score in (("15m", 0.0), ("1h", 0.0), ("4h", 0.0), ("1d", 1.0)):
            frame = make_frame(count=RESONANCE_MIN_BARS + 5)
            frame.attrs["interval"] = interval
            stance = stance_for_interval(add_indicators(frame))
            stance.score = score
            stance.stance = "bull" if score > 0 else "neutral"
            stances.append(stance)
        combined = resonance(stances)
        self.assertAlmostEqual(combined["score"], 0.40, places=4)
        self.assertAlmostEqual(combined["score_100"], 70.0, places=1)


class UpstreamFailureTests(unittest.IsolatedAsyncioTestCase):
    """A regional block and a dead proxy must not read the same to the user."""

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        app.state.cache = None

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_geoblock_is_explained_as_such(self):
        def blocked(request):
            return httpx.Response(
                403,
                headers={"x-amz-cf-pop": "LAX54-P7"},
                json={"error": "The Amazon CloudFront distribution is configured to block access from your country"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(blocked)) as upstream:
            app.state.client = upstream
            response = await self.client.get("/bybit/v5/market/tickers", params={"symbol": "AAPLUSDT"})
        self.assertEqual(response.status_code, 403)
        detail = response.json()["detail"]
        self.assertIn("按地区拒绝", detail)
        self.assertIn("LAX54-P7", detail)
        self.assertIn("切换", detail)
        # The user must not be sent looking at their own proxy configuration.
        self.assertNotIn("请检查网络及代理设置", detail)

    async def test_transport_failure_names_the_transport(self):
        def broken(request):
            raise httpx.ConnectError("proxy refused", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as upstream:
            app.state.client = upstream
            response = await self.client.get("/bybit/v5/market/tickers", params={"symbol": "AAPLUSDT"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("连接失败", response.json()["detail"])

    async def test_upstream_500_is_not_reported_as_a_quote_error(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503, text="upstream down"))
        ) as upstream:
            app.state.client = upstream
            response = await self.client.get("/bybit/v5/market/tickers", params={"symbol": "AAPLUSDT"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("HTTP 503", response.json()["detail"])


class PanelEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        app.state.cache = None

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_instruments_endpoint_exposes_pools_and_timeframes(self):
        response = await self.client.get("/api/instruments")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([pool["count"] for pool in body["pools"]], [15, 2])
        # The capability list, not the signal set: `1w` is fetchable and backtestable
        # while `CORE_TIMEFRAMES` stays at four for signal eligibility and resonance.
        self.assertEqual(body["timeframes"], ["15m", "1h", "4h", "1d", "1w"])
        self.assertEqual(len(body["instruments"]), 17)

    async def test_resonance_rejects_unknown_symbol_and_interval(self):
        for params in ({"symbol": "DOGEUSDT"}, {"symbol": "AAPLUSDT", "intervals": "5m"}):
            response = await self.client.get("/api/resonance", params=params)
            self.assertEqual(response.status_code, 422)

    async def test_resonance_combines_engine_stances_and_survives_partial_history(self):
        calls: list[str] = []

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def kline_snapshot(self, symbol, interval, *, limit=300, **kwargs):
                calls.append(interval)
                return make_rows(count=min(limit, 60), step_ms=INTERVAL_MS[interval])

            def configured_instruments(self):
                return {}

            def close(self):
                return None

        from quantdesk.api import panels

        with (
            patch.object(panels, "BybitClient", FakeClient),
            patch.object(panels, "_load_local_intervals", return_value={}),
            patch.object(panels, "get_cache", return_value=panels.TTLCache(ttl=0)),
        ):
            response = await self.client.get("/api/resonance", params={"symbol": "AMD", "bars": RESONANCE_MIN_BARS})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["venueSymbol"], "AMDSTOCKUSDT")
        self.assertEqual(sorted(calls), ["15m", "1d", "1h", "4h"])
        # Only 60 closed bars were available, so every frame must be reported
        # as short of the EMA200/ADX requirement rather than silently scored.
        self.assertEqual(len(body["unavailable"]), 4)
        for entry in body["unavailable"]:
            self.assertEqual(entry["required"], RESONANCE_MIN_BARS)
            self.assertEqual(entry["bars"], 60)

    async def test_resonance_response_is_valid_json_without_nan(self):
        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def kline_snapshot(self, symbol, interval, *, limit=300, **kwargs):
                return make_rows(count=limit, step_ms=INTERVAL_MS[interval])

            def close(self):
                return None

        from quantdesk.api import panels

        with (
            patch.object(panels, "BybitClient", FakeClient),
            patch.object(panels, "_load_local_intervals", return_value={}),
            patch.object(panels, "get_cache", return_value=panels.TTLCache(ttl=0)),
        ):
            response = await self.client.get("/api/resonance", params={"symbol": "BTCUSDT"})
        self.assertEqual(response.status_code, 200)
        for frame in response.json()["timeframes"]:
            for value in (frame["notes"]["adx"], frame["notes"]["rsi"]):
                self.assertFalse(isinstance(value, float) and math.isnan(value))
            self.assertIn(frame["interval"], {"15m", "1h", "4h", "1d"})

    async def test_resonance_prefers_background_candle_cache(self):
        from quantdesk.api import panels

        local = {
            interval: make_rows(count=RESONANCE_MIN_BARS, step_ms=INTERVAL_MS[interval])
            for interval in ("15m", "1h", "4h", "1d")
        }

        class UnexpectedClient:
            def __init__(self, **kwargs):
                raise AssertionError("complete local cache must avoid Bybit")

        with (
            patch.object(panels, "BybitClient", UnexpectedClient),
            patch.object(panels, "_load_local_intervals", return_value=local),
            patch.object(panels, "get_cache", return_value=panels.TTLCache(ttl=0)),
        ):
            response = await self.client.get("/api/resonance", params={"symbol": "BTCUSDT"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dataSources"], {item: "local" for item in local})

    async def test_instruments_info_marks_etf_contracts(self):
        class FakeClient:
            def configured_instruments(self):
                return {
                    "SOXLUSDT": {
                        "status": "Trading",
                        "symbolType": "ETF",
                        "lotSizeFilter": {"minOrderQty": "0.01", "qtyStep": "0.01"},
                        "priceFilter": {"tickSize": "0.01"},
                        "leverageFilter": {"maxLeverage": "10"},
                    }
                }

            def close(self):
                return None

        with patch("quantdesk.api.server._client", return_value=FakeClient()):
            response = await self.client.get("/api/instruments-info", params={"symbol": "SOXL"})
        self.assertEqual(response.status_code, 200)
        row = response.json()["instruments"][0]
        self.assertEqual(row["symbolType"], "ETF")
        self.assertEqual(row["productType"], "etf")
        self.assertTrue(row["trading"])
        self.assertEqual(row["tickSize"], "0.01")


if __name__ == "__main__":
    unittest.main()
