"""Built-in strategy registry and multi-strategy backtest boundary."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quantdesk.plugins import PluginManager
from quantdesk.strategy import StrategyRegistry, generate_builtin_events


def candles(count: int = 120) -> list[dict]:
    rows = []
    for index in range(count):
        close = 100 + index * 0.1
        if 35 <= index < 50:
            close -= (index - 34) * 1.2
        if index >= 50:
            close += (index - 49) * 0.7
        rows.append({"ts": 1_700_000_000_000 + index * 3_600_000, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 100})
    return rows


class StrategyRegistryTests(unittest.TestCase):
    def test_catalog_has_the_builtin_strategies(self):
        with tempfile.TemporaryDirectory() as tmp:
            strategies, errors = StrategyRegistry(PluginManager(Path(tmp))).catalog()
        self.assertEqual(errors, [])
        self.assertEqual(
            {item["id"] for item in strategies},
            {"ma_cross", "channel_breakout", "rsi_reversal", "cpa_cycle", "buy_hold"},
        )
        cpa = next(item for item in strategies if item["id"] == "cpa_cycle")
        # Every CPA threshold must be publishable with a unit and a sentence of help,
        # and the catalogue must cite the rule version and the simplified-position
        # notice rather than claiming the original strategy.
        self.assertTrue(cpa["parameterVersion"].startswith("cpa-qd/"))
        self.assertTrue(cpa["notices"] and "简化版本" in cpa["notices"][0])
        self.assertTrue(all(item["unit"] is not None and item["help"] for item in cpa["parameters"]))
        self.assertNotIn("Oliver Kell 原版", cpa["name"])

    def test_the_baseline_enters_once_and_never_exits(self):
        """The entry is held for the first bars, then never signalled again.

        Repeating it is deliberate: the engine acts on bar `i` from the signal of bar
        `i - 1 - latency`, so a lone event on bar 0 asks it to enter on bar 1, and a
        single-bar window is a fragile place to put the comparison baseline.
        """
        events = generate_builtin_events(candles(), "buy_hold", {})
        self.assertEqual(events[:3], [1, 1, 1])
        self.assertTrue(all(item is None for item in events[3:]))
        self.assertNotIn(0, events, "基准不应产生任何离场信号")

    def test_each_builtin_generates_aligned_event_series(self):
        rows = candles()
        for strategy_id, parameters in (
            ("ma_cross", {"fastPeriod": 5, "slowPeriod": 12}),
            ("channel_breakout", {"lookback": 10}),
            ("rsi_reversal", {"period": 7, "oversold": 35, "overbought": 65}),
        ):
            events = generate_builtin_events(rows, strategy_id, parameters)
            self.assertEqual(len(events), len(rows))
            self.assertTrue(all(item in (None, -1, 0, 1) for item in events))

    def test_strategy_parameters_are_checked(self):
        with self.assertRaisesRegex(ValueError, "慢线周期"):
            generate_builtin_events(candles(), "ma_cross", {"fastPeriod": 20, "slowPeriod": 10})

