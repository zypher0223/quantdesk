"""Built-in rule strategies. New skills/strategies drop in here or load via
YAML (M5 plugin loader)."""

from __future__ import annotations

from ..base import Signal, Strategy

DEFAULT_WEIGHTS = {"1d": 0.40, "4h": 0.30, "1h": 0.20, "15m": 0.10}


class ResonanceBreakout(Strategy):
    """Multi-TF resonance threshold crossing.

    features row expects: res_score (as-of merged resonance score), plus base
    TF indicator fields for gating. Enter long when score crosses above
    entry_threshold; enter short when below -entry_threshold (unless
    long_only). Exit when score crosses back through exit_threshold (0).
    """

    name = "resonance_breakout"

    def __init__(
        self,
        entry_threshold: float = 0.4,
        exit_threshold: float = 0.0,
        weights: dict[str, float] | None = None,
        long_only: bool = False,
        adx_min: float = 0.0,   # optional base-TF ADX gate
    ):
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.weights = weights or DEFAULT_WEIGHTS
        self.long_only = long_only
        self.adx_min = adx_min

    def on_bar(self, features: dict) -> Signal:
        score = features.get("res_score")
        if score is None or score != score:  # NaN until higher TFs have data
            return Signal("flat", reason="res_score 未就绪")
        prev = features.get("res_score_prev", score)
        adx = features.get("adx14")
        gate = adx is None or adx != adx or adx >= self.adx_min
        crossed_up = prev < self.entry_threshold <= score
        crossed_down = prev > -self.entry_threshold >= score
        if crossed_up and gate:
            return Signal("long", score, f"共振分上穿 {self.entry_threshold}（score={score:.2f}）")
        if not self.long_only and crossed_down:
            return Signal("short", score, f"共振分下穿 -{self.entry_threshold}（score={score:.2f}）")
        # exits
        if features.get("position") == "long" and score <= self.exit_threshold:
            return Signal("flat", score, f"共振分回落至 {score:.2f} ≤ {self.exit_threshold}")
        if features.get("position") == "short" and score >= -self.exit_threshold:
            return Signal("flat", score, f"共振分回升至 {score:.2f}")
        return Signal("flat", score)
