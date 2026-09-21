"""Manifest-driven external repository plugin system."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from quantdesk.api.server import app
from quantdesk.cli import app as cli_app
from quantdesk.plugins import PluginError, PluginManager, PluginRegistry, load_manifest
from quantdesk.plugins.protocol import (
    AnalyticsCorrelation,
    AnalyticsPortfolioRequest,
    AnalyticsPosition,
    AnalyticsReturnPoint,
    AnalyticsScenarioRequest,
)
from quantdesk.plugins.sandbox import (
    SandboxStatus,
    _mac_profile,
    _probe_backend,
    _signal_name,
    diagnose,
    status,
    wrap as sandbox_wrap,
)


def write_plugin(
    root: Path,
    *,
    plugin_id: str = "fixture-plugin",
    capabilities: tuple[str, ...] = ("strategy",),
    api_version: str = "1",
    required_env: tuple[str, ...] = (),
    version: str = "0.1.0",
    requirements_file: str = "",
    required_executables: tuple[str, ...] = (),
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = f"""
[plugin]
id = "{plugin_id}"
name = "Fixture Plugin"
version = "{version}"
api_version = "{api_version}"
description = "test adapter"
capabilities = [{", ".join(json.dumps(item) for item in capabilities)}]
command = ["python", "plugin.py"]
timeout_seconds = 5

[plugin.permissions]
network = false
env = [{", ".join(json.dumps(item) for item in required_env)}]
"""
    if requirements_file or required_executables:
        manifest += f"""

[plugin.dependencies]
requirements = {json.dumps(requirements_file)}
executables = [{", ".join(json.dumps(item) for item in required_executables)}]
"""
    (root / "quantdesk-plugin.toml").write_text(textwrap.dedent(manifest), encoding="utf-8")
    (root / "plugin.py").write_text(
        textwrap.dedent(
            """
            import json
            import os
            import sys

            request = json.loads(sys.stdin.readline())
            if request["method"] == "health":
                result = {
                    "ok": True,
                    "home": os.environ.get("HOME"),
                    "secretVisible": bool(os.environ.get("TEST_PLUGIN_TOKEN")),
                    "unlistedVisible": bool(os.environ.get("UNLISTED_SECRET")),
                }
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}))
            elif request["method"] == "strategy.echo":
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": request["params"]}))
            elif request["method"] == "strategy.describe":
                result = {"strategies": [{
                    "id": "fixture", "name": "Fixture", "description": "test",
                    "parameters": [{"key": "threshold", "label": "阈值", "type": "number", "default": 1.0}],
                }]}
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}))
            elif request["method"] == "strategy.generate":
                candles = request["params"]["candles"]
                result = {"signals": [{"time": candles[-2]["time"], "direction": "long", "strength": 0.8, "reason": "fixture"}], "warnings": []}
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}))
            elif request["method"] == "analytics.portfolio":
                positions = request["params"]["positions"]
                symbols = [item["symbol"] for item in positions]
                size = len(symbols)
                result = {
                    "provider": "fixture-api",
                    "asOf": request["params"]["asOf"],
                    "metrics": {"volatility": 0.21, "var": -0.03, "cvar": -0.045, "maxDrawdown": -0.12},
                    "riskContributions": [
                        {"symbol": name, "value": round(1 / size, 4), "percentage": round(100 / size, 2)}
                        for name in symbols
                    ],
                    "correlation": {"symbols": symbols, "matrix": [[1.0 if a == b else 0.2 for b in symbols] for a in symbols]},
                    "optimization": {"method": "fixture", "weights": {name: round(1 / size, 4) for name in symbols}},
                    "source": "fixture",
                    "requestId": request["id"],
                    "warnings": ["fixture warning"],
                }
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}))
            elif request["method"] == "analytics.scenario":
                params = request["params"]
                result = {
                    "provider": "fixture-api",
                    "asOf": params["asOf"],
                    "scenario": params["scenario"],
                    "equityBefore": 1000.0,
                    "equityAfter": 900.0,
                    "equityChange": -100.0,
                    "equityChangePct": -10.0,
                    "positions": [{"symbol": item["symbol"], "pnl": -50.0, "pnlPct": -10.0} for item in params["positions"]],
                    "marginUsageBefore": 0.3,
                    "marginUsageAfter": 0.36,
                    "breachesAccountRisk": False,
                    "breachedLimits": [],
                    "source": "fixture",
                    "requestId": request["id"],
                    "warnings": [],
                }
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}))
            elif request["method"] == "security.probe":
                target = request["params"]["path"]
                try:
                    open(target, encoding="utf-8").read()
                    read_blocked = False
                except (OSError, PermissionError):
                    read_blocked = True
                try:
                    open(target, "w", encoding="utf-8").write("changed")
                    write_blocked = False
                except (OSError, PermissionError):
                    write_blocked = True
                result = {"readBlocked": read_blocked, "writeBlocked": write_blocked}
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}))
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -1, "message": "unknown"}}))
            """
        ),
        encoding="utf-8",
    )
    return root


class PluginManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.manager = PluginManager(self.home)

    def test_local_install_is_validated_and_disabled_by_default(self):
        source = write_plugin(self.base / "source")
        installed = self.manager.install(str(source))
        self.assertEqual(installed.manifest.id, "fixture-plugin")
        self.assertFalse(installed.enabled)
        self.assertEqual(installed.path, self.home / "plugins" / "fixture-plugin")
        plugins, invalid = self.manager.discover()
        self.assertEqual(invalid, [])
        self.assertEqual([plugin.manifest.id for plugin in plugins], ["fixture-plugin"])

    def test_enable_state_is_persisted_with_private_permissions(self):
        self.manager.install(str(write_plugin(self.base / "source")))
        enabled = self.manager.set_enabled("fixture-plugin", True)
        self.assertTrue(enabled.enabled)
        self.assertTrue(PluginManager(self.home).get("fixture-plugin").enabled)
        mode = stat.S_IMODE(self.manager.state_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_invoke_uses_json_rpc_and_capability_guard(self):
        self.manager.install(str(write_plugin(self.base / "source")))
        self.manager.set_enabled("fixture-plugin", True)
        result = self.manager.invoke(
            "fixture-plugin", "strategy.echo", {"direction": "long"}, capability="strategy"
        )
        self.assertEqual(result["result"], {"direction": "long"})
        with self.assertRaises(PluginError):
            self.manager.invoke(
                "fixture-plugin", "data.candles", {}, capability="data_provider"
            )

    def test_only_manifest_allowlisted_environment_is_forwarded(self):
        source = write_plugin(self.base / "source", required_env=("TEST_PLUGIN_TOKEN",))
        self.manager.install(str(source))
        with patch.dict(
            os.environ,
            {"TEST_PLUGIN_TOKEN": "allowed", "UNLISTED_SECRET": "blocked"},
            clear=False,
        ):
            result = self.manager.health("fixture-plugin")["result"]
        self.assertTrue(result["secretVisible"])
        self.assertFalse(result["unlistedVisible"])
        self.assertIn("plugin-data/fixture-plugin", result["home"])

    def test_missing_required_environment_is_reported_before_execution(self):
        source = write_plugin(self.base / "source", required_env=("TEST_PLUGIN_TOKEN",))
        self.manager.install(str(source))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(PluginError, "TEST_PLUGIN_TOKEN"):
                self.manager.health("fixture-plugin")

    def test_headless_invocation_loads_required_environment_from_keys_file(self):
        source = write_plugin(self.base / "source", required_env=("TEST_PLUGIN_TOKEN",))
        self.manager.install(str(source))
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "keys.env").write_text("TEST_PLUGIN_TOKEN=saved-value\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            result = self.manager.health("fixture-plugin")["result"]
        self.assertTrue(result["secretVisible"])
        self.assertFalse(result["unlistedVisible"])

    def test_invalid_manifest_is_reported_without_breaking_discovery(self):
        write_plugin(self.home / "plugins" / "bad", capabilities=("root_access",))
        plugins, invalid = self.manager.discover()
        self.assertEqual(plugins, [])
        self.assertEqual(len(invalid), 1)
        self.assertIn("不支持的能力", invalid[0]["error"])

    def test_api_version_and_remote_source_are_strict(self):
        source = write_plugin(self.base / "source", api_version="99")
        with self.assertRaisesRegex(PluginError, "API 版本"):
            load_manifest(source)
        with self.assertRaises(PluginError):
            self.manager.validate_github_source("https://gitlab.com/owner/repo")
        self.assertEqual(
            self.manager.validate_github_source("https://github.com/owner/repo"),
            ("owner", "repo"),
        )

    def test_v1_manifests_keep_working_unchanged(self):
        # Adding v2 must not be a migration for anyone: an untouched v1 plugin
        # still loads, with the same capabilities it always had.
        manifest = load_manifest(write_plugin(self.base / "v1", capabilities=("strategy", "research_tool")))
        self.assertEqual(manifest.api_version, "1")
        self.assertEqual(manifest.capabilities, ("strategy", "research_tool"))

    def test_v2_is_accepted_and_gates_the_analytics_capability(self):
        manifest = load_manifest(write_plugin(self.base / "v2", api_version="2", capabilities=("analytics",)))
        self.assertEqual(manifest.api_version, "2")
        self.assertIn("analytics", manifest.capabilities)

    def test_a_v1_manifest_cannot_claim_analytics(self):
        # The capability belongs to the version that defines its messages, so a v1
        # manifest declaring it is rejected at load rather than at first call.
        source = write_plugin(self.base / "v1-analytics", api_version="1", capabilities=("analytics",))
        with self.assertRaisesRegex(PluginError, "不支持能力"):
            load_manifest(source)

    def test_v2_manifests_still_cannot_invent_capabilities(self):
        source = write_plugin(self.base / "v2-bad", api_version="2", capabilities=("trading",))
        with self.assertRaisesRegex(PluginError, "不支持的能力"):
            load_manifest(source)

    def test_cli_reports_plugin_error_without_traceback(self):
        result = CliRunner().invoke(
            cli_app,
            ["plugins", "enable", "missing"],
            env={"QUANTDESK_HOME": str(self.home)},
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("plugin error: 没有找到插件 missing", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_business_registry_validates_strategy_catalog_and_signals(self):
        from quantdesk.plugins import PluginRegistry, StrategyGenerateRequest

        self.manager.install(str(write_plugin(self.base / "source")))
        plugin = self.manager.set_enabled("fixture-plugin", True)
        registry = PluginRegistry(self.manager)
        described = registry.describe_strategies(plugin)
        self.assertEqual(described.strategies[0].id, "fixture")
        candles = [
            {"time": 1_000 + i, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 2}
            for i in range(4)
        ]
        generated = registry.generate_signals(
            "fixture-plugin",
            StrategyGenerateRequest(
                strategyId="fixture", symbol="BTCUSDT", timeframe="1h", candles=candles
            ),
        )
        self.assertEqual(generated.signals[0].direction, "long")

    def test_update_replaces_code_disables_plugin_and_preserves_private_data(self):
        source = write_plugin(self.base / "source")
        self.manager.install(str(source))
        self.manager.set_enabled("fixture-plugin", True)
        private = self.home / "plugin-data" / "fixture-plugin" / "saved.txt"
        private.write_text("keep", encoding="utf-8")

        write_plugin(source, version="0.2.0")
        result = self.manager.update("fixture-plugin")

        self.assertEqual(result["previousVersion"], "0.1.0")
        self.assertTrue(result["changed"])
        self.assertTrue(result["reviewRequired"])
        self.assertEqual(self.manager.get("fixture-plugin").manifest.version, "0.2.0")
        self.assertFalse(self.manager.get("fixture-plugin").enabled)
        self.assertEqual(private.read_text(encoding="utf-8"), "keep")

    def test_invalid_update_restores_previous_code_and_enabled_state(self):
        source = write_plugin(self.base / "source")
        self.manager.install(str(source))
        self.manager.set_enabled("fixture-plugin", True)
        write_plugin(source, plugin_id="other-plugin", version="9.0.0")

        with self.assertRaisesRegex(PluginError, "不一致"):
            self.manager.update("fixture-plugin")

        restored = self.manager.get("fixture-plugin")
        self.assertEqual(restored.manifest.version, "0.1.0")
        self.assertTrue(restored.enabled)

    def test_uninstall_requires_disabled_and_preserves_data_by_default(self):
        self.manager.install(str(write_plugin(self.base / "source")))
        self.manager.set_enabled("fixture-plugin", True)
        with self.assertRaisesRegex(PluginError, "停用"):
            self.manager.uninstall("fixture-plugin")
        self.manager.set_enabled("fixture-plugin", False)
        private = self.home / "plugin-data" / "fixture-plugin" / "saved.txt"
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_text("keep", encoding="utf-8")
        runtime = self.home / "plugin-runtimes" / "fixture-plugin"
        runtime.mkdir(parents=True)

        result = self.manager.uninstall("fixture-plugin")

        self.assertTrue(result["removed"])
        self.assertFalse(result["dataRemoved"])
        self.assertTrue(private.exists())
        self.assertFalse(runtime.exists())
        with self.assertRaises(PluginError):
            self.manager.get("fixture-plugin")

    def test_dependency_lock_requires_fixed_hashed_wheels(self):
        source = write_plugin(
            self.base / "source",
            requirements_file="requirements.lock",
        )
        (source / "requirements.lock").write_text("requests>=2\n", encoding="utf-8")
        self.manager.install(str(source))
        status = self.manager.dependency_status("fixture-plugin")
        self.assertFalse(status["ready"])
        self.assertIn("固定版本", status["problems"][0])

    def test_dependency_install_uses_hashes_binary_wheels_and_private_runtime(self):
        source = write_plugin(
            self.base / "source",
            requirements_file="requirements.lock",
        )
        (source / "requirements.lock").write_text(
            f"example-package==1.2.3 --hash=sha256:{'a' * 64}\n",
            encoding="utf-8",
        )
        self.manager.install(str(source))

        def create_fake_venv(*args):
            destination = Path(args[-1])
            python = destination / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")

        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("quantdesk.plugins.manager.venv.EnvBuilder.create", side_effect=create_fake_venv),
            patch("quantdesk.plugins.manager.subprocess.run", return_value=completed) as run,
        ):
            status = self.manager.install_dependencies("fixture-plugin")

        # install_dependencies also probes the sandbox, so locate the pip call by
        # its content instead of assuming it is the first subprocess.run.
        pip_calls = [call.args[0] for call in run.call_args_list if any("pip" in str(part) for part in call.args[0])]
        self.assertTrue(pip_calls, "expected the dependency install to shell out to pip")
        command = pip_calls[0]
        self.assertIn("--require-hashes", command)
        self.assertIn("--only-binary=:all:", command)
        self.assertTrue(status["ready"])
        self.assertTrue((self.home / "plugin-runtimes" / "fixture-plugin" / "bin" / "python").exists())

    def test_required_unavailable_sandbox_blocks_enable(self):
        self.manager.install(str(write_plugin(self.base / "source")))
        unavailable = SandboxStatus("required", "sandbox-exec", False, False, "sandbox unavailable")
        with patch("quantdesk.plugins.manager.sandbox_status", return_value=unavailable):
            with self.assertRaisesRegex(PluginError, "sandbox unavailable"):
                self.manager.set_enabled("fixture-plugin", True)

    def test_enforced_sandbox_blocks_files_outside_plugin_private_data(self):
        self.manager.install(str(write_plugin(self.base / "source")))
        secret = self.base / "outside-secret.txt"
        secret.write_text("sensitive", encoding="utf-8")
        result = self.manager.invoke(
            "fixture-plugin",
            "security.probe",
            {"path": str(secret)},
            allow_disabled=True,
        )
        if not result["sandbox"]["enforced"]:
            self.skipTest("当前主机没有可用的操作系统沙箱")
        self.assertTrue(result["result"]["readBlocked"])
        self.assertTrue(result["result"]["writeBlocked"])
        self.assertEqual(secret.read_text(encoding="utf-8"), "sensitive")


class PluginAnalyticsProtocolTests(unittest.TestCase):
    """The v2 capability: a risk calculator that never supplies market data."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.manager = PluginManager(self.home)
        self.manager.install(str(write_plugin(self.base / "source", api_version="2",
                                              capabilities=("analytics",))))
        self.manager.set_enabled("fixture-plugin", True)
        self.registry = PluginRegistry(self.manager)

    def _request(self) -> AnalyticsPortfolioRequest:
        return AnalyticsPortfolioRequest(
            asOf="2026-09-15T10:00:00Z",
            positions=[
                AnalyticsPosition(symbol="NVDAUSDT", group="半导体", side="long", quantity=2,
                                  entryPrice=180, markPrice=184, notional=368, margin=73.6),
                AnalyticsPosition(symbol="BTCUSDT", group="加密资产", side="short", quantity=0.01,
                                  entryPrice=60000, markPrice=59000, notional=590, margin=59),
            ],
            returns={"NVDAUSDT": [AnalyticsReturnPoint(time=1789000000000, value=0.012)]},
            marketSnapshotVersion="51df6b99a689b4a2",
        )

    def test_the_portfolio_request_carries_contract_codes_and_the_snapshot_version(self):
        payload = self._request().model_dump(by_alias=True)
        self.assertEqual([item["symbol"] for item in payload["positions"]], ["NVDAUSDT", "BTCUSDT"])
        self.assertEqual(payload["marketSnapshotVersion"], "51df6b99a689b4a2")
        # The JSON name of a return point is `return`, which is a Python keyword.
        self.assertEqual(payload["returns"]["NVDAUSDT"][0]["return"], 0.012)

    def test_metrics_risk_contributions_and_correlation_come_back_typed(self):
        result = self.registry.portfolio_analytics("fixture-plugin", self._request())
        self.assertEqual(result.provider, "fixture-api")
        self.assertEqual(result.metrics.var, -0.03)
        self.assertEqual(result.metrics.cvar, -0.045)
        self.assertEqual([item.symbol for item in result.riskContributions], ["NVDAUSDT", "BTCUSDT"])
        self.assertEqual(result.correlation.symbols, ["NVDAUSDT", "BTCUSDT"])
        self.assertEqual(len(result.correlation.matrix), 2)
        self.assertEqual(result.optimization.weights["NVDAUSDT"], 0.5)

    def test_a_ragged_correlation_matrix_is_rejected(self):
        # A matrix that does not match its labels would silently mis-align risks.
        with self.assertRaisesRegex(Exception, "方阵|不一致"):
            AnalyticsCorrelation(symbols=["A", "B"], matrix=[[1.0, 0.1]])

    def test_scenario_results_report_equity_margin_and_limits(self):
        result = self.registry.scenario_analytics(
            "fixture-plugin",
            AnalyticsScenarioRequest(
                asOf="2026-09-15T10:00:00Z",
                positions=[AnalyticsPosition(symbol="NVDAUSDT", side="long", quantity=2,
                                             entryPrice=180, markPrice=184, notional=368, margin=73.6)],
                scenario="semiconductor_shock",
                shocks={"group:半导体": -15.0},
                marketSnapshotVersion="51df6b99a689b4a2",
            ),
        )
        self.assertEqual(result.scenario, "semiconductor_shock")
        self.assertEqual(result.equityChangePct, -10.0)
        self.assertFalse(result.breachesAccountRisk)
        self.assertEqual(result.positions[0].symbol, "NVDAUSDT")

    def test_a_v1_plugin_is_refused_the_v2_method(self):
        self.manager.install(str(write_plugin(self.base / "old", plugin_id="legacy-plugin",
                                              api_version="1", capabilities=("strategy",))))
        self.manager.set_enabled("legacy-plugin", True)
        with self.assertRaisesRegex(PluginError, "未声明能力"):
            self.registry.portfolio_analytics("legacy-plugin", self._request())


class PluginSandboxTests(unittest.TestCase):
    def test_mac_profile_hides_account_and_blocks_network(self):
        profile = _mac_profile(
            Path.home() / ".quantdesk/plugins/example",
            Path.home() / ".quantdesk/plugin-data/example",
            Path.home() / ".quantdesk",
            None,
            False,
        )
        self.assertIn("(deny default)", profile)
        self.assertIn(f'(allow file-read* (subpath "{Path.home() / ".quantdesk/plugins/example"}"))', profile)
        self.assertNotIn("(allow network*)", profile)

    def test_bubblewrap_command_hides_home_and_mounts_only_private_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "quantdesk"
            plugin = home / "plugins" / "example"
            data = home / "plugin-data" / "example"
            plugin.mkdir(parents=True)
            data.mkdir(parents=True)
            available = SandboxStatus("required", "bubblewrap", True, True, "ok")
            with (
                patch("quantdesk.plugins.sandbox.status", return_value=available),
                patch("quantdesk.plugins.sandbox.shutil.which", return_value="/usr/bin/bwrap"),
            ):
                command, status = sandbox_wrap(
                    ["python", "plugin.py"],
                    plugin_path=plugin,
                    data_path=data,
                    quantdesk_home=home,
                    runtime_path=None,
                    network=False,
                )
        self.assertTrue(status.enforced)
        self.assertIn("--unshare-all", command)
        self.assertNotIn("--share-net", command)
        self.assertIn(str(Path.home()), command)
        self.assertIn(str(home.resolve()), command)
        self.assertIn("--ro-bind", command)
        self.assertIn("--bind", command)


class SandboxDiagnosticTests(unittest.TestCase):
    """A sandbox that silently does not apply is worse than one that fails loudly.

    On macOS 27 a restricted `(subpath ...)` read rule plus `(allow process*)`
    aborts the child (SIGABRT) with no stderr, so the reason used to be lost.
    """

    def test_signal_termination_is_named(self):
        self.assertEqual(_signal_name(-6), "SIGABRT")
        self.assertEqual(_signal_name(-9), "SIGKILL")
        self.assertEqual(_signal_name(-15), "SIGTERM")
        self.assertIsNone(_signal_name(0))
        self.assertIsNone(_signal_name(71))

    def test_probe_failure_reports_signal_and_never_an_empty_reason(self):
        fake = subprocess.CompletedProcess(args=["probe"], returncode=-6, stdout="", stderr="")
        with patch("quantdesk.plugins.sandbox.subprocess.run", return_value=fake):
            backend, available, error, fired, command = _probe_backend()
        self.assertEqual(backend, "sandbox-exec")
        self.assertFalse(available)
        self.assertEqual(fired, "SIGABRT")
        self.assertTrue(error, "a signal kill must still produce a reason")
        self.assertIn("SIGABRT", error)
        self.assertTrue(command, "the probe command must be reported for reproduction")

    def test_status_detail_states_the_real_consequence_per_policy(self):
        fake = subprocess.CompletedProcess(args=["probe"], returncode=-6, stdout="", stderr="")
        with patch("quantdesk.plugins.sandbox.subprocess.run", return_value=fake):
            with patch.dict(os.environ, {"QUANTDESK_PLUGIN_SANDBOX": "preferred"}):
                preferred = status()
            with patch.dict(os.environ, {"QUANTDESK_PLUGIN_SANDBOX": "required"}):
                required = status()
        self.assertFalse(preferred.enforced)
        self.assertIn("SIGABRT", preferred.detail)
        self.assertIn("没有操作系统级隔离", preferred.detail)
        self.assertIn("拒绝运行", required.detail)

    def test_probe_uses_the_same_profile_shape_as_enforcement(self):
        # The probe must not test a different allowlist than the one plugins run
        # under, or a probe failure says nothing about enforcement.
        from quantdesk.plugins.sandbox import BASE_READABLE, _probe_profile

        profile = _probe_profile()
        self.assertIn("(deny default)", profile)
        self.assertIn("(allow process*)", profile)
        for path in BASE_READABLE:
            self.assertIn(f'(subpath "{path}")', profile)
        self.assertNotIn("(allow network*)", profile)

    def test_diagnose_explains_a_signal_failure(self):
        fake = subprocess.CompletedProcess(args=["probe"], returncode=-6, stdout="", stderr="")
        with patch("quantdesk.plugins.sandbox.subprocess.run", return_value=fake):
            report = diagnose()
        self.assertIn("SIGABRT", report.diagnostic)
        self.assertIn("probeCommand", report.as_dict())
        self.assertTrue(report.as_dict()["probeCommand"])


class PluginApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        source = write_plugin(Path(self._tmp.name) / "source")
        manager = PluginManager(self.home)
        manager.install(str(source))
        self._env = patch.dict(os.environ, {"QUANTDESK_HOME": str(self.home)}, clear=False)
        self._env.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    async def test_http_install_rejects_server_local_paths(self):
        response = await self.client.post(
            "/api/plugins/install", json={"source": str(Path(self._tmp.name) / "source")}
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("github.com", response.json()["detail"])

    async def asyncTearDown(self):
        await self.client.aclose()
        self._env.stop()
        self._tmp.cleanup()

    async def test_list_enable_and_health_endpoints(self):
        response = await self.client.get("/api/plugins")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        # The engine's newest version, with the versions it still accepts.
        self.assertEqual(body["apiVersion"], "4")
        self.assertEqual(body["supportedApiVersions"], ["1", "2", "3", "4"])
        self.assertIn("sandbox", body)
        self.assertEqual(body["plugins"][0]["id"], "fixture-plugin")
        self.assertFalse(body["plugins"][0]["enabled"])

        enabled = await self.client.put(
            "/api/plugins/fixture-plugin/enabled", json={"enabled": True}
        )
        self.assertEqual(enabled.status_code, 200)
        self.assertTrue(enabled.json()["enabled"])

        health = await self.client.post("/api/plugins/fixture-plugin/health")
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()["result"]["ok"])

    async def test_dependencies_update_and_uninstall_endpoints(self):
        dependencies = await self.client.get("/api/plugins/fixture-plugin/dependencies")
        self.assertEqual(dependencies.status_code, 200)
        self.assertTrue(dependencies.json()["ready"])

        update = await self.client.post("/api/plugins/fixture-plugin/update", json={})
        self.assertEqual(update.status_code, 409)
        self.assertIn("本地", update.json()["detail"])

        removed = await self.client.delete("/api/plugins/fixture-plugin")
        self.assertEqual(removed.status_code, 200)
        self.assertTrue(removed.json()["removed"])
        listing = await self.client.get("/api/plugins")
        self.assertEqual(listing.json()["plugins"], [])

    async def test_unknown_plugin_returns_named_conflict(self):
        response = await self.client.post("/api/plugins/missing/health")
        self.assertEqual(response.status_code, 409)
        self.assertIn("没有找到插件", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()


class PluginV3ProtocolTests(unittest.TestCase):
    """v3 adds factor research and validation without disturbing v1/v2."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.manager = PluginManager(self.home)

    def test_v3_manifests_load_with_the_new_capabilities(self):
        manifest = load_manifest(write_plugin(
            self.base / "v3", api_version="3",
            capabilities=("factor_provider", "backtest_validator"),
        ))
        self.assertEqual(manifest.api_version, "3")
        self.assertIn("factor_provider", manifest.capabilities)

    def test_v1_and_v2_cannot_claim_v3_capabilities(self):
        for version in ("1", "2"):
            source = write_plugin(self.base / f"old-{version}", api_version=version,
                                  capabilities=("factor_provider",))
            with self.assertRaisesRegex(PluginError, "不支持能力"):
                load_manifest(source)

    def test_a_v4_manifest_may_still_provide_earlier_capabilities(self):
        """A newer plugin is not forced to give up the older message shapes.

        Nothing about v4 removes `factor_provider`; requiring an exact version match
        would have made "factor library plus self-improvement agent in one plugin"
        impossible to declare, which is exactly the plugin the lab ships.
        """
        manifest = load_manifest(write_plugin(
            self.base / "v4-mixed", api_version="4",
            capabilities=("factor_provider", "backtest_validator", "strategy_agent"),
        ))
        self.assertEqual(manifest.api_version, "4")
        self.assertEqual(
            manifest.capabilities,
            ("factor_provider", "backtest_validator", "strategy_agent"),
        )

    def test_v1_capabilities_still_load_unchanged_under_a_v3_engine(self):
        manifest = load_manifest(write_plugin(
            self.base / "v1", api_version="1", capabilities=("strategy", "research_tool", "notifier"),
        ))
        self.assertEqual(manifest.api_version, "1")
        self.assertEqual(len(manifest.capabilities), 3)

    def test_a_v2_analytics_plugin_is_unaffected(self):
        manifest = load_manifest(write_plugin(
            self.base / "v2", api_version="2", capabilities=("analytics",),
        ))
        self.assertEqual(manifest.capabilities, ("analytics",))

    def test_an_unknown_capability_is_still_refused_at_v3(self):
        source = write_plugin(self.base / "v3-bad", api_version="3", capabilities=("factor_provider", "trading"))
        with self.assertRaisesRegex(PluginError, "不支持的能力"):
            load_manifest(source)

    def test_the_v3_messages_are_typed(self):
        from quantdesk.plugins.protocol import (
            FactorComputeRequest,
            FactorDefinition,
            ValidationAnalyzeResult,
        )

        definition = FactorDefinition(
            id="vibe:momentum-20", name="20周期动量", family="momentum",
            requiredFields=["close"], warmupBars=20, supportedTimeframes=["15m", "1h"],
            implementationVersion="0.1.15", sources=["bybit"],
        )
        self.assertEqual(definition.warmupBars, 20)
        request = FactorComputeRequest(symbol="BTCUSDT", timeframe="1h", factorIds=["vibe:momentum-20"])
        self.assertEqual(request.factorIds, ["vibe:momentum-20"])
        result = ValidationAnalyzeResult(provider="quantdesk-stats")
        # The path simulation must never present itself as a significance test.
        self.assertIn("不构成策略显著性检验", result.pathRisk.interpretation)

    def test_a_factor_series_rejects_nonsense(self):
        from pydantic import ValidationError

        from quantdesk.plugins.protocol import FactorDefinition

        with self.assertRaises(ValidationError):
            FactorDefinition(id="has spaces", name="x")
        with self.assertRaises(ValidationError):
            FactorDefinition(id="ok-id", name="x", warmupBars=-1)


class PluginContextVersionTests(unittest.TestCase):
    """Each plugin is spoken to in the version it declared, not the engine's."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.manager = PluginManager(self.home)

    def _context_for(self, plugin_id: str, version: str, capabilities: tuple[str, ...]) -> dict:
        """Invoke a plugin that echoes the context it was handed."""
        source = write_plugin(self.base / plugin_id, plugin_id=plugin_id, api_version=version,
                              capabilities=capabilities)
        (source / "plugin.py").write_text(
            textwrap.dedent(
                """
                import json
                import sys

                request = json.loads(sys.stdin.readline())
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": request.get("context", {})}))
                """
            ),
            encoding="utf-8",
        )
        self.manager.install(str(source))
        return self.manager.invoke(plugin_id, "health", {}, allow_disabled=True)["result"]

    def test_a_v1_plugin_gets_a_v1_context(self):
        context = self._context_for("legacy-one", "1", ("strategy",))
        self.assertEqual(context["api_version"], "1", "插件按自己声明的版本被调用")
        self.assertEqual(context["engine_api_version"], "4")
        self.assertEqual(context["supported_api_versions"], ["1", "2", "3", "4"])

    def test_a_v2_plugin_gets_a_v2_context(self):
        context = self._context_for("portfolio-two", "2", ("analytics",))
        self.assertEqual(context["api_version"], "2")
        self.assertEqual(context["engine_api_version"], "4")

    def test_a_v3_plugin_gets_a_v3_context(self):
        context = self._context_for("factors-three", "3", ("factor_provider", "backtest_validator"))
        self.assertEqual(context["api_version"], "3")
        self.assertEqual(context["engine_api_version"], "4")

    def test_a_v4_plugin_gets_a_v4_context(self):
        context = self._context_for("agent-four", "4", ("strategy_agent",))
        self.assertEqual(context["api_version"], "4")
        self.assertEqual(context["engine_api_version"], "4")

    def test_the_shipped_adapters_still_declare_their_own_versions(self):
        # The two adapters that exist today must keep their declared versions:
        # OpenBB is v1, Fincept is v2, and neither becomes a v3 plugin by accident.
        adapters = Path(__file__).resolve().parents[2] / "plugins"
        self.assertEqual(load_manifest(adapters / "openbb-research").api_version, "1")
        self.assertEqual(load_manifest(adapters / "fincept-analytics").api_version, "2")


class PluginV4AgentTests(unittest.TestCase):
    """v4 的自我改进代理：能力门禁、白名单校验、越界即拒。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.manager = PluginManager(self.base / "home")

    def _agent_plugin(self, plugin_id: str = "agent-four", *, api_version: str = "4",
                      capabilities: tuple[str, ...] = ("strategy_agent",)) -> str:
        """A v4 provider whose manifest space is narrow on purpose."""
        source = write_plugin(self.base / plugin_id, plugin_id=plugin_id,
                              api_version=api_version, capabilities=capabilities)
        (source / "plugin.py").write_text(
            textwrap.dedent(
                """
                import json
                import sys

                MANIFEST = {
                    "agentVersion": "fixture-agent/1",
                    "mode": "deterministic_search",
                    "proposalSpace": {
                        "factorIds": ["vibe.momentum.24", "vibe.atr.14"],
                        "parameters": {"fastPeriod": [5, 30], "slowPeriod": [20, 120]},
                        "ruleTemplates": ["threshold"],
                        "maxProposalsPerRound": 8,
                        "maxRounds": 5,
                    },
                    "requires": ["candles"],
                    "never": ["order_placement", "venue_data", "keys", "frontend"],
                    "providerVersion": "fixture/1",
                }


                DEFAULT = [{
                    "proposalId": "p-1",
                    "kind": "parameter_set",
                    "factorIds": ["vibe.momentum.24"],
                    "parameters": {"fastPeriod": 9},
                    "hypothesis": "动量延续",
                    "expectedFailureMode": "震荡市反复止损",
                }]


                def proposals(params):
                    # A test writes proposals.json next to the plugin to say what it
                    # should answer with. The request itself carries no back door:
                    # the protocol drops fields it does not know, which is exactly
                    # the property that keeps a proposal from smuggling data in.
                    import os
                    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proposals.json")
                    if os.path.exists(path):
                        with open(path, encoding="utf-8") as handle:
                            return json.load(handle)
                    return DEFAULT


                request = json.loads(sys.stdin.readline())
                method = request["method"]
                params = request.get("params") or {}
                if method == "health":
                    result = {"ok": True}
                elif method == "agent.manifest":
                    result = MANIFEST
                elif method == "agent.propose":
                    result = {"proposals": proposals(params), "warnings": [], "stopReason": ""}
                elif method == "agent.reflect":
                    result = {"reflection": "上一轮偏保守", "proposals": proposals(params),
                              "stopReason": "", "warnings": []}
                else:
                    result = {"error": "unsupported"}
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}))
                """
            ),
            encoding="utf-8",
        )
        self.manager.install(str(source))
        self.manager.set_enabled(plugin_id, True)
        return plugin_id

    def _proposals_file(self, proposals: list[dict]) -> None:
        """Tell the *installed* fixture what to answer with.

        The installed copy is what runs, so the answer file goes there - writing it
        to the source directory would test nothing.
        """
        target = self.manager.install_root / "agent-four" / "proposals.json"
        target.write_text(json.dumps(proposals, ensure_ascii=False), encoding="utf-8")

    def _request(self, **overrides):
        from quantdesk.plugins import AgentProposeRequest

        body = {
            "campaignId": "cmp-1", "round": 1, "snapshotHash": "snap",
            "universe": ["BTCUSDT"], "interval": "1h", "group": "crypto",
            "factorIds": ["vibe.momentum.24"],
            "dataProfile": {"bars": 1000},
            "budget": {"proposals": 8, "deadlineMs": 60_000},
        }
        body.update(overrides)
        return AgentProposeRequest(**body)

    def test_a_version_three_plugin_cannot_claim_the_agent(self):
        source = write_plugin(self.base / "v3-agent", api_version="3",
                              capabilities=("strategy_agent",))
        with self.assertRaisesRegex(PluginError, "不支持能力|不支持的能力"):
            load_manifest(source)

    def test_the_manifest_declares_the_bounds_and_the_refusals(self):
        plugin_id = self._agent_plugin()
        registry = PluginRegistry(self.manager)
        manifest = registry.agent_manifest(plugin_id)
        self.assertEqual(manifest.mode, "deterministic_search")
        self.assertEqual(manifest.proposalSpace.maxProposalsPerRound, 8)
        self.assertIn("order_placement", manifest.never)

    def test_a_proposal_inside_the_declared_space_is_accepted(self):
        plugin_id = self._agent_plugin()
        registry = PluginRegistry(self.manager)
        manifest = registry.agent_manifest(plugin_id)
        result = registry.agent_propose(plugin_id, self._request(), manifest)
        self.assertEqual(len(result.proposals), 1)
        self.assertEqual(result.proposals[0].parameters["fastPeriod"], 9)

    def test_a_factor_outside_the_manifest_space_is_refused(self):
        plugin_id = self._agent_plugin()
        registry = PluginRegistry(self.manager)
        manifest = registry.agent_manifest(plugin_id)
        self._proposals_file([{
            "proposalId": "p-bad", "factorIds": ["vibe.momentum.96"],
            "parameters": {"fastPeriod": 9}, "hypothesis": "换个因子",
        }])
        request = self._request()
        with self.assertRaisesRegex(PluginError, "未声明的因子"):
            registry.agent_propose(plugin_id, request, manifest)

    def test_a_parameter_outside_its_range_is_refused(self):
        plugin_id = self._agent_plugin()
        registry = PluginRegistry(self.manager)
        manifest = registry.agent_manifest(plugin_id)
        self._proposals_file([{
            "proposalId": "p-wide", "factorIds": ["vibe.momentum.24"],
            "parameters": {"fastPeriod": 900}, "hypothesis": "更快",
        }])
        request = self._request()
        with self.assertRaisesRegex(PluginError, "超出声明范围"):
            registry.agent_propose(plugin_id, request, manifest)

    def test_an_undeclared_rule_template_is_refused(self):
        plugin_id = self._agent_plugin()
        registry = PluginRegistry(self.manager)
        manifest = registry.agent_manifest(plugin_id)
        self._proposals_file([{
            "proposalId": "p-rule", "kind": "rule", "factorIds": ["vibe.atr.14"],
            "parameters": {}, "rule": {"type": "ml_signal"}, "hypothesis": "用模型",
        }])
        request = self._request()
        with self.assertRaisesRegex(PluginError, "未声明的规则模板"):
            registry.agent_propose(plugin_id, request, manifest)

    def test_a_proposal_that_reports_performance_is_refused_by_the_protocol(self):
        """提案里带收益字段 = 越界：v4 的提案类型直接拒收多余字段。"""
        plugin_id = self._agent_plugin()
        registry = PluginRegistry(self.manager)
        manifest = registry.agent_manifest(plugin_id)
        self._proposals_file([{
            "proposalId": "p-pnl", "factorIds": ["vibe.momentum.24"],
            "parameters": {"fastPeriod": 9}, "hypothesis": "x", "netPnl": 1234.5,
        }])
        request = self._request()
        with self.assertRaisesRegex(PluginError, "不符合业务协议"):
            registry.agent_propose(plugin_id, request, manifest)

    def test_more_proposals_than_the_round_budget_is_refused(self):
        plugin_id = self._agent_plugin()
        registry = PluginRegistry(self.manager)
        manifest = registry.agent_manifest(plugin_id)
        many = [{"proposalId": f"p-{index}", "factorIds": ["vibe.momentum.24"],
                 "parameters": {"fastPeriod": 9}, "hypothesis": "h"} for index in range(9)]
        self._proposals_file(many)
        request = self._request(budget={"proposals": 8, "deadlineMs": 60_000})
        with self.assertRaisesRegex(PluginError, "超过本轮预算"):
            registry.agent_propose(plugin_id, request, manifest)

    def test_more_proposals_than_the_manifest_allows_is_refused_even_with_budget(self):
        """本轮预算够、但 manifest 自己声明的上限更小：以 manifest 为准。"""
        plugin_id = self._agent_plugin()
        registry = PluginRegistry(self.manager)
        manifest = registry.agent_manifest(plugin_id)
        many = [{"proposalId": f"p-{index}", "factorIds": ["vibe.momentum.24"],
                 "parameters": {"fastPeriod": 9}, "hypothesis": "h"} for index in range(9)]
        self._proposals_file(many)
        request = self._request(budget={"proposals": 16, "deadlineMs": 60_000})
        with self.assertRaisesRegex(PluginError, "超过 manifest 声明的每轮上限"):
            registry.agent_propose(plugin_id, request, manifest)

    def test_reflection_comes_back_with_proposals_and_the_same_checks(self):
        from quantdesk.plugins import AgentReflectRequest

        plugin_id = self._agent_plugin()
        registry = PluginRegistry(self.manager)
        manifest = registry.agent_manifest(plugin_id)
        request = AgentReflectRequest(
            campaignId="cmp-1", round=2,
            trials=[{"proposalId": "p-1", "segment": "validation", "sharpe": 0.8,
                     "verdict": "warn", "reason": "验证段衰减"}],
            budget={"proposals": 8, "deadlineMs": 60_000}, remainingRounds=3,
        )
        result = registry.agent_reflect(plugin_id, request, manifest)
        self.assertTrue(result.reflection)
        self.assertEqual(len(result.proposals), 1)

    def test_the_agent_is_never_handed_the_test_segment(self):
        """试次摘要的类型只允许 train/validation——这是样本外封存的类型级保证。"""
        from quantdesk.plugins import AgentTrialSummary

        AgentTrialSummary(proposalId="p-1", segment="train")
        AgentTrialSummary(proposalId="p-1", segment="validation")
        with self.assertRaises(Exception):
            AgentTrialSummary(proposalId="p-1", segment="test")
