"""QuantDesk adapter shim standing in for Vibe's settings accessor.

The vendored factor package asks this module exactly one question: whether to use
``bottleneck`` for rolling argmax/argmin (``base.py`` imports ``HAS_BOTTLENECK`` and
``bn`` from ``src.factors._backend``, whose lazy accessor reads the flag here). The
real upstream module reaches into Vibe's whole settings tree, which would pull
pydantic and the agent configuration into a factor calculator that is supposed to
be a pure function of the panel it is handed.

The answer is fixed rather than configured: QuantDesk installs no bottleneck and
forces the numpy fallback, so the numbers are identical on every host instead of
depending on what happens to be importable.
"""

from __future__ import annotations

from types import SimpleNamespace


def get_env_config() -> SimpleNamespace:
    """Always report "disable bottleneck" so the numpy path is taken."""
    return SimpleNamespace(
        agent_tuning=SimpleNamespace(vibe_trading_disable_bottleneck=True)
    )
