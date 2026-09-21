"""Cycle of Price Action — QuantDesk 规则化适配版（阶段 A：识别层）。

概念来源是 Oliver Kell 公开描述的 Cycle of Price Action；本包中的阈值、状态机与
信号映射均为 QuantDesk 的研究实现，不是作者原始规则。识别过程是确定性的：只读已
收盘K线、不调用大模型、不访问网络。

对外只暴露四件事：`analyze`（完整阶段序列）、`events_for`（回测事件）、
`describe`（规则与版本说明）、`resolve_parameters`（按资产类别与周期取默认参数）。
"""

from .defaults import (
    PARAMETER_SPECS,
    PARAMETER_VERSION,
    PHASE_LABELS,
    PHASES,
    STRATEGY_NAME,
    defaults_for,
)
from .models import HigherTimeframeView, PhaseRecord, PhaseSeries, Pivot
from .signals import (
    SIMPLE_POSITION_NOTICE,
    analyze,
    describe,
    events_for,
    higher_intervals_for,
    intents_for,
    position_notice,
    resolve_parameters,
)
from .state_machine import run as run_state_machine

__all__ = [
    "PARAMETER_SPECS",
    "PARAMETER_VERSION",
    "PHASES",
    "PHASE_LABELS",
    "STRATEGY_NAME",
    "SIMPLE_POSITION_NOTICE",
    "HigherTimeframeView",
    "PhaseRecord",
    "PhaseSeries",
    "Pivot",
    "analyze",
    "defaults_for",
    "describe",
    "events_for",
    "intents_for",
    "position_notice",
    "higher_intervals_for",
    "resolve_parameters",
    "run_state_machine",
]
