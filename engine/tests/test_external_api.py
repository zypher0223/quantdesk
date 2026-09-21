"""External research and analytics endpoints.

Everything here is read-only with respect to trading, and every endpoint has to
behave when the external side is off, unconfigured or broken - which is the state
this repository ships in, so it is also the state the tests can run in.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quantdesk.api.server import app
from quantdesk.datahub.db import Database


class ExternalApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"QUANTDESK_HOME": self.tmp.name}, clear=False)
        self.env.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.env.stop()
        self.tmp.cleanup()

    async def test_an_upgraded_config_inherits_the_new_section_defaults(self):
        # A config.toml written before [external] existed must not read as "no
        # limits, no cache windows": that is how a rate limit disappears.
        (self.home / "config.toml").write_text(
            "[app]\ndefault_cash_usd = 50000\n", encoding="utf-8"
        )
        status = (await self.client.get("/api/external/status")).json()
        self.assertEqual(status["external"]["requestsPerMinute"], 60)
        self.assertEqual(status["external"]["timeoutSeconds"], 30)
        self.assertTrue(status["external"]["cacheTtlMinutes"])
        self.assertEqual(status["external"]["retentionDays"]["evidence"], 400)
        self.assertIn("news", status["providers"]["openbb"])

    async def test_an_operator_value_is_not_overwritten_by_the_defaults(self):
        (self.home / "config.toml").write_text(
            "[external]\nrequests_per_minute = 7\n\n[openbb.providers]\nnews = \"benzinga\"\n",
            encoding="utf-8",
        )
        status = (await self.client.get("/api/external/status")).json()
        self.assertEqual(status["external"]["requestsPerMinute"], 7)
        self.assertEqual(status["providers"]["openbb"]["news"], "benzinga")
        self.assertEqual(status["providers"]["openbb"]["fundamentals"], "sec", "缺失项由默认值补齐")

    async def test_status_reports_the_shipped_defaults_without_a_key(self):
        response = await self.client.get("/api/external/status")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["external"]["openbbEnabled"])
        self.assertFalse(body["external"]["finceptEnabled"])
        self.assertIn("news", body["providers"]["openbb"])
        self.assertEqual(body["plugins"]["research"]["pluginId"], None)
        self.assertIn("cacheTtlMinutes", body["external"])
        self.assertIn("AGPL", json.dumps(body["licences"], ensure_ascii=False))

    async def test_status_never_returns_a_key(self):
        (self.home / "keys.env").write_text("FINCEPT_API_KEY=super-secret\nOPENBB_FRED_API_KEY=another-secret\n", encoding="utf-8")
        with patch.dict(os.environ, {"FINCEPT_API_KEY": "super-secret", "OPENBB_FRED_API_KEY": "another-secret"}):
            response = await self.client.get("/api/external/status")
        text = response.text
        self.assertNotIn("super-secret", text)
        self.assertNotIn("another-secret", text)

    async def test_evidence_reports_a_mapping_and_rejects_unknown_symbols(self):
        ok = await self.client.get("/api/external/evidence?symbol=NVDAUSDT")
        self.assertEqual(ok.status_code, 200)
        body = ok.json()
        self.assertEqual(body["mapping"]["mapping"]["openbbSymbol"], "NVDA")
        self.assertTrue(body["mapping"]["referenceOptional"] is False)
        spcx = await self.client.get("/api/external/evidence?symbol=SPCXUSDT")
        self.assertTrue(spcx.json()["mapping"]["referenceOptional"])
        missing = await self.client.get("/api/external/evidence?symbol=DOGEUSDT")
        self.assertEqual(missing.status_code, 404)

    async def test_evidence_rows_carry_provider_dates_and_staleness(self):
        db = Database(self.home / "quantdesk.db")
        db.upsert_external_evidence(
            {
                "cache_key": "k1", "provider": "sec", "endpoint": "equity.fundamental.income",
                "symbol": "NVDAUSDT", "topic": "fundamentals", "as_of": "2026-07-31",
                "published_at": "2026-08-20T20:00:00Z", "observed_at": "2026-09-15T10:00:00Z",
                "source_url": "https://www.sec.gov/x", "payload_json": json.dumps({"revenue": 1}),
                "content_hash": "abc", "point_in_time": 1, "status": "ok",
                "updated_ts": 1,
            }
        )
        response = await self.client.get("/api/external/evidence?symbol=NVDAUSDT")
        item = response.json()["evidence"][0]
        self.assertEqual(item["provider"], "sec")
        self.assertEqual(item["publishedAt"], "2026-08-20T20:00:00Z")
        self.assertTrue(item["pointInTime"])
        self.assertTrue(item["stale"], "远早于 TTL 的记录应标记为过期")

    async def test_scenarios_are_listed_with_the_reports_five(self):
        response = await self.client.get("/api/external/scenarios")
        ids = {item["id"] for item in response.json()["scenarios"]}
        for expected in ("all_equities_down", "semiconductor_shock", "crypto_selloff",
                         "volatility_spike", "funding_anomaly"):
            self.assertIn(expected, ids)

    async def test_portfolio_risk_refuses_cleanly_when_the_feature_is_off(self):
        response = await self.client.post("/api/external/portfolio-risk", json={})
        self.assertEqual(response.status_code, 409)
        self.assertIn("未启用", response.json()["detail"])

    async def test_portfolio_risk_reports_no_adapter_when_enabled_but_uninstalled(self):
        (self.home / "config.toml").write_text("[external]\nfincept_enabled = true\n", encoding="utf-8")
        response = await self.client.post("/api/external/portfolio-risk", json={})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("analytics", body["unavailable"])

    async def test_a_scenario_refuses_cleanly_when_the_feature_is_off(self):
        response = await self.client.post("/api/external/scenario", json={"scenario": "crypto_selloff"})
        self.assertEqual(response.status_code, 409)


class ExternalSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"QUANTDESK_HOME": self.tmp.name}, clear=False)
        self.env.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.env.stop()
        self.tmp.cleanup()

    async def test_settings_toggle_the_features_and_survive_a_reload(self):
        response = await self.client.post(
            "/api/external/settings",
            json={"openbbEnabled": True, "finceptEnabled": True, "requestsPerMinute": 30,
                  "cacheTtlMinutes": {"news": 15}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        status = (await self.client.get("/api/external/status")).json()
        self.assertTrue(status["external"]["openbbEnabled"])
        self.assertTrue(status["external"]["finceptEnabled"])
        self.assertEqual(status["external"]["requestsPerMinute"], 30)
        self.assertEqual(status["external"]["cacheTtlMinutes"]["news"], 15)
        # Everything else in the file is preserved.
        self.assertIn("news", status["providers"]["openbb"])

    async def test_an_unknown_provider_is_refused_rather_than_saved(self):
        response = await self.client.post(
            "/api/external/settings", json={"providers": {"news": "not-a-provider"}}
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("未知的 OpenBB Provider", response.json()["detail"])

    async def test_a_fallback_with_a_typo_is_refused_too(self):
        response = await self.client.post(
            "/api/external/settings", json={"fallbacks": {"news": ["yfinance", "nope"]}}
        )
        self.assertEqual(response.status_code, 422)

    async def test_an_empty_save_is_refused(self):
        response = await self.client.post("/api/external/settings", json={})
        self.assertEqual(response.status_code, 422)

    async def test_the_connectivity_check_reports_state_without_spending(self):
        response = await self.client.post("/api/external/test", json={"capability": "research_tool"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["paid"])
        self.assertFalse(body["called"])
        self.assertEqual(body["outcome"], "该能力未启用")

    async def test_an_enabled_but_uninstalled_provider_says_so(self):
        await self.client.post("/api/external/settings", json={"openbbEnabled": True})
        response = await self.client.post("/api/external/test", json={"capability": "research_tool"})
        self.assertEqual(response.json()["outcome"], "没有启用对应插件")

    async def _install_research_plugin(self) -> None:
        from quantdesk.plugins import PluginError, PluginManager

        source = Path(__file__).resolve().parents[2] / "plugins" / "openbb-research"
        manager = PluginManager(self.home)
        manager.install(str(source))
        try:
            manager.set_enabled("openbb-research", True)
        except PluginError as exc:
            if "沙箱" not in str(exc) and "sandbox" not in str(exc).lower():
                raise
            self.skipTest(f"当前主机没有可用的操作系统沙箱：{exc}")

    async def test_a_paid_probe_is_refused_unless_explicitly_allowed(self):
        await self.client.post("/api/external/settings", json={"openbbEnabled": True})
        await self._install_research_plugin()
        response = await self.client.post(
            "/api/external/test", json={"capability": "research_tool", "allowPaid": False}
        )
        body = response.json()
        self.assertFalse(body["called"], "未授权时不得真的调用接口")
        self.assertIn("付费探测未获授权", body["note"])

    async def test_an_authorised_probe_reports_what_happened(self):
        await self.client.post("/api/external/settings", json={"openbbEnabled": True})
        await self._install_research_plugin()
        response = await self.client.post(
            "/api/external/test",
            json={"capability": "research_tool", "topic": "company_profile", "allowPaid": True},
        )
        body = response.json()
        self.assertTrue(body["paid"])
        self.assertTrue(body["called"], "获授权后应真的发起一次调用")
        # No OpenBB runtime is installed in this repository, so the honest outcome
        # is the runtime being unavailable - not a fabricated reading.
        self.assertIn("OpenBB", body["outcome"])


class MonitoringIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"QUANTDESK_HOME": self.tmp.name}, clear=False)
        self.env.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.env.stop()
        self.tmp.cleanup()

    async def test_monitoring_reports_both_external_providers_as_off_not_missing(self):
        response = await self.client.get("/api/monitoring")
        self.assertEqual(response.status_code, 200, response.text)
        external = response.json()["components"]["external"]
        self.assertFalse(external["openbb"]["configured"])
        self.assertFalse(external["fincept"]["configured"])
        self.assertEqual(external["fincept"]["calls"], 0)
        self.assertIsNone(external["fincept"]["successRate"])
        self.assertIn("AGPL", json.dumps(external["licences"], ensure_ascii=False))

    async def test_monitoring_counts_failures_and_rates_without_calling_out(self):
        from quantdesk.datahub.db import Database

        db = Database(self.home / "quantdesk.db")
        db.record_external_analytics({"provider": "fincept-api", "kind": "portfolio", "input_hash": "a",
                                      "status": "ok", "duration_ms": 100})
        db.record_external_analytics({"provider": "fincept-api", "kind": "portfolio", "input_hash": "b",
                                      "status": "error", "error": "Fincept API 429：rate limited",
                                      "duration_ms": 50})
        response = await self.client.get("/api/monitoring")
        fincept = response.json()["components"]["external"]["fincept"]
        self.assertEqual(fincept["calls"], 2)
        self.assertEqual(fincept["failures"], 1)
        self.assertEqual(fincept["rateLimited"], 1)
        self.assertEqual(fincept["averageMs"], 75.0)
        self.assertEqual(fincept["successRate"], 0.5)
        self.assertIn("429", fincept["lastError"]["error"])


class PaperSnapshotTests(unittest.TestCase):
    """The snapshot the analytics call is built from, without a provider."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"QUANTDESK_HOME": self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_an_empty_book_produces_an_empty_but_valid_snapshot(self):
        from quantdesk.api.external import _paper_snapshot

        snapshot = _paper_snapshot(points=60)
        self.assertEqual(snapshot.positions, [])
        self.assertEqual(snapshot.market_version, "no-history")
        self.assertEqual(snapshot.gross_exposure, 0.0)

    def test_a_book_without_history_is_still_reported_with_a_version(self):
        from quantdesk.api.external import _paper_snapshot
        from quantdesk.datahub.db import Database
        from quantdesk.paper.engine import PaperEngine

        from quantdesk.analytics import paper_config

        db = Database(self.home / "quantdesk.db")
        engine = PaperEngine(db, paper_config(self.home))
        engine.open_position(
            symbol="NVDAUSDT", side="long", notional=180.0, leverage=2.0,
            mark_price=180.0, rationale="test",
        )
        snapshot = _paper_snapshot(points=60)
        self.assertEqual([item["symbol"] for item in snapshot.positions], ["NVDAUSDT"])
        self.assertTrue(snapshot.market_version)
        self.assertIn("NVDAUSDT", snapshot.returns)
        self.assertEqual(snapshot.returns["NVDAUSDT"], [], "没有历史就没有收益率，不能编造")


if __name__ == "__main__":
    unittest.main()
