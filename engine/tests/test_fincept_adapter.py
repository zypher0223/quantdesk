"""The Fincept adapter: REST only, risk numbers only, and no order path.

The tests run the real plugin process through the real manager against a local
server that speaks the shapes Fincept returns. Nothing here touches the Fincept
repository, and nothing here needs a real subscription.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from quantdesk.plugins import PluginError, PluginManager, PluginRegistry
from quantdesk.plugins.protocol import (
    AnalyticsPortfolioRequest,
    AnalyticsPosition,
    AnalyticsReturnPoint,
    AnalyticsScenarioRequest,
)

PLUGIN_SOURCE = Path(__file__).resolve().parents[2] / "plugins" / "fincept-analytics"


def load_plugin_module():
    spec = importlib.util.spec_from_file_location("fincept_plugin", PLUGIN_SOURCE / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NormalisationTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin_module()

    def test_metrics_are_read_under_several_spellings(self):
        named, extra = self.plugin.normalise_metrics(
            {"VaR": -0.031, "expectedShortfall": -0.047, "annualizedVolatility": 0.21, "sharpe": 1.4,
             "unrelated": 5, "label": "x"}
        )
        self.assertEqual(named["var"], -0.031)
        self.assertEqual(named["cvar"], -0.047)
        self.assertEqual(named["volatility"], 0.21)
        self.assertEqual(named["sharpe"], 1.4)
        # Unrecognised numeric fields are kept as extras, never dropped silently.
        self.assertEqual(extra["unrelated"], 5)
        self.assertNotIn("label", extra)

    def test_contributions_accept_a_map_or_a_list(self):
        mapped = self.plugin.normalise_contributions({"riskContributions": {"NVDAUSDT": 0.6, "BTCUSDT": 0.4}})
        self.assertEqual(mapped[0]["symbol"], "NVDAUSDT")
        self.assertEqual(round(mapped[0]["percentage"], 2), 60.0)
        listed = self.plugin.normalise_contributions(
            {"contributions": [{"asset": "NVDAUSDT", "componentVaR": 0.6},
                               {"asset": "BTCUSDT", "componentVaR": 0.4}]}
        )
        self.assertEqual([item["symbol"] for item in listed], ["NVDAUSDT", "BTCUSDT"])

    def test_contributions_without_percentages_get_them_derived(self):
        items = self.plugin.normalise_contributions({"riskContributions": [{"symbol": "A", "value": 3},
                                                                          {"symbol": "B", "value": 1}]})
        self.assertAlmostEqual(items[0]["percentage"], 75.0)

    def test_a_correlation_matrix_is_normalised_into_labels_and_a_square(self):
        correlation = self.plugin.normalise_correlation(
            {"correlation": {"symbols": ["A", "B"], "matrix": [[1, 0.2], [0.2, 1]]}}
        )
        self.assertEqual(correlation["symbols"], ["A", "B"])
        self.assertEqual(len(correlation["matrix"]), 2)
        # ...and a deployment that returns the correlation object at the top level.
        flat = self.plugin.normalise_correlation({"symbols": ["A"], "matrix": [[1.0]]})
        self.assertEqual(flat["symbols"], ["A"])
        self.assertIsNone(self.plugin.normalise_correlation({"nothing": 1}))

    def test_weights_are_read_as_an_optimization(self):
        optimization = self.plugin.normalise_optimization(
            {"weights": {"A": 0.7, "B": 0.3}, "method": "mean-variance", "expectedVolatility": 0.18}
        )
        self.assertEqual(optimization["weights"], {"A": 0.7, "B": 0.3})
        self.assertEqual(optimization["expectedVolatility"], 0.18)
        self.assertIsNone(self.plugin.normalise_optimization({"weights": {}}))

    def test_a_scenario_result_derives_the_percentage_when_it_is_missing(self):
        result = self.plugin.scenario_result(
            {"asOf": "2026-09-15T10:00:00Z", "scenario": "semiconductor_shock"},
            {"equityBefore": 1000.0, "equityAfter": 900.0,
             "positions": [{"symbol": "NVDAUSDT", "pnl": -100.0}]},
            "req-1",
        )
        self.assertEqual(result["equityChange"], -100.0)
        self.assertEqual(result["equityChangePct"], -10.0)
        self.assertEqual(result["positions"][0]["symbol"], "NVDAUSDT")
        self.assertEqual(result["provider"], "fincept-api")

    def test_the_request_body_carries_contract_codes_and_quantdesk_returns(self):
        body = self.plugin.portfolio_body(
            {"asOf": "2026-09-15T10:00:00Z", "positions": [{"symbol": "NVDAUSDT", "side": "long",
                                                           "quantity": 2, "entryPrice": 180,
                                                           "markPrice": 184, "notional": 368, "margin": 73.6}],
             "returns": {"NVDAUSDT": [{"time": 1, "return": 0.01}]}}
        )
        self.assertEqual(body["positions"][0]["symbol"], "NVDAUSDT")
        self.assertIn("NVDAUSDT", body["returns"])

    def test_the_adapter_has_no_order_or_position_method(self):
        for name in ("order", "place_order", "submit", "trade", "position", "leverage", "withdraw"):
            self.assertNotIn(name, self.plugin.METHODS, f"适配器不应暴露 {name}")
        self.assertEqual(sorted(self.plugin.METHODS), ["analytics.portfolio", "analytics.scenario", "health"])

    def test_a_missing_key_refuses_without_calling_anything(self):
        calls: list[str] = []

        def factory():
            calls.append("built")
            raise self.plugin.AnalyticsUnavailable("未配置 FINCEPT_API_KEY")

        result = self.plugin.portfolio({"params": {"asOf": "2026-09-15T10:00:00Z"}}, client_factory=factory)
        self.assertIn("FINCEPT_API_KEY", result["unavailable"])
        self.assertEqual(result["metrics"], {})
        self.assertEqual(calls, ["built"])

    def test_health_reports_credential_presence_without_leaking_it(self):
        with patch.dict(os.environ, {"FINCEPT_API_KEY": "super-secret-value"}, clear=False):
            state = self.plugin.health({})
        self.assertTrue(state["credentialPresent"])
        self.assertTrue(state["runtimeReady"])
        self.assertNotIn("super-secret-value", json.dumps(state))
        self.assertNotIn("secret", json.dumps(state).lower())

    def test_health_without_a_key_says_so(self):
        with patch.dict(os.environ, {}, clear=True):
            state = self.plugin.health({})
        self.assertFalse(state["runtimeReady"])
        self.assertIn("FINCEPT_API_KEY", state["note"])


class _FinceptStubHandler(BaseHTTPRequestHandler):
    seen: list[tuple[str, dict]] = []

    def do_POST(self):  # noqa: N802 - http.server's contract
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        _FinceptStubHandler.seen.append((self.path, body))
        if self.path.endswith("/risk"):
            payload = {
                "portfolioVolatility": 0.21, "var": -0.031, "cvar": -0.047, "maxDrawdown": -0.12,
                "riskContributions": [{"symbol": "NVDAUSDT", "value": 0.6},
                                      {"symbol": "BTCUSDT", "value": 0.4}],
                "correlation": {"symbols": ["NVDAUSDT", "BTCUSDT"], "matrix": [[1, 0.3], [0.3, 1]]},
                "source": "Fincept QuantLib API",
            }
        elif self.path.endswith("/stress"):
            payload = {
                "scenario": body.get("scenario"), "equityBefore": 1000.0, "equityAfter": 940.0,
                "positions": [{"symbol": "NVDAUSDT", "pnl": -60.0, "pnlPct": -16.3}],
                "marginUsageBefore": 0.13, "marginUsageAfter": 0.15, "breachesAccountRisk": False,
            }
        else:
            payload = {"weights": {"NVDAUSDT": 0.5, "BTCUSDT": 0.5}}
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Request-Id", "req-stub")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        return


class AdapterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name) / "home"
        home.mkdir(parents=True, exist_ok=True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FinceptStubHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.manager = PluginManager(home)
        self.manager.install(str(PLUGIN_SOURCE))
        self.registry = PluginRegistry(self.manager)
        _FinceptStubHandler.seen.clear()

    def _enable(self):
        env = patch.dict(os.environ, {"FINCEPT_API_URL": self.base_url, "FINCEPT_API_KEY": "test-key"})
        env.start()
        self.addCleanup(env.stop)
        try:
            self.manager.set_enabled("fincept-analytics", True)
        except PluginError as exc:
            if "沙箱" not in str(exc) and "sandbox" not in str(exc).lower():
                raise
            self.skipTest(f"当前主机没有可用的操作系统沙箱：{exc}")

    def _request(self):
        return AnalyticsPortfolioRequest(
            asOf="2026-09-15T10:00:00Z",
            positions=[
                AnalyticsPosition(symbol="NVDAUSDT", group="半导体", side="long", quantity=2,
                                  entryPrice=180, markPrice=184, notional=368, margin=73.6),
                AnalyticsPosition(symbol="BTCUSDT", group="加密资产", side="short", quantity=0.01,
                                  entryPrice=60000, markPrice=59000, notional=590, margin=59),
            ],
            returns={"NVDAUSDT": [AnalyticsReturnPoint(time=1_789_000_000_000, value=0.012)]},
            marketSnapshotVersion="51df6b99a689b4a2",
        )

    def test_the_manifest_is_v2_and_declares_only_analytics(self):
        self._enable()
        plugins, invalid = self.manager.discover()
        self.assertEqual(invalid, [])
        manifest = plugins[0].manifest
        self.assertEqual(manifest.api_version, "2")
        self.assertEqual(manifest.capabilities, ("analytics",))

    def test_a_portfolio_call_round_trips_through_the_registry(self):
        self._enable()
        result = self.registry.portfolio_analytics("fincept-analytics", self._request())
        self.assertEqual(result.provider, "fincept-api")
        self.assertEqual(result.metrics.var, -0.031)
        self.assertEqual(result.metrics.cvar, -0.047)
        self.assertEqual([item.symbol for item in result.riskContributions], ["NVDAUSDT", "BTCUSDT"])
        self.assertEqual(result.correlation.symbols, ["NVDAUSDT", "BTCUSDT"])
        self.assertEqual(result.requestId, "req-stub")
        path, body = _FinceptStubHandler.seen[0]
        self.assertTrue(path.endswith("/v1/quant/portfolio/risk"))
        self.assertEqual([item["symbol"] for item in body["positions"]], ["NVDAUSDT", "BTCUSDT"])
        self.assertIn("NVDAUSDT", body["returns"])

    def test_a_scenario_call_round_trips_through_the_registry(self):
        self._enable()
        result = self.registry.scenario_analytics(
            "fincept-analytics",
            AnalyticsScenarioRequest(
                asOf="2026-09-15T10:00:00Z",
                positions=[AnalyticsPosition(symbol="NVDAUSDT", side="long", quantity=2,
                                             entryPrice=180, markPrice=184, notional=368, margin=73.6)],
                scenario="semiconductor_shock",
                shocks={"NVDAUSDT": -15.0},
                marketSnapshotVersion="v1",
            ),
        )
        self.assertEqual(result.scenario, "semiconductor_shock")
        self.assertEqual(result.equityChangePct, -6.0)
        self.assertFalse(result.breachesAccountRisk)
        path, body = _FinceptStubHandler.seen[0]
        self.assertTrue(path.endswith("/v1/quant/portfolio/stress"))
        self.assertEqual(body["shocks"], {"NVDAUSDT": -15.0})

    def test_without_a_key_the_call_reports_unavailable_instead_of_failing_the_engine(self):
        env = patch.dict(os.environ, {"FINCEPT_API_URL": self.base_url}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FINCEPT_API_KEY", None)
        try:
            self.manager.set_enabled("fincept-analytics", True)
        except PluginError as exc:
            if "沙箱" not in str(exc) and "sandbox" not in str(exc).lower():
                raise
            self.skipTest(f"当前主机没有可用的操作系统沙箱：{exc}")
        result = self.registry.portfolio_analytics("fincept-analytics", self._request())
        self.assertIsNone(result.metrics.var)
        self.assertIn("FINCEPT_API_KEY", result.unavailable, "不可用必须是结构化答案而非协议错误")
        self.assertEqual(result.provider, "fincept-api", "provider 名称由协议固定，不代表计算成功")
        self.assertTrue(any("FINCEPT_API_KEY" in warning for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()
