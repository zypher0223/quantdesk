"""Strategy plugin interface + built-in rule strategies."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Signal:
    direction: str          # long / short / flat
    strength: float = 0.0   # resonance score [-1, 1]
    reason: str = ""
    sl_price: float | None = None
    tp_price: float | None = None


class Strategy:
    """Base class. on_bar receives one feature row (bar closed) -> Signal."""

    name = "base"
    long_only: bool = False

    def on_bar(self, features: dict) -> Signal:
        raise NotImplementedError
