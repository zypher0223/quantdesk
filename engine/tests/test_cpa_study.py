"""Stage C: the ablation comparison, its grouping, and the unified strategy entry.

What must hold, in the report's own terms:

* the comparison covers the variants it lists, and a variant that loses money is
  reported rather than treated as a failure - correctness and reproducibility pass,
  profitability explicitly is not a pass condition;
* groups stay apart: the stock aggregate never contains the leveraged ETFs, and
  SOXL/SOXS are reported as their own group;
* a missing figure stays missing (never averaged in as a zero);
* the corrections come from the engine's existing implementations, so the table
  carries a deflated Sharpe and a PBO, or the reason it could not;
* every signal path goes through one entry - the structural guard is a source scan,
  because "we refactored it once" is not a property of the code.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from quantdesk.datahub.db import Database
from quantdesk.strategy import cpa
from quantdesk.strategy.cpa import study
from quantdesk.strategy.cpa.study import VARIANTS, group_report, run_ablation, variants_for


def fake_runner(table: dict[str, dict[str, float]]):
    """A study stand-in: deterministic metrics keyed by (strategy id, symbol)."""

    def run(db, request):
        key = request.strategyId if request.strategyId != "cpa_cycle" else "cpa_cycle"
        row = table.get(key, {})
        equity = [10_000.0 * (1 + row.get("step", 0.0)) ** index for index in range(200)]
        return {
            "symbol": request.symbol, "bars": 200,
            "trades": [{}] * int(row.get("trades", 0)),
            "final_equity": equity[-1], "net_return_pct": row.get("returnPct"),
            "max_drawdown_pct": row.get("drawdownPct"), "sharpe": row.get("sharpe"),
            "sortino": None, "profit_factor": None, "win_rate_pct": None,
            "total_fees": row.get("fees", 0.0), "total_funding": 0.0,
            "exposure_pct": 50.0, "equity_curve": [{"time": i, "equity": v}
                                                   for i, v in enumerate(equity)],
            "degraded": False, "dataReady": True,
        }

    return run


class VariantListTests(unittest.TestCase):
    def test_the_comparison_covers_the_variants_the_report_asked_for(self):
        keys = {variant.key for variant in VARIANTS}
        for required in ("buy_hold", "ma_cross", "channel_breakout", "cpa_wedge_pop",
                         "cpa_wedge_pop_htf", "cpa_wedge_pop_volume", "cpa_full"):
            self.assertIn(required, keys)
        full = next(v for v in VARIANTS if v.key == "cpa_full")
        self.assertEqual(
            full.parameters["entryStages"], ["wedge_pop", "ema_crossback", "base_n_break"]
        )

    def test_every_variant_says_what_it_changed(self):
        for variant in VARIANTS:
            self.assertTrue(variant.label and variant.note, variant.key)

    def test_the_crypto_list_is_a_separate_configuration(self):
        stock = {variant.key: variant.parameters for variant in variants_for("stock")}
        crypto = {variant.key: variant.parameters for variant in variants_for("crypto")}
        self.assertNotEqual(stock["cpa_full"], crypto["cpa_full"])


class GroupingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")
        self.runner = fake_runner({
            "buy_hold": {"sharpe": 0.5, "returnPct": 12.0, "drawdownPct": -20.0, "trades": 1},
            "ma_cross": {"sharpe": -0.3, "returnPct": -4.0, "drawdownPct": -30.0, "trades": 8},
            "channel_breakout": {"sharpe": 0.1, "returnPct": 2.0, "drawdownPct": -18.0, "trades": 5},
            "cpa_cycle": {"sharpe": 0.2, "returnPct": 3.0, "drawdownPct": -15.0, "trades": 6},
        })

    def test_the_stock_aggregate_never_contains_the_leveraged_etfs(self):
        report = run_ablation(self.db, group="stock", interval="1h", bars=200,
                              runner=self.runner)
        self.assertNotIn("SOXLUSDT", report["universe"])
        self.assertNotIn("SOXSUSDT", report["universe"])
        self.assertEqual(len(report["universe"]), 13)

    def test_the_leveraged_etfs_are_reported_as_their_own_group(self):
        report = run_ablation(self.db, group="leveraged_etf", interval="1h", bars=200,
                              runner=self.runner)
        self.assertEqual(sorted(report["universe"]), ["SOXLUSDT", "SOXSUSDT"])
        self.assertTrue(any("杠杆" in warning for warning in report["warnings"]))

    def test_crypto_is_its_own_group(self):
        report = run_ablation(self.db, group="crypto", interval="1h", bars=200,
                              runner=self.runner)
        self.assertEqual(sorted(report["universe"]), ["BTCUSDT", "ETHUSDT"])

    def test_a_group_too_small_to_compare_is_refused(self):
        with self.assertRaises(ValueError):
            run_ablation(self.db, group="crypto", interval="1h", bars=200,
                         symbols=["BTCUSDT"], runner=self.runner)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def test_a_losing_variant_is_reported_not_failed(self):
        """The report was explicit: profitability is not a pass condition."""
        runner = fake_runner({
            "buy_hold": {"sharpe": 0.9, "returnPct": 40.0, "drawdownPct": -25.0, "trades": 1},
            "ma_cross": {"sharpe": -1.2, "returnPct": -30.0, "drawdownPct": -45.0, "trades": 12},
            "channel_breakout": {"sharpe": None, "returnPct": None, "drawdownPct": None, "trades": 0},
            "cpa_cycle": {"sharpe": -0.4, "returnPct": -8.0, "drawdownPct": -22.0, "trades": 4},
        })
        report = run_ablation(self.db, group="crypto", interval="1h", bars=200, runner=runner)
        losing = next(row for row in report["table"] if row["variant"] == "ma_cross")
        # The fake runner supplies a Sharpe but no usable equity curve for the metrics
        # module, so the summary may legitimately report "—" - what this test pins is
        # that a losing variant is *present and reported*, never dropped or marked
        # failed, and that the report says profitability is not a pass condition.
        self.assertIn("ma_cross", {row["variant"] for row in report["table"]})
        self.assertNotIn("failed", {key for row in report["table"] for key in row})
        self.assertIn("不以「收益为正」作为通过条件", report["interpretation"])
        hard_losing = next(row for row in report["table"]
                           if row["variant"] == "ma_cross")["perSymbol"]
        self.assertTrue(hard_losing, "每个合约的结果都要在表里")

    def test_a_missing_figure_stays_missing(self):
        runner = fake_runner({
            "buy_hold": {"sharpe": None, "returnPct": None, "drawdownPct": None, "trades": 0},
            "ma_cross": {"sharpe": None, "returnPct": None, "drawdownPct": None, "trades": 0},
            "channel_breakout": {"sharpe": None, "returnPct": None, "drawdownPct": None, "trades": 0},
            "cpa_cycle": {"sharpe": None, "returnPct": None, "drawdownPct": None, "trades": 0},
        })
        report = run_ablation(self.db, group="crypto", interval="1h", bars=200, runner=runner)
        for row in report["table"]:
            self.assertIsNone(row["summary"]["meanSharpe"])
            self.assertEqual(row["summary"]["measurable"], 0)
        self.assertTrue(any("没有 Sharpe" in warning for warning in report["warnings"]))

    def test_the_corrections_are_present_or_explain_themselves(self):
        runner = fake_runner({
            "buy_hold": {"sharpe": 0.5, "returnPct": 10.0, "drawdownPct": -20.0, "trades": 1, "step": 0.001},
            "ma_cross": {"sharpe": 0.2, "returnPct": 4.0, "drawdownPct": -22.0, "trades": 6, "step": 0.0005},
            "channel_breakout": {"sharpe": 0.1, "returnPct": 2.0, "drawdownPct": -18.0, "trades": 5, "step": 0.0003},
            "cpa_cycle": {"sharpe": 0.3, "returnPct": 6.0, "drawdownPct": -16.0, "trades": 7, "step": 0.0007},
        })
        report = run_ablation(self.db, group="crypto", interval="1h", bars=200, runner=runner)
        self.assertEqual(report["statisticsScope"]["trialDimension"], "strategy-variants")
        self.assertEqual(report["pbo"]["proposals"], len(VARIANTS))
        self.assertEqual(set(report["pbo"]["selectionFrequency"]), {variant.key for variant in VARIANTS})
        for row in report["table"]:
            self.assertTrue(row["dsrAvailable"] or row["dsrReason"], row["variant"])
            self.assertTrue(row["pboAvailable"] or row["pboReason"], row["variant"])
            self.assertEqual(row["pboScope"], "variant-set")
            self.assertEqual(len(row["perSymbol"]), 2)

    def test_the_report_says_the_positive_return_rule_out_loud(self):
        runner = fake_runner({"cpa_cycle": {"sharpe": 0.1, "returnPct": 1.0,
                                           "drawdownPct": -5.0, "trades": 2}})
        report = run_ablation(self.db, group="crypto", interval="1h", bars=200, runner=runner)
        self.assertIn("不以「收益为正」作为通过条件", report["interpretation"])
        self.assertIn("简化模型", report["simplePositionNotice"])

    def test_the_text_table_prints_a_dash_for_an_unknown_figure(self):
        runner = fake_runner({"cpa_cycle": {"sharpe": None, "returnPct": None,
                                           "drawdownPct": None, "trades": 0}})
        report = run_ablation(self.db, group="crypto", interval="1h", bars=200, runner=runner)
        text = group_report(report)
        self.assertIn("—", text)
        self.assertIn("参数版本", text)

    def test_the_same_inputs_give_the_same_report(self):
        runner = fake_runner({"cpa_cycle": {"sharpe": 0.25, "returnPct": 5.0,
                                           "drawdownPct": -9.0, "trades": 3}})
        first = run_ablation(self.db, group="crypto", interval="1h", bars=200, runner=runner)
        second = run_ablation(self.db, group="crypto", interval="1h", bars=200, runner=runner)
        self.assertEqual([row["summary"] for row in first["table"]],
                         [row["summary"] for row in second["table"]])
        self.assertEqual([row["deflatedSharpe"] for row in first["table"]],
                         [row["deflatedSharpe"] for row in second["table"]])

    def test_a_variant_that_raises_is_recorded_per_symbol(self):
        def broken(db, request):
            # The study layer addresses contracts by their display symbol.
            if request.symbol in ("ETH", "ETHUSDT"):
                raise RuntimeError("回测炸了")
            return fake_runner({"cpa_cycle": {"sharpe": 0.2, "returnPct": 2.0,
                                             "drawdownPct": -3.0, "trades": 1}})(db, request)

        report = run_ablation(self.db, group="crypto", interval="1h", bars=200, runner=broken)
        row = report["table"][0]
        self.assertIn("ETHUSDT", row["failures"])
        self.assertIn("回测炸了", row["failures"]["ETHUSDT"])


class UnifiedEntryTests(unittest.TestCase):
    """The report's third group: one signal path, everywhere."""

    def test_the_four_study_paths_no_longer_generate_their_own_events(self):
        import quantdesk.cli
        import quantdesk.studies as studies_module
        from quantdesk.backtest import engine as engine_module

        for module in (studies_module, engine_module, quantdesk.cli):
            source = inspect.getsource(module)
            self.assertIn("generate_events", source, module.__name__)
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or "import" in stripped:
                    continue
                self.assertNotIn(
                    "generate_builtin_events(", stripped,
                    f"{module.__name__} 仍在直接调用 generate_builtin_events：{stripped}",
                )

    def test_the_carried_risk_switch_is_threaded_from_the_parameter(self):
        """The engine flag is useless if the study never passes the CPA parameter on."""
        import inspect as _inspect

        import quantdesk.studies as studies_module

        source = _inspect.getsource(studies_module)
        self.assertIn("enforce_open_risk=", source)
        self.assertIn('resolved_cpa.get("enforceOpenRisk", True)', source)
        signature = _inspect.signature(
            __import__("quantdesk.backtest.engine", fromlist=["run_backtest"]).run_backtest
        )
        self.assertIs(signature.parameters["enforce_open_risk"].default, True)

    def test_one_entry_gives_one_event_series_for_cpa(self):
        import random

        from quantdesk.plugins import PluginManager
        from quantdesk.strategy.registry import (
            StrategyRegistry,
            generate_builtin_events,
            generate_events,
        )

        rng = random.Random(7)
        price = 100.0
        candles = []
        for index in range(300):
            price *= 1 + rng.gauss(0.001, 0.01)
            candles.append({"ts": 1_700_000_000_000 + index * 3_600_000, "open": price,
                            "high": price * 1.004, "low": price * 0.996, "close": price,
                            "volume": 100 + index % 7})
        parameters = {"entryStages": ["wedge_pop"]}
        facade = generate_events(candles, "cpa_cycle", parameters,
                                 asset_class="crypto", interval="1h")
        internal = generate_builtin_events(candles, "cpa_cycle", parameters)
        self.assertEqual(facade, internal, "facade 与内置分支必须给出同一串事件")
        with tempfile.TemporaryDirectory() as tmp:
            registry = StrategyRegistry(PluginManager(Path(tmp)))
            through_registry, warnings = registry.generate(
                "cpa_cycle", candles, symbol="BTCUSDT", timeframe="1h",
                parameters=parameters, asset_class="crypto",
            )
        self.assertEqual(through_registry, facade)
        self.assertEqual(warnings, [])

    def test_registry_forwards_higher_timeframe_context(self):
        from quantdesk.plugins import PluginManager
        from quantdesk.strategy.registry import StrategyRegistry

        candles = [{"ts": index, "open": 100, "high": 101, "low": 99,
                    "close": 100, "volume": 10} for index in range(2)]
        records = [
            cpa.PhaseRecord(time=0, phase="wedge_pop", status="confirmed"),
            cpa.PhaseRecord(time=1),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            registry = StrategyRegistry(PluginManager(Path(tmp)))
            blocked, _ = registry.generate(
                "cpa_cycle", candles, symbol="BTCUSDT", timeframe="1h",
                parameters={"entryStages": ["wedge_pop"], "requireHigherTimeframe": True},
                asset_class="crypto", records=records,
                higher_trends=["bearish", "bearish"],
            )
            allowed, _ = registry.generate(
                "cpa_cycle", candles, symbol="BTCUSDT", timeframe="1h",
                parameters={"entryStages": ["wedge_pop"], "requireHigherTimeframe": True},
                asset_class="crypto", records=records,
                higher_trends=["bullish", "bullish"],
            )
        self.assertNotIn(1, blocked)
        self.assertEqual(allowed[0], 1)


if __name__ == "__main__":
    unittest.main()
