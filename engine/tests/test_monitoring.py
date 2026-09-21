from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from quantdesk.api.server import app
from quantdesk.datahub.bybit import BybitClient
from quantdesk.datahub.db import Database
from quantdesk.monitoring import DataQualityMonitor


def seed_recent(db: Database, symbol: str = "BTCUSDT") -> None:
    now = int(time.time() * 1000)
    for timeframe, step in {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}.items():
        latest = (now // step) * step - step
        rows = [
            {"ts": latest - (99 - index) * step, "open": 100 + index, "high": 102 + index, "low": 99 + index, "close": 101 + index, "volume": 100 + index}
            for index in range(100)
        ]
        db.upsert_candles("bybit", symbol, timeframe, rows)


class DataQualityTests(unittest.TestCase):
    def test_recent_complete_crypto_data_is_signal_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "quantdesk.db")
            seed_recent(db)
            result = DataQualityMonitor(Path(tmp)).check_symbol("BTC")
        self.assertTrue(result["signalEligible"])
        self.assertTrue(all(frame["status"] == "fresh" for frame in result["frames"]))

    def test_recent_gap_closes_signal_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            db = Database(home / "quantdesk.db")
            seed_recent(db)
            latest = db.load_candles("bybit", "BTCUSDT", "1h", limit=2)[-2]["ts"]
            db.execute("DELETE FROM candles WHERE symbol=? AND interval='1h' AND open_ts=?", ("BTCUSDT", latest))
            result = DataQualityMonitor(home).check_symbol("BTCUSDT")
        self.assertFalse(result["signalEligible"])
        self.assertTrue(next(frame for frame in result["frames"] if frame["timeframe"] == "1h")["recentGap"])

    def test_bybit_retries_and_emits_attempt_telemetry(self):
        events = []
        request = httpx.Request("GET", "https://api.bybit.com/v5/market/kline")
        limited = httpx.Response(429, request=request)
        success = httpx.Response(200, request=request, json={"retCode": 0, "result": {"list": []}})
        client = BybitClient(telemetry=events.append)
        client._client = MagicMock()
        client._client.get.side_effect = [limited, success]
        with patch("quantdesk.datahub.bybit.time.sleep"):
            result = client._get("/v5/market/kline", {"symbol": "BTCUSDT"})
        self.assertEqual(result, {"list": []})
        self.assertEqual([event["http_status"] for event in events], [429, 200])
        self.assertEqual(events[-1]["attempt"], 2)

    def test_ticker_keeps_the_exchange_timestamp_from_the_response_envelope(self):
        request = httpx.Request("GET", "https://api.bybit.com/v5/market/tickers")
        response = httpx.Response(
            200,
            request=request,
            json={
                "retCode": 0,
                "time": 1_800_000_000_123,
                "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "100"}]},
            },
        )
        client = BybitClient()
        client._client = MagicMock()
        client._client.get.return_value = response

        ticker = client.ticker("linear", "BTCUSDT")

        self.assertEqual(ticker["exchangeTs"], 1_800_000_000_123)

    def test_funding_start_time_is_paired_with_an_end_time(self):
        client = BybitClient()
        client._get = MagicMock(return_value={"list": []})
        with patch("quantdesk.datahub.bybit.utc_now_ms", return_value=1_800_000_010_000):
            client.funding_history("BTCUSDT", start_ms=1_800_000_000_000)

        _, params = client._get.call_args.args
        self.assertEqual(params["startTime"], 1_800_000_000_000)
        self.assertEqual(params["endTime"], 1_800_000_010_000)


class MonitoringApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"QUANTDESK_HOME": self.tmp.name}, clear=False)
        self.env.start()
        seed_recent(Database(Path(self.tmp.name) / "quantdesk.db"))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.env.stop()
        self.tmp.cleanup()

    async def test_overview_reports_quality_provider_and_components(self):
        response = await self.client.get("/api/monitoring")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body["instruments"]), 17)
        self.assertTrue(body["instruments"][15]["signalEligible"])
        self.assertIn("marketScheduler", body["components"])
        self.assertIn("rateLimited", body["provider"])


if __name__ == "__main__":
    unittest.main()
