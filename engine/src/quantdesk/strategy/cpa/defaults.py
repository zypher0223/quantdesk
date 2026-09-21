"""CPA defaults: every threshold the adapter uses, in one place, versioned.

The concept comes from Oliver Kell's published descriptions of a price cycle
(reversal extension, wedge pop, EMA crossback, base n' break, exhaustion extension,
wedge drop, and the downside mirror image). Those descriptions do **not** publish a
fixed set of numeric thresholds for every market, so nothing here is presented as
Kell's own rule. Every number below is a QuantDesk research default: configurable,
validated, and stamped with the version that produced a given phase series.

Two rules this module exists to enforce:

* a threshold is never hard-coded in the detector - it is read from here (or from the
  host configuration), so a result can always name the parameters it used;
* the defaults differ by asset class and interval, because a 15-minute tokenised
  equity contract and a 1-day crypto perp do not share the same noise scale.
"""

from __future__ import annotations

from typing import Any

# Bumped whenever a rule or a default changes meaning. Recorded in every phase
# record and in every strategy version, so two results can be compared honestly.
PARAMETER_VERSION = "cpa-qd/1.3.0"

# The public name. Deliberately not "Oliver Kell's strategy": the rules below are
# QuantDesk's own thresholds applied to a published concept.
STRATEGY_NAME = "Cycle of Price Action — QuantDesk 规则化适配版"

PHASES: tuple[str, ...] = (
    "reversal_extension",
    "wedge_pop",
    "ema_crossback",
    "base_n_break",
    "exhaustion_extension",
    "wedge_drop",
    "downside_ema_crossback",
    "downside_base_n_break",
)
PHASE_LABELS: dict[str, str] = {
    "none": "无阶段",
    "reversal_extension": "反转延伸（Reversal Extension）",
    "wedge_pop": "楔形突破（Wedge Pop）",
    "ema_crossback": "均线回踩（EMA Crossback）",
    "base_n_break": "平台突破（Base n’ Break）",
    "exhaustion_extension": "延伸衰竭（Exhaustion Extension）",
    "wedge_drop": "楔形下跌（Wedge Drop）",
    "downside_ema_crossback": "下行均线回踩（Downside EMA Crossback）",
    "downside_base_n_break": "下行平台突破（Downside Base n’ Break）",
}
# Which side of the market a phase belongs to. `candidate` phases are observations:
# they never place an order on their own.
PHASE_DIRECTION: dict[str, str] = {
    "reversal_extension": "bullish",
    "wedge_pop": "bullish",
    "ema_crossback": "bullish",
    "base_n_break": "bullish",
    "exhaustion_extension": "bullish",
    "wedge_drop": "bearish",
    "downside_ema_crossback": "bearish",
    "downside_base_n_break": "bearish",
}
OBSERVATION_PHASES: tuple[str, ...] = ("reversal_extension", "exhaustion_extension")

# execution interval -> (management interval, background interval). The report's
# mapping: the timeframe that manages the trade, and the one that sets the backdrop.
HIGHER_TIMEFRAME_MAP: dict[str, tuple[str, str]] = {
    "15m": ("1h", "4h"),
    "1h": ("4h", "1d"),
    "4h": ("1d", "1w"),
    "1d": ("1w", "1w"),
    "1w": ("1w", "1w"),
}


def defaults_for(asset_class: str, interval: str) -> dict[str, Any]:
    """The research defaults for one asset class and execution interval.

    `asset_class` is the engine's own product split (`stock`, `etf`, `crypto`), so a
    leveraged ETF can be given its own expansion thresholds instead of inheriting the
    ones tuned for a single name.
    """
    base: dict[str, Any] = {
        # Trend backbone. Kell's published material uses 10/20 EMA for the swing and
        # 50/200 SMA for the long backdrop; both are kept configurable.
        "emaFast": 10,
        "emaSlow": 20,
        "longSma": 50,
        "backdropSma": 200,
        # Extension: how far from the fast EMA counts as stretched, in ATR units.
        "extensionAtr": 1.5,
        "exhaustionAtr": 3.0,
        # Contraction: the window over which range/EMA distance must shrink, and how
        # much of it must have shrunk to call it a wedge or a base.
        #
        # 0.30, not the 0.50/0.55 this shipped with. The stricter value demanded that
        # the recent window's mean range *halve*, and on the venue's own history that
        # reading almost never landed on the bar that then has to pop - so no upside
        # cycle could start, and `ema_crossback` / `base_n_break` (both gated on an
        # existing upside cycle) were unreachable with it. Measured on BTC 1d over 2000
        # bars: 35 contraction readings, 2 of them closing back above both EMAs, 0
        # confirmed pops, 0 trades on every daily series tested; at 0.30: BTC 1d 4 pops
        # / 3 trades, ETH 1d 6 / 3, BTC 1h 14 / 5, NVDA 1h 10 / 3. Chosen for
        # *reachability* - a wedge has to be a rare pattern, not an impossible one -
        # and explicitly not for its returns: at this value the universe-wide results
        # are mostly negative and the daily PBO is 0.83, so the calibration is not
        # validated by performance. `emaGapNarrow` shares this threshold.
        "contractionWindow": 10,
        "contractionThreshold": 0.30,
        # Pivot structure: highs/lows are taken strictly *before* the current bar.
        "pivotLookback": 20,
        # Volume confirmation: current volume against the mean of the prior window.
        "volumeWindow": 20,
        "volumeConfirm": 1.3,
        "downsideVolumeConfirm": 1.4,
        # How close to the EMA zone a crossback may be, in ATR units.
        "crossbackToleranceAtr": 0.6,
        # Exits and behaviour.
        "exitOnExhaustion": True,
        "exhaustionTrailAtr": 2.0,
        "requireHigherTimeframe": False,
        "sideMode": "long_only",
        # Entry stages the first version may open a position on.
        "entryStages": ["wedge_pop", "ema_crossback", "base_n_break"],
        # Warmup: enough bars for the slowest indicator plus the pivot window.
        "minBars": 60,
        # --- 阶段 D：仓位意图模型（默认 single，与阶段 B 逐位一致）---
        "positionModel": "single",
        "initialExposurePct": 50.0,
        "addExposurePct": 25.0,
        "reduceExposurePct": 50.0,
        # The risk budget is a share of equity, but a *stop distance* is measured in
        # price and scales with the timeframe: a structural stop on a daily chart is
        # routinely 4-8% away, while an hourly one is under 2%. A single number would
        # therefore mean "trade" on one interval and "refuse everything" on another -
        # measured: 2% rejected all 288 intents on BTC 1d. The default is per timeframe
        # for that reason, and the parameter stays overridable.
        #
        # The proper fix, when someone wants it, is to size the position from the
        # budget (qty = budget / stop distance) instead of refusing it; that belongs in
        # the engine's intent branch and is not done here.
        "maxPortfolioRiskPct": 8.0 if interval in ("1d", "1w") else 2.0,
        # The same budget also caps the risk an *open* position carries. A trade that
        # runs in favour sits further from its stop, so its carried risk grows even
        # though its entry risk was inside the budget; with this on, the engine trims
        # the position back to the budget (or closes it when the trim is too small to
        # trade). Off is for comparing against the entry-only behaviour.
        "enforceOpenRisk": True,
        "pivotFailureExit": True,
    }
    if asset_class == "etf":
        # A 3x ETF moves further than its index on the same bar, so the same ATR
        # multiple is a tighter call. Loosened deliberately, and reported separately.
        base.update({"extensionAtr": 1.8, "exhaustionAtr": 3.5, "volumeConfirm": 1.4})
    if asset_class == "crypto":
        # 24/7 trading: no opening gap structure, more continuous volume, so the
        # volume test is easier to satisfy and the extension test is stricter.
        base.update({"extensionAtr": 1.8, "exhaustionAtr": 3.2, "volumeConfirm": 1.25})
    if interval in ("15m", "1h"):
        # Intraday bars are noisier: require a little more volume before calling a
        # break. The contraction threshold is *not* raised here any more - at 0.55 it
        # was the reason the wedge pop never fired intraday either.
        base.update({"volumeConfirm": round(base["volumeConfirm"] + 0.1, 3)})
    if interval == "1w":
        # Weekly bars carry more structure per bar, so adjacent windows overlap in
        # regime and the same 0.30 is harder to reach: 0 pops in 300 weekly bars at
        # 0.30, 2 pops plus a base n' break at 0.20.
        base.update({"pivotLookback": 12, "contractionWindow": 8, "minBars": 40,
                     "contractionThreshold": 0.20})
    return base


# The schema the API and the strategy catalogue publish. `unit` and `help` are what
# the parameters page shows; the version is what a result cites. The position-model
# parameters are appended so the intent path is configurable exactly like the rest.
PARAMETER_SPECS: tuple[dict[str, Any], ...] = (
    {"key": "emaFast", "label": "快线 EMA 周期", "type": "integer", "default": 10,
     "minimum": 2, "maximum": 100, "unit": "根", "help": "中期趋势的快速均线，公开资料使用 10。"},
    {"key": "emaSlow", "label": "慢线 EMA 周期", "type": "integer", "default": 20,
     "minimum": 3, "maximum": 200, "unit": "根", "help": "中期趋势的慢速均线，公开资料使用 20。"},
    {"key": "longSma", "label": "长期 SMA 周期", "type": "integer", "default": 50,
     "minimum": 5, "maximum": 400, "unit": "根", "help": "长期背景均线，公开资料使用 50。"},
    {"key": "backdropSma", "label": "背景 SMA 周期", "type": "integer", "default": 200,
     "minimum": 20, "maximum": 600, "unit": "根", "help": "更长期的背景均线，仅在数据足够时计算。"},
    {"key": "extensionAtr", "label": "延伸距离（ATR 倍数）", "type": "number", "default": 1.5,
     "minimum": 0.2, "maximum": 8.0, "unit": "ATR",
     "help": "价格偏离快线的程度，超过即视为延伸；用于反转延伸的候选条件。"},
    {"key": "exhaustionAtr", "label": "衰竭距离（ATR 倍数）", "type": "number", "default": 3.0,
     "minimum": 0.5, "maximum": 12.0, "unit": "ATR",
     "help": "延伸进入衰竭观察的距离阈值；只是风险提示，不直接反手。"},
    {"key": "contractionWindow", "label": "收缩窗口", "type": "integer", "default": 10,
     "minimum": 3, "maximum": 60, "unit": "根", "help": "判断振幅与均线距离收缩所用的窗口。"},
    {"key": "contractionThreshold", "label": "收缩比例", "type": "number", "default": 0.3,
     "minimum": 0.05, "maximum": 1.0, "unit": "比例",
     "help": "最近窗口的振幅相对紧邻前一窗口必须缩小的比例，0.3 表示缩了三成。"
             "周线默认 0.2：周线相邻窗口的制度重叠更多，同样的比例更难达到。"},
    {"key": "pivotLookback", "label": "枢轴回看窗口", "type": "integer", "default": 20,
     "minimum": 3, "maximum": 200, "unit": "根",
     "help": "枢轴只取当前 K 线之前的数据，突破判定用当前收盘价。"},
    {"key": "volumeWindow", "label": "成交量均量窗口", "type": "integer", "default": 20,
     "minimum": 3, "maximum": 200, "unit": "根", "help": "成交量确认所用的历史均量窗口。"},
    {"key": "volumeConfirm", "label": "放量确认倍数", "type": "number", "default": 1.3,
     "minimum": 0.5, "maximum": 5.0, "unit": "倍", "help": "突破 K 线成交量相对均量的下限。"},
    {"key": "downsideVolumeConfirm", "label": "下行放量倍数", "type": "number", "default": 1.4,
     "minimum": 0.5, "maximum": 5.0, "unit": "倍",
     "help": "下行阶段单独使用：下跌的量能结构可能与上涨不对称。"},
    {"key": "crossbackToleranceAtr", "label": "回踩容差（ATR 倍数）", "type": "number", "default": 0.6,
     "minimum": 0.05, "maximum": 4.0, "unit": "ATR", "help": "价格距均线多远仍算作回踩。"},
    {"key": "exitOnExhaustion", "label": "衰竭即退出", "type": "boolean", "default": True,
     "minimum": None, "maximum": None, "unit": "",
     "help": "开启后，确认的衰竭观察会平掉多仓；关闭则只禁止加仓。"},
    {"key": "exhaustionTrailAtr", "label": "衰竭后跟踪止损（ATR 倍数）", "type": "number", "default": 2.0,
     "minimum": 0.5, "maximum": 10.0, "unit": "ATR",
     "help": "保留的研究参数；当前 single 与 intent 执行器均未使用该数值，结果不会声称已启用跟踪止损。"},
    {"key": "requireHigherTimeframe", "label": "要求高周期确认", "type": "boolean", "default": False,
     "minimum": None, "maximum": None, "unit": "",
     "help": "开启后，只有高周期未处于明确下行状态时才允许入场；数据不足时报背景不足而不入场。"},
    {"key": "sideMode", "label": "方向模式", "type": "string", "default": "long_only",
     "minimum": None, "maximum": None, "unit": "", "options": ["long_only", "symmetric"],
     "help": "long_only 只做多；symmetric 才启用下行阶段做空。"},
    {"key": "entryStages", "label": "入场阶段", "type": "string", "default":
     ["wedge_pop", "ema_crossback", "base_n_break"], "minimum": None, "maximum": None, "unit": "",
     "options": ["wedge_pop", "ema_crossback", "base_n_break"],
     "help": "第一期允许开首仓的阶段；必须全部可以只靠 OHLCV 判定。"},
    {"key": "minBars", "label": "最少K线数", "type": "integer", "default": 60,
     "minimum": 30, "maximum": 1000, "unit": "根",
     "help": "低于该数量直接报告样本不足，不输出阶段结论。"},
)

# 阶段 D：结构化仓位意图模型的两条声明。模式不同，声明就不同——一句"简化版"盖住
# 分批建仓与结构止损会低估它，反过来把回测说成实盘会高估它。
INTENT_POSITION_NOTICE = (
    "已启用结构化仓位意图模型：分批建仓、分批减仓与结构止损均已模拟，"
    "但仍为回测模拟，不接实盘、不下单、不改动任何账户。"
)

POSITION_MODELS = ("single", "intent")

POSITION_PARAMETER_SPECS: tuple[dict[str, Any], ...] = (
    {"key": "positionModel", "label": "仓位模型", "type": "string", "default": "single",
     "minimum": None, "maximum": None, "unit": "", "options": ["single", "intent"],
     "help": "single 为单仓位简化版（与阶段 B 完全一致）；intent 启用分批建仓、分批减仓与结构止损。"},
    {"key": "initialExposurePct", "label": "首仓敞口（占权益）", "type": "number", "default": 50,
     "minimum": 1.0, "maximum": 100.0, "unit": "%",
     "help": "首次入场时希望持有的名义敞口，占当时权益的百分比。"},
    {"key": "addExposurePct", "label": "每次加仓增幅", "type": "number", "default": 25,
     "minimum": 1.0, "maximum": 100.0, "unit": "%",
     "help": "每出现一次加仓候选阶段，目标敞口增加这么多，累计不超过 100%。"},
    {"key": "reduceExposurePct", "label": "减仓后保留比例", "type": "number", "default": 50,
     "minimum": 0.0, "maximum": 100.0, "unit": "%",
     "help": "分批减仓后保留的目标敞口占原目标的比例；0 等同全部退出。"},
    {"key": "maxPortfolioRiskPct", "label": "总风险预算（占权益）", "type": "number",
     "default": 2.0,
     "minimum": 0.1, "maximum": 100.0, "unit": "%",
     "help": "入场价到结构止损的距离×数量不得超过权益的这个比例：超限的开仓/加仓被拒绝，"
             "持仓浮动风险超限则减仓回预算内（超出部分太小时整笔平仓），三处记录都可查。"},
    {"key": "enforceOpenRisk", "label": "预算同时约束持仓浮动风险", "type": "boolean",
     "default": True, "minimum": None, "maximum": None, "unit": "",
     "help": "开启后每根K线收盘按“当前价到结构止损的距离×持仓数量”复核预算，超限则在下一根"
             "开盘减仓；关闭后预算只约束开仓/加仓（即改动前的行为，用于对比）。"},
    {"key": "pivotFailureExit", "label": "枢轴失败即撤单/离场", "type": "boolean", "default": True,
     "minimum": None, "maximum": None, "unit": "",
     "help": "入场后枢轴失效：成交前撤销剩余挂单，成交后按结构失效价离场。"},
)

# Vibe factors that may be used as an *optional* explanation or filter layer later.
# They are never required: the CPA phases must run on OHLCV alone.
OPTIONAL_FACTOR_LAYER: dict[str, str] = {
    "trend_strength": "vibe.trend_strength.24",
    "ema_slope": "vibe.ema_slope.24",
    "volatility": "vibe.volatility.24",
    "atr": "vibe.atr.14",
    "range_expansion": "vibe.range_expansion.12",
    "volume_z": "vibe.volume_z.24",
    "efficiency_ratio": "vibe.efficiency_ratio.24",
    "carry": "vibe.carry.3",
    "oi_change": "vibe.oi_change.6",
}

PARAMETER_SPECS = PARAMETER_SPECS + POSITION_PARAMETER_SPECS
