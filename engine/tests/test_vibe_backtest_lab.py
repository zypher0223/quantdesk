"""The Vibe-Trading alpha zoo plugin: its build artifact and its protocol surface.

Two layers are tested, and they are deliberately separate:

* the **artifact** (`factor_allowlist.json` next to the vendored source) is checked
  against the bytes on disk and against the engine's own protocol models, so a
  drifted or hand-edited allowlist fails here rather than at request time;
* the **runtime contract** — one JSON line in, one JSON line out — is exercised by
  running `plugin.py` under the plugin's own interpreter, which is the only place
  the pinned pandas exists. Those tests skip when the plugin has not been installed
  on this machine, because a fresh checkout has every right not to have run
  `plugins dependencies --install` yet.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from pathlib import Path

from quantdesk.plugins import PluginError, load_manifest
from quantdesk.plugins.protocol import AgentManifestResult, FactorComputeResult, FactorDefinition

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "vibe-backtest-lab"
VENDOR_ROOT = PLUGIN_DIR / "vendor" / "vibe-trading"
ALLOWLIST = PLUGIN_DIR / "factor_allowlist.json"
RUNTIME_PYTHON = (
    Path.home() / ".quantdesk" / "plugin-runtimes" / "vibe-backtest-lab" / "bin" / "python"
)

KNOWN_GATES = {
    "requires_fundamentals",
    "requires_true_vwap",
    "requires_sector",
    "requires_panel_field",
    "cross_sectional_needs_panel",
    "no_usable_values",
    "non_deterministic",
    "degenerate_constant",
}


def load_allowlist() -> dict:
    return json.loads(ALLOWLIST.read_text(encoding="utf-8"))


def run_plugin(method: str, params: dict | None = None, timeout: int = 120) -> dict:
    """One request, one response, exactly as the manager does it."""
    request = {"jsonrpc": "2.0", "id": "test-1", "method": method, "params": params or {}}
    result = subprocess.run(
        [str(RUNTIME_PYTHON), "plugin.py"],
        cwd=str(PLUGIN_DIR),
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AssertionError(
            f"插件必须只写一行响应，实际 {len(lines)} 行；stderr={result.stderr[-500:]}"
        )
    return json.loads(lines[0])


class VibeBacktestLabManifestTests(unittest.TestCase):
    def test_the_manifest_loads_and_claims_v4_factor_provider(self):
        manifest = load_manifest(PLUGIN_DIR)
        self.assertEqual(manifest.id, "vibe-backtest-lab")
        self.assertEqual(manifest.api_version, "4")
        # The agent capability is v4, so the plugin that hosts it must declare v4 -
        # and the manager refuses the reverse combination at load time.
        self.assertEqual(manifest.capabilities, ("factor_provider", "strategy_agent"))
        self.assertEqual(manifest.requirements_file, "requirements.lock")

    def test_the_plugin_declares_no_network_and_no_environment(self):
        manifest = load_manifest(PLUGIN_DIR)
        self.assertFalse(manifest.network)
        self.assertEqual(manifest.required_env, ())
        self.assertEqual(manifest.optional_env, ())

    def test_the_dependency_lock_is_a_hashed_pypi_subset(self):
        """No URL, no path, no unpinned package — the manager's own rules."""
        from quantdesk.plugins.manager import PluginManager

        digest = PluginManager._validate_requirements_lock(PLUGIN_DIR / "requirements.lock")
        self.assertEqual(len(digest), 64)
        names = [
            line.split("==")[0]
            for line in (PLUGIN_DIR / "requirements.lock").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertEqual(
            sorted(names), ["numpy", "pandas", "python-dateutil", "pytz", "six", "tzdata"]
        )

    def test_no_vendored_module_reaches_for_network_or_filesystem(self):
        """The isolation claim, checked on the bytes rather than asserted in prose."""
        forbidden = (
            "import socket", "import urllib", "import httpx", "import requests",
            "import aiohttp", "import subprocess", "import multiprocessing",
            "os.environ", "os.getenv",
        )
        offenders = []
        for path in sorted((VENDOR_ROOT / "src").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in source:
                    offenders.append(f"{path.name}: {needle}")
        self.assertEqual(offenders, [])


class VibeBacktestLabAllowlistTests(unittest.TestCase):
    """The allowlist must describe the vendored bytes, not an intention."""

    def setUp(self):
        self.data = load_allowlist()

    def test_every_factor_hashes_to_the_module_on_disk(self):
        for item in self.data["factors"]:
            path = VENDOR_ROOT / (item["modulePath"].replace(".", "/") + ".py")
            self.assertTrue(path.is_file(), f"缺少模块 {item['modulePath']}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(digest, item["moduleSha256"], item["id"])

    def test_the_allowlist_covers_every_alpha_exactly_once(self):
        included = [item["id"] for item in self.data["factors"]]
        excluded = [item["id"] for item in self.data["excluded"]]
        self.assertEqual(len(included), len(set(included)))
        self.assertEqual(len(excluded), len(set(excluded)))
        self.assertEqual(set(included) & set(excluded), set())
        self.assertEqual(len(included) + len(excluded), self.data["stats"]["upstreamTotal"])
        self.assertEqual(len(included), self.data["stats"]["included"])
        self.assertEqual(len(excluded), self.data["stats"]["excluded"])

    def test_the_vendored_tree_holds_the_whole_upstream_zoo(self):
        """462 alphas: a quietly missing module would shrink the corpus silently."""
        manifest = json.loads((VENDOR_ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["health"]["loaded"], 462)
        self.assertEqual(sum(len(zoo["alphas"]) for zoo in manifest["zoos"]), 462)
        self.assertEqual(
            {zoo["zoo_id"] for zoo in manifest["zoos"]},
            {"academic", "alpha101", "fundamental", "gtja191", "qlib158"},
        )

    def test_every_exclusion_names_a_known_gate_and_a_reason(self):
        for item in self.data["excluded"]:
            self.assertTrue(item["gates"], item["id"])
            self.assertEqual(item["reason"], item["gates"][0]["gate"])
            for gate in item["gates"]:
                self.assertIn(gate["gate"], KNOWN_GATES)
                self.assertTrue(gate["detail"].strip())

    def test_no_included_factor_needs_something_the_panel_cannot_carry(self):
        servable = {"open", "high", "low", "close", "volume", "amount"}
        for item in self.data["factors"]:
            self.assertTrue(set(item["requiredFields"]) <= servable, item["id"])
            self.assertTrue(set(item["requiredFields"]), item["id"])

    def test_warmup_is_measured_and_never_shorter_than_declared(self):
        for item in self.data["factors"]:
            self.assertGreaterEqual(item["measuredWarmupBars"], 1, item["id"])
            self.assertEqual(
                item["warmupBars"], max(item["declaredWarmupBars"], item["measuredWarmupBars"])
            )

    def test_the_factors_upstream_understates_are_still_in_the_allowlist(self):
        """qlib158 declares n where the window needs n+1; that must not be dropped."""
        understated = [
            item for item in self.data["factors"]
            if item["measuredWarmupBars"] > item["declaredWarmupBars"]
        ]
        self.assertEqual(len(understated), self.data["stats"]["declaredWarmupTooShort"])
        self.assertTrue(all(item["zoo"] == "qlib158" for item in understated))
        self.assertGreater(len(understated), 0)

    def test_the_five_sources_of_exclusion_are_all_represented(self):
        gates = self.data["stats"]["excludedByGate"]
        for gate in (
            "cross_sectional_needs_panel",
            "requires_true_vwap",
            "requires_sector",
            "requires_fundamentals",
            "requires_panel_field",
        ):
            self.assertGreater(gates.get(gate, 0), 0, gate)


class VibeBacktestLabCatalogShapeTests(unittest.TestCase):
    """The catalogue entries must satisfy the protocol model the engine validates."""

    def test_every_catalogue_entry_is_a_valid_factor_definition(self):
        data = load_allowlist()
        for item in data["factors"]:
            definition = FactorDefinition(
                id=f"vibezoo:{item['id']}",
                name=item["name"],
                family=f"vibe-trading/{item['zoo']}",
                mode="time_series",
                requiredFields=item["requiredFields"],
                warmupBars=item["warmupBars"],
                supportedTimeframes=["1d"],
                implementationVersion=item["moduleSha256"][:16],
                sources=["bybit", "derived"],
                formulaHash=item["moduleSha256"],
                description="x",
            )
            self.assertEqual(definition.id, f"vibezoo:{item['id']}")
            self.assertEqual(definition.mode, "time_series")

    def test_the_agent_manifest_message_is_still_only_what_v4_defines(self):
        """The agent surface is bounded by the protocol, not by good intentions."""
        manifest = load_manifest(PLUGIN_DIR)
        self.assertIn("strategy_agent", manifest.capabilities)
        self.assertEqual(manifest.api_version, "4", "strategy_agent 属于 v4")
        with self.assertRaises(Exception):
            AgentManifestResult(agentVersion="", mode="deterministic_search")
        with self.assertRaises(Exception):
            AgentManifestResult(agentVersion="x", mode="something_clever")


@unittest.skipUnless(RUNTIME_PYTHON.is_file(), "插件独立运行时尚未安装")
class VibeBacktestLabProtocolTests(unittest.TestCase):
    """The real thing: the plugin's own interpreter, one JSON line each way."""

    def test_health_reports_the_pinned_vendor_commit(self):
        response = run_plugin("health")
        self.assertNotIn("error", response)
        result = response["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["vendorCommit"], load_allowlist()["source"]["commit"])
        self.assertFalse(result["network"])
        # Two libraries, one capability: the vendored daily zoo, plus QuantDesk's
        # own 28 factors whose ids every existing factor run already uses.
        self.assertEqual(result["zooFactors"], load_allowlist()["stats"]["included"])
        self.assertEqual(result["localFactors"], 28)
        self.assertEqual(result["factors"], result["zooFactors"] + result["localFactors"])
        self.assertEqual(result["intervals"], ["15m", "1d", "1h", "4h"])

    def test_catalog_returns_every_allowlisted_factor_exactly_once(self):
        response = run_plugin("factor.catalog")
        self.assertNotIn("error", response)
        result = response["result"]
        allowlist = load_allowlist()
        ids = [item["id"] for item in result["factors"]]
        self.assertEqual(len(set(ids)), len(ids), "目录里有重复 ID")
        zoo = [item for item in result["factors"] if item["id"].startswith("vibezoo:")]
        local = [item for item in result["factors"] if not item["id"].startswith("vibezoo:")]
        self.assertEqual(len(zoo), allowlist["stats"]["included"])
        self.assertEqual(len(local), 28)
        self.assertEqual(
            {item["id"] for item in zoo},
            {f"vibezoo:{item['id']}" for item in allowlist["factors"]},
        )
        for item in zoo:
            self.assertEqual(item["mode"], "time_series")
            self.assertEqual(item["supportedTimeframes"], ["1d"])
            self.assertIn("bybit", item["sources"])
        for item in local:
            self.assertEqual(item["mode"], "time_series")
            self.assertEqual(item["supportedTimeframes"], ["15m", "1h", "4h", "1d"])

    def test_an_hourly_request_serves_the_quantdesk_library(self):
        """The ids every pre-existing factor run used must still be answered."""
        bars = _synthetic_bars(400, step_ms=3_600_000)
        response = run_plugin(
            "factor.compute",
            {
                "symbol": "BTCUSDT",
                "timeframe": "1h",
                "snapshotHash": "hourly",
                "factorIds": ["vibe.rsi.14", "vibe.momentum.24"],
                "candles": bars,
            },
        )
        self.assertNotIn("error", response)
        result = FactorComputeResult.model_validate(response["result"])
        self.assertEqual(
            [series.factorId for series in result.series], ["vibe.rsi.14", "vibe.momentum.24"]
        )
        for series in result.series:
            self.assertTrue(any(point.value is not None for point in series.values))

    def test_a_mixed_request_keeps_the_requested_order(self):
        bars = _synthetic_bars(400)
        response = run_plugin(
            "factor.compute",
            {
                "symbol": "BTCUSDT",
                "timeframe": "1d",
                "snapshotHash": "mixed",
                "factorIds": ["vibe.rsi.14", "vibezoo:qlib158_roc60", "vibe.momentum.96"],
                "candles": bars,
            },
        )
        self.assertNotIn("error", response)
        result = FactorComputeResult.model_validate(response["result"])
        self.assertEqual(
            [series.factorId for series in result.series],
            ["vibe.rsi.14", "vibezoo:qlib158_roc60", "vibe.momentum.96"],
        )

    def test_the_zoo_refuses_an_intraday_request_even_beside_local_factors(self):
        bars = _synthetic_bars(400, step_ms=3_600_000)
        response = run_plugin(
            "factor.compute",
            {
                "symbol": "BTCUSDT",
                "timeframe": "1h",
                "snapshotHash": "mixed-hourly",
                "factorIds": ["vibe.rsi.14", "vibezoo:qlib158_roc60"],
                "candles": bars,
            },
        )
        self.assertIn("error", response)
        self.assertIn("1d", response["error"]["message"])

    def test_compute_returns_one_series_per_factor_over_the_requested_bars(self):
        allowlist = load_allowlist()
        picks = [item["id"] for item in allowlist["factors"]][:3]
        bars = _synthetic_bars(300)
        response = run_plugin(
            "factor.compute",
            {
                "symbol": "BTCUSDT",
                "timeframe": "1d",
                "snapshotHash": "abc",
                "factorIds": [f"vibezoo:{item}" for item in picks],
                "candles": bars,
            },
        )
        self.assertNotIn("error", response)
        result = FactorComputeResult.model_validate(response["result"])
        self.assertEqual(result.snapshotHash, "abc")
        self.assertEqual([series.factorId for series in result.series], [f"vibezoo:{i}" for i in picks])
        for series in result.series:
            self.assertEqual([point.time for point in series.values], [bar["time"] for bar in bars])
            self.assertTrue(any(point.value is not None for point in series.values))

    def test_an_hourly_request_is_refused_rather_than_relabelled(self):
        response = run_plugin(
            "factor.compute",
            {
                "symbol": "BTCUSDT",
                "timeframe": "1h",
                "factorIds": ["vibezoo:qlib158_roc60"],
                "candles": _synthetic_bars(300),
            },
        )
        self.assertIn("error", response)
        self.assertIn("1d", response["error"]["message"])

    def test_an_unknown_factor_is_refused_by_name(self):
        response = run_plugin(
            "factor.compute",
            {
                "symbol": "BTCUSDT",
                "timeframe": "1d",
                "factorIds": ["vibezoo:not-a-factor"],
                "candles": _synthetic_bars(300),
            },
        )
        self.assertIn("error", response)
        self.assertIn("not-a-factor", response["error"]["message"])

    def test_too_few_bars_is_refused_with_the_number_required(self):
        response = run_plugin(
            "factor.compute",
            {
                "symbol": "BTCUSDT",
                "timeframe": "1d",
                "factorIds": ["vibezoo:qlib158_roc60"],
                "candles": _synthetic_bars(10),
            },
        )
        self.assertIn("error", response)
        self.assertIn("61", response["error"]["message"])

    def test_a_tampered_module_is_refused_at_startup(self):
        """The integrity check, exercised end to end on a copy of the plugin."""
        import shutil
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            copy = Path(raw) / "lab"
            shutil.copytree(PLUGIN_DIR, copy, ignore=shutil.ignore_patterns("__pycache__"))
            target = copy / "vendor" / "vibe-trading" / "src" / "factors" / "zoo"
            victim = next(
                (target / item["modulePath"].split(".")[-2] / f"{item['modulePath'].split('.')[-1]}.py")
                for item in load_allowlist()["factors"]
            )
            victim.write_text(victim.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
            result = subprocess.run(
                [str(RUNTIME_PYTHON), "plugin.py"],
                cwd=str(copy),
                input=json.dumps({"jsonrpc": "2.0", "id": "1", "method": "health", "params": {}}) + "\n",
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("不一致", result.stderr)


def _synthetic_bars(count: int, step_ms: int = 86_400_000) -> list[dict]:
    """A deterministic walk: no test may depend on the operator's market data."""
    import math

    bars = []
    price = 100.0
    for index in range(count):
        price *= 1 + 0.01 * math.sin(index / 7.0)
        bars.append(
            {
                "time": 1_600_000_000_000 + index * step_ms,
                "open": price * 0.995,
                "high": price * 1.005,
                "low": price * 0.99,
                "close": price,
                "volume": 1000.0 + index,
                "turnover": price * (1000.0 + index),
            }
        )
    return bars


if __name__ == "__main__":
    unittest.main()
