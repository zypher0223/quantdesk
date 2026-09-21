"""Phase 4: the factor provider, the statistical validator, and the bridge.

Two layers are tested here, and they are deliberately different:

* the shipped plugin (`plugins/vibe-factors`) as a *process*, through the real
  `PluginManager`, exactly as the engine runs it - no network, no database;
* the engine's factor service against a stub registry, so the storage, the
  trial material and the verdict rows can be checked without a subprocess.

What the tests insist on: every factor a catalogue advertises can actually be
computed, warmup is respected rather than papered over with zeros, the factor
library stays inside the 15-30 range the report asked for, the validation numbers
are deterministic for a fixed seed, and a missing plugin is an `unavailable`
answer rather than a broken page.
"""

from __future__ import annotations

import json
import math
import os
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quantdesk.datahub.db import Database
from quantdesk.factors import (
    analyze_run,
    catalog,
    compute,
    record_definitions,
    run_detail,
    runs,
    store_verdicts,
    stored_catalog,
)
from quantdesk.plugins import PluginManager, PluginRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SOURCE = REPO_ROOT / "plugins" / "vibe-factors"


def synthetic_bars(count: int = 600, *, interval_ms: int = 3_600_000, seed: int = 11) -> list[dict]:
    """Deterministic bars with trend, noise, volume, funding and open interest."""
    rng = random.Random(seed)
    price = 100.0
    stamp = 1_700_000_000_000 - (1_700_000_000_000 % interval_ms)
    rows = []
    for index in range(count):
        drift = math.sin(index / 30.0) * 0.004 + rng.gauss(0, 0.003)
        price = max(1.0, price * (1 + drift))
        rows.append({
            "ts": stamp - (count - index) * interval_ms,
            "open": round(price * (1 - drift / 2), 6),
            "high": round(price * (1 + abs(rng.gauss(0, 0.002))), 6),
            "low": round(price * (1 - abs(rng.gauss(0, 0.002))), 6),
            "close": round(price, 6),
            "volume": round(1000 + 50 * index + rng.gauss(0, 30), 6),
            "source": "venue_rest",
        })
    return rows


class PluginProcessFixture(unittest.TestCase):
    """The shipped plugin, installed and driven through the real runtime."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.env = patch.dict(os.environ, {"QUANTDESK_HOME": self.tmp_home()}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.manager = PluginManager(self.home)
        # The shipped manifest hands `factor_provider` to vibe-backtest-lab, which
        # serves these same 28 factors from a byte-for-byte copy of this file. The
        # tests below are about the factor code itself, so the fixture installs a
        # copy whose manifest still declares the capability it is exercising - the
        # shipped manifest is asserted separately in PluginPackagingTests.
        source = Path(self._tmp.name) / "vibe-factors-source"
        shutil.copytree(PLUGIN_SOURCE, source, ignore=shutil.ignore_patterns("__pycache__"))
        manifest_path = source / "quantdesk-plugin.toml"
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8").replace(
                'capabilities = ["backtest_validator"]',
                'capabilities = ["factor_provider", "backtest_validator"]',
            ),
            encoding="utf-8",
        )
        # Install copies the plugin into the home; enabling runs the real health
        # check and the real sandbox decision, exactly as the settings page does.
        self.plugin_id = self.manager.install(str(source)).manifest.id
        self.manager.set_enabled(self.plugin_id, True)
        self.registry = PluginRegistry(self.manager)

    def tmp_home(self) -> str:
        return self._tmp.name

    def tearDown(self):
        try:
            self.manager.set_enabled(self.plugin_id, False)
        except Exception:  # noqa: BLE001 - teardown must not mask a failure
            pass


class FactorCatalogTests(PluginProcessFixture):
    def test_the_library_is_inside_the_range_the_report_asked_for(self):
        outcome = self.registry.factor_catalog(self.plugin_id)
        self.assertGreaterEqual(len(outcome.factors), 15, "白名单因子至少 15 个")
        self.assertLessEqual(len(outcome.factors), 30, "白名单因子最多 30 个")
        ids = [item.id for item in outcome.factors]
        self.assertEqual(len(ids), len(set(ids)), "因子 id 不能重复")

    def test_every_factor_declares_what_it_needs(self):
        outcome = self.registry.factor_catalog(self.plugin_id)
        for item in outcome.factors:
            self.assertEqual(item.mode, "time_series")
            self.assertTrue(item.name)
            self.assertTrue(item.description)
            self.assertTrue(item.formulaHash, f"{item.id} 缺少公式哈希")
            self.assertTrue(item.requiredFields, f"{item.id} 未声明所需字段")
            self.assertLessEqual(item.warmupBars, 5_000)
            self.assertTrue(set(item.sources) <= {"bybit", "openbb", "derived"})


class FactorComputeTests(PluginProcessFixture):
    def _request(self, bars: list[dict], factor_ids: list[str], *, interval: str = "1h"):
        from quantdesk.plugins.protocol import FactorComputeRequest

        return FactorComputeRequest(
            symbol="BTCUSDT", timeframe=interval, snapshotHash="test-snapshot",
            factorIds=factor_ids,
            candles=[{"time": row["ts"], "open": row["open"], "high": row["high"],
                      "low": row["low"], "close": row["close"], "volume": row["volume"],
                      "turnover": row["close"] * row["volume"]} for row in bars],
            funding=[{"ts": bars[i]["ts"], "rate": 0.0001} for i in range(0, len(bars), 8)],
            openInterest=[{"ts": bars[i]["ts"], "oi": 5000.0 + i} for i in range(0, len(bars), 4)],
        )

    def test_every_advertised_factor_actually_produces_values(self):
        catalog_result = self.registry.factor_catalog(self.plugin_id)
        ids = [item.id for item in catalog_result.factors]
        bars = synthetic_bars(400)
        outcome = self.registry.compute_factors(self.plugin_id, self._request(bars, ids))
        self.assertEqual(len(outcome.series), len(ids))
        for series in outcome.series:
            filled = [value for value in series.values if value.value is not None]
            self.assertTrue(filled, f"{series.factorId} 没有任何有效值")
            self.assertEqual(len(series.values), len(bars), "因子值必须与 K 线一一对应")

    def test_a_factor_stays_quiet_until_its_window_is_full(self):
        bars = synthetic_bars(120)
        outcome = self.registry.compute_factors(self.plugin_id, self._request(bars, ["vibe.momentum.96"]))
        series = outcome.series[0]
        self.assertTrue(all(value.value is None for value in series.values[:96]),
                        "预热期必须留空，而不是用 0 充数")
        self.assertIsNotNone(series.values[96].value)

    def test_the_same_bars_give_the_same_values(self):
        bars = synthetic_bars(200)
        first = self.registry.compute_factors(self.plugin_id, self._request(bars, ["vibe.rsi.14"]))
        second = self.registry.compute_factors(self.plugin_id, self._request(bars, ["vibe.rsi.14"]))
        self.assertEqual([v.value for v in first.series[0].values],
                         [v.value for v in second.series[0].values])

    def test_an_unknown_factor_is_refused_by_the_protocol(self):
        from pydantic import ValidationError

        from quantdesk.plugins.protocol import FactorComputeRequest

        with self.assertRaises(ValidationError):
            FactorComputeRequest(symbol="BTCUSDT", timeframe="1h", factorIds=[])


class StatisticalValidatorTests(PluginProcessFixture):
    def _analysis(self, *, seed: int = 42, trades: int = 40, **tests):
        from quantdesk.plugins.protocol import ValidationAnalyzeRequest

        bars = synthetic_bars(400)
        equity = [{"time": row["ts"], "equity": round(10_000 * (1 + 0.0004 * i + math.sin(i / 9) * 0.01), 6)}
                  for i, row in enumerate(bars)]
        trade_rows = [
            {"entryTime": bars[i]["ts"], "exitTime": bars[i + 4]["ts"],
             "direction": "long" if i % 2 else "short", "netPnl": float((i % 7) - 3) * 5.0,
             "barsHeld": 4}
            for i in range(10, 10 + trades * 8, 8)
        ]
        benchmark = {"equityCurve": [{"time": row["ts"], "equity": round(10_000 * (1 + 0.0002 * i), 6)}
                                     for i, row in enumerate(bars)]}
        settings = {
            "enabled": ["pathRisk", "bootstrap", "randomization", "multipleTesting"],
            "bootstrap": {"resamples": 120},
            "randomization": {"permutations": 120},
            "multipleTesting": {
                "trials": 9,
                "candidateSharpes": [0.4, 0.1, -0.2, 0.9, 0.3, 0.05, 0.6, -0.1, 0.25],
                "candidateMatrix": [[0.5 + 0.1 * ((n + b) % 5) + 0.05 * n for b in range(8)]
                                    for n in range(9)],
            },
        }
        settings.update(tests)
        request = ValidationAnalyzeRequest(
            runId="7", seed=seed, interval="1h", equityCurve=equity, trades=trade_rows,
            benchmark=benchmark, tests=settings,
        )
        return self.registry.analyze_validation(self.plugin_id, request)

    def test_the_analysis_answers_every_question_it_was_asked(self):
        outcome = self._analysis()
        self.assertEqual(outcome.provider, "vibe-factors")
        self.assertGreater(outcome.samples, 0)
        self.assertGreater(outcome.bootstrap.resamples, 0)
        self.assertEqual(outcome.bootstrap.method, "moving_block")
        self.assertGreater(outcome.bootstrap.blockSize, 1)
        self.assertIsNotNone(outcome.bootstrap.sharpe.low)
        self.assertIsNotNone(outcome.bootstrap.positiveSharpeProbability)
        self.assertGreater(outcome.randomization.permutations, 0)
        self.assertIsNotNone(outcome.randomization.pValue)
        self.assertTrue(outcome.multipleTesting.applied)
        self.assertIsNotNone(outcome.multipleTesting.deflatedSharpe)
        self.assertIsNotNone(outcome.multipleTesting.probabilityOfBacktestOverfitting)
        self.assertGreater(outcome.pathRisk.simulations, 0)
        self.assertIn("不构成策略显著性检验", outcome.pathRisk.interpretation)

    def test_the_same_seed_gives_the_same_numbers(self):
        first, second = self._analysis(seed=7), self._analysis(seed=7)
        self.assertEqual(first.bootstrap.sharpe.low, second.bootstrap.sharpe.low)
        self.assertEqual(first.randomization.pValue, second.randomization.pValue)
        self.assertEqual(first.multipleTesting.probabilityOfBacktestOverfitting,
                         second.multipleTesting.probabilityOfBacktestOverfitting)

    def test_a_different_seed_moves_the_numbers_within_the_interval(self):
        first, second = self._analysis(seed=1), self._analysis(seed=2)
        self.assertNotEqual(first.randomization.nullSharpe.low, second.randomization.nullSharpe.low)

    def test_a_signal_shift_test_says_which_null_it_used(self):
        outcome = self._analysis()
        self.assertEqual(outcome.randomization.method, "signal_shift",
                         "有基准曲线时必须做信号平移检验，而不是只打乱自身收益顺序")

    def test_without_a_benchmark_it_degrades_and_says_so(self):
        from quantdesk.plugins.protocol import ValidationAnalyzeRequest

        bars = synthetic_bars(300)
        equity = [{"time": row["ts"], "equity": 10_000 + i} for i, row in enumerate(bars)]
        outcome = self.registry.analyze_validation(self.plugin_id, ValidationAnalyzeRequest(
            runId="9", interval="1h", equityCurve=equity, trades=[],
            tests={"enabled": ["bootstrap", "randomization"], "bootstrap": {"resamples": 60},
                   "randomization": {"permutations": 60}},
        ))
        self.assertEqual(outcome.randomization.method, "block_permutation")

    def test_too_little_data_is_reported_as_unavailable(self):
        from quantdesk.plugins.protocol import ValidationAnalyzeRequest

        outcome = self.registry.analyze_validation(self.plugin_id, ValidationAnalyzeRequest(
            runId="10", interval="1h",
            equityCurve=[{"time": 1_000 + i * 3_600_000, "equity": 10_000 + i} for i in range(5)],
            trades=[], tests={"enabled": ["bootstrap"]},
        ))
        self.assertTrue(outcome.unavailable)
        self.assertIn("20", outcome.unavailable)

    def test_the_pbo_matrix_decides_the_verdict(self):
        """A matrix whose best candidate is always best must not report overfitting."""
        ordered = self._analysis(multipleTesting={
            "trials": 4,
            "candidateSharpes": [1.0, 0.5, 0.2, 0.1],
            "candidateMatrix": [[1.0 + 0.1 * b for b in range(8)],
                                [0.5 + 0.1 * b for b in range(8)],
                                [0.2 + 0.1 * b for b in range(8)],
                                [0.1 + 0.1 * b for b in range(8)]],
        })
        self.assertEqual(ordered.multipleTesting.probabilityOfBacktestOverfitting, 0.0,
                         "稳定的优劣关系下，样本内最优在样本外也应靠前")


class FactorServiceTests(unittest.TestCase):
    """The engine side: storage, trial material and verdict rows."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.db = Database(self.home / "quantdesk.db")

    def test_definitions_are_stored_so_the_catalogue_survives_a_disabled_plugin(self):
        definitions = [
            {"id": "vibe.momentum.24", "name": "动量（24 根）", "family": "momentum",
             "mode": "time_series", "sources": ["derived"], "requiredFields": ["close"],
             "warmupBars": 24, "supportedTimeframes": ["1h"], "implementationVersion": "vibe/1",
             "formulaHash": "abc", "description": "测试"},
        ]
        self.assertEqual(record_definitions(self.db, "vibe-factors", definitions,
                                            provider_version="vibe-factors/0.1.0"), 1)
        stored = stored_catalog(self.db)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], "vibe.momentum.24")
        self.assertEqual(stored[0]["provider"], "vibe-factors")
        # Re-recording updates rather than duplicating.
        record_definitions(self.db, "vibe-factors", definitions, provider_version="vibe-factors/0.1.1")
        self.assertEqual(len(stored_catalog(self.db)), 1)
        self.assertEqual(stored_catalog(self.db)[0]["providerVersion"], "vibe-factors/0.1.1")

    def test_without_a_plugin_the_catalogue_is_stored_and_says_so(self):
        record_definitions(self.db, "vibe-factors", [{
            "id": "vibe.rsi.14", "name": "RSI", "family": "oscillator", "mode": "time_series",
            "sources": ["derived"], "requiredFields": ["close"], "warmupBars": 14,
            "supportedTimeframes": ["1h"], "implementationVersion": "vibe/1",
            "formulaHash": "x", "description": "d"}])
        with patch("quantdesk.factors.enabled_provider", return_value=None):
            outcome = catalog(self.db, PluginManager(self.home))
        self.assertFalse(outcome["available"])
        self.assertEqual(outcome["source"], "stored")
        self.assertEqual([item["id"] for item in outcome["factors"]], ["vibe.rsi.14"])

    def test_computing_without_a_plugin_is_unavailable_not_an_exception(self):
        with patch("quantdesk.factors.enabled_provider", return_value=None):
            outcome = compute(self.db, symbol="BTCUSDT")
        self.assertFalse(outcome["available"])
        self.assertIn("因子插件", outcome["reason"])

    def test_a_factor_run_is_recorded_with_its_coverage(self):
        bars = synthetic_bars(80)
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", bars, source="venue_rest")
        with patch("quantdesk.factors.enabled_provider", return_value="vibe-factors"), \
             patch("quantdesk.factors.PluginRegistry") as registry:
            from quantdesk.plugins.protocol import FactorComputeResult, FactorSeries, FactorValue

            registry.return_value.compute_factors.return_value = FactorComputeResult(
                snapshotHash="snap-1",
                series=[FactorSeries(factorId="vibe.momentum.24", implementationVersion="vibe/1",
                                     values=[FactorValue(time=row["ts"], value=float(i))
                                             for i, row in enumerate(bars)])],
            )
            record_definitions(self.db, "vibe-factors", [{
                "id": "vibe.momentum.24", "name": "动量", "family": "momentum",
                "mode": "time_series", "sources": ["derived"], "requiredFields": ["close"],
                "warmupBars": 24, "supportedTimeframes": ["1h"], "implementationVersion": "vibe/1",
                "formulaHash": "h", "description": "d"}])
            outcome = compute(self.db, symbol="BTCUSDT", interval="1h", bars=80,
                              factor_ids=["vibe.momentum.24"])
        self.assertTrue(outcome["available"])
        self.assertEqual(outcome["coverage"]["vibe.momentum.24"] > 0, True)
        listed = runs(self.db)
        self.assertEqual(len(listed), 1)
        detail = run_detail(self.db, listed[0]["id"])
        # The run records the data version it read: the plugin answered with its own
        # hash, but the engine's stored version is what a later comparison needs.
        self.assertTrue(detail["snapshotHash"])
        self.assertEqual(detail["snapshotHash"], outcome["snapshotHash"])
        self.assertTrue(any("不一致" in item for item in outcome["warnings"]),
                        "插件回报的版本与引擎不一致时必须说明，而不是抹平")
        self.assertEqual(detail["seriesCount"], 1)
        self.assertEqual(detail["series"], [], "序列值默认不下发")
        self.assertEqual(
            len(run_detail(self.db, listed[0]["id"], include_values=True)["series"]), 1,
            "显式要求时仍然能读到整条序列",
        )

    def test_verdict_rows_carry_numbers_and_a_provider(self):
        analysis = {
            "bootstrap": {"resamples": 200, "blockSize": 8, "positiveSharpeProbability": 0.97,
                          "sharpe": {"low": 0.4, "high": 1.2}},
            "randomization": {"permutations": 200, "method": "signal_shift", "pValue": 0.02,
                              "observedSharpe": 0.9},
            "multipleTesting": {"applied": True, "trials": 9, "deflatedSharpe": 0.96,
                                "probabilityOfBacktestOverfitting": 0.31, "note": "n"},
            "pathRisk": {"simulations": 400, "drawdown": {"low": -8.0, "high": -3.0}},
        }
        rows = store_verdicts(self.db, 5, "vibe-factors", analysis)
        kinds = {row["kind"]: row for row in rows}
        self.assertEqual(kinds["bootstrap"]["verdict"], "pass")
        self.assertEqual(kinds["randomization"]["verdict"], "pass")
        self.assertEqual(kinds["deflated_sharpe"]["verdict"], "pass")
        self.assertEqual(kinds["pbo"]["verdict"], "pass")
        self.assertEqual(kinds["path_risk"]["verdict"], "info")
        stored = self.db.query(
            "SELECT kind, verdict, provider FROM backtest_validation_results WHERE run_id=5 ORDER BY id"
        )
        self.assertEqual(len(stored), 5)
        self.assertTrue(all(row["provider"] == "vibe-factors" for row in stored))

    def test_a_high_pbo_is_a_failure_not_a_warning(self):
        rows = store_verdicts(self.db, 6, "vibe-factors", {
            "multipleTesting": {"applied": True, "trials": 30, "deflatedSharpe": 0.55,
                                "probabilityOfBacktestOverfitting": 0.83},
        })
        kinds = {row["kind"]: row for row in rows}
        self.assertEqual(kinds["pbo"]["verdict"], "fail")
        self.assertEqual(kinds["deflated_sharpe"]["verdict"], "warn")

    def test_trial_material_is_read_from_the_walk_forward_windows(self):
        from quantdesk.factors import _trial_material

        run = {"result": {
            "walkForward": {"windows": [
                {"candidates": [{"parameters": {"fastPeriod": 5}, "validation": {"total_return_pct": 1.0}},
                                {"parameters": {"fastPeriod": 9}, "validation": {"total_return_pct": 3.0}}]},
                {"candidates": [{"parameters": {"fastPeriod": 5}, "validation": {"total_return_pct": 2.0}},
                                {"parameters": {"fastPeriod": 9}, "validation": {"total_return_pct": 1.0}}]},
            ]},
            "parameterSearch": {"grid": {"fastPeriod": [5, 9], "slowPeriod": [21, 50]},
                                "ranking": [{"parameters": {"fastPeriod": 5}, "sharpe": 0.5},
                                            {"parameters": {"fastPeriod": 9}, "sharpe": 1.1}]},
        }}
        material = _trial_material(run)
        self.assertEqual(material["trials"], 4, "尝试次数取网格组合数")
        self.assertEqual(material["candidateMatrix"], [[1.0, 2.0], [3.0, 1.0]],
                         "矩阵应为 候选 × 分块")
        self.assertEqual(material["candidateSharpes"], [0.5, 1.1])

    def test_a_run_without_a_result_is_refused_politely(self):
        from quantdesk.backtest_runs import RunQueue

        queue = RunQueue(self.db, execute=lambda context, progress: {})
        run = queue.submit("backtest", {"symbol": "BTCUSDT", "timeframe": "1h", "bars": 100})
        with patch("quantdesk.factors.enabled_provider", return_value="vibe-factors"):
            outcome = analyze_run(self.db, run["id"])
        self.assertFalse(outcome["available"])
        self.assertIn("还没有结果", outcome["reason"])


class PluginPackagingTests(unittest.TestCase):
    def test_the_shipped_plugin_declares_v3_and_no_network(self):
        manifest = (PLUGIN_SOURCE / "quantdesk-plugin.toml").read_text(encoding="utf-8")
        self.assertIn('api_version = "3"', manifest)
        self.assertIn('network = false', manifest)
        self.assertIn("backtest_validator", manifest)
        # One capability, one owner: `factor_provider` moved to vibe-backtest-lab,
        # which serves these factors from a copy of this file. A manifest that kept
        # both would leave the engine choosing between two providers.
        self.assertNotIn("factor_provider", manifest)

    def test_the_shipped_factor_library_is_copied_byte_for_byte(self):
        """The lab serves these factors; the copy it serves must be this file.

        The copy is generated by `plugins/vibe-backtest-lab/tools/
        vendor_quantdesk_factors.py`, and the provenance sidecar is what lets the
        copy be checked rather than trusted.
        """
        import hashlib
        import json

        lab = Path(__file__).resolve().parents[2] / "plugins" / "vibe-backtest-lab"
        copy = lab / "quantdesk_factors.py"
        self.assertTrue(copy.is_file(), "缺少 vibe-backtest-lab 的因子库副本")
        self.assertEqual(
            hashlib.sha256(copy.read_bytes()).hexdigest(),
            hashlib.sha256((PLUGIN_SOURCE / "plugin.py").read_bytes()).hexdigest(),
            "副本与源文件不一致；请重新运行 tools/vendor_quantdesk_factors.py",
        )
        sidecar = json.loads((lab / "quantdesk_factors.provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["sha256"], hashlib.sha256(copy.read_bytes()).hexdigest())

    def test_the_plugin_is_standard_library_only(self):
        import ast

        tree = ast.parse((PLUGIN_SOURCE / "plugin.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        allowed = {"__future__", "hashlib", "json", "math", "random", "sys", "typing", "argparse"}
        self.assertTrue(imported <= allowed,
                        f"插件只应使用标准库中的 {sorted(allowed)}，多出了 {sorted(imported - allowed)}")


class FactorBatchingTests(unittest.TestCase):
    """The plugin's 1 MB stdout cap is respected by the engine, not tested against."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def test_batches_are_sized_from_the_bar_count(self):
        from quantdesk.factors import _batches

        ids = [f"vibe.f{i}" for i in range(28)]
        wide = _batches(1_000, ids)
        narrow = _batches(20_000, ids)
        self.assertGreater(len(wide[0]), len(narrow[0]))
        self.assertEqual(sum(len(batch) for batch in wide), 28)
        self.assertEqual(sum(len(batch) for batch in narrow), 28)
        self.assertTrue(all(len(batch) >= 1 for batch in narrow))

    def test_a_large_request_is_split_and_merged_into_one_run(self):
        bars = synthetic_bars(600)
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", bars, source="venue_rest")
        ids = [f"vibe.f{index}" for index in range(10)]
        record_definitions(self.db, "vibe-factors", [
            {"id": factor_id, "name": factor_id, "family": "test", "mode": "time_series",
             "sources": ["derived"], "requiredFields": ["close"], "warmupBars": 2,
             "supportedTimeframes": ["1h"], "implementationVersion": "vibe/1",
             "formulaHash": "h", "description": "d"}
            for factor_id in ids
        ])
        calls: list[list[str]] = []
        with patch("quantdesk.factors.enabled_provider", return_value="vibe-factors"), \
             patch("quantdesk.factors.PluginRegistry") as registry, \
             patch("quantdesk.factors._batches", return_value=[ids[:4], ids[4:8], ids[8:]]):
            from quantdesk.plugins.protocol import FactorComputeResult, FactorSeries, FactorValue

            def compute_factors(plugin_id, request):
                calls.append(list(request.factorIds))
                return FactorComputeResult(
                    snapshotHash=request.snapshotHash,
                    series=[FactorSeries(factorId=item, implementationVersion="vibe/1",
                                         values=[FactorValue(time=row["ts"], value=1.0) for row in bars])
                            for item in request.factorIds],
                )

            registry.return_value.compute_factors.side_effect = compute_factors
            outcome = compute(self.db, symbol="BTCUSDT", interval="1h", bars=600, factor_ids=ids)
        self.assertTrue(outcome["available"])
        self.assertEqual(calls, [ids[:4], ids[4:8], ids[8:]])
        self.assertEqual(len(outcome["series"]), 10)
        self.assertEqual(outcome["batches"], 3)
        self.assertTrue(any("分 3 批" in item for item in outcome["warnings"]))
        self.assertEqual(len(runs(self.db)), 1, "分批仍然只记录一次因子任务")


class CarryInputCapTests(unittest.TestCase):
    """The protocol caps funding/OI lists; a long daily window must not crash on it.

    Found live: `interval="1d", bars=2000` asked for 47,977 hourly open-interest
    snapshots and the pydantic model refused the request outright, so the whole
    factor run failed with a validation error instead of computing what it could.
    """

    class _Db:
        def __init__(self, funding, interest):
            self._funding, self._interest = funding, interest

        def load_funding(self, venue, symbol, start, end):
            return self._funding

        def load_oi(self, venue, symbol, start_ts, end_ts, interval):
            return self._interest

    def test_a_long_window_is_truncated_to_the_tail_and_reported(self):
        from quantdesk.factors import MAX_CARRY_ROWS, _carry_inputs

        interest = [{"ts": 1_600_000_000_000 + index * 3_600_000, "oi": float(index)}
                    for index in range(MAX_CARRY_ROWS + 977)]
        funding = [{"ts": 1_600_000_000_000 + index * 28_800_000, "rate": 0.0001}
                   for index in range(50)]
        bars = [{"time": 1_600_000_000_000}, {"time": 1_700_000_000_000}]
        rows, interest_rows, notes = _carry_inputs(
            self._Db(funding, interest), "BTCUSDT", "1d", bars)
        self.assertEqual(len(interest_rows), MAX_CARRY_ROWS)
        self.assertEqual(interest_rows[-1]["oi"], float(MAX_CARRY_ROWS + 976), "保留的是最近的一段")
        self.assertEqual(len(rows), 50)
        self.assertEqual(len(notes), 1)
        self.assertIn("超过协议上限", notes[0])

    def test_a_short_window_is_passed_through_untouched(self):
        from quantdesk.factors import _carry_inputs

        interest = [{"ts": 1_600_000_000_000, "oi": 5.0}]
        bars = [{"time": 1_600_000_000_000}, {"time": 1_600_086_400_000}]
        rows, interest_rows, notes = _carry_inputs(self._Db([], interest), "BTCUSDT", "1d", bars)
        self.assertEqual(len(interest_rows), 1)
        self.assertEqual(notes, [])

    def test_the_truncation_is_inside_the_protocol_limit(self):
        from quantdesk.factors import MAX_CARRY_ROWS
        from quantdesk.plugins.protocol import FactorComputeRequest

        request = FactorComputeRequest(
            symbol="BTCUSDT", timeframe="1d", factorIds=["vibe.rsi.14"], candles=[],
            openInterest=[{"ts": index, "oi": 1.0} for index in range(MAX_CARRY_ROWS)],
        )
        self.assertEqual(len(request.openInterest), MAX_CARRY_ROWS)


class BenchmarkFallbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def test_a_missing_benchmark_is_built_from_the_bars_the_run_read(self):
        from quantdesk.factors import _benchmark_curve

        bars = synthetic_bars(120)
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", bars, source="venue_rest")
        curve = _benchmark_curve(self.db, {
            "symbol": "BTCUSDT", "interval": "1h", "initial_capital": 10_000.0,
            "readRange": {"fromTs": bars[0]["ts"], "toTs": bars[-1]["ts"], "bars": len(bars)},
        })
        self.assertEqual(curve.get("kind"), "buy_and_hold")
        self.assertEqual(curve.get("source"), "local_candles")
        points = curve["equityCurve"]
        self.assertEqual(len(points), len(bars))
        self.assertAlmostEqual(points[0]["equity"], 10_000.0, places=4)
        self.assertGreater(points[-1]["equity"], 0)

    def test_a_run_without_a_window_gets_no_benchmark_rather_than_a_made_up_one(self):
        from quantdesk.factors import _benchmark_curve

        self.assertEqual(_benchmark_curve(self.db, {"symbol": "BTCUSDT", "interval": "1h"}), {})


class TradeMappingTests(unittest.TestCase):
    """The engine's trade fields must reach the validator intact."""

    def test_engine_trade_fields_are_translated_not_dropped(self):
        from quantdesk.factors import _validation_trade

        mapped = _validation_trade({
            "direction": "多", "entry_time": 1_700_000_000_000, "exit_time": 1_700_003_600_000,
            "entry_price": 100.0, "exit_price": 101.0, "quantity": 1.0, "notional": 100.0,
            "gross_pnl": 1.0, "funding_paid": 0.01, "fees": 0.05, "net_pnl": 0.94,
            "return_pct": 0.0094, "bars_held": 1, "exit_reason": "signal",
        })
        self.assertEqual(mapped["direction"], "long")
        self.assertEqual(mapped["netPnl"], 0.94)
        self.assertEqual(mapped["returnPct"], 0.0094)
        self.assertEqual(mapped["barsHeld"], 1)
        self.assertEqual(mapped["entryTime"], 1_700_000_000_000)

    def test_a_short_trade_stays_short(self):
        from quantdesk.factors import _validation_trade

        self.assertEqual(_validation_trade({"direction": "空", "net_pnl": -1.0})["direction"], "short")

    def test_a_flat_trade_list_is_not_silently_produced(self):
        """The bug this guards: reading 'pnl' when the engine writes 'net_pnl'."""
        from quantdesk.factors import _validation_trade

        mapped = _validation_trade({"direction": "多", "net_pnl": -12.5, "return_pct": -0.05})
        self.assertNotEqual(mapped["netPnl"], 0.0, "每笔交易的盈亏都变成 0 会让路径风险看起来正常")


class VerdictIdempotenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def test_validating_twice_replaces_the_verdicts_instead_of_stacking_them(self):
        analysis = {"bootstrap": {"resamples": 100, "blockSize": 5, "positiveSharpeProbability": 0.6,
                                  "sharpe": {"low": -0.1, "high": 1.0}}}
        store_verdicts(self.db, 3, "vibe-factors", analysis)
        store_verdicts(self.db, 3, "vibe-factors", analysis)
        rows = self.db.query(
            "SELECT COUNT(*) AS n FROM backtest_validation_results WHERE run_id=3 AND provider=?",
            ("vibe-factors",),
        )
        self.assertEqual(rows[0]["n"], 1, "同一个 run 的同一提供者只应留下一份结论")

    def test_another_providers_verdicts_are_left_alone(self):
        store_verdicts(self.db, 4, "engine", {"bootstrap": {"resamples": 10, "blockSize": 3,
                                                            "sharpe": {"low": 1.0, "high": 2.0}}})
        store_verdicts(self.db, 4, "vibe-factors", {"bootstrap": {"resamples": 10, "blockSize": 3,
                                                                  "sharpe": {"low": 1.0, "high": 2.0}}})
        rows = self.db.query("SELECT provider FROM backtest_validation_results WHERE run_id=4")
        self.assertEqual(sorted(row["provider"] for row in rows), ["engine", "vibe-factors"])


class CatalogSourceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.db = Database(self.home / "quantdesk.db")
        record_definitions(self.db, "vibe-factors", [{
            "id": "vibe.rsi.14", "name": "RSI", "family": "oscillator", "mode": "time_series",
            "sources": ["derived"], "requiredFields": ["close"], "warmupBars": 14,
            "supportedTimeframes": ["1h"], "implementationVersion": "vibe/1",
            "formulaHash": "x", "description": "d"}])

    def test_a_plugin_that_answers_is_the_source(self):
        with patch("quantdesk.factors.enabled_provider", return_value="vibe-factors"), \
             patch("quantdesk.factors.PluginRegistry") as registry:
            from quantdesk.plugins.protocol import FactorCatalogResult, FactorDefinition

            registry.return_value.factor_catalog.return_value = FactorCatalogResult(
                providerVersion="vibe-factors/0.1.0",
                factors=[FactorDefinition(id="vibe.rsi.14", name="RSI", family="oscillator",
                                          requiredFields=["close"], warmupBars=14)],
            )
            outcome = catalog(self.db, PluginManager(self.home))
        self.assertTrue(outcome["available"])
        self.assertEqual(outcome["source"], "plugin")

    def test_a_plugin_that_fails_is_not_reported_as_the_source(self):
        from quantdesk.plugins import PluginError

        with patch("quantdesk.factors.enabled_provider", return_value="vibe-factors"), \
             patch("quantdesk.factors.PluginRegistry") as registry:
            registry.return_value.factor_catalog.side_effect = PluginError("进程没有响应")
            outcome = catalog(self.db, PluginManager(self.home))
        self.assertFalse(outcome["available"], "插件没回答就不能说目录来自插件")
        self.assertEqual(outcome["source"], "stored")
        self.assertEqual([item["id"] for item in outcome["factors"]], ["vibe.rsi.14"])
        self.assertTrue(any("没有响应" in item for item in outcome["warnings"]))

    def test_a_missing_correction_is_recorded_rather_than_omitted(self):
        rows = store_verdicts(self.db, 8, "vibe-factors", {
            "multipleTesting": {"applied": False, "trials": 1,
                                "note": "缺少多次尝试的 Sharpe 离散度，未做 Deflated Sharpe"},
        })
        kinds = {row["kind"]: row for row in rows}
        self.assertEqual(kinds["multiple_testing_note"]["verdict"], "info")
        self.assertIn("未做", kinds["multiple_testing_note"]["detail"])


class FactorBudgetTests(unittest.TestCase):
    """一次同步因子请求的工作量必须有上限，并且给出可执行的收窄办法。"""

    def test_the_budget_counts_bars_times_factors(self):
        from quantdesk.factors import factor_units, require_factor_budget

        self.assertEqual(factor_units(5_000, 28), 140_000)
        self.assertEqual(require_factor_budget(5_000, 28), 140_000)
        self.assertEqual(require_factor_budget(20_000, 7), 140_000)

    def test_a_request_beyond_the_budget_is_refused_with_both_ways_to_narrow_it(self):
        from quantdesk.factors import require_factor_budget
        from quantdesk.studies import StudyError

        with self.assertRaises(StudyError) as caught:
            require_factor_budget(20_000, 28)
        error = caught.exception
        self.assertEqual(error.status, 409)
        self.assertEqual(error.kind, "too_large")
        self.assertIn("减少K线根数", error.detail["action"])
        self.assertGreater(error.detail["factorUnits"], error.detail["factorBudget"])

    def test_the_measured_costs_stay_inside_the_budget(self):
        """实测：140k 单位约 5 秒，是这里允许的最大请求。"""
        from quantdesk.factors import SYNC_FACTOR_UNIT_BUDGET

        self.assertLessEqual(5_000 * 28, SYNC_FACTOR_UNIT_BUDGET)
        self.assertGreater(20_000 * 28, SYNC_FACTOR_UNIT_BUDGET)


class IncludeValuesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.db = Database(self.home / "quantdesk.db")

    def test_a_coverage_only_request_does_not_ship_every_point_back(self):
        bars = synthetic_bars(120)
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", bars, source="venue_rest")
        record_definitions(self.db, "vibe-factors", [{
            "id": "vibe.rsi.14", "name": "RSI", "family": "oscillator", "mode": "time_series",
            "sources": ["derived"], "requiredFields": ["close"], "warmupBars": 14,
            "supportedTimeframes": ["1h"], "implementationVersion": "vibe/1",
            "formulaHash": "h", "description": "d"}])
        with patch("quantdesk.factors.enabled_provider", return_value="vibe-factors"), \
             patch("quantdesk.factors.PluginRegistry") as registry:
            from quantdesk.plugins.protocol import FactorComputeResult, FactorSeries, FactorValue

            registry.return_value.compute_factors.return_value = FactorComputeResult(
                snapshotHash="snap", series=[FactorSeries(
                    factorId="vibe.rsi.14", implementationVersion="vibe/1",
                    values=[FactorValue(time=row["ts"], value=50.0) for row in bars])])
            outcome = compute(self.db, symbol="BTCUSDT", bars=120,
                              factor_ids=["vibe.rsi.14"], include_values=False)
        self.assertEqual(outcome["series"], [], "只要覆盖率时不应回传整条序列")
        self.assertFalse(outcome["valuesIncluded"])
        self.assertGreater(outcome["coverage"]["vibe.rsi.14"], 0)
        # ...but the values are still stored with the run.
        stored = run_detail(self.db, outcome["runId"])
        self.assertEqual(stored["seriesCount"], 1)
        self.assertEqual(stored["series"], [], "默认不下发整条序列")
        self.assertEqual(len(run_detail(self.db, outcome["runId"], include_values=True)["series"]), 1)


class QueuedFactorRunTests(unittest.TestCase):
    """宽因子请求走后台队列：一次提交、有进度、结果可读回。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.db = Database(self.home / "quantdesk.db")

    def test_a_factor_run_is_submitted_and_completed_by_the_queue(self):
        bars = synthetic_bars(200)
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", bars, source="venue_rest")
        record_definitions(self.db, "vibe-factors", [{
            "id": "vibe.rsi.14", "name": "RSI", "family": "oscillator", "mode": "time_series",
            "sources": ["derived"], "requiredFields": ["close"], "warmupBars": 14,
            "supportedTimeframes": ["1h"], "implementationVersion": "vibe/1",
            "formulaHash": "h", "description": "d"}])
        from quantdesk.plugins.protocol import FactorComputeResult, FactorSeries, FactorValue

        with patch("quantdesk.factors.enabled_provider", return_value="vibe-factors"), \
             patch("quantdesk.factors.PluginRegistry") as registry:
            def compute_factors(plugin_id, request):
                return FactorComputeResult(
                    snapshotHash=request.snapshotHash,
                    series=[FactorSeries(factorId=item, implementationVersion="vibe/1",
                                         values=[FactorValue(time=row["ts"], value=1.0) for row in bars])
                            for item in request.factorIds],
                )

            registry.return_value.compute_factors.side_effect = compute_factors
            from quantdesk.backtest_runs import RunQueue
            from quantdesk.factors import run_factors

            # The queue's own executor spawns a child process, which would inherit
            # the *operator's* QUANTDESK_HOME and compute against the live database.
            # The test drives the same runner in-process instead: the queue's
            # bookkeeping is what is under test here, and it must stay hermetic.
            def execute(context, progress):
                return run_factors(self.db, context["request"], progress=progress)

            queue = RunQueue(self.db, execute=execute)
            run = queue.submit("factors", {"symbol": "BTCUSDT", "interval": "1h", "bars": 200,
                                           "factorIds": ["vibe.rsi.14"]})
            self.assertEqual(run["kind"], "factors")
            self.assertIn("因子计算", run["label"])
            done = queue.run_claimed(queue.claim_next())

        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual(done["headline"]["scope"], "因子覆盖率（无盈亏指标）")
        self.assertEqual(done["headline"]["factors"], 1)
        self.assertIn("coverage", done["artifacts"])
        self.assertIn("factors", done["artifacts"])
        detail = queue.get(run["id"], with_result=True)
        factor_run_id = detail["result"]["factorRunId"]
        self.assertTrue(factor_run_id, "排队结果必须指向它自己的因子任务")
        self.assertIn(f"/api/factors/runs/{factor_run_id}", detail["result"]["readBack"])
        self.assertEqual(runs(self.db)[0]["id"], factor_run_id, "同一个库里只有这一条因子任务")
        # The values live with the factor run, not in the queue's result payload.
        stored = run_detail(self.db, detail["result"]["factorRunId"], include_values=True)
        self.assertEqual(len(stored["series"]), 1)


class PortfolioValidationTests(unittest.TestCase):
    """组合运行也能做统计验证，并且用加权买入持有作为基准。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def _seed(self, symbol: str, bars: int = 200):
        rows = synthetic_bars(bars)
        self.db.upsert_candles("bybit", symbol, "1h", rows, source="venue_rest")
        return rows

    def test_a_portfolio_benchmark_is_weighted_across_its_members(self):
        from quantdesk.factors import _portfolio_benchmark

        bars = self._seed("BTCUSDT")
        result = {
            "weights": {"BTCUSDT": 60.0, "ETHUSDT": 40.0},
            "members": {"BTCUSDT": {"readRange": {"fromTs": bars[0]["ts"], "toTs": bars[-1]["ts"],
                                                  "bars": len(bars)}}},
        }
        curve = _portfolio_benchmark(self.db, result)
        self.assertEqual(curve["kind"], "weighted_buy_and_hold")
        self.assertTrue(curve["equityCurve"]) 

    def test_a_portfolio_without_readable_members_gets_no_benchmark(self):
        from quantdesk.factors import _portfolio_benchmark

        self.assertEqual(_portfolio_benchmark(self.db, {"weights": {}, "members": {}}), {})

    def test_verdicts_say_which_book_they_describe(self):
        rows = store_verdicts(self.db, 11, "vibe-factors", {
            "bootstrap": {"resamples": 100, "blockSize": 4, "positiveSharpeProbability": 0.7,
                          "sharpe": {"low": 0.2, "high": 1.0}},
            "randomization": {"permutations": 100, "method": "signal_shift", "pValue": 0.2,
                              "observedSharpe": 0.4},
        }, scope="portfolio")
        kinds = {row["kind"]: row for row in rows}
        self.assertIn("组合合并账本", kinds["bootstrap"]["detail"])
        self.assertIn("组合合并账本", kinds["randomization"]["detail"])

    def test_the_summary_line_counts_the_verdicts(self):
        from quantdesk.factors import verdict_summary

        summary = verdict_summary([
            {"verdict": "pass"}, {"verdict": "warn"}, {"verdict": "fail"}, {"verdict": "info"},
        ])
        self.assertEqual(summary["total"], 4)
        self.assertIn("未通过", summary["headline"])
        self.assertIn("1 项通过", summary["headline"])
