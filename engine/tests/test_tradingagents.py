"""Portable TradingAgents integration without calling models or market APIs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx

from quantdesk.api.server import app
from quantdesk.config.instruments import require_instrument
from quantdesk.config.settings import LLMProfile
from quantdesk.tradingagents_bybit import BybitDailyBridge
from quantdesk.tradingagents_runner import run_tradingagents, target_for


class TargetTests(unittest.TestCase):
    def test_stock_graph_uses_exact_exchange_contract_and_separate_fundamental_symbol(self):
        target = target_for(require_instrument("AMDSTOCKUSDT"))
        self.assertEqual(target.symbol, "AMDSTOCKUSDT")
        self.assertEqual(target.fundamental_symbol, "AMD")
        self.assertEqual(target.asset_type, "stock")
        self.assertIn("fundamentals", target.analysts)

    def test_crypto_uses_crypto_graph_and_hyperliquid_symbol(self):
        target = target_for(require_instrument("BTCUSDT"))
        self.assertEqual(target.symbol, "BTC-USD")
        self.assertEqual(target.asset_type, "crypto")
        self.assertEqual(target.analysts, ("market",))

    def test_spcx_and_skhy_keep_fundamental_analyst_enabled(self):
        spcx = target_for(require_instrument("SPCXUSDT"))
        skhy = target_for(require_instrument("SKHYUSDT"))
        self.assertEqual((spcx.symbol, spcx.fundamental_symbol), ("SPCXUSDT", "SPCX"))
        self.assertEqual((skhy.symbol, skhy.fundamental_symbol), ("SKHYUSDT", "SKHY"))
        self.assertIn("fundamentals", spcx.analysts)
        self.assertIn("fundamentals", skhy.analysts)


class BybitBridgeTests(unittest.TestCase):
    def test_contract_candles_are_normalized_for_tradingagents(self):
        rows = [
            {"ts": 1_757_548_800_000, "open": 100.0, "high": 110.0, "low": 95.0, "close": 108.0, "volume": 42.0},
        ]
        bridge = BybitDailyBridge("SPCXUSDT", "SPCX", "SpaceX")
        with patch("quantdesk.tradingagents_bybit.BybitClient") as client_type:
            client_type.return_value.kline.return_value = rows
            frame = bridge.load_ohlcv("SPCXUSDT", "2025-09-12")
        self.assertEqual(list(frame.columns), ["Date", "Open", "High", "Low", "Close", "Volume"])
        self.assertEqual(frame.iloc[0]["Close"], 108.0)
        args = client_type.return_value.kline.call_args.args
        self.assertEqual(args[:3], ("linear", "SPCXUSDT", "1d"))

    def test_bridge_rejects_another_contract_symbol(self):
        bridge = BybitDailyBridge("SPCXUSDT", "SPCX", "SpaceX")
        with self.assertRaisesRegex(RuntimeError, "拒绝"):
            bridge.load_ohlcv("NVDAUSDT", "2025-09-12")


class RuntimeImportTests(unittest.TestCase):
    """The worker runs inside the TradingAgents venv, not the engine's.

    That interpreter has the graph and its dependencies but none of the engine's,
    so every engine module the worker imports must survive on the standard
    library alone. A module-scope import of an engine-only package breaks the
    stock path at startup and looks like a model failure, not an environment one.
    """

    def _run_without(self, blocked: str, source: str) -> tuple[int, str, str]:
        engine = Path(__file__).resolve().parents[1]
        script = (
            "import sys, importlib.abc\n"
            "class Block(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            f"        if name == {blocked!r}:\n"
            f"            raise ModuleNotFoundError(\"No module named {blocked!r}\")\n"
            "        return None\n"
            "sys.meta_path.insert(0, Block())\n"
            f"{source}\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, check=False,
            env={**os.environ, "PYTHONPATH": str(engine / "src")},
        )
        return completed.returncode, completed.stdout, completed.stderr

    def test_worker_bridges_import_without_the_engine_dependencies(self):
        # tomli-w is the engine's TOML writer: installed in the engine venv and
        # absent from the graph runtime, which is exactly how the stock path died
        # on `import tomli_w` before reaching a single model call. httpx and
        # pandas are shared with the graph, so they are not blocked here.
        for module in ("quantdesk.tradingagents_bybit", "quantdesk.tradingagents_hyperliquid"):
            code, out, err = self._run_without("tomli_w", f"import {module}\nprint('ok')")
            self.assertEqual((code, out.strip()), (0, "ok"), f"{module}: {err[-600:]}")

    def test_saving_config_reports_the_missing_writer_instead_of_crashing(self):
        code, out, err = self._run_without(
            "tomli_w",
            "from quantdesk.config.settings import toml_dumps\n"
            "print('imported')\n"
            "try:\n"
            "    toml_dumps({'a': 1})\n"
            "except RuntimeError as exc:\n"
            "    print('runtime-error', 'tomli-w' in str(exc))\n",
        )
        self.assertEqual(code, 0, err[-600:])
        self.assertEqual(out.split(), ["imported", "runtime-error", "True"])


class RunnerTests(unittest.TestCase):
    def test_runner_invokes_machine_worker_and_passes_key_only_in_environment(self):
        profile = LLMProfile(
            name="deepseek", provider="deepseek", api_key_env="CUSTOM_DEEPSEEK_KEY",
            deep_model="deep-model", quick_model="quick-model", max_tokens=12000,
        )
        output = {
            "ok": True, "symbol": "NVDAUSDT", "asset_type": "stock", "trade_date": "2026-09-13",
            "rating": "Hold", "reports": {"market_report": "ok"}, "debates": {}, "meta": {},
        }
        with (
            patch.dict(os.environ, {"CUSTOM_DEEPSEEK_KEY": "secret-value"}, clear=False),
            patch("quantdesk.tradingagents_runner.subprocess.run") as run,
        ):
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps(output)
            run.return_value.stderr = ""
            result = run_tradingagents(require_instrument("NVDA"), profile, "2026-09-13")
        args, kwargs = run.call_args
        self.assertEqual(args[0][1:], ["-m", "quantdesk.tradingagents_worker"])
        self.assertNotIn("secret-value", " ".join(args[0]))
        self.assertNotIn("secret-value", kwargs["input"])
        self.assertEqual(kwargs["env"]["DEEPSEEK_API_KEY"], "secret-value")
        self.assertEqual(result["venue_symbol"], "NVDAUSDT")


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {"QUANTDESK_HOME": self.tmp.name, "TEST_TA_KEY": "test-key"},
            clear=False,
        )
        self.env.start()
        Path(self.tmp.name, "llm.toml").write_text(
            '[profiles.test]\nprovider="deepseek"\napi_key_env="TEST_TA_KEY"\n'
            'deep_model="deep"\nquick_model="quick"\n\n[roles]\ntradingagents="test"\n',
            encoding="utf-8",
        )
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.env.stop()
        self.tmp.cleanup()

    async def test_readiness_reports_runtime_profile_and_target(self):
        with patch("quantdesk.api.tradingagents.runtime_status", return_value={"ready": True, "commit": "abc"}):
            response = await self.client.get("/api/tradingagents/readiness?symbol=NVDAUSDT")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ready"])
        self.assertEqual(body["target"]["symbol"], "NVDAUSDT")

    async def test_run_returns_real_graph_shape_and_archives_it(self):
        fake = {
            "ok": True, "symbol": "BTC-USD", "trade_date": "2026-09-13", "asset_type": "crypto",
            "analysts": ["market"], "rating": "Hold",
            "reports": {"market_report": "market", "final_trade_decision": "Hold"},
            "debates": {"risk": {"history": "debate"}}, "meta": {"duration_seconds": 1.2},
            "venue_symbol": "BTCUSDT", "display_symbol": "BTC", "profile": "test",
        }
        with patch("quantdesk.tradingagents_runner.run_tradingagents", return_value=fake):
            response = await self.client.post(
                "/api/tradingagents/run", json={"symbol": "BTCUSDT", "tradeDate": "2026-09-13"}
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["runId"])
        listing = await self.client.get("/api/tradingagents/runs")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["runs"][0]["rating"], "Hold")

    async def test_spcx_is_accepted_and_archived(self):
        fake = {
            "ok": True, "symbol": "SPCXUSDT", "trade_date": "2026-09-13", "asset_type": "stock",
            "analysts": ["market", "social", "news", "fundamentals"], "rating": "Hold",
            "reports": {"fundamentals_report": "fundamentals"}, "debates": {}, "meta": {"market_data_source": "bybit"},
            "venue_symbol": "SPCXUSDT", "display_symbol": "SPCX", "profile": "test",
        }
        with patch("quantdesk.tradingagents_runner.run_tradingagents", return_value=fake):
            response = await self.client.post(
                "/api/tradingagents/run", json={"symbol": "SPCXUSDT", "tradeDate": "2026-09-13"}
            )
        self.assertEqual(response.status_code, 200, response.text)

    async def test_async_job_endpoint_persists_and_cancels_without_running_graph(self):
        today = date.today().isoformat()
        created = await self.client.post(
            "/api/tradingagents/jobs", json={"symbol": "ETHUSDT", "tradeDate": today}
        )
        self.assertEqual(created.status_code, 202, created.text)
        job_id = created.json()["jobId"]
        detail = await self.client.get(f"/api/tradingagents/jobs/{job_id}")
        self.assertEqual(detail.json()["status"], "queued")
        cancelled = await self.client.post(f"/api/tradingagents/jobs/{job_id}/cancel")
        self.assertEqual(cancelled.json()["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
