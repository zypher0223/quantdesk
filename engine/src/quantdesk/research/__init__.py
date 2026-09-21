"""TradingAgents research layer.

The engine assembles an evidence bundle from data it already trusts — four
timeframe stances computed by the resonance engine, funding and open interest
readings, a rule backtest summary — and asks a configured LLM to reason over it.

Two rules shape this module:

1. The model never supplies market data. Every number in a report must be
   traceable to the bundle; claims that cite something else are flagged rather
   than published as findings.
2. The output is an observation with provenance, not an instruction. The result
   carries its sources, timestamps, and the conditions that would invalidate it.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict, dataclass, field

from ..features.resonance import DEFAULT_WEIGHTS

RESEARCH_SYSTEM_PROMPT = """你是 QuantDesk 的合约交易计划助手，服务对象是一位短线交易者。

你会收到一份由系统采集的证据包（engine_evidence），其中包含四个周期的立场、
衍生品数据、规则回测摘要，以及 price_structure（真实摆动高低点与 ATR）。

你的输出有四件事：事实、推断、情形、以及**可执行的交易计划**。

硬性要求：
1. **禁止编造数据**。任何引用的价格、指标值、时间戳都必须能在证据包里找到。
   证据包没给的，写进 missing_evidence，不要推测。
2. **必须给出具体点位**。trading_plan 的 entry、stop_loss、take_profit_1、take_profit_2
   必须是具体数字，并落在证据包给出的价格结构上（例如多头止损放在 swing_lows 之下）。
   不要写“视情况而定”“自行判断”。
3. **点位必须自洽**：
   - long：stop_loss < entry < take_profit_1 < take_profit_2
   - short：stop_loss > entry > take_profit_1 > take_profit_2
   - wait：四个点位仍须给出（表示“若触发则如此执行”），并在 trigger 里写触发条件。
4. **止损不能过近**：止损距离应大于该周期的 ATR（证据包给出 atr），否则是噪音止损。
5. **盈亏比由系统核对**：你写的 risk_reward 会按 (TP1-entry)/(entry-SL) 重新计算并比对。
6. **必须写明失效条件**，以及计划的有效期限。写失效条件与情形触发价时，
   请直接引用证据包里的读数键（如 price_structure.recent_low、resonance.score_100），
   或用「某读数 ± N 倍 ATR」这类可从证据包算出的写法，不要新造一个凭空的阈值数字。
7. **量能说明**：session_thin 为 true 的周期成交额处于休市水平，不要当作放量证据。
8. 只输出 JSON，不要 Markdown 代码块，不要额外解释。

JSON 结构：
{
  "headline": "一句话概括，不超过 40 字",
  "confidence": "high/medium/low",
  "facts": [{"statement": "可直接从证据包读到的观察", "evidence": ["4h.stance"]}],
  "inferences": [{"statement": "你的解读", "basis": ["1d.stance"], "confidence": "high/medium/low"}],
  "trading_plan": {
    "direction": "long/short/wait",
    "entry": 数字,
    "entry_zone": [下限, 上限],
    "stop_loss": 数字,
    "take_profit_1": 数字,
    "take_profit_2": 数字,
    "risk_reward": 数字,
    "position_size_pct": 数字（占账户净值百分比，建议不超过 20）,
    "timeframe": "该计划适用的周期",
    "rationale": "为什么是这些价位，引用证据键",
    "trigger": "direction=wait 时的触发条件，否则 null",
    "valid_until": "计划有效期限的描述"
  },
  "scenarios": [{"name": "情形名称", "condition": "可判定条件", "implication": "对结构的影响"}],
  "invalidation": ["使上述判断失效的可判定条件"],
  "missing_evidence": ["证据包缺失、因而无法判断的事项"],
  "data_caveats": ["数据质量方面的提醒，如休市空 bar、日线K线不足"]
}"""


# `price` marks readings on the instrument's price scale. Only those define the
# range a derived level may fall in — percentages, scores and timestamps would
# otherwise widen it until any invented number looked plausible.
PRICE_KIND = "price"
METRIC_KIND = "metric"
# Bounded scales (RSI, ADX, 0-100 scores). A level derived from these must stay
# inside the scale; a price band would wrongly admit a value like 50.
SCALE_KIND = "scale"
# Categorical readings: not numbers, yet still measurements a claim may rest on.
CATEGORICAL_KEYS = {"resonance.label", "derivatives.funding_all_zero"}
CATEGORICAL_SUFFIXES = (".stance",)


@dataclass
class EvidenceItem:
    """One traceable input, addressable by key so a report can cite it."""

    key: str
    label: str
    value: object
    source: str
    observed_at: int
    note: str = ""
    kind: str = METRIC_KIND


@dataclass
class EvidenceBundle:
    symbol: str
    display_symbol: str
    product_type: str
    venue_symbol: str
    built_at: int
    timeframes: dict
    resonance: dict
    derivatives: dict
    signals: dict
    backtest: dict | None
    price_structure: dict = field(default_factory=dict)
    news: str = ""
    missing: list[str] = field(default_factory=list)
    items: list[EvidenceItem] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)

    def key_map(self) -> dict[str, EvidenceItem]:
        return {item.key: item for item in self.items}

    def prompt_payload(self) -> dict:
        """The bundle as the model sees it: values plus how to cite them."""
        return {
            "symbol": self.display_symbol,
            "venue_symbol": self.venue_symbol,
            "product_type": self.product_type,
            "built_at": self.built_at,
            "timeframes": self.timeframes,
            "resonance": self.resonance,
            "derivatives": self.derivatives,
            "rule_signals": self.signals,
            "backtest": self.backtest,
            # Prose caveats from the engine; readable, but not citable as numbers.
            "backtest_warnings": (self.backtest or {}).get("warnings") or [],
            "price_structure": self.price_structure,
            "news": self.news or None,
            "missing": self.missing,
            "citable_keys": [item.key for item in self.items],
        }


def compute_price_structure(candles: list[dict], lookback: int = 20, swings: int = 8) -> dict:
    """Real swing levels and ATR, so entry/stop/target can land on actual structure.

    A pivot high is a bar whose high exceeds the `lookback` bars on each side,
    and the reverse for lows. ATR is the simple mean of the last 14 true ranges,
    which is what tells us whether a proposed stop sits inside the noise.
    """
    if not candles:
        return {}
    ordered = sorted(candles, key=lambda row: int(row["ts"]))
    highs = [float(bar["high"]) for bar in ordered]
    lows = [float(bar["low"]) for bar in ordered]
    closes = [float(bar["close"]) for bar in ordered]

    swing_highs: list[float] = []
    swing_lows: list[float] = []
    for index in range(lookback, len(ordered) - lookback):
        if all(highs[index] >= highs[other] for other in range(index - lookback, index + lookback + 1) if other != index):
            swing_highs.append(round(highs[index], 8))
        if all(lows[index] <= lows[other] for other in range(index - lookback, index + lookback + 1) if other != index):
            swing_lows.append(round(lows[index], 8))

    true_ranges: list[float] = []
    for index in range(1, len(ordered)):
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    atr = round(sum(true_ranges[-14:]) / len(true_ranges[-14:]), 8) if true_ranges else None

    window = min(20, len(ordered))
    recent_high = round(max(highs[-window:]), 8)
    recent_low = round(min(lows[-window:]), 8)
    last = closes[-1]
    return {
        "last_close": round(last, 8),
        "atr": atr,
        "atr_source": "最近14根K线真实波幅的均值",
        "recent_high": recent_high,
        "recent_low": recent_low,
        "recent_range_position_pct": round((last - recent_low) / (recent_high - recent_low) * 100, 4) if recent_high > recent_low else None,
        "swing_highs": swing_highs[-swings:],
        "swing_lows": swing_lows[-swings:],
        "bars": len(ordered),
    }


def flatten_items(bundle: EvidenceBundle) -> list[EvidenceItem]:
    """Addressable evidence rows: one per citable number."""
    items: list[EvidenceItem] = []

    def add(key: str, label: str, value: object, source: str, note: str = "", kind: str = METRIC_KIND) -> None:
        if value is None:
            return
        items.append(
            EvidenceItem(key=key, label=label, value=value, source=source, observed_at=bundle.built_at, note=note, kind=kind)
        )

    for interval, frame in bundle.timeframes.items():
        add(f"{interval}.close", f"{interval} 收盘价", frame.get("close"), "bybit.kline", kind=PRICE_KIND)
        add(f"{interval}.stance", f"{interval} 立场", frame.get("stance"), "engine.resonance")
        add(f"{interval}.score", f"{interval} 立场分", frame.get("score"), "engine.resonance", kind=SCALE_KIND)
        add(f"{interval}.trend", f"{interval} 趋势票", frame.get("trend"), "engine.resonance")
        add(f"{interval}.momentum", f"{interval} 动量票", frame.get("momentum"), "engine.resonance")
        add(f"{interval}.volume", f"{interval} 量能票", frame.get("volume"), "engine.resonance")
        add(f"{interval}.adx", f"{interval} ADX", frame.get("adx"), "engine.indicators", kind=SCALE_KIND)
        add(f"{interval}.rsi", f"{interval} RSI", frame.get("rsi"), "engine.indicators", kind=SCALE_KIND)
        add(f"{interval}.bars", f"{interval} 已收盘K线数", frame.get("bars"), "bybit.kline")
        add(
            f"{interval}.session_thin",
            f"{interval} 是否休市空 bar",
            frame.get("session_thin"),
            "engine.resonance",
            "为 true 时该周期成交额处于休市水平，不可当作放量",
        )

    add("resonance.score_100", "共振评分", bundle.resonance.get("score_100"), "engine.resonance", kind=SCALE_KIND)
    add("resonance.label", "共振标签", bundle.resonance.get("label"), "engine.resonance")
    add("resonance.unavailable", "数据不足的周期", bundle.resonance.get("unavailable"), "engine.resonance")
    add("derivatives.funding_rate", "当前资金费率", bundle.derivatives.get("funding_rate"), "bybit.tickers")
    add("derivatives.funding_interval_hours", "资金费结算周期(小时)", bundle.derivatives.get("funding_interval_hours"), "bybit.tickers")
    add("derivatives.funding_all_zero", "资金费历史是否全为 0", bundle.derivatives.get("funding_all_zero"), "bybit.funding")
    add("derivatives.funding_points", "资金费历史点数", bundle.derivatives.get("funding_points"), "engine.db")
    add("derivatives.open_interest_value", "持仓量名义额(USDT)", bundle.derivatives.get("open_interest_value"), "bybit.tickers")
    add("derivatives.open_interest_qty", "持仓量张数", bundle.derivatives.get("open_interest_qty"), "bybit.tickers")
    add("derivatives.turnover_24h", "24H成交额(USDT)", bundle.derivatives.get("turnover_24h"), "bybit.tickers")
    add("derivatives.price_24h_pct", "24H涨跌幅", bundle.derivatives.get("price_24h_pct"), "bybit.tickers")
    add("mark_price", "标记价", bundle.derivatives.get("mark_price"), "bybit.tickers", kind=PRICE_KIND)
    add("index_price", "指数价", bundle.derivatives.get("index_price"), "bybit.tickers", kind=PRICE_KIND)

    for key, value in bundle.signals.items():
        add(f"signals.{key}", f"规则信号 {key}", value, "engine.rules")

    structure = bundle.price_structure or {}
    for key, label, kind in (
        ("last_close", "最新收盘价", PRICE_KIND),
        ("atr", "ATR（最近14根）", METRIC_KIND),
        ("recent_high", "近20根最高", PRICE_KIND),
        ("recent_low", "近20根最低", PRICE_KIND),
        ("recent_range_position_pct", "在近20根区间中的位置(%)", SCALE_KIND),
        ("bars", "结构样本K线数", METRIC_KIND),
    ):
        add(f"price_structure.{key}", label, structure.get(key), "engine.structure", kind=kind)
    for index, value in enumerate(structure.get("swing_highs", []) or []):
        add(f"price_structure.swing_highs.{index}", f"摆动高点 #{index}", value, "engine.structure", kind=PRICE_KIND)
    for index, value in enumerate(structure.get("swing_lows", []) or []):
        add(f"price_structure.swing_lows.{index}", f"摆动低点 #{index}", value, "engine.structure", kind=PRICE_KIND)
    # An explanatory field, listed so the audit can tell "not a reading" apart from
    # "does not exist" when a report cites it.
    add("price_structure.atr_source", "ATR 计算口径", structure.get("atr_source"), "engine.structure")

    # Data gaps are evidence too: a report that cites "220 bars required" must be
    # able to point at the reading that said so.
    for index, entry in enumerate(bundle.resonance.get("unavailable") or []):
        add(f"resonance.unavailable.{index}.interval", "数据不足的周期", entry.get("interval"), "engine.resonance")
        add(f"resonance.unavailable.{index}.bars", "该周期已收盘K线数", entry.get("bars"), "engine.resonance")
        add(f"resonance.unavailable.{index}.required", "该周期所需K线数", entry.get("required"), "engine.resonance")

    if bundle.backtest:
        for key in (
            "net_return_pct",
            "max_drawdown_pct",
            "win_rate_pct",
            "profit_factor",
            "trades",
            "total_fees",
            "total_funding",
            "liquidations",
            "timeframe",
        ):
            add(f"backtest.{key}", f"回测 {key}", bundle.backtest.get(key), "engine.backtest")
        # Backtest warnings are prose, not readings, so they are folded into the
        # bundle as narrative rather than offered as citable evidence — a report
        # that cites one should be describing a caveat, not sourcing a number.

    for index, value in enumerate(bundle.missing):
        add(f"missing.{index}", "缺失项", value, "engine", "证据包未包含，报告中不应推测")
    return items


def build_bundle(
    *,
    spec,
    timeframe: str,
    frames: dict[str, dict],
    resonance: dict,
    ticker: dict,
    funding_points: int,
    funding_all_zero: bool,
    signals: dict,
    backtest: dict | None = None,
    price_structure: dict | None = None,
    news: str = "",
    missing: list[str] | None = None,
) -> EvidenceBundle:
    bundle = EvidenceBundle(
        symbol=spec.display_symbol,
        display_symbol=spec.display_symbol,
        product_type=spec.product_type,
        venue_symbol=spec.venue_symbol,
        built_at=int(time.time() * 1000),
        timeframes=frames,
        resonance={
            "score": resonance.get("score"),
            "score_100": resonance.get("score_100"),
            "label": resonance.get("label"),
            "weights": resonance.get("weights", DEFAULT_WEIGHTS),
            "unavailable": resonance.get("unavailable", []),
            "chart_timeframe": timeframe,
        },
        derivatives={
            "mark_price": ticker.get("markPrice"),
            "index_price": ticker.get("indexPrice"),
            "funding_rate": ticker.get("fundingRate"),
            "funding_interval_hours": ticker.get("fundingIntervalHour"),
            "next_funding_time": ticker.get("nextFundingTime"),
            "funding_points": funding_points,
            "funding_all_zero": funding_all_zero,
            "open_interest_qty": ticker.get("openInterest"),
            "open_interest_value": ticker.get("openInterestValue"),
            "turnover_24h": ticker.get("turnover24h"),
            "volume_24h": ticker.get("volume24h"),
            "price_24h_pct": ticker.get("price24hPcnt"),
        },
        signals=signals,
        backtest=backtest,
        price_structure=price_structure or {},
        news=news,
        missing=missing or [],
    )
    bundle.items = flatten_items(bundle)
    return bundle


# -- trading plan validation --------------------------------------------

PLAN_LEVELS = ("entry", "stop_loss", "take_profit_1", "take_profit_2")
MAX_POSITION_PCT = 20.0
# A stop inside a fraction of one ATR is inside the noise, not a risk limit.
MIN_STOP_ATR_MULTIPLE = 0.5
MAX_RISK_PCT = 20.0
# How far a level derived from the evidence may sit from the median price
# reading. Trip levels land inside this; an invented price does not.
DERIVED_BAND_PCT = 0.20


def validate_plan(plan: dict | None, bundle: EvidenceBundle) -> dict:
    """Check a proposed plan for arithmetic coherence and sane risk.

    The engine computes the ratios rather than trusting the model's own
    arithmetic, and rejects a plan whose levels are ordered the wrong way or
    whose stop sits inside one bar's noise.
    """
    if not plan or not isinstance(plan, dict):
        return {"present": False, "problems": ["模型未给出 trading_plan"], "ok": False}

    problems: list[str] = []
    notes: list[str] = []
    direction = str(plan.get("direction", "")).lower()
    if direction not in {"long", "short", "wait"}:
        problems.append(f"direction 非法：{plan.get('direction')!r}")

    levels: dict[str, float] = {}
    for key in PLAN_LEVELS:
        raw = plan.get(key)
        try:
            levels[key] = float(raw)
        except (TypeError, ValueError):
            problems.append(f"{key} 不是数字：{raw!r}")

    computed: dict[str, float | None] = {
        "riskPerUnit": None,
        "rewardPerUnit": None,
        "riskReward": None,
        "riskPct": None,
        "rewardPct": None,
        "stopAtrMultiple": None,
    }

    if len(levels) == 4:
        entry, stop = levels["entry"], levels["stop_loss"]
        tp1, tp2 = levels["take_profit_1"], levels["take_profit_2"]
        if direction == "long" and not (stop < entry < tp1 < tp2):
            problems.append("多头点位顺序必须满足 stop_loss < entry < take_profit_1 < take_profit_2")
        if direction == "short" and not (stop > entry > tp1 > tp2):
            problems.append("空头点位顺序必须满足 stop_loss > entry > take_profit_1 > take_profit_2")
        risk = abs(entry - stop)
        reward = abs(tp1 - entry)
        if risk > 0:
            computed["riskPerUnit"] = round(risk, 8)
            computed["rewardPerUnit"] = round(reward, 8)
            computed["riskReward"] = round(reward / risk, 4)
            computed["riskPct"] = round(risk / entry * 100, 4) if entry else None
            computed["rewardPct"] = round(reward / entry * 100, 4) if entry else None
            atr = (bundle.price_structure or {}).get("atr")
            if atr:
                computed["stopAtrMultiple"] = round(risk / float(atr), 4)
                if risk < float(atr) * MIN_STOP_ATR_MULTIPLE:
                    problems.append(f"止损距离仅 {risk:.4g}，不足 0.5 倍 ATR（{float(atr):.4g}），属于噪音止损")
            if computed["riskPct"] is not None and computed["riskPct"] > MAX_RISK_PCT:
                problems.append(f"单笔风险 {computed['riskPct']:.2f}% 超过 {MAX_RISK_PCT:g}% 上限")
        else:
            problems.append("entry 与 stop_loss 相同，无法计算风险")

        claimed = plan.get("risk_reward")
        if claimed is not None and computed["riskReward"] is not None:
            try:
                claimed_value = float(claimed)
                if abs(claimed_value - computed["riskReward"]) > max(0.15, computed["riskReward"] * 0.2):
                    notes.append(f"模型自报盈亏比 {claimed_value:g}，按点位重算为 {computed['riskReward']:g}；以重算值为准")
            except (TypeError, ValueError):
                notes.append(f"risk_reward 不是数字：{claimed!r}")

        structure = bundle.price_structure or {}
        swing_lows = [float(value) for value in structure.get("swing_lows", []) or []]
        swing_highs = [float(value) for value in structure.get("swing_highs", []) or []]
        if direction == "long" and swing_lows and stop > max(swing_lows):
            notes.append("多头止损高于最近的摆动低点，容易被回踩扫掉")
        if direction == "short" and swing_highs and stop < min(swing_highs):
            notes.append("空头止损低于最近的摆动高点，容易被反弹扫掉")

    size = plan.get("position_size_pct")
    if size is not None:
        try:
            size_value = float(size)
            if not 0 < size_value <= 100:
                problems.append(f"position_size_pct 必须在 (0, 100] 之间：{size_value}")
            elif size_value > MAX_POSITION_PCT:
                notes.append(f"建议仓位 {size_value:g}% 高于 {MAX_POSITION_PCT:g}% 上限，已按上限提示")
        except (TypeError, ValueError):
            problems.append(f"position_size_pct 不是数字：{size!r}")
    else:
        notes.append("未给出建议仓位")

    if not plan.get("invalidation") and not (plan.get("valid_until")):
        notes.append("未给出计划失效条件或有效期")

    entry_zone = []
    for value in plan.get("entry_zone") or []:
        try:
            entry_zone.append(float(value))
        except (TypeError, ValueError):
            problems.append(f"entry_zone 含有非数字：{value!r}")

    return {
        "present": True,
        "direction": direction,
        "levels": levels or None,
        "entryZone": entry_zone or None,
        # A degenerate price (entry == stop) yields a non-finite ratio; JSON has
        # no NaN, so it is reported as null rather than dropped by the encoder.
        "computed": {key: (value if value is None or math.isfinite(value) else None) for key, value in computed.items()},
        "problems": problems,
        "notes": notes,
        "ok": not problems,
    }


# -- report validation --------------------------------------------------

# Numbers that look like a price or indicator reading.
#
# The sign is captured, because stripping it would let a report flip a -5.38%
# loss into +5.38% and still pass. The lookbehind keeps a hyphen between two
# digits ("332.11-333.73") as a range separator rather than a minus sign.
_NUMBER = re.compile(r"(?<![\d.])[-+]?\d[\d,]*(?:\.\d+)?")
# Advisory framing: the wording that turns a plan into an instruction. Bare
# direction words ("回踩做多计划", "追多盈亏比不理想") are how a trading plan is
# described and are not flagged; "建议买入" is.
_DIRECTIVE = re.compile(
    r"(建议|应当|应该|必须|不妨|推荐)\s*(您|你|投资者)?\s*"
    r"(买入|卖出|做多|做空|开多|开空|加仓|减仓|建仓|平仓|止损|止盈|进场|离场|持有)"
    r"|请\s*(买入|卖出|做多|做空|建仓|平仓|加仓|减仓)"
    r"|(建议|可以考虑|不妨)[^。；]{0,12}?(建立|开设)[^。；]{0,6}?(多|空)头"
)


def is_reading(value: object) -> bool:
    """Is this value a measurement rather than descriptive prose?

    Numbers count, and so do numeric strings — the venue sends ticker fields as
    strings. Booleans count too (they are states). Free text does not.
    """
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value.replace(",", ""))
        except ValueError:
            return False
        return True
    return False


def _numbers_in(text: str) -> set[float]:
    out: set[float] = set()
    for raw in _NUMBER.findall(text or ""):
        try:
            out.add(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def _bundle_numbers(bundle: EvidenceBundle) -> set[float]:
    """Every number the bundle actually contains, at several roundings.

    Derived from the evidence values themselves, so a report citing 333.2 is
    checked against that reading rather than a hardcoded list.
    """
    numbers: set[float] = set()

    def add_number(number: float) -> None:
        for scale in (1, 10, 100, 1000):
            numbers.add(round(number * scale) / scale)

    def harvest(value: object, key: str = "", depth: int = 0) -> None:
        if depth > 6 or value is None or isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            number = float(value)
            add_number(number)
            # A ratio field is legitimately quoted as a percentage: a funding
            # rate of 0.004823 and "+0.4823%" are the same reading.
            if any(token in key for token in ("pct", "rate", "ratio")):
                add_number(number * 100)
            return
        if isinstance(value, str):
            found = _numbers_in(value)
            numbers.update(found)
            # Ticker fields arrive as strings; scale them the same way so a
            # quoted percentage matches a stored ratio.
            if any(token in key for token in ("pct", "rate", "ratio")):
                for number in found:
                    add_number(number * 100)
            return
        if isinstance(value, dict):
            for item_key, item in value.items():
                harvest(item, str(item_key), depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                harvest(item, key, depth + 1)

    harvest(bundle.prompt_payload())
    return numbers


def validate_report(report: dict, bundle: EvidenceBundle, plan_check: dict | None = None) -> dict:
    """Check a report against its own evidence bundle.

    Plan levels are model-chosen, so they are checked structurally rather than
    against the bundle; everything else must cite a reading that exists.
    """
    known = _bundle_numbers(bundle)

    def accept(value: object, depth: int = 0) -> None:
        """Treat a number as accounted for at several roundings."""
        if depth > 4 or value is None or isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            for scale in (1, 10, 100, 1000, 10_000):
                known.add(round(float(value) * scale) / scale)
            return
        if isinstance(value, dict):
            for item in value.values():
                accept(item, depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                accept(item, depth + 1)

    # Plan levels, the entry zone and — importantly — the ratios the engine
    # computed from them are all legitimate things for a report to quote. They
    # are the model's own proposal or the engine's own arithmetic, so they are
    # judged for coherence (see validate_plan), not looked up in the bundle.
    plan = report.get("trading_plan") if isinstance(report.get("trading_plan"), dict) else {}
    for key in ("entry", "stop_loss", "take_profit_1", "take_profit_2", "risk_reward", "position_size_pct"):
        accept(plan.get(key))
    accept(plan.get("entry_zone"))
    if plan_check:
        accept(plan_check.get("levels"))
        accept(plan_check.get("entryZone"))
        accept(plan_check.get("computed"))
    items = bundle.key_map()
    # Evidence is a *reading*: a number, or a small categorical value such as a
    # stance of bull/bear/neutral. Explanatory prose (the ATR source note) is not
    # a measurement, so citing it must not make a claim look sourced.
    evidence_keys = {
        key
        for key, item in items.items()
        # Ticker fields arrive as numeric strings, so "is it parseable" matters
        # more than the Python type it happens to have.
        if is_reading(item.value)
        or key in CATEGORICAL_KEYS
        or key.endswith(CATEGORICAL_SUFFIXES)
    }
    keys = set(items)

    cited: set[str] = set()
    unsupported: list[dict] = []
    derived: list[dict] = []
    sign_flips: list[dict] = []
    directives: list[str] = []

    # A scenario or invalidation legitimately names a level derived from the
    # readings (entry minus one ATR, for example). Those sit near the price
    # evidence, so they are reported as derived rather than invented; a price a
    # long way from every reading is still rejected.
    # Only outright price levels define the band. ATR is a distance, and drawdown
    # percentages or timestamps are not prices at all; including them would widen
    # the band until any invented number looked plausible.
    price_values = [
        float(item.value)
        for item in bundle.items
        if item.kind == PRICE_KIND and isinstance(item.value, (int, float)) and not isinstance(item.value, bool)
    ]
    if price_values:
        anchor = sorted(price_values)[len(price_values) // 2]  # median, robust to outliers
        band = abs(anchor) * DERIVED_BAND_PCT
        derived_low, derived_high = anchor - band, anchor + band
    else:
        derived_low = derived_high = None

    # Bounded indicators live on a fixed scale, so a threshold derived from them
    # must stay inside it. A price band would otherwise admit a value like 50 for
    # an indicator that ranges 0-100 and is currently reading 66.7.
    # Only actual indicator readings set the scale. Harvesting the whole bundle
    # would pull "0-100" out of the ATR description and widen the scale to
    # everything, which is how a fabricated threshold of 50 got through.
    scale_values = [
        float(item.value)
        for item in bundle.items
        if item.kind == SCALE_KIND and isinstance(item.value, (int, float)) and not isinstance(item.value, bool)
    ]
    if scale_values:
        # A threshold derived from an indicator sits near the readings that were
        # taken, on the indicator's own 0-100 axis. Anchoring to the observed
        # window is what rejects a fabricated "score below 50" when the score is
        # 66.7 and nothing in the evidence is anywhere near 50.
        scale_low = max(0.0, min(scale_values) - 20.0)
        scale_high = min(100.0, max(scale_values) + 20.0)
    else:
        scale_low = scale_high = None

    def scan_numbers(text: object, where: str) -> None:
        body = str(text or "")
        if not body:
            return
        for number in _numbers_in(body):
            # Tolerate rounding: a value within half a percent of a known
            # reading is treated as that reading.
            if any(abs(number - candidate) <= max(abs(candidate) * 0.005, 1e-9) for candidate in known):
                continue
            # Sign flips are checked before any "plausible derivation" rule:
            # quoting a -5.38% drawdown as +5.38% is a specific, nameable error
            # that a loose magnitude rule would otherwise wave through.
            if any(abs(-number - candidate) <= max(abs(candidate) * 0.005, 1e-9) for candidate in known):
                sign_flips.append({"where": where, "number": number, "text": body[:160]})
                continue
            if derived_low is not None and derived_low <= number <= derived_high:
                derived.append({"where": where, "number": number})
                continue
            # A threshold on an indicator's own axis.
            #
            # Known limit: from digits alone the audit cannot separate a level the
            # model derived from a reading (entry minus one ATR) from one it
            # invented inside the same window. These are reported separately as
            # `derivedNumbers` so a human sees which numbers were not looked up,
            # rather than having them presented as verified evidence.
            if scale_low is not None and scale_low <= number <= scale_high:
                derived.append({"where": where, "number": number})
                continue
            unsupported.append({"where": where, "number": number, "text": body[:160]})

    def scan(text: object, where: str) -> None:
        body = str(text or "")
        if not body:
            return
        scan_numbers(body, where)
        if _DIRECTIVE.search(body):
            directives.append(f"{where}: {body[:120]}")

    def scan_keys(values: object, where: str) -> None:
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple)):
            return
        for entry in values:
            text = str(entry)
            # Citation keys look like "4h.stance" or "backtest.net_return_pct".
            # Timeframe prefixes start with a digit, so the leading character
            # class must accept one.
            for candidate in re.findall(r"[A-Za-z0-9_]+\.[A-Za-z0-9_.]+", text):
                cited.add(candidate)

    scan(report.get("headline"), "headline")
    for index, fact in enumerate(report.get("facts") or []):
        scan(fact.get("statement") if isinstance(fact, dict) else fact, f"facts[{index}]")
        if isinstance(fact, dict):
            scan_keys(fact.get("evidence"), f"facts[{index}].evidence")
    for index, inference in enumerate(report.get("inferences") or []):
        scan(inference.get("statement") if isinstance(inference, dict) else inference, f"inferences[{index}]")
        if isinstance(inference, dict):
            scan_keys(inference.get("basis"), f"inferences[{index}].basis")
    for index, scenario in enumerate(report.get("scenarios") or []):
        if not isinstance(scenario, dict):
            continue
        scan(scenario.get("condition"), f"scenarios[{index}].condition")
        scan(scenario.get("implication"), f"scenarios[{index}].implication")
        scan(scenario.get("name"), f"scenarios[{index}].name")
    for index, line in enumerate(report.get("invalidation") or []):
        scan(line, f"invalidation[{index}]")
    for index, line in enumerate(report.get("missing_evidence") or []):
        scan(line, f"missing_evidence[{index}]")
    for index, line in enumerate(report.get("data_caveats") or []):
        scan(line, f"data_caveats[{index}]")

    # The plan block is meant to be actionable, so action wording there is
    # expected; only its arithmetic is judged (see validate_plan). Its numbers
    # are still checked, because a stray price in the rationale would still be
    # a number the bundle cannot account for.
    plan = report.get("trading_plan") if isinstance(report.get("trading_plan"), dict) else None
    if plan:
        scan_numbers(plan.get("rationale"), "trading_plan.rationale")
        for index, value in enumerate(plan.get("entry_zone") or []):
            scan_numbers(value, f"trading_plan.entry_zone[{index}]")

    unknown = sorted(key for key in cited if key not in keys)
    non_evidence = sorted(key for key in cited if key in keys and key not in evidence_keys)
    return {
        "unsupportedNumbers": unsupported,
        "nonEvidenceCitations": non_evidence,
        "signFlippedNumbers": sign_flips,
        "derivedNumbers": derived,
        "directives": directives,
        "citedKeys": sorted(cited),
        "unknownCitedKeys": unknown,
        "plan": plan_check or {"present": False, "ok": False, "problems": ["未校验"], "notes": []},
        "verified": not unsupported and not sign_flips and not directives and not unknown and not non_evidence,
    }


def parse_report(text: str) -> dict | None:
    """Tolerate a fenced or chatty response; return None when it is not JSON."""
    if not text:
        return None
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.S)
    if fence:
        candidate = fence.group(1).strip()
    if not candidate.startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        candidate = candidate[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def report_markdown(report: dict, bundle: EvidenceBundle, validation: dict) -> str:
    """The archived form: findings plus the provenance needed to audit them."""
    lines = [
        f"# {bundle.display_symbol} 研判报告",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(bundle.built_at / 1000))}",
        f"- 合约：{bundle.venue_symbol}（{bundle.product_type}）",
        f"- 图表周期：{bundle.resonance.get('chart_timeframe')}",
        f"- 结论：{report.get('headline', '—')}",
        f"- 置信度：{report.get('confidence', '—')}",
        "",
        "## 事实（来自证据包）",
        "",
    ]
    for fact in report.get("facts") or []:
        statement = fact.get("statement") if isinstance(fact, dict) else fact
        cited = ", ".join(str(item) for item in ((fact.get("evidence") if isinstance(fact, dict) else None) or [])) or "未标注"
        lines.append(f"- {statement}  \n  依据：{cited}")
    lines += ["", "## 推断", ""]
    for inference in report.get("inferences") or []:
        statement = inference.get("statement") if isinstance(inference, dict) else inference
        basis = ", ".join(str(item) for item in ((inference.get("basis") if isinstance(inference, dict) else None) or [])) or "未标注"
        level = inference.get("confidence", "—") if isinstance(inference, dict) else "—"
        lines.append(f"- {statement}（置信度 {level}）  \n  依据：{basis}")
    lines += ["", "## 情形", ""]
    for scenario in report.get("scenarios") or []:
        if isinstance(scenario, dict):
            lines.append(f"- **{scenario.get('name', '')}**：{scenario.get('condition', '')} → {scenario.get('implication', '')}")

    plan = report.get("trading_plan") if isinstance(report.get("trading_plan"), dict) else None
    lines += ["", "## 交易计划（模拟盘候选）", ""]
    if not plan:
        lines.append("- 模型未给出计划")
    else:
        computed = (validation.get("plan") or {}).get("computed") or {}
        lines += [
            f"- 方向：{plan.get('direction', '—')}",
            f"- 入场：{plan.get('entry', '—')}" + (f"（区间 {plan.get('entry_zone')}）" if plan.get("entry_zone") else ""),
            f"- 止损：{plan.get('stop_loss', '—')}",
            f"- 止盈：TP1 {plan.get('take_profit_1', '—')} / TP2 {plan.get('take_profit_2', '—')}",
            f"- 盈亏比：{computed.get('riskReward', '—')}（系统按点位重算；模型自报 {plan.get('risk_reward', '—')}）",
            f"- 单笔风险：{computed.get('riskPct', '—')}% · 止损距离/ATR：{computed.get('stopAtrMultiple', '—')}",
            f"- 建议仓位：{plan.get('position_size_pct', '—')}%",
            f"- 适用周期：{plan.get('timeframe', '—')}",
            f"- 触发条件：{plan.get('trigger') or '—'}",
            f"- 有效期：{plan.get('valid_until', '—')}",
            f"- 依据：{plan.get('rationale', '—')}",
        ]
        plan_result = validation.get("plan") or {}
        if plan_result.get("problems"):
            lines += ["", "### 计划问题", ""] + [f"- {item}" for item in plan_result["problems"]]
        if plan_result.get("notes"):
            lines += ["", "### 计划提醒", ""] + [f"- {item}" for item in plan_result["notes"]]

    lines += ["", "## 失效条件", ""]
    lines += [f"- {line}" for line in report.get("invalidation") or []] or ["- 未提供"]
    lines += ["", "## 缺失证据", ""]
    lines += [f"- {line}" for line in report.get("missing_evidence") or []] or ["- 无"]
    lines += ["", "## 数据提醒", ""]
    lines += [f"- {line}" for line in report.get("data_caveats") or []] or ["- 无"]
    lines += [
        "",
        "## 校验",
        "",
        f"- 数值全部可在证据包中定位：{'是' if not validation['unsupportedNumbers'] else '否'}",
        f"- 无符号颠倒（把亏损写成盈利）：{'是' if not validation.get('signFlippedNumbers') else '否'}",
        f"- 未出现越权指令用语：{'是' if not validation['directives'] else '否'}",
        f"- 引用键有效：{'是' if not validation['unknownCitedKeys'] else '否'}",
        f"- 引用键均为读数：{'是' if not validation.get('nonEvidenceCitations') else '否'}",
        f"- 引用键数量：{len(validation['citedKeys'])}",
        f"- 交易计划自洽：{'是' if (validation.get('plan') or {}).get('ok') else '否'}",
    ]
    if validation["unsupportedNumbers"]:
        lines += ["", "### 无法定位的数值"]
        lines += [f"- {item['where']}：{item['number']}" for item in validation["unsupportedNumbers"][:20]]
    if validation["directives"]:
        lines += ["", "### 疑似指令用语"]
        lines += [f"- {item}" for item in validation["directives"][:20]]
    if validation["unknownCitedKeys"]:
        lines += ["", "### 无效引用键"]
        lines += [f"- {item}" for item in validation["unknownCitedKeys"][:20]]
    lines += ["", "> 本报告由模型基于上述证据生成，不是交易指令。价格与指标以交易所实时数据为准。"]
    return "\n".join(lines)
