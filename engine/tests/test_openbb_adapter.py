"""The OpenBB research adapter, exercised without OpenBB and without spending.

OpenBB is not installed into the engine environment and the tests do not install
it: the adapter's job is to translate and to refuse, and both are testable with a
stub. The end-to-end test runs the real plugin process through the real plugin
manager against a local HTTP server that speaks the OpenBB REST shape.
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

from quantdesk.datahub.db import Database
from quantdesk.plugins import PluginError, PluginManager, PluginRegistry
from quantdesk.plugins.protocol import ResearchCollectRequest
from quantdesk.research.external import (
    STATUS_OK,
    STATUS_UNAVAILABLE,
    ExternalEvidenceService,
)

PLUGIN_SOURCE = Path(__file__).resolve().parents[2] / "plugins" / "openbb-research"


def load_plugin_module():
    spec = importlib.util.spec_from_file_location("openbb_plugin", PLUGIN_SOURCE / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingTransport:
    """A stand-in runtime: returns scripted payloads, records what was asked."""

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, str, dict]] = []

    def call(self, endpoint, provider, params):
        self.calls.append((endpoint, provider, params))
        outcome = self.responses.get((provider, endpoint))
        if outcome is None:
            outcome = self.responses.get(provider, {"results": []})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ShapingTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin_module()

    def test_a_publication_date_and_a_period_end_stay_apart(self):
        items = self.plugin.shape_evidence(
            topic="fundamentals", endpoint="equity.fundamental.income", provider="sec",
            symbol="NVDAUSDT", reference="NVDA", observed_at="2026-09-15T10:00:00Z",
            payload={"results": [{"period_ending": "2026-07-31", "filing_date": "2026-08-20",
                                  "total_revenue": 46_700_000_000}]},
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["asOf"], "2026-07-31")
        self.assertEqual(items[0]["publishedAt"], "2026-08-20")
        self.assertEqual(items[0]["provider"], "sec", "必须保留真实 Provider")
        self.assertTrue(items[0]["pointInTime"])

    def test_a_reading_without_a_publication_date_says_so(self):
        items = self.plugin.shape_evidence(
            topic="fundamentals", endpoint="equity.fundamental.income", provider="yfinance",
            symbol="NVDAUSDT", reference="NVDA", observed_at="2026-09-15T10:00:00Z",
            payload={"results": [{"period_ending": "2026-07-31", "total_revenue": 1}]},
        )
        self.assertEqual(items[0]["publishedAt"], "")
        self.assertTrue(any("发布时间" in warning for warning in items[0]["warnings"]))

    def test_a_same_day_publish_and_period_end_is_flagged(self):
        items = self.plugin.shape_evidence(
            topic="earnings", endpoint="equity.calendar.earnings", provider="yfinance",
            symbol="NVDAUSDT", reference="NVDA", observed_at="2026-09-15T10:00:00Z",
            payload={"results": [{"date": "2026-07-31", "report_date": "2026-07-31"}]},
        )
        self.assertTrue(any("期末" in warning for warning in items[0]["warnings"]))

    def test_epoch_timestamps_are_read_as_dates(self):
        self.assertTrue(self.plugin.publication_time({"published_at": 1789000000000}).startswith("20"))
        self.assertEqual(self.plugin.publication_time({"published_at": "2026-08-20"}), "2026-08-20")

    def test_a_large_payload_is_bounded(self):
        rows = [{"period_ending": "2026-07-31", "filing_date": "2026-08-20", "blob": "x" * 5000}
                for _ in range(200)]
        items = self.plugin.shape_evidence(
            topic="fundamentals", endpoint="equity.fundamental.income", provider="sec",
            symbol="NVDAUSDT", reference="NVDA", observed_at="2026-09-15T10:00:00Z",
            payload={"results": rows},
        )
        self.assertLessEqual(len(items), self.plugin.MAX_EVIDENCE_ITEMS)
        self.assertLessEqual(len(items[0]["value"]["blob"]), self.plugin.MAX_STRING)

    def test_an_unknown_topic_is_reported_not_invented(self):
        self.assertNotIn("insider_trading", self.plugin.TOPIC_ENDPOINTS)


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin_module()
        self.payload = {"results": [{"period_ending": "2026-07-31", "filing_date": "2026-08-20",
                                     "total_revenue": 1}]}

    def request(self, **params):
        base = {"symbol": "NVDAUSDT", "tradeDate": "2026-09-15", "topics": ["fundamentals"],
                "mapping": {"openbbSymbol": "NVDA"}, "providers": ["sec"]}
        base.update(params)
        return {"jsonrpc": "2.0", "id": "1", "method": "research.collect", "params": base}

    def test_a_successful_read_names_the_provider_that_answered(self):
        transport = RecordingTransport({"sec": self.payload})
        result = self.plugin.collect(self.request(), transport_factory=lambda: transport)
        self.assertEqual(result["provider"], "sec")
        self.assertEqual(result["evidence"][0]["provider"], "sec")
        self.assertEqual(transport.calls[0][2], {"symbol": "NVDA"})

    def test_the_provider_plan_is_tried_in_order(self):
        transport = RecordingTransport({
            "sec": RuntimeError("sec 不可用"),
            "yfinance": self.payload,
        })
        result = self.plugin.collect(
            self.request(providers=["sec", "yfinance"]), transport_factory=lambda: transport
        )
        self.assertEqual(result["provider"], "yfinance")
        self.assertEqual(result["attempts"][0]["provider"], "sec")
        self.assertIn("不可用", result["attempts"][0]["reason"])
        self.assertEqual(sorted({call[1] for call in transport.calls}), ["sec", "yfinance"])

    def test_nobody_having_data_is_an_unavailable_answer_not_an_empty_one(self):
        transport = RecordingTransport({"sec": {"results": []}, "yfinance": {"results": []}})
        result = self.plugin.collect(
            self.request(providers=["sec", "yfinance"]), transport_factory=lambda: transport
        )
        self.assertEqual(result["evidence"], [])
        self.assertIn("全部 Provider", result["unavailableReason"])
        self.assertEqual(len(result["attempts"]), 2)

    def test_a_missing_reference_code_refuses_without_calling_anyone(self):
        transport = RecordingTransport({"sec": self.payload})
        result = self.plugin.collect(
            self.request(mapping={}), transport_factory=lambda: transport
        )
        self.assertEqual(transport.calls, [])
        self.assertIn("不猜测替代代码", result["unavailableReason"])

    def test_no_configured_provider_refuses_without_calling_anyone(self):
        transport = RecordingTransport({"sec": self.payload})
        result = self.plugin.collect(
            self.request(providers=[]), transport_factory=lambda: transport
        )
        self.assertEqual(transport.calls, [])
        self.assertIn("没有为该接口配置 Provider", result["unavailableReason"])

    def test_an_absent_runtime_is_reported_as_unavailable(self):
        def factory():
            raise self.plugin.RuntimeUnavailable("OpenBB 运行环境不可用：未安装")

        result = self.plugin.collect(self.request(), transport_factory=factory)
        self.assertIn("运行环境不可用", result["unavailableReason"])
        self.assertEqual(result["evidence"], [])

    def test_health_reports_state_without_calling_a_paid_endpoint(self):
        state = self.plugin.health({})
        self.assertTrue(state["ok"])
        self.assertIn(state["transport"], {"rest", "sdk"})
        self.assertFalse(state["runtimeReady"], "本仓库不安装 OpenBB")

    def test_the_transport_choice_follows_the_environment(self):
        self.assertIsInstance(
            self.plugin.build_transport({"OPENBB_API_URL": "http://127.0.0.1:1"}), self.plugin.RestTransport
        )
        with self.assertRaises(self.plugin.RuntimeUnavailable):
            self.plugin.build_transport({})


class _OpenBBStubHandler(BaseHTTPRequestHandler):
    """A local stand-in for the OpenBB Platform REST API."""

    seen: list[str] = []

    def do_GET(self):  # noqa: N802 - http.server's contract
        _OpenBBStubHandler.seen.append(self.path)
        path = self.path.split("?")[0]
        if path == "/api/v1/equity/fundamental/income":
            body = {"results": [{"period_ending": "2026-07-31", "filing_date": "2026-08-20",
                                 "total_revenue": 46_700_000_000, "url": "https://www.sec.gov/x"}]}
        elif path == "/api/v1/equity/profile":
            body = {"results": [{"name": "NVIDIA Corporation", "sector": "Technology"}]}
        else:
            body = {"results": []}
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # noqa: A003 - silence the test server
        return


class AdapterIntegrationTests(unittest.TestCase):
    """The real plugin process, through the real manager, into the real store."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenBBStubHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        def stop_server():
            # One cleanup in the right order: addCleanup runs LIFO, so registering
            # join and shutdown separately would join a thread that is still serving.
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)

        self.addCleanup(stop_server)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

        self.manager = PluginManager(self.home)
        self.manager.install(str(PLUGIN_SOURCE))
        self.manager.set_enabled("openbb-research", True)
        self.registry = PluginRegistry(self.manager)
        self.db = Database(self.home / "quantdesk.db")

    def _invoke(self, topics, providers=("sec",)):
        env = patch.dict(os.environ, {"OPENBB_API_URL": self.base_url, "OPENBB_FMP_API_KEY": "test"})
        env.start()
        self.addCleanup(env.stop)
        return self.registry.collect_research(
            "openbb-research",
            ResearchCollectRequest(
                symbol="NVDAUSDT", tradeDate="2026-09-15", topics=list(topics),
                mapping={"openbbSymbol": "NVDA"}, providers=list(providers),
                openbbBaseUrl=self.base_url,
            ),
        )

    def test_the_manifest_is_installed_and_the_runtime_is_reported(self):
        plugins, invalid = self.manager.discover()
        self.assertEqual(invalid, [])
        self.assertEqual([item.manifest.id for item in plugins], ["openbb-research"])
        self.assertEqual(plugins[0].manifest.api_version, "1")
        self.assertTrue(plugins[0].manifest.network)
        self.assertIn("OPENBB_FRED_API_KEY", plugins[0].manifest.optional_env)
        self.assertEqual(plugins[0].manifest.required_env, (), "缺少密钥不应阻止启用")

    def test_a_reading_round_trips_from_the_adapter_into_the_evidence_store(self):
        try:
            outcome = self._invoke(["fundamentals"])
        except PluginError as exc:
            # Only a missing OS sandbox is a reason to skip; anything else is a
            # real failure and must be seen.
            if "沙箱" not in str(exc) and "sandbox" not in str(exc).lower():
                raise
            self.skipTest(f"当前主机没有可用的操作系统沙箱：{exc}")
        self.assertEqual(outcome.evidence[0].provider, "sec")
        self.assertEqual(outcome.evidence[0].endpoint, "equity.fundamental.income")
        self.assertEqual(outcome.evidence[0].asOf, "2026-07-31")
        self.assertEqual(outcome.evidence[0].publishedAt, "2026-08-20")

        service = ExternalEvidenceService(
            self.db,
            {"cache_ttl_minutes": {"fundamentals": 1440}, "retention_days": {"evidence": 400}},
            {"providers": {"fundamentals": "sec"}, "point_in_time": {"default": True}},
        )
        bundle = service.collect(
            symbol="NVDAUSDT", trade_date="2026-09-15", topics=["fundamentals"],
            fetcher=lambda provider, topic, symbol: {
                "evidence": [item.model_dump() for item in outcome.evidence],
                "source": outcome.evidence[0].source,
                "observedAt": outcome.evidence[0].observedAt,
            },
        )
        self.assertEqual(len(bundle.usable), 1)
        self.assertFalse(bundle.degraded)
        stored = self.db.list_external_evidence(symbol="NVDAUSDT")
        self.assertEqual(stored[0]["status"], STATUS_OK)
        self.assertEqual(stored[0]["provider"], "sec")

    def test_the_same_reading_is_rejected_when_the_judgement_predates_it(self):
        try:
            outcome = self._invoke(["fundamentals"])
        except PluginError as exc:
            if "沙箱" not in str(exc) and "sandbox" not in str(exc).lower():
                raise
            self.skipTest(f"当前主机没有可用的操作系统沙箱：{exc}")
        service = ExternalEvidenceService(
            self.db, {"cache_ttl_minutes": {"fundamentals": 1440}},
            {"providers": {"fundamentals": "sec"}, "point_in_time": {"default": True}},
        )
        bundle = service.collect(
            symbol="NVDAUSDT", trade_date="2026-06-30", topics=["fundamentals"],
            fetcher=lambda provider, topic, symbol: {
                "evidence": [item.model_dump() for item in outcome.evidence],
                "observedAt": outcome.evidence[0].observedAt,
            },
        )
        self.assertEqual(bundle.usable, [])
        self.assertTrue(bundle.degraded)
        self.assertIn("发布于研判日期之后", bundle.rejected[0]["reason"])


if __name__ == "__main__":
    unittest.main()
