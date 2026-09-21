"""Background market collection and persistent TradingAgents queue."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx

from quantdesk.api.server import app
from quantdesk.datahub.db import Database
from quantdesk.config.settings import load_app_config, write_scheduler_settings
from quantdesk.config.instruments import VENUE_SYMBOLS
from quantdesk.scheduler.service import BackgroundTaskScheduler, MarketCollector
from quantdesk.tradingagents_queue import TradingAgentsJobQueue


class MarketCollectorTests(unittest.TestCase):
    def test_derivatives_only_rotation_skips_candles_but_keeps_history_fresh(self):
        # With the realtime market service running, candles arrive over the
        # WebSocket. Funding and open interest have no realtime channel, so the
        # falling rotation must keep writing those to SQLite.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            scheduler = BackgroundTaskScheduler(home)
            with patch("quantdesk.scheduler.service.BybitClient") as client_type:
                client = client_type.return_value
                client.kline_snapshot.return_value = [
                    {"ts": 1_700_000_000_000, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 2}
                ]
                client.funding_history.return_value = [{"ts": 1_700_000_000_000, "rate": 0.0001}]
                client.open_interest.return_value = [{"ts": 1_700_000_000_000, "oi": 123}]
                asyncio.run(scheduler._collect_derivatives_once())
                client.kline_snapshot.assert_not_called()
            db = Database(home / "quantdesk.db")
            symbol = VENUE_SYMBOLS[0]
            self.assertEqual(db.count_candles("bybit", symbol, "1h"), 0, "candles must come from the stream, not REST")
            self.assertEqual(len(db.load_funding("bybit", symbol)), 1)
            self.assertEqual(len(db.load_open_interest("bybit", symbol)), 1)
            # The next tick must move to the next symbol, not repeat this one.
            with patch("quantdesk.scheduler.service.BybitClient") as client_type:
                client = client_type.return_value
                client.funding_history.return_value = []
                client.open_interest.return_value = []
                asyncio.run(scheduler._collect_derivatives_once())
            self.assertEqual(scheduler._next_symbol, 2)

    def test_one_symbol_persists_candles_funding_and_open_interest(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch("quantdesk.scheduler.service.BybitClient") as client_type:
                client = client_type.return_value
                client.kline_snapshot.return_value = [
                    {"ts": 1_700_000_000_000, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 2}
                ]
                client.funding_history.return_value = [{"ts": 1_700_000_000_000, "rate": 0.0001}]
                client.open_interest.return_value = [{"ts": 1_700_000_000_000, "oi": 123}]
                result = MarketCollector(home).collect_symbol("BTCUSDT", bars=100)
            db = Database(home / "quantdesk.db")
            # The collector persists every timeframe in TIMEFRAMES, which now includes the
            # weekly series: it is kept fresh by the scheduler even though it is not
            # streamed live and not part of the default backfill matrix.
            self.assertEqual(result["candles"], {"15m": 1, "1h": 1, "4h": 1, "1d": 1, "1w": 1})
            self.assertEqual(db.count_candles("bybit", "BTCUSDT", "1h"), 1)
            self.assertEqual(len(db.load_funding("bybit", "BTCUSDT")), 1)
            self.assertEqual(len(db.load_open_interest("bybit", "BTCUSDT")), 1)

    def test_scheduler_evaluates_alerts_after_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            scheduler = BackgroundTaskScheduler(Path(tmp))
            collected = {
                "symbol": "BTCUSDT", "candles": {"15m": 1, "1h": 1, "4h": 1, "1d": 1},
                "funding": 1, "openInterest": 1, "collectedAt": 1,
            }
            alert_result = {
                "symbol": "BTCUSDT", "evaluated": 2, "triggered": [{"id": "event"}],
                "unavailable": [], "evaluatedAt": 2,
            }
            with patch.object(scheduler.collector, "collect_symbol", return_value=collected), patch.object(
                scheduler.alerts, "evaluate_symbol", return_value=alert_result
            ):
                result = asyncio.run(scheduler.run_market_once("BTCUSDT"))
            self.assertEqual(result["alerts"], {"evaluated": 2, "triggered": 1, "unavailable": 0, "blocked": 0})
            self.assertEqual(scheduler.state["alerts"]["lastResult"]["triggered"], 1)

    def test_scheduler_settings_round_trip_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_scheduler_settings(
                {
                    "market_collection_enabled": False,
                    "market_symbol_interval_sec": 45,
                    "market_backfill_bars": 250,
                    "daily_ta_enabled": False,
                    "daily_ta_time": "20:15",
                    "daily_ta_symbols": ["BTCUSDT"],
                },
                home,
            )
            config = load_app_config(home).scheduler
            status = BackgroundTaskScheduler(home).status()
            self.assertEqual(config["market_symbol_interval_sec"], 45)
            self.assertFalse(status["config"]["marketCollectionEnabled"])
            self.assertEqual(status["config"]["dailyTradingAgentsSymbols"], ["BTCUSDT"])


class TradingAgentsQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        Path(self.home, "llm.toml").write_text(
            '[profiles.test]\nprovider="deepseek"\napi_key_env="TEST_TA_QUEUE_KEY"\n'
            'deep_model="deep"\nquick_model="quick"\n\n[roles]\ntradingagents="test"\n',
            encoding="utf-8",
        )
        # The job path also reads the process home (notifications, governance
        # ledger, database). Pointing it at this temporary directory keeps the
        # test off the operator's live database, where a write lock held by a
        # running gateway turns into a stall and a mysterious failure.
        self.env = patch.dict(
            os.environ,
            {"TEST_TA_QUEUE_KEY": "test-key", "QUANTDESK_HOME": self.tmp.name},
            clear=False,
        )
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def test_worker_executes_archives_and_persists_result(self):
        queue = TradingAgentsJobQueue(self.home)
        fake = {
            "ok": True, "symbol": "BTC-USD", "trade_date": date.today().isoformat(), "asset_type": "crypto",
            "analysts": ["market"], "rating": "Hold", "reports": {"market_report": "ok"},
            "debates": {}, "meta": {"duration_seconds": 1}, "venue_symbol": "BTCUSDT",
            "display_symbol": "BTC", "profile": "test",
        }

        def run(spec, profile, trade_date, *, analysts, timeout_seconds, callbacks, external_evidence=None):
            for callback in callbacks:
                callback.absorb("deepseek-flash", {"cacheMiss": 12_000, "output": 900, "calls": 5})
            return fake

        with patch("quantdesk.tradingagents_runner.run_tradingagents", side_effect=run):
            await queue.start()
            job_id = await queue.enqueue("BTCUSDT", date.today().isoformat())
            for _ in range(50):
                if queue.get(job_id)["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.02)
            job = queue.get(job_id)
            await queue.stop()
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        self.assertEqual(job["result"]["rating"], "Hold")
        self.assertTrue(job["result"]["runId"])

    async def test_a_queued_run_is_budgeted_and_ledgered_like_the_sync_route(self):
        # The research button uses this queue. A job that could run without the
        # budget, the reuse window and the ledger would spend money nothing
        # accounted for - which is how this path started out.
        from quantdesk.llm.governance import AgentCostGovernor, RunBudget

        queue = TradingAgentsJobQueue(self.home)
        fake = {
            "ok": True, "symbol": "BTC-USD", "trade_date": date.today().isoformat(), "asset_type": "crypto",
            "analysts": ["market"], "rating": "Buy", "reports": {"market_report": "ok"},
            "debates": {}, "meta": {}, "venue_symbol": "BTCUSDT", "display_symbol": "BTC", "profile": "test",
        }

        def run(spec, profile, trade_date, *, analysts, timeout_seconds, callbacks, external_evidence=None):
            for callback in callbacks:
                callback.absorb("deepseek-flash", {"cacheMiss": 100_000, "output": 5_000, "calls": 6})
            return fake

        with patch("quantdesk.tradingagents_runner.run_tradingagents", side_effect=run):
            await queue.start()
            job_id = await queue.enqueue("BTCUSDT", date.today().isoformat())
            for _ in range(50):
                if queue.get(job_id)["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.02)
            job = queue.get(job_id)
            await queue.stop()

        result = job["result"]
        self.assertEqual(result["rating"], "Buy")
        self.assertGreater(result["cost"]["usd"], 0, "the job reports what it cost")
        self.assertEqual(result["cost"]["usage"]["deepseek-flash"]["calls"], 6)
        self.assertIsNotNone(result["cost"]["budget"])
        self.assertTrue(result["analystCoverage"]["complete"])
        ledger = AgentCostGovernor(self.home, RunBudget.from_config({})).ledger()
        self.assertEqual(len(ledger), 1, "the job left exactly one ledger row")
        self.assertEqual(ledger[0]["run_id"], result["runId"])
        self.assertGreater(ledger[0]["cost_usd"], 0)
        archived = queue._db().query("SELECT rating, error, meta FROM tradingagents_runs")
        self.assertEqual(len(archived), 1)
        meta = json.loads(archived[0]["meta"])
        self.assertGreater(meta["costUsd"], 0)
        # A stored report has to be checkable on its own: which prompt asked the
        # question, which panel was asked, which data it read, and what it cost.
        self.assertTrue(meta["promptVersion"])
        self.assertEqual(meta["analysts"], ["market"])
        self.assertTrue(meta["configFingerprint"])
        self.assertEqual(meta["analystCoverage"]["covered"], ["market"])
        self.assertEqual(archived[0]["rating"], "Buy")
        self.assertIsNone(archived[0]["error"])

    async def test_queued_job_can_be_cancelled(self):
        queue = TradingAgentsJobQueue(self.home)
        job_id = await queue.enqueue("ETHUSDT", date.today().isoformat())
        job = queue.cancel(job_id)
        self.assertEqual(job["status"], "cancelled")


class SchedulerEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"QUANTDESK_HOME": self.tmp.name}, clear=False)
        self.env.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.env.stop()
        self.tmp.cleanup()

    async def test_settings_endpoint_validates_and_persists(self):
        payload = {
            "marketCollectionEnabled": False,
            "marketSymbolIntervalSec": 60,
            "marketBackfillBars": 300,
            "dailyTradingAgentsEnabled": False,
            "dailyTradingAgentsTime": "21:30",
            "dailyTradingAgentsSymbols": ["BTC", "ETHUSDT"],
        }
        saved = await self.client.put("/api/scheduler/settings", json=payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["config"]["dailyTradingAgentsSymbols"], ["BTCUSDT", "ETHUSDT"])
        status = await self.client.get("/api/scheduler/status")
        self.assertEqual(status.json()["config"]["marketSymbolIntervalSec"], 60)

        invalid = await self.client.put(
            "/api/scheduler/settings", json={**payload, "dailyTradingAgentsTime": "25:99"}
        )
        self.assertEqual(invalid.status_code, 422)
