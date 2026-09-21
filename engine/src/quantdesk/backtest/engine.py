"""Rule backtest with the costs a perpetual actually charges.

The engine owns this so the CLI and the web UI cannot drift into two different
answers. It models what the first prototype left out:

* the venue's risk ladder, so maintenance margin and the leverage cap follow the
  position's own notional instead of one global constant
* mark-price bars for liquidation, valuation and funding, with the bar close used
  only when no mark series is available locally and declared as a fallback
* historical funding settled at the venue's own settlement timestamps, not at
  "every 8 hours from the first bar"
* isolated-margin liquidation, checked against each mark bar's adverse extreme
* leverage, which scales notional exposure while the loss stays bounded by margin
* tick / step quantisation, a minimum order notional and a participation-aware
  slippage model
* off-hours bars, where a stock perp prints but does not really trade
* leveraged-ETF decay, which a rule backtest on a 3x product silently inherits

Execution assumption is fixed and stated in every result: a signal is known only
after bar i-1 closes, so fills happen at bar i's open.

From the event-driven layer (all of it inert unless a config field asks for it):

* every order is an object with a lifecycle - created, accepted, partially_filled,
  filled, cancelled, rejected - and the run returns the whole order log
* a participation-capped or resting entry can carry its unfilled remainder across
  bars instead of being truncated at the signal bar
* stop loss / take profit / trailing stop, evaluated inside the bar with the
  adverse extreme first, and never on information from a bar that has not closed
* a maker entry that only fills when the bar actually trades through its limit
* a liquidation fee kept apart from the trading fee
* higher-timeframe inputs that stay invisible until the bar that carries them has
  closed, and a strict as-of rule for every other input
* proxy / derived data declared in machine-readable form next to the result
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from math import floor

from ..datahub.venue import INTERVAL_MS
from ..risk import RiskProfile, liquidation_price, maintenance_margin_for

DEFAULT_TAKER_FEE_BPS = 10.0
DEFAULT_SLIPPAGE_BPS = 5.0
DEFAULT_MAINTENANCE_MARGIN_RATE = 0.005
# A sanity ceiling only: the venue's ladder decides the real cap per contract
# (BTCUSDT allows 150x at its smallest notional, SOXLUSDT 100x, AMDSTOCKUSDT 50x).
MAX_LEVERAGE = 200.0
FUNDING_INTERVAL_HOURS = 8.0

# An off-hours bar is one whose traded notional is negligible against its recent
# typical. Same rule the resonance panel uses.
THIN_SESSION_RATIO = 0.10
THIN_SESSION_MIN_NOTIONAL = 5_000.0

# Candle sources that are not a venue print. A result built on them is a result
# about a proxy, and has to say so.
NON_VENUE_SOURCES = ("local_derived", "imported", "upload", "synthetic", "proxy", "spot_history")

# Exit reasons the protection layer can produce (on top of the historical three).
PROTECTION_REASONS = ("stop_loss", "take_profit", "trailing_stop")

# How many orders the result keeps. A 20,000-bar run trades thousands of times and
# the order log is the biggest part of the payload the queue stores and the browser
# fetches; past this point the counts stay exact and the log says it was trimmed.
MAX_ORDER_LOG = 3_000


@dataclass
class BacktestConfig:
    strategy_id: str = "ma_cross"
    strategy_params: dict = field(default_factory=dict)
    fast_period: int = 9
    slow_period: int = 21
    direction: str = "both"  # both | long
    initial_capital: float = 10_000.0
    allocation_pct: float = 50.0
    fee_bps: float = DEFAULT_TAKER_FEE_BPS
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    leverage: float = 1.0
    maintenance_margin_rate: float = DEFAULT_MAINTENANCE_MARGIN_RATE
    include_funding: bool = True
    include_liquidation: bool = True
    # fill_on_thin: "skip" defers entries to the next tradable bar, "allow"
    # treats the price as fillable. Skipping is the honest default.
    fill_on_thin: str = "skip"
    tick_size: float | None = None
    qty_step: float | None = None
    min_order_notional: float = 5.0
    # fixed: one spread cost per side. participation: that plus an impact term
    # that grows with the share of the bar's traded notional the order takes.
    slippage_model: str = "fixed"
    impact_coefficient: float = 0.1
    # Cap on participation before the impact term is applied, so a tiny bar with
    # an outsized order is reported honestly instead of producing a silly price.
    max_participation: float = 1.0
    # Bars between the signal and the fill. 0 means "the next bar's open", which
    # is what a signal computed on a closed bar can actually reach; a larger value
    # models a slower path to the venue, and costs whatever the market did meanwhile.
    latency_bars: int = 0
    # ignore: fill the whole order and charge the capped impact (the historical
    # behaviour, kept as the default so old results stay comparable).
    # cap:    fill at most max_participation of the bar's volume, carry the
    #         remainder to the next bar while the signal still wants it, and
    #         report what was never filled when the intent ends.
    partial_fill: str = "ignore"
    # ---- order lifecycle / protection (all inert by default) ----
    # Fee charged when the entry was a resting limit that got hit. None = the
    # taker fee, i.e. no maker discount is assumed.
    maker_fee_bps: float | None = None
    # never:        every fill crosses the spread (historical behaviour).
    # passive_only: the entry rests at the signal bar's close and only fills if
    #               the bar trades through it; the exit is still a taker.
    maker_fill: str = "never"
    # How many bars a resting maker entry stays alive before it is cancelled.
    maker_order_bars: int = 1
    # Percentages off the entry price. None = the protection is not simulated,
    # which is what every result before this layer assumed.
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None
    # conservative: when one bar contains both a stop and a take profit, the
    # adverse one is assumed to have happened first (the default and the only
    # defensible answer without tick data). optimistic: the favourable one first,
    # for sensitivity analysis only.
    bar_path: str = "conservative"
    # Charged on top of the trading fee when a position is liquidated. Kept out of
    # `fees` so a liquidation's cost is not hidden inside the trading fee.
    liquidation_fee_bps: float = 0.0


@dataclass
class Order:
    """One order and what happened to it, from signal to terminal state.

    The engine keeps this because "the backtest traded 30 times" hides the part
    that matters: how much of each order the book actually took, how long it took,
    and whether the order died unfilled.
    """

    order_id: int
    created_index: int
    created_time: int
    side: str  # buy / sell
    purpose: str  # entry / exit
    order_type: str  # market / limit / stop / take_profit / trailing_stop / liquidation
    quantity: float
    reason: str | None = None  # signal / stop_loss / take_profit / trailing_stop / liquidation
    limit_price: float | None = None
    status: str = "created"  # created/accepted/partially_filled/filled/cancelled/rejected
    filled_quantity: float = 0.0
    unfilled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    filled_index: int | None = None
    filled_time: int | None = None
    bars_to_fill: int = 0
    fee_kind: str = "taker"  # taker / maker
    rejected_reason: str | None = None
    note: str | None = None
    fills: list[dict] = field(default_factory=list)

    @property
    def filled(self) -> bool:
        return self.status == "filled"


@dataclass
class BacktestTrade:
    direction: str  # 多 / 空
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    quantity: float
    notional: float
    gross_pnl: float
    funding_paid: float
    fees: float
    net_pnl: float
    return_pct: float
    bars_held: int
    exit_reason: str  # signal / liquidation / end_of_data / stop_loss / take_profit / trailing_stop
    liquidated: bool = False
    thin_entry: bool = False
    risk_tier_id: int | None = None
    maintenance_margin_rate: float | None = None
    # --- additive: how the entry was assembled and what the exit cost ---
    entry_fills: int = 1
    entry_delay_bars: int = 0  # bars between the first and the last entry fill
    exit_fee: float = 0.0
    # Charged separately on liquidation and *not* folded into `fees`.
    liquidation_fee: float = 0.0


@dataclass
class BacktestResult:
    config: dict
    instrument: dict
    initial_capital: float
    final_equity: float
    net_return_pct: float
    max_drawdown_pct: float
    win_rate_pct: float
    profit_factor: float | None
    total_fees: float
    total_funding: float
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    data_quality: dict = field(default_factory=dict)
    risk: dict = field(default_factory=dict)
    # How a signal became a fill: the rule, the latency, the participation policy
    # and what was assumed away. Every result carries it, because a return number
    # without its execution assumptions is not a result anybody can act on.
    execution_model: dict = field(default_factory=dict)
    # The order log: status transitions, per-fill quantity/price, fill delay.
    orders: list[Order] = field(default_factory=list)
    # Machine-readable proxy annotations: [{field, kind, note}].
    data_proxies: list[dict] = field(default_factory=list)
    # Liquidation fees, kept out of total_fees on purpose.
    total_liquidation_fees: float = 0.0

    def as_dict(self) -> dict:
        payload = asdict(self)
        return payload


@dataclass
class _Position:
    direction: int  # 1 long, -1 short
    entry_time: int
    entry_price: float
    quantity: float
    notional: float
    entry_fee: float
    margin: float
    liq_price: float | None
    funding_paid: float = 0.0
    entry_index: int = 0
    thin_entry: bool = False
    risk_tier_id: int | None = None
    maintenance_margin_rate: float = DEFAULT_MAINTENANCE_MARGIN_RATE
    maintenance_margin: float = 0.0
    # --- event-driven layer ---
    fill_count: int = 1
    last_fill_index: int = 0
    # Best price seen since entry, updated after each bar's protection checks so a
    # trailing stop can never be computed from the same bar it triggers in.
    best_price: float = 0.0
    mm_deduction: float = 0.0


@dataclass
class _Fill:
    """One entry leg: what the book took, at what price, for which fee."""

    quantity: float
    price: float
    fee: float
    margin: float
    notional: float
    impact_extra: float
    fee_kind: str
    tier: object | None
    tier_mmr: float
    tier_maintenance: float
    mm_deduction: float


@dataclass
class _PendingEntry:
    """An entry order that still has quantity to fill after this bar."""

    order: Order
    direction: int
    remaining: float
    thin_entry: bool = False
    # None: alive while the signal still wants the direction (participation cap).
    # int:  a resting maker limit, which dies after this many match attempts.
    attempts_left: int | None = None


@dataclass(frozen=True)
class PositionIntent:
    """A structured position instruction for one bar.

    The event contract (`1/-1/0/None`) can only say "be long", which cannot express
    "add a third", "take half off" or "the pivot broke, drop the resting order". An
    intent says what the position should *be* - a target exposure - and the engine
    works out the order that gets there. It is still data: an action name, a target
    percentage, a stop price and a reason. Nothing here is executable code.

    Intents are read with the same latency as events: the intent on bar `i` is decided
    from that bar's close and filled at bar `i + 1`'s open, never earlier.
    """

    action: str = "hold"  # hold | open | increase | reduce | exit | cancel
    direction: str = "long"  # long | short
    target_exposure_pct: float | None = None
    stage: str = ""
    stop_price: float | None = None
    reason: str = ""

    ACTIONS = ("hold", "open", "increase", "reduce", "exit", "cancel")

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "direction": self.direction,
            "targetExposurePct": self.target_exposure_pct,
            "stage": self.stage,
            "stopPrice": self.stop_price,
            "reason": self.reason,
        }


def _validated_intents(intents: list | None, bars: int) -> list["PositionIntent | None"] | None:
    """Refuse an intent series the engine cannot align with the bars.

    A mismatch here would silently shift every decision by some number of bars, which
    is exactly the class of bug the rest of this module is written to prevent.
    """
    if intents is None:
        return None
    if len(intents) != bars:
        raise ValueError(f"仓位意图数量（{len(intents)}）必须与K线数量（{bars}）一致")
    for index, item in enumerate(intents):
        if item is None:
            continue
        if not isinstance(item, PositionIntent):
            raise ValueError(f"第 {index} 根仓位意图必须是 PositionIntent 或 None")
        if item.action not in PositionIntent.ACTIONS:
            raise ValueError(
                f"第 {index} 根仓位意图的 action 无效：{item.action}；"
                f"可用：{', '.join(PositionIntent.ACTIONS)}"
            )
        if item.direction not in ("long", "short"):
            raise ValueError(f"第 {index} 根仓位意图的 direction 无效：{item.direction}")
        if item.action in ("open", "increase", "reduce"):
            target = item.target_exposure_pct
            if target is None or not 0 <= float(target) <= 100 * 10:
                raise ValueError(
                    f"第 {index} 根仓位意图的 target_exposure_pct 缺失或越界：{target}"
                )
    return list(intents)


def stop_fill_price(direction: int, stop_price: float, bar_open: float) -> float:
    """Where a structural stop actually fills.

    A bar that opens beyond the stop has already gapped through it, so the fill is the
    open - worse than the stop for both directions. This is the same convention the
    percentage-stop path uses, applied to a price taken from market structure.
    """
    return min(bar_open, stop_price) if direction == 1 else max(bar_open, stop_price)


def liquidation_price(
    direction: int,
    entry_price: float,
    leverage: float | None = None,
    mmr: float = 0.0,
    *,
    quantity: float | None = None,
    margin: float | None = None,
    mm_deduction: float = 0.0,
) -> float | None:
    """Isolated-margin liquidation price.

    Kept as a thin wrapper over the shared risk module so the CLI, the API and
    the paper book all answer with the same arithmetic. The legacy positional
    form `(direction, entry, leverage, mmr)` is still accepted.
    """
    from ..risk import liquidation_price as _price

    if quantity is None:
        if leverage is None or leverage <= 0:
            return None
        quantity = 1.0
        margin = entry_price / leverage
    return _price(
        direction,
        entry_price,
        quantity,
        margin if margin is not None else 0.0,
        mmr,
        mm_deduction,
        leverage=leverage,
    )


def _maker_fee_bps(config: BacktestConfig) -> float:
    return float(config.fee_bps if config.maker_fee_bps is None else config.maker_fee_bps)


def _fee_rate(config: BacktestConfig, kind: str) -> float:
    return (_maker_fee_bps(config) if kind == "maker" else config.fee_bps) / 10_000


def _protection_active(config: BacktestConfig) -> bool:
    return bool(config.stop_loss_pct or config.take_profit_pct or config.trailing_stop_pct)


def _execution_model(
    config: BacktestConfig,
    unfilled_orders: int,
    unfilled_notional: float,
    *,
    unfilled_quantity: float = 0.0,
    max_fill_delay: int = 0,
    stop_exits: int = 0,
) -> dict:
    """The execution assumptions, stated once and carried by every result."""
    maker = _maker_fee_bps(config)
    simplifications = [
        "平仓按整笔成交，不建模退出时的部分成交",
        "不建模排队位置、盘口深度与下单被拒",
        "同一根K线内不区分成交先后，先判强平与退出再判开仓",
    ]
    if config.partial_fill == "cap":
        simplifications = [
            item.replace(
                "平仓按整笔成交，不建模退出时的部分成交",
                "开仓可以跨K线分笔成交，平仓仍按整笔成交",
            )
            for item in simplifications
        ]
    if config.maker_fill == "passive_only":
        simplifications.append("挂单只按限价成交，不建模排队位置与撤单竞争")
    if _protection_active(config):
        simplifications.append(
            "止损/止盈只用K线（或标记价）极值判断，同根K线内的先后顺序按配置的保守路径假定"
        )
    return {
        "fillRule": _fill_rule(config),
        "signalBar": "已收盘K线",
        "fillPrice": (
            "延迟后那根K线的开盘价加滑点"
            if config.maker_fill == "never"
            else "入场为限价挂单（触及才成交、按 maker 费率），出场为延迟后那根K线的开盘价加滑点"
        ),
        "latencyBars": int(config.latency_bars),
        "slippageModel": config.slippage_model,
        "slippageBps": float(config.slippage_bps),
        "impactCoefficient": float(config.impact_coefficient),
        "maxParticipation": float(config.max_participation),
        "partialFill": config.partial_fill,
        "unfilledOrders": int(unfilled_orders),
        "unfilledNotional": round(float(unfilled_notional), 6),
        "unfilledQuantity": round(float(unfilled_quantity), 8),
        "maxFillDelayBars": int(max_fill_delay),
        "orderLifecycle": "created → accepted → partially_filled → filled / cancelled / rejected",
        "feeModel": {
            "takerBps": float(config.fee_bps),
            "makerBps": maker,
            "makerFill": config.maker_fill,
            "applied": (
                "开平都吃单"
                if config.maker_fill == "never"
                else "入场挂单吃 maker（未触及即不成交），出场吃 taker"
            ),
        },
        "liquidationFeeBps": float(config.liquidation_fee_bps),
        "protection": {
            "stopLossPct": config.stop_loss_pct,
            "takeProfitPct": config.take_profit_pct,
            "trailingStopPct": config.trailing_stop_pct,
            "barPath": config.bar_path,
            "stopExits": int(stop_exits),
        },
        "asOfRule": "第 i 根只用 ts<=t_i 的信息：信号取已收盘K线，高低极值属当根，高周期K线收盘后才可见",
        "feeTiming": "开平各计一次，开仓手续费在成交时从未实现盈亏中扣除",
        "fundingTiming": "按交易所结算时间用当时标记价计收",
        "liquidationBasis": "逐仓，维持保证金按风险档位，取标记价不利极值",
        "thinBarPolicy": config.fill_on_thin,
        "simplifications": simplifications,
    }


def _fill_rule(config: BacktestConfig) -> str:
    """The sentence a reader needs to know when a signal became a fill."""
    base = (
        "收盘确认交叉，下一根K线开盘成交"
        if config.strategy_id == "ma_cross"
        else f"策略 {config.strategy_id} 在收盘确认信号，下一根K线开盘成交"
    )
    if config.latency_bars:
        return base.replace("下一根K线开盘", f"再延迟 {config.latency_bars} 根K线后的开盘")
    return base


def thin_session_flags(candles: list[dict], lookback: int = 20) -> list[bool]:
    """Per-bar: did this bar trade a negligible fraction of its recent typical?"""
    notional = [float(c["close"]) * float(c.get("volume") or 0) for c in candles]
    flags: list[bool] = []
    for index, value in enumerate(notional):
        window = sorted(v for v in notional[max(0, index - lookback + 1) : index] if v > 0)
        if len(window) < 5:
            flags.append(False)
            continue
        median = window[len(window) // 2]
        flags.append(bool(median > 0 and value / median < THIN_SESSION_RATIO) or value < THIN_SESSION_MIN_NOTIONAL)
    return flags


def align_higher_timeframe(
    base: list[dict],
    higher: list[dict],
    higher_interval_ms: int,
    *,
    base_key: str = "ts",
) -> list[dict | None]:
    """For each base bar, the newest higher-timeframe bar that has already closed.

    A 4h bar that opens at 08:00 covers [08:00, 12:00) and cannot be known before
    12:00, so the first 1h bar allowed to see it is the one opening at 12:00. The
    rule is exactly `base.ts >= higher.ts + higher_interval_ms`; `None` means no
    higher bar has closed yet. Nothing here looks forward.
    """
    if higher_interval_ms <= 0:
        raise ValueError("高周期长度必须大于 0 毫秒")
    ordered_higher = sorted(higher, key=lambda row: int(row["ts"]))
    visible: list[dict | None] = []
    cursor = 0  # number of higher bars that have closed at or before this base bar
    for row in base:
        ts = int(row[base_key])
        while cursor < len(ordered_higher) and int(ordered_higher[cursor]["ts"]) + higher_interval_ms <= ts:
            cursor += 1
        visible.append(ordered_higher[cursor - 1] if cursor > 0 else None)
    return visible


def _round_to(value: float, step: float | None) -> float:
    if not step or step <= 0:
        return value
    return round(value / step) * step


def _floor_to(value: float, step: float | None) -> float:
    """Round an order quantity down so quantisation never spends extra margin."""
    if not step or step <= 0:
        return value
    return floor((value + step * 1e-12) / step) * step


def _size_fill(
    quantity: float,
    *,
    direction: int,
    bar: dict,
    bar_open: float,
    config: BacktestConfig,
    equity: float,
    fee_rate: float,
    limit_price: float | None,
    impact_bps: Callable[[float, float, dict], float],
    risk_profile: RiskProfile | None,
    leverage_breaches: list[str],
) -> _Fill | None:
    """Size and price one entry leg.

    `partial_fill="ignore"` calls this once per signal with the whole order, which
    is exactly the arithmetic the engine used before the order layer existed; the
    event-driven path calls it once per bar with the remainder. `limit_price` is
    the resting maker price: the fill happens at the limit, pays no spread, and
    charges no impact (it is not taking anything).
    """
    extra = impact_bps(quantity, bar_open, bar) if (quantity > 0 and limit_price is None) else 0.0
    if limit_price is None:
        slipped = (config.slippage_bps + extra) / 10_000
        price = _round_to(bar_open * (1 + slipped if direction == 1 else 1 - slipped), config.tick_size)
    else:
        price = _round_to(limit_price, config.tick_size)
    if not (quantity > 0 and quantity * price >= config.min_order_notional):
        return None
    fee = quantity * price * fee_rate
    margin = quantity * price / config.leverage
    if margin > equity:
        quantity = _floor_to(equity * config.leverage / price, config.qty_step)
        margin = quantity * price / config.leverage
        fee = quantity * price * fee_rate
    if quantity <= 0:
        return None
    # The rung this position sits in decides its maintenance margin and the
    # highest leverage the venue would have allowed.
    notional = quantity * price
    tier = risk_profile.tier_for(notional, leverage=config.leverage) if risk_profile else None
    tier_mmr = tier.maintenance_margin_rate if tier else config.maintenance_margin_rate
    tier_maintenance = maintenance_margin_for(tier, notional, config.maintenance_margin_rate)
    if risk_profile is not None and tier is not None and config.leverage > tier.max_leverage:
        leverage_breaches.append(
            f"{config.leverage:g}x 超过 {tier.risk_limit_value:,.0f} 档位允许的 {tier.max_leverage:g}x"
        )
    return _Fill(
        quantity=quantity,
        price=price,
        fee=fee,
        margin=margin,
        notional=notional,
        impact_extra=extra,
        fee_kind="maker" if limit_price is not None else "taker",
        tier=tier,
        tier_mmr=tier_mmr,
        tier_maintenance=tier_maintenance,
        mm_deduction=tier.mm_deduction if tier else 0.0,
    )


def _limit_reachable(side: str, limit_price: float, bar: dict) -> bool:
    """Would a resting limit at this price have been hit inside this bar?"""
    if side == "buy":
        return float(bar["low"]) <= limit_price
    return float(bar["high"]) >= limit_price


def data_proxies(
    *,
    instrument: dict | None,
    ordered: list[dict],
    config: BacktestConfig,
    marks: list[dict] | None,
) -> list[dict]:
    """Machine-readable `[{field, kind, note}]` for every input that is not the real thing.

    A backtest on a proxy is a legitimate research step; a backtest on a proxy that
    does not say so is a number somebody will trade. Levels and levels of leveraged
    ETF decay are declared here too, because a 3x ETF is not a linear proxy of the
    index it tracks.
    """
    out: list[dict] = []
    meta = instrument or {}

    declared = meta.get("dataProxy") or meta.get("data_proxies")
    if isinstance(declared, dict):
        out.append(
            {
                "field": str(declared.get("field") or "instrument"),
                "kind": str(declared.get("kind") or "declared"),
                "note": str(declared.get("note") or "调用方声明该输入为代理数据"),
            }
        )
    elif isinstance(declared, str) and declared:
        out.append({"field": "instrument", "kind": "declared", "note": declared})

    sources = sorted({str(row.get("source") or "") for row in ordered if row.get("source")})
    non_venue = [item for item in sources if item in NON_VENUE_SOURCES or (item and not item.startswith("venue"))]
    if non_venue:
        out.append(
            {
                "field": "candles",
                "kind": "non_venue_source",
                "note": f"K线来源 {', '.join(non_venue)} 不是交易所原始成交，价格与深度都是代理",
            }
        )

    if config.slippage_model == "participation" and not any(row.get("turnover") is not None for row in ordered):
        out.append(
            {
                "field": "turnover",
                "kind": "derived_close_times_volume",
                "note": "本地K线没有成交额字段，冲击成本按 收盘价×成交量 推算",
            }
        )

    if not marks and (config.include_liquidation or config.include_funding):
        out.append(
            {
                "field": "marks",
                "kind": "bar_close_fallback",
                "note": "没有标记价序列，强平/估值/资金费改用K线收盘价近似",
            }
        )

    if str(meta.get("riskClass") or "") == "leveraged_etf" or str(meta.get("productType") or "") == "etf":
        out.append(
            {
                "field": "instrument",
                "kind": "leveraged_etf_underlying",
                "note": "标的是三倍杠杆ETF（如 SOXL/SOXS），存在每日再平衡的复利衰减，不能当作指数的线性代理",
            }
        )
    return out


def run_backtest(
    candles: list[dict],
    config: BacktestConfig,
    *,
    funding: list[dict] | None = None,
    marks: list[dict] | None = None,
    risk_profile: RiskProfile | None = None,
    instrument: dict | None = None,
    interval: str | None = None,
    signal_events: list[int | None] | None = None,
    position_intents: list["PositionIntent | None"] | None = None,
    max_portfolio_risk_pct: float | None = None,
    enforce_open_risk: bool = True,
    strategy_warnings: list[str] | None = None,
    higher_timeframes: dict[str, list[dict]] | None = None,
    signal_source: Callable[[list[dict], dict, dict[str, list[dict | None]]], list[int | None]] | None = None,
) -> BacktestResult:
    """Run one registered strategy over closed candles, oldest first.

    `higher_timeframes` maps an interval name ("4h", "1d") to its bars. Each base
    bar only ever sees a higher-timeframe bar that has already closed, and a
    `signal_source(ordered, parameters, aligned)` callback receives those aligned
    views so a multi-timeframe strategy cannot read the future even by accident.

    `position_intents` is the structured alternative to `signal_events`: one intent
    per bar, aligned with the bars and filled with the same latency, but able to say
    "add to a third", "take half off" or "the pivot broke, drop the resting order".
    The two are mutually exclusive; when intents are supplied the event path is not
    evaluated at all, so a run cannot accidentally act on both. `max_portfolio_risk_pct`
    caps the risk carried to the structural stop (stop distance times size) as a share
    of equity. It binds twice: an intent that would take the book over the budget on
    entry is refused - with a recorded reason - and a position whose risk has grown past
    the budget as price moved is reduced back inside it (or closed outright when the
    excess is too small to trade), counted in `riskReductions` / `riskExits`. The second
    check is marked on each bar's close and executes at the next bar's open, the same
    latency the intents follow, so `enforce_open_risk=False` restores the entry-only
    behaviour - useful for seeing what the constraint cost. Neither argument changes
    anything when omitted, which is what keeps every earlier result reproducible.
    """
    if config.strategy_id == "ma_cross" and (config.fast_period < 2 or config.slow_period <= config.fast_period):
        raise ValueError("均线周期无效：慢线必须大于快线，且快线至少为 2")
    if not 0 < config.allocation_pct <= 100:
        raise ValueError("每次仓位比例必须在 (0, 100] 之间")
    if config.initial_capital <= 0:
        raise ValueError("初始资金必须大于 0")
    if not 1.0 <= config.leverage <= MAX_LEVERAGE:
        raise ValueError(f"杠杆必须在 1 到 {MAX_LEVERAGE:g} 之间")
    if risk_profile is not None and not risk_profile.tiers:
        risk_profile = None
    if config.direction not in {"both", "long"}:
        raise ValueError("direction 只能是 both 或 long")
    if config.fill_on_thin not in {"skip", "allow"}:
        raise ValueError("fill_on_thin 只能是 skip 或 allow")
    if config.slippage_model not in {"fixed", "participation"}:
        raise ValueError("slippage_model 只能是 fixed 或 participation")
    if config.impact_coefficient < 0:
        raise ValueError("impact_coefficient 不能为负")
    if not 0 < config.max_participation <= 1:
        raise ValueError("max_participation 必须在 (0, 1] 之间")
    if config.partial_fill not in {"ignore", "cap"}:
        raise ValueError("partial_fill 只能是 ignore 或 cap")
    if not 0 <= int(config.latency_bars) <= 10:
        raise ValueError("latency_bars 必须在 0 到 10 之间")
    if config.maker_fill not in {"never", "passive_only"}:
        raise ValueError("maker_fill 只能是 never 或 passive_only")
    if config.maker_fee_bps is not None and config.maker_fee_bps < 0:
        raise ValueError("maker_fee_bps 不能为负")
    if not 1 <= int(config.maker_order_bars) <= 100:
        raise ValueError("maker_order_bars 必须在 1 到 100 之间")
    if config.liquidation_fee_bps < 0:
        raise ValueError("liquidation_fee_bps 不能为负")
    if config.bar_path not in {"conservative", "optimistic"}:
        raise ValueError("bar_path 只能是 conservative 或 optimistic")
    if position_intents is not None and signal_events is not None:
        raise ValueError("仓位意图与事件信号不能同时传入：一次运行只有一条信号路径")
    if max_portfolio_risk_pct is not None and not 0 < float(max_portfolio_risk_pct) <= 100:
        raise ValueError("max_portfolio_risk_pct 必须在 (0, 100] 之间，或用 None 关闭")
    for name, value in (
        ("stop_loss_pct", config.stop_loss_pct),
        ("take_profit_pct", config.take_profit_pct),
        ("trailing_stop_pct", config.trailing_stop_pct),
    ):
        if value is not None and not 0 < float(value) < 100:
            raise ValueError(f"{name} 必须在 (0, 100) 之间，或用 None 关闭")
    if config.strategy_id == "ma_cross":
        required_bars = int(config.strategy_params.get("slowPeriod", config.slow_period)) + 3
    elif config.strategy_id == "channel_breakout":
        required_bars = int(config.strategy_params.get("lookback", 20)) + 3
    elif config.strategy_id == "rsi_reversal":
        required_bars = int(config.strategy_params.get("period", 14)) + 3
    else:
        required_bars = 3
    if len(candles) < required_bars:
        raise ValueError(f"当前只有 {len(candles)} 根K线，策略至少需要 {required_bars} 根")

    ordered = sorted(candles, key=lambda row: int(row["ts"]))
    aligned_views: dict[str, list[dict | None]] = {}
    for name, rows in (higher_timeframes or {}).items():
        span = INTERVAL_MS.get(name)
        if not span:
            raise ValueError(f"未知的高周期 {name}，可用：{', '.join(sorted(INTERVAL_MS))}")
        aligned_views[name] = align_higher_timeframe(ordered, rows, int(span))
    if signal_events is None:
        parameters = dict(config.strategy_params)
        if config.strategy_id == "ma_cross":
            parameters.setdefault("fastPeriod", config.fast_period)
            parameters.setdefault("slowPeriod", config.slow_period)
        if signal_source is not None:
            signal_events = signal_source(ordered, parameters, aligned_views)
        else:
            from ..strategy.registry import generate_events

            # The engine stays plugin-free, but not strategy-free: the same entry the
            # studies use, so a CPA backtest and a CPA validation cannot diverge.
            signal_events = generate_events(
                ordered, config.strategy_id, parameters,
                asset_class=str(getattr(config, "asset_class", "") or "stock"),
                interval=str(getattr(config, "interval", "") or ""),
            )
    if len(signal_events) != len(ordered):
        raise ValueError("策略信号数量与K线数量不一致")
    position_intents = _validated_intents(position_intents, len(ordered))
    thin = thin_session_flags(ordered)
    unfilled_notional = 0.0
    unfilled_orders = 0
    unfilled_quantity = 0.0
    max_fill_delay = 0
    # Intent-mode bookkeeping. The structural stop is a property of the open position,
    # so it lives beside it rather than inside a config field: two positions opened by
    # two intents can name two different invalidation levels over a run.
    position_stop: float | None = None
    intent_records: list[dict] = []
    rejected_intents: list[dict] = []
    max_risk_seen = 0.0
    # A close that leaves the book over budget arms the next bar; the direction is kept
    # so a stale arm can never be spent on a position opened since.
    risk_alert_direction: int | None = None
    risk_alerts = 0
    risk_reductions = 0
    risk_exits = 0
    funding_rows = sorted(funding or [], key=lambda row: int(row["ts"]))
    mark_by_ts = {int(row["ts"]): row for row in (marks or []) if row.get("ts") is not None}
    mark_series = sorted(mark_by_ts)

    def mark_at(ts: int) -> dict | None:
        """The mark bar covering this timestamp, or None when there is no series.

        A funding settlement can land between two mark bars; the newest bar at or
        before it is the mark the venue had at that moment.
        """
        if not mark_series:
            return None
        import bisect

        position = bisect.bisect_right(mark_series, ts) - 1
        return mark_by_ts[mark_series[position]] if position >= 0 else None

    def impact_bps(quantity: float, price: float, bar: dict) -> float:
        """Extra slippage from taking a share of the bar's traded notional."""
        if config.slippage_model == "fixed":
            return 0.0
        traded = abs(float(bar.get("volume") or 0.0)) * float(bar.get("close") or price)
        if traded <= 0:
            # No traded notional at all: the bar cannot absorb the order, and the
            # caller already sees the thin-session flag. Charge the cap.
            return config.slippage_bps + config.impact_coefficient * 10_000 * config.max_participation
        participation = min(config.max_participation, (quantity * price) / traded)
        return config.impact_coefficient * participation * 10_000

    fee_rate = config.fee_bps / 10_000
    slip = config.slippage_bps / 10_000
    funding_cursor = 0
    impact_charges = 0
    leverage_breaches: list[str] = []
    equity = config.initial_capital
    total_fees = 0.0
    total_liquidation_fees = 0.0
    total_funding = 0.0
    position: _Position | None = None
    pending_entry: _PendingEntry | None = None
    orders: list[Order] = []
    trades: list[BacktestTrade] = []
    equity_curve: list[dict] = []
    funding_settlements: list[dict] = []
    liquidations: list[dict] = []
    stop_exits = 0
    warnings: list[str] = list(strategy_warnings or [])

    def risk_budget_of() -> float | None:
        """The risk budget at this moment, or None when no budget was set.

        The budget is a share of *live* equity, and equity moves as fees and PnL are
        booked, so it is derived from wherever `equity` stands right now. Entry risk and
        carried risk both go through here: one definition is what stops the two checks
        from drifting apart.
        """
        if not max_portfolio_risk_pct:
            return None
        return equity * (float(max_portfolio_risk_pct) / 100)

    if config.strategy_id == "ma_cross":
        warmup = config.slow_period + 1
    elif config.strategy_id == "channel_breakout":
        warmup = int(config.strategy_params.get("lookback", 20)) + 1
    elif config.strategy_id == "rsi_reversal":
        warmup = int(config.strategy_params.get("period", 14)) + 1
    else:
        warmup = 2

    def new_order(
        *,
        index: int,
        ts: int,
        side: str,
        purpose: str,
        order_type: str,
        quantity: float,
        reason: str | None = None,
        limit_price: float | None = None,
    ) -> Order:
        order = Order(
            order_id=len(orders) + 1,
            created_index=index,
            created_time=ts,
            side=side,
            purpose=purpose,
            order_type=order_type,
            quantity=quantity,
            reason=reason,
            limit_price=limit_price,
        )
        orders.append(order)
        return order

    def record_fill(order: Order, fill: _Fill, *, index: int, ts: int) -> None:
        nonlocal max_fill_delay
        order.fills.append(
            {
                "index": index,
                "time": ts,
                "quantity": round(fill.quantity, 8),
                "price": round(fill.price, 8),
                "fee": round(fill.fee, 8),
                "feeKind": fill.fee_kind,
                "slippageBps": round(fill.impact_extra, 6),
            }
        )
        order.filled_quantity += fill.quantity
        order.avg_fill_price = (
            order.avg_fill_price * (order.filled_quantity - fill.quantity) + fill.notional
        ) / order.filled_quantity
        order.filled_index = index
        order.filled_time = ts
        order.bars_to_fill = index - order.created_index
        order.fee_kind = fill.fee_kind
        max_fill_delay = max(max_fill_delay, order.bars_to_fill)
        if order.filled_quantity >= order.quantity - 1e-12:
            order.status = "filled"
            order.unfilled_quantity = 0.0
        else:
            order.status = "partially_filled"
            order.unfilled_quantity = order.quantity - order.filled_quantity

    def apply_entry_fill(fill: _Fill, *, direction: int, index: int, ts: int, thin_entry: bool) -> None:
        """Book one entry leg: charge its fee, then create or extend the position."""
        nonlocal equity, total_fees, impact_charges, position
        equity -= fill.fee
        total_fees += fill.fee
        if fill.impact_extra:
            impact_charges += 1
        if position is None:
            position = _Position(
                direction=direction,
                entry_time=ts,
                entry_price=fill.price,
                quantity=fill.quantity,
                notional=fill.notional,
                entry_fee=fill.fee,
                margin=fill.margin,
                liq_price=liquidation_price(
                    direction,
                    fill.price,
                    quantity=fill.quantity,
                    margin=fill.margin,
                    mmr=fill.tier_mmr,
                    mm_deduction=fill.mm_deduction,
                ),
                entry_index=index,
                thin_entry=thin_entry,
                risk_tier_id=fill.tier.tier_id if fill.tier else None,
                maintenance_margin_rate=fill.tier_mmr,
                maintenance_margin=fill.tier_maintenance,
                fill_count=1,
                last_fill_index=index,
                best_price=fill.price,
                mm_deduction=fill.mm_deduction,
            )
            return
        # A later leg: the position now sits at the size-weighted entry price, and
        # its maintenance requirement follows the final rung, not the first.
        position.fill_count += 1
        position.last_fill_index = index
        position.quantity += fill.quantity
        position.notional += fill.notional
        position.entry_fee += fill.fee
        position.margin += fill.margin
        position.entry_price = position.notional / position.quantity
        if fill.tier is not None:
            position.risk_tier_id = fill.tier.tier_id
        position.maintenance_margin_rate = fill.tier_mmr
        position.maintenance_margin = maintenance_margin_for(fill.tier, position.notional, config.maintenance_margin_rate)
        position.mm_deduction = fill.mm_deduction
        position.liq_price = liquidation_price(
            position.direction,
            position.entry_price,
            quantity=position.quantity,
            margin=position.margin,
            mmr=fill.tier_mmr,
            mm_deduction=fill.mm_deduction,
        )

    def cancel_order(order: Order, *, remaining: float, price: float, note: str) -> None:
        """Give up on the rest of an order and report what it never filled."""
        nonlocal unfilled_orders, unfilled_notional, unfilled_quantity
        order.status = "cancelled"
        order.unfilled_quantity = remaining
        order.note = note
        if remaining > 0:
            unfilled_orders += 1
            unfilled_notional += remaining * price
            unfilled_quantity += remaining

    def entry_fill_for(
        order: Order,
        *,
        direction: int,
        bar: dict,
        index: int,
        quantity: float,
        limit_price: float | None,
    ) -> tuple[_Fill | None, str]:
        """Try to fill `quantity` of a resting/alive entry order on this bar.

        Returns (fill, outcome) where outcome is "filled", "resting" (the bar could
        not have taken it: the limit was never traded to, or there was no volume)
        or "refused" (the size itself is not orderable: below the minimum notional,
        or no margin left).
        """
        if quantity <= 0:
            return None, "refused"
        if limit_price is not None and not _limit_reachable(order.side, limit_price, bar):
            return None, "resting"
        capped = quantity
        if config.partial_fill == "cap":
            capacity = config.max_participation * float(bar.get("volume") or 0.0)
            if capacity <= 0:
                return None, "resting"
            if capped > capacity:
                capped = _floor_to(capacity, config.qty_step)
        fill = _size_fill(
            capped,
            direction=direction,
            bar=bar,
            bar_open=float(bar["open"]),
            config=config,
            equity=equity,
            fee_rate=_fee_rate(config, "maker" if limit_price is not None else "taker"),
            limit_price=limit_price,
            impact_bps=impact_bps,
            risk_profile=risk_profile,
            leverage_breaches=leverage_breaches,
        )
        return fill, ("filled" if fill is not None else "refused")

    for index in range(max(2, warmup), len(ordered)):
        bar = ordered[index]
        bar_open = float(bar["open"])
        bar_ts = int(bar["ts"])

        # ---- funding settles at the venue's own timestamp, on the mark price
        if position and config.include_funding:
            while funding_cursor < len(funding_rows) and int(funding_rows[funding_cursor]["ts"]) <= bar_ts:
                row = funding_rows[funding_cursor]
                settle_ts = int(row["ts"])
                if settle_ts > position.entry_time:
                    mark_row = mark_at(settle_ts)
                    settle_price = float(mark_row["close"]) if mark_row else float(bar["close"])
                    cost = position.quantity * settle_price * float(row["rate"])
                    if position.direction == -1:
                        cost = -cost
                    equity -= cost
                    position.funding_paid += cost
                    total_funding += cost
                    funding_settlements.append(
                        {"ts": settle_ts, "rate": float(row["rate"]), "price": settle_price, "cost": round(cost, 8)}
                    )
                funding_cursor += 1

        # The signal is read from a bar that closed at least `latency_bars + 1` bars
        # ago: a fill at this bar's open may only depend on information already
        # known, and the previous closed bar is the latest volume observation too.
        signal_index = index - 1 - config.latency_bars
        if signal_index < 0:
            event = 0
        else:
            event = signal_events[signal_index]
        crossed_up = event == 1
        crossed_down = event == -1
        exit_to_flat = event == 0
        entry_thin = thin[signal_index] if signal_index >= 0 else False
        tradable = config.fill_on_thin == "allow" or not entry_thin

        if position_intents is not None:
            intent = position_intents[signal_index] if signal_index >= 0 else None
            risk_budget = risk_budget_of()
            exiting_this_bar = intent is not None and intent.action == "exit"

            # ---- carried risk: the budget caps what the book already risks, not only
            # what the next order would add. The float is marked on the previous bar's
            # close - known before this bar opened - and corrected here at this bar's
            # open, the same latency the intents themselves follow. Acting on the close
            # it had just observed would fill at a price the run could not have traded.
            if risk_alert_direction is not None:
                alert_stop = float(position_stop) if position_stop is not None else None
                beyond_stop = (
                    position is not None
                    and alert_stop is not None
                    and (
                        (position.direction == 1 and bar_open <= alert_stop)
                        or (position.direction == -1 and bar_open >= alert_stop)
                    )
                )
                if (
                    position is None
                    or alert_stop is None
                    or position.direction != risk_alert_direction
                    or risk_budget is None
                    # 本根意图就是离场：同一个开盘价上先减一次只会多付一笔手续费。
                    or exiting_this_bar
                    # 开盘已越过结构失效价：本根由结构止损处理，不重复下单。
                    or beyond_stop
                ):
                    risk_alert_direction = None
                else:
                    carried_direction = position.direction
                    distance = abs(bar_open - alert_stop)
                    keep = (
                        _floor_to(risk_budget / distance, config.qty_step)
                        if distance > 0
                        else 0.0
                    )
                    excess = position.quantity - keep
                    exit_price = bar_open * (1 - slip if carried_direction == 1 else 1 + slip)
                    if keep >= position.quantity:
                        # 这一根的开盘已把风险带回预算内：警报作废，不下单。
                        risk_alert_direction = None
                    else:
                        risk_alert_direction = None
                        risk_at_open = position.quantity * distance
                        # 超出部分小到无法单独下单时整笔平掉：把一笔不合规的风险留在
                        # 那里，比多付一笔手续费更糟。
                        whole = keep <= 1e-12 or (
                            excess * exit_price < config.min_order_notional
                        )
                        quantity = position.quantity if whole else excess
                        risk_order = new_order(
                            index=index, ts=bar_ts,
                            side="sell" if carried_direction == 1 else "buy",
                            purpose="exit", order_type="market",
                            quantity=quantity, reason="risk_budget",
                        )
                        if whole:
                            equity, trade = _close(
                                equity, position, bar, exit_price, index, config,
                                "risk_budget", order=risk_order,
                            )
                            trades.append(trade)
                            total_fees += trade.fees - position.entry_fee
                            risk_exits += 1
                            status = "risk_exited"
                            note = (
                                f"浮动风险 {risk_at_open:,.2f} > 预算 {risk_budget:,.2f}，"
                                f"超出部分 {excess:,.8g} 不足以单独下单，整笔平仓"
                            )
                            position = None
                            position_stop = None
                            if pending_entry is not None:
                                cancel_order(
                                    pending_entry.order, remaining=pending_entry.remaining,
                                    price=bar_open, note="风控平仓后，剩余开仓挂单撤销",
                                )
                                pending_entry = None
                        else:
                            equity, trade = _close_partial(
                                equity, position, bar, exit_price, index, config,
                                "risk_budget", quantity=excess, order=risk_order,
                            )
                            trades.append(trade)
                            # 开仓费在开仓时已计入 total_fees，这里只新增这一腿的离场费。
                            total_fees += trade.exit_fee
                            risk_reductions += 1
                            status = "risk_reduced"
                            note = (
                                f"浮动风险 {risk_at_open:,.2f} > 预算 {risk_budget:,.2f}，"
                                f"减仓 {excess:,.8g} 回到预算内"
                            )
                        intent_records.append(
                            {
                                "action": "exit" if whole else "reduce",
                                "direction": "long" if carried_direction == 1 else "short",
                                "stage": "risk_budget",
                                "targetExposurePct": None,
                                "stopPrice": alert_stop,
                                "reason": note,
                                "index": index, "time": bar_ts, "status": status,
                                "orderId": risk_order.order_id,
                                "quantity": round(quantity, 8),
                                "price": round(exit_price, 8),
                                "fee": round(trade.exit_fee, 8),
                                "positionQuantity": round(
                                    position.quantity if position is not None else 0.0, 8
                                ),
                                "riskBudget": round(risk_budget, 6),
                            }
                        )
            if intent is not None and intent.action != "hold":
                # 风控减仓可能刚记过盈亏与手续费，预算要按当下的权益重算。
                risk_budget = risk_budget_of()
                if intent.action == "cancel":
                    if pending_entry is not None:
                        cancel_order(
                            pending_entry.order,
                            remaining=pending_entry.remaining,
                            price=bar_open,
                            note=intent.reason or "枢轴失败，撤销剩余开仓挂单",
                        )
                        pending_entry = None
                        intent_records.append(
                            {**intent.as_dict(), "index": index, "time": bar_ts, "status": "cancelled"}
                        )
                    else:
                        intent_records.append(
                            {**intent.as_dict(), "index": index, "time": bar_ts,
                             "status": "ignored", "note": "没有可撤销的挂单"}
                        )
                elif intent.action in ("open", "increase"):
                    direction = 1 if intent.direction == "long" else -1
                    target_pct = float(intent.target_exposure_pct or 0)
                    target_notional = equity * (target_pct / 100) * config.leverage
                    if not tradable:
                        rejected_intents.append(
                            {**intent.as_dict(), "index": index, "time": bar_ts,
                             "status": "rejected", "note": "休市空 bar，未按意图建仓"}
                        )
                    elif position is not None and position.direction != direction:
                        rejected_intents.append(
                            {**intent.as_dict(), "index": index, "time": bar_ts,
                             "status": "rejected",
                             "note": "持仓方向与意图相反：先用 exit 平掉，再 open 反向"}
                        )
                    elif pending_entry is not None and pending_entry.direction != direction:
                        rejected_intents.append(
                            {**intent.as_dict(), "index": index, "time": bar_ts,
                             "status": "rejected", "note": "已有反向挂单未成交，先 cancel"}
                        )
                    else:
                        current_qty = position.quantity if position is not None else 0.0
                        current_notional = abs(position.notional) if position is not None else 0.0
                        delta_notional = target_notional - current_notional
                        if delta_notional <= 0:
                            intent_records.append(
                                {**intent.as_dict(), "index": index, "time": bar_ts,
                                 "status": "ignored",
                                 "note": f"当前敞口 {current_notional:,.2f} 已达目标 {target_notional:,.2f}"}
                            )
                        else:
                            quantity = _floor_to(delta_notional / bar_open, config.qty_step)
                            stop = float(intent.stop_price) if intent.stop_price else position_stop
                            projected_qty = current_qty + quantity
                            projected_risk = abs(bar_open - stop) * projected_qty if stop else None
                            if (
                                risk_budget is not None
                                and projected_risk is not None
                                and projected_risk > risk_budget
                            ):
                                rejected_intents.append(
                                    {**intent.as_dict(), "index": index, "time": bar_ts,
                                     "status": "rejected",
                                     "note": (
                                         f"风险预算超限：持仓风险 {projected_risk:,.2f} > "
                                         f"权益的 {float(max_portfolio_risk_pct):g}%（{risk_budget:,.2f}）"
                                     )}
                                )
                            else:
                                order = new_order(
                                    index=index, ts=bar_ts,
                                    side="buy" if direction == 1 else "sell",
                                    purpose="entry", order_type="market",
                                    quantity=quantity, reason=intent.stage or "intent",
                                )
                                fill, outcome = entry_fill_for(
                                    order, direction=direction, bar=bar, index=index,
                                    quantity=quantity, limit_price=None,
                                )
                                if fill is None:
                                    order.status = "rejected"
                                    order.rejected_reason = (
                                        "按步长取整后的下单名义额低于最小下单量，或可用保证金不足"
                                        if outcome == "refused"
                                        else "当根K线没有可成交量，挂单跨K线等待"
                                    )
                                    intent_records.append(
                                        {**intent.as_dict(), "index": index, "time": bar_ts,
                                         "status": "unfilled", "note": order.rejected_reason}
                                    )
                                else:
                                    apply_entry_fill(
                                        fill, direction=direction, index=index,
                                        ts=bar_ts, thin_entry=entry_thin,
                                    )
                                    record_fill(order, fill, index=index, ts=bar_ts)
                                    if intent.stop_price:
                                        position_stop = float(intent.stop_price)
                                    intent_records.append(
                                        {
                                            **intent.as_dict(), "index": index, "time": bar_ts,
                                            "status": "filled",
                                            "orderId": order.order_id,
                                            "quantity": round(fill.quantity, 8),
                                            "price": round(fill.price, 8),
                                            "fee": round(fill.fee, 8),
                                            "positionQuantity": round(position.quantity, 8),
                                            "positionEntryPrice": round(position.entry_price, 8),
                                            "stopPrice": position_stop,
                                        }
                                    )
                                remaining = order.quantity - order.filled_quantity
                                if order.status in {"accepted", "partially_filled"} and remaining > 0:
                                    order.unfilled_quantity = remaining
                                    pending_entry = _PendingEntry(
                                        order=order, direction=direction, remaining=remaining,
                                        thin_entry=entry_thin, attempts_left=None,
                                    )
                elif intent.action == "reduce":
                    if position is None:
                        intent_records.append(
                            {**intent.as_dict(), "index": index, "time": bar_ts,
                             "status": "ignored", "note": "没有仓位可减"}
                        )
                    else:
                        target_pct = float(intent.target_exposure_pct or 0)
                        target_notional = equity * (target_pct / 100) * config.leverage
                        keep = min(
                            position.quantity,
                            _floor_to(target_notional / bar_open, config.qty_step),
                        )
                        quantity = position.quantity - keep
                        if quantity <= 0:
                            intent_records.append(
                                {**intent.as_dict(), "index": index, "time": bar_ts,
                                 "status": "ignored", "note": "当前仓位已不高于目标敞口"}
                            )
                        else:
                            reduce_order = new_order(
                                index=index, ts=bar_ts,
                                side="sell" if position.direction == 1 else "buy",
                                purpose="exit", order_type="market",
                                quantity=quantity, reason=f"reduce:{intent.stage or 'intent'}",
                            )
                            exit_price = bar_open * (1 - slip if position.direction == 1 else 1 + slip)
                            equity, trade = _close_partial(
                                equity, position, bar, exit_price, index, config, "signal",
                                quantity=quantity, order=reduce_order,
                            )
                            trades.append(trade)
                            # The entry fee was charged at open and already sits in
                            # total_fees; only this leg's exit fee is new.
                            total_fees += trade.exit_fee
                            intent_records.append(
                                {
                                    **intent.as_dict(), "index": index, "time": bar_ts,
                                    "status": "reduced",
                                    "orderId": reduce_order.order_id,
                                    "quantity": round(quantity, 8),
                                    "price": round(exit_price, 8),
                                    "fee": round(trade.exit_fee, 8),
                                    "positionQuantity": round(position.quantity, 8),
                                }
                            )
                            if position.quantity <= 1e-12:
                                position = None
                                position_stop = None
                elif intent.action == "exit":
                    if position is None:
                        intent_records.append(
                            {**intent.as_dict(), "index": index, "time": bar_ts,
                             "status": "ignored", "note": "没有仓位可平"}
                        )
                    else:
                        exit_order = new_order(
                            index=index, ts=bar_ts,
                            side="sell" if position.direction == 1 else "buy",
                            purpose="exit", order_type="market",
                            quantity=position.quantity, reason=intent.stage or "intent",
                        )
                        exit_price = bar_open * (1 - slip if position.direction == 1 else 1 + slip)
                        equity, trade = _close(
                            equity, position, bar, exit_price, index, config, "signal",
                            order=exit_order,
                        )
                        trades.append(trade)
                        total_fees += trade.fees - position.entry_fee
                        intent_records.append(
                            {
                                **intent.as_dict(), "index": index, "time": bar_ts,
                                "status": "exited",
                                "orderId": exit_order.order_id,
                                "quantity": round(trade.quantity, 8),
                                "price": round(exit_price, 8),
                                "fee": round(trade.exit_fee, 8),
                            }
                        )
                        position = None
                        position_stop = None
                        if pending_entry is not None:
                            cancel_order(
                                pending_entry.order,
                                remaining=pending_entry.remaining,
                                price=bar_open,
                                note="离场意图后，剩余开仓挂单撤销",
                            )
                            pending_entry = None

            # ---- structural stop: the level the intent itself named.
            # Checked before the percentage protection because it is the level the
            # decision was made against, and because an adverse level is exactly what
            # the conservative bar path assumes happened first.
            if position is not None and position_stop is not None:
                adverse = float(bar["low"]) if position.direction == 1 else float(bar["high"])
                if (position.direction == 1 and adverse <= position_stop) or (
                    position.direction == -1 and adverse >= position_stop
                ):
                    direction_of_stop = position.direction
                    level = stop_fill_price(direction_of_stop, float(position_stop), bar_open)
                    stop_order = new_order(
                        index=index, ts=bar_ts,
                        side="sell" if direction_of_stop == 1 else "buy",
                        purpose="exit", order_type="stop",
                        quantity=position.quantity, reason="stop_loss",
                    )
                    stop_price = level * (1 - slip if direction_of_stop == 1 else 1 + slip)
                    equity, trade = _close(
                        equity, position, bar, stop_price, index, config, "stop_loss",
                        order=stop_order,
                    )
                    trades.append(trade)
                    total_fees += trade.fees - position.entry_fee
                    stop_exits += 1
                    intent_records.append(
                        {
                            "action": "stop",
                            "direction": "long" if direction_of_stop == 1 else "short",
                            "stage": "structural_stop",
                            "targetExposurePct": None,
                            "stopPrice": position_stop,
                            "reason": "结构失效价被触发",
                            "index": index, "time": bar_ts, "status": "stopped",
                            "orderId": stop_order.order_id,
                            "price": round(stop_price, 8),
                            "fee": round(trade.exit_fee, 8),
                        }
                    )
                    position = None
                    position_stop = None
                    if pending_entry is not None:
                        cancel_order(
                            pending_entry.order, remaining=pending_entry.remaining,
                            price=bar_open, note="结构止损后，剩余开仓挂单撤销",
                        )
                        pending_entry = None

            # What risk the book was carrying at this bar's close, measured after this
            # bar's orders and against the structural stop at the *mark* rather than at
            # entry: a trade that has run in favour sits further from its stop, so it
            # carries more risk than it did when it was opened. Exceeding the budget here
            # arms the next bar, which is the earliest price that could have been traded
            # after seeing this close - `riskAlerts` counts those bars.
            if position is not None and position_stop is not None:
                carried = abs(float(bar["close"]) - float(position_stop)) * position.quantity
                max_risk_seen = max(max_risk_seen, carried)
                budget_now = risk_budget_of()
                if (
                    enforce_open_risk
                    and budget_now is not None
                    and carried > budget_now
                ):
                    risk_alert_direction = position.direction
                    risk_alerts += 1
        else:
            # ---- signal exit
            if position and (exit_to_flat or (position.direction == 1 and crossed_down) or (position.direction == -1 and crossed_up)):
                exit_order = new_order(
                    index=index,
                    ts=bar_ts,
                    side="sell" if position.direction == 1 else "buy",
                    purpose="exit",
                    order_type="market",
                    quantity=position.quantity,
                    reason="signal",
                )
                exit_price = bar_open * (1 - slip if position.direction == 1 else 1 + slip)
                equity, trade = _close(equity, position, bar, exit_price, index, config, "signal", order=exit_order)
                trades.append(trade)
                # The entry fee was already deducted on open; only the exit leg is new.
                total_fees += trade.fees - position.entry_fee
                position = None
                # The signal that closed the position also cancels what is left of its
                # entry order: the intent it was placed for no longer exists.
                if pending_entry is not None:
                    cancel_order(
                        pending_entry.order,
                        remaining=pending_entry.remaining,
                        price=bar_open,
                        note="信号已反向/离场，剩余开仓挂单撤销",
                    )
                    pending_entry = None

            # ---- resting entry: carry a partial fill, or wait for a maker limit
            if pending_entry is not None:
                order = pending_entry.order
                intent_alive = (
                    (crossed_up and pending_entry.direction == 1)
                    or (crossed_down and config.direction == "both" and pending_entry.direction == -1)
                ) and not exit_to_flat
                if not intent_alive:
                    cancel_order(order, remaining=pending_entry.remaining, price=bar_open, note="信号不再支持该方向")
                    pending_entry = None
                elif pending_entry.attempts_left is not None and pending_entry.attempts_left <= 0:
                    cancel_order(
                        order,
                        remaining=pending_entry.remaining,
                        price=bar_open,
                        note=f"限价挂单 {config.maker_order_bars} 根内未被触及",
                    )
                    pending_entry = None
                elif index > order.created_index:
                    remaining = pending_entry.remaining
                    fill, _outcome = entry_fill_for(
                        order,
                        direction=pending_entry.direction,
                        bar=bar,
                        index=index,
                        quantity=remaining,
                        limit_price=order.limit_price,
                    )
                    if fill is not None:
                        apply_entry_fill(
                            fill,
                            direction=pending_entry.direction,
                            index=index,
                            ts=bar_ts,
                            thin_entry=pending_entry.thin_entry,
                        )
                        record_fill(order, fill, index=index, ts=bar_ts)
                    pending_entry.remaining = order.unfilled_quantity
                    if pending_entry.attempts_left is not None:
                        pending_entry.attempts_left -= 1
                    if pending_entry.remaining <= 0:
                        pending_entry = None

            # ---- entry
            if (
                position is None
                and pending_entry is None
                and (crossed_up or (crossed_down and config.direction == "both"))
                and tradable
            ):
                direction = 1 if crossed_up else -1
                wanted = equity * (config.allocation_pct / 100) * config.leverage
                order_side = "buy" if direction == 1 else "sell"
                if config.partial_fill == "cap" or config.maker_fill == "passive_only":
                    # ---- event-driven entry: an order that can live across bars
                    limit_price = None
                    if config.maker_fill == "passive_only":
                        # Rest at the signal bar's close: the last price that was known
                        # when the decision was made.
                        limit_price = _round_to(float(ordered[signal_index]["close"]), config.tick_size)
                    target = _floor_to(wanted / bar_open, config.qty_step)
                    order = new_order(
                        index=index,
                        ts=bar_ts,
                        side=order_side,
                        purpose="entry",
                        order_type="limit" if limit_price is not None else "market",
                        quantity=target,
                        reason="signal",
                        limit_price=limit_price,
                    )
                    fill, outcome = entry_fill_for(
                        order, direction=direction, bar=bar, index=index, quantity=target, limit_price=limit_price
                    )
                    if fill is None:
                        if outcome == "refused":
                            order.status = "rejected"
                            order.rejected_reason = (
                                "按步长取整后的下单名义额低于最小下单量，或可用保证金不足"
                            )
                        else:
                            order.status = "accepted"
                            order.note = (
                                "限价挂单未在信号K线内被触及"
                                if limit_price is not None
                                else "当根K线没有可成交量，挂单跨K线等待"
                            )
                    else:
                        apply_entry_fill(fill, direction=direction, index=index, ts=bar_ts, thin_entry=entry_thin)
                        record_fill(order, fill, index=index, ts=bar_ts)
                    remaining = order.quantity - order.filled_quantity
                    if order.status in {"accepted", "partially_filled"} and remaining > 0:
                        order.unfilled_quantity = remaining
                        pending_entry = _PendingEntry(
                            order=order,
                            direction=direction,
                            remaining=remaining,
                            thin_entry=entry_thin,
                            # The order is already alive on its creation bar, so a
                            # maker limit with `maker_order_bars=1` gets exactly that
                            # one bar and is cancelled on the next.
                            attempts_left=config.maker_order_bars - 1 if limit_price is not None else None,
                        )
                else:
                    # ---- historical single-bar entry, kept verbatim for comparability
                    quantity = _floor_to(wanted / bar_open, config.qty_step)
                    order = new_order(
                        index=index,
                        ts=bar_ts,
                        side=order_side,
                        purpose="entry",
                        order_type="market",
                        quantity=quantity,
                        reason="signal",
                    )
                    fill = _size_fill(
                        quantity,
                        direction=direction,
                        bar=bar,
                        bar_open=bar_open,
                        config=config,
                        equity=equity,
                        fee_rate=fee_rate,
                        limit_price=None,
                        impact_bps=impact_bps,
                        risk_profile=risk_profile,
                        leverage_breaches=leverage_breaches,
                    )
                    if fill is None:
                        order.status = "rejected"
                        order.rejected_reason = "下单名义额低于最小下单量，或可用保证金不足"
                    else:
                        apply_entry_fill(fill, direction=direction, index=index, ts=bar_ts, thin_entry=entry_thin)
                        record_fill(order, fill, index=index, ts=bar_ts)

        # ---- protection: stop loss / take profit / trailing stop, inside the bar
        if position is not None and _protection_active(config):
            protection_mark = mark_at(bar_ts)
            high = float(protection_mark["high"]) if protection_mark else float(bar["high"])
            low = float(protection_mark["low"]) if protection_mark else float(bar["low"])
            hit = _intrabar_protection(
                position,
                bar_open=bar_open,
                high=high,
                low=low,
                config=config,
                liquidation_on=bool(config.include_liquidation and position.liq_price),
            )
            if hit is not None:
                reason, level = hit
                exit_order = new_order(
                    index=index,
                    ts=bar_ts,
                    side="sell" if position.direction == 1 else "buy",
                    purpose="exit",
                    order_type="stop" if reason in {"stop_loss", "trailing_stop"} else "take_profit",
                    quantity=position.quantity,
                    reason=reason,
                )
                exit_price = level * (1 - slip if position.direction == 1 else 1 + slip)
                equity, trade = _close(equity, position, bar, exit_price, index, config, reason, order=exit_order)
                trades.append(trade)
                total_fees += trade.fees - position.entry_fee
                stop_exits += 1
                position = None
                if pending_entry is not None:
                    cancel_order(
                        pending_entry.order,
                        remaining=pending_entry.remaining,
                        price=bar_open,
                        note="保护性退出后，剩余开仓挂单撤销",
                    )
                    pending_entry = None
            else:
                # Only now may the bar's favourable extreme move the trailing level:
                # a trail that used the same bar it triggers in would be reading the
                # future of its own decision.
                if position.direction == 1:
                    position.best_price = max(position.best_price or position.entry_price, high)
                else:
                    position.best_price = min(position.best_price or position.entry_price, low)

        # The bar's high/low occur after its open. This check therefore runs
        # after open-price exits and entries, and includes the entry and final bars.
        # The extreme is read from the mark series when there is one, because that
        # is the price the venue liquidates against.
        mark_row = mark_at(bar_ts)
        adverse_high = float(mark_row["high"]) if mark_row else float(bar["high"])
        adverse_low = float(mark_row["low"]) if mark_row else float(bar["low"])
        valuation = float(mark_row["close"]) if mark_row else float(bar["close"])
        if position and config.include_liquidation and position.liq_price:
            hit = (position.direction == 1 and adverse_low <= position.liq_price) or (
                position.direction == -1 and adverse_high >= position.liq_price
            )
            if hit:
                liq_order = new_order(
                    index=index,
                    ts=bar_ts,
                    side="sell" if position.direction == 1 else "buy",
                    purpose="exit",
                    order_type="liquidation",
                    quantity=position.quantity,
                    reason="liquidation",
                )
                equity, trade = _close(
                    equity, position, bar, position.liq_price, index, config, "liquidation", order=liq_order
                )
                trades.append(trade)
                total_fees += trade.fees - position.entry_fee
                total_liquidation_fees += trade.liquidation_fee
                liquidations.append(
                    {
                        "time": bar_ts,
                        "tierId": position.risk_tier_id,
                        "maintenanceMarginRate": position.maintenance_margin_rate,
                        "maintenanceMargin": round(position.maintenance_margin, 6),
                        "liqPrice": position.liq_price,
                        # What the position actually cost: capped margin plus the
                        # fees and funding it already paid.
                        "loss": round(trade.net_pnl, 6),
                        "liquidationFee": round(trade.liquidation_fee, 6),
                    }
                )
                position = None
                if pending_entry is not None:
                    cancel_order(
                        pending_entry.order,
                        remaining=pending_entry.remaining,
                        price=bar_open,
                        note="强平后剩余开仓挂单撤销",
                    )
                    pending_entry = None

        marked = equity + (position.direction * position.quantity * (valuation - position.entry_price) if position else 0.0)
        equity_curve.append(
            {
                "time": bar_ts,
                "equity": round(marked, 6),
                "markPrice": round(valuation, 8),
                "marginRatio": _margin_ratio(
                    position,
                    valuation,
                    position.maintenance_margin_rate if position else config.maintenance_margin_rate,
                    position.maintenance_margin if position else 0.0,
                ),
            }
        )

    # ---- settle whatever is still open at the last close
    last = ordered[-1]
    if pending_entry is not None:
        # An order that is still resting when the data ends was never filled; the
        # result has to say how much of it was left rather than quietly dropping it.
        cancel_order(
            pending_entry.order,
            remaining=pending_entry.remaining,
            price=float(last["open"]),
            note="回测数据结束仍未完全成交",
        )
        pending_entry = None
    if position:
        last_mark = mark_at(int(last["ts"]))
        final_reference = float(last_mark["close"]) if last_mark else float(last["close"])
        final_price = final_reference * (1 - slip if position.direction == 1 else 1 + slip)
        final_order = new_order(
            index=len(ordered) - 1,
            ts=int(last["ts"]),
            side="sell" if position.direction == 1 else "buy",
            purpose="exit",
            order_type="market",
            quantity=position.quantity,
            reason="end_of_data",
        )
        equity, trade = _close(
            equity, position, last, final_price, len(ordered) - 1, config, "end_of_data", order=final_order
        )
        trades.append(trade)
        # The entry fee was already deducted on open; only the exit leg is new.
        total_fees += trade.fees - position.entry_fee
        position = None
    final_point = {"time": int(last["ts"]), "equity": round(equity, 6), "marginRatio": None}
    if equity_curve and equity_curve[-1]["time"] == final_point["time"]:
        equity_curve[-1] = final_point
    else:
        equity_curve.append(final_point)

    peak = config.initial_capital
    max_drawdown = 0.0
    for point in equity_curve:
        peak = max(peak, point["equity"])
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - point["equity"]) / peak * 100)

    winners = [t for t in trades if t.net_pnl > 0]
    gross_wins = sum(t.net_pnl for t in winners)
    gross_losses = abs(sum(t.net_pnl for t in trades if t.net_pnl < 0))

    proxies = data_proxies(instrument=instrument, ordered=ordered, config=config, marks=marks)

    thin_entries = sum(1 for t in trades if t.thin_entry)
    if thin_entries and config.fill_on_thin == "allow":
        warnings.append(f"有 {thin_entries} 笔成交发生在休市空 bar 上，成交价不具备真实可成交深度")
    if not funding_rows and config.include_funding:
        warnings.append("本地没有该合约的资金费率历史，回测未计入资金费；可先运行 fetch derivatives 拉取")
    product_type = (instrument or {}).get("productType")
    if product_type == "etf":
        warnings.append("该合约标的是三倍杠杆 ETF，标的自身存在每日再平衡导致的复利衰减，本回测未建模该衰减")
    if config.leverage > 1:
        if risk_profile is not None and risk_profile.tiers:
            warnings.append(
                f"已启用 {config.leverage:g}x 逐仓杠杆，维持保证金率与杠杆上限按交易所风险档位逐笔取值"
            )
        else:
            warnings.append(
                f"已启用 {config.leverage:g}x 逐仓杠杆，本地没有该合约的风险档位，"
                f"按维持保证金率 {config.maintenance_margin_rate * 100:g}% 估算"
            )
    if risk_profile is not None and risk_profile.tiers:
        warnings.append(
            f"风险档位来自 {risk_profile.source}（同步于 {risk_profile.synced_at or '未知时间'}），共 {len(risk_profile.tiers)} 档"
        )
    for breach in dict.fromkeys(leverage_breaches):
        warnings.append(f"杠杆超出档位上限：{breach}")
    if mark_series:
        warnings.append(f"强平、估值与资金费使用交易所标记价序列（{len(mark_series)} 根）")
    elif config.include_liquidation or config.include_funding:
        warnings.append("本地没有标记价序列，强平与资金费改用K线收盘价近似")
    if config.slippage_model == "participation" and impact_charges:
        warnings.append(
            f"滑点按参与率建模：{impact_charges} 笔成交额外计入冲击成本（系数 {config.impact_coefficient:g}）"
        )
    if unfilled_orders:
        warnings.append(
            f"参与率上限（{config.max_participation:g}）截断了 {unfilled_orders} 笔开仓，"
            f"合计 {unfilled_notional:,.0f} 名义额、{unfilled_quantity:g} 数量未成交；"
            f"结果是按能成交的部分计算的"
        )
    if config.partial_fill == "cap" and max_fill_delay:
        warnings.append(
            f"部分成交跨K线累计：最慢的开仓单用了 {max_fill_delay} 根K线才成交完；"
            f"信号失效时剩余挂单会被撤销并计入未成交"
        )
    if config.maker_fill == "passive_only":
        warnings.append(
            f"入场按被动限价挂单（费率 {_maker_fee_bps(config):g} bps，存活 {config.maker_order_bars} 根K线）："
            f"价格没有回踩到信号收盘价的信号不会成交，成交价不含滑点也不含冲击成本"
        )
    if _protection_active(config):
        parts = []
        if config.stop_loss_pct:
            parts.append(f"止损 {config.stop_loss_pct:g}%")
        if config.take_profit_pct:
            parts.append(f"止盈 {config.take_profit_pct:g}%")
        if config.trailing_stop_pct:
            parts.append(f"移动止损 {config.trailing_stop_pct:g}%")
        warnings.append(
            f"已启用{'、'.join(parts)}，同一根K线内按"
            f"{'不利方向优先（保守）' if config.bar_path == 'conservative' else '有利方向优先（乐观，仅供敏感性分析）'}"
            f"判定，跳空按开盘价成交"
        )
    if config.liquidation_fee_bps:
        warnings.append(
            f"强平额外计收清算费 {config.liquidation_fee_bps:g} bps（{total_liquidation_fees:,.2f}），"
            f"该费用单独记录，不在 total_fees 里"
        )
    if config.latency_bars:
        warnings.append(
            f"信号与成交之间计入 {config.latency_bars} 根K线延迟，成交价为延迟后那根的开盘价"
        )
    for proxy in proxies:
        warnings.append(f"代理数据：{proxy['field']} — {proxy['note']}")

    assumptions = [
        _fill_rule(config),
        "仓位按当前净值百分比开仓（复利），杠杆同时放大名义敞口与盈亏",
        (
            "资金费按交易所实际结算时间与当时标记价计收"
            if mark_series
            else "资金费按交易所实际结算时间计收，标记价缺失时用当根K线收盘价近似"
        ),
        "强平按逐仓模型，用维持保证金与档位上限判定，取标记价的不利极值",
        "手续费按双边计收，滑点按模型在开平时各计一次",
        "休市空 bar 默认不建仓，避免在无深度时成交",
    ]
    if config.partial_fill == "cap":
        assumptions.append("开仓按参与率上限跨K线累计成交，信号失效或数据结束时撤销剩余部分")
    if config.maker_fill == "passive_only":
        assumptions.append("入场为被动限价挂单：只有价格回踩到限价才算成交，未触及即不成交")
    if _protection_active(config):
        assumptions.append("止损/止盈只按K线极值触发，同根K线内先后顺序按保守路径假定，跳空按开盘价成交")
    if config.liquidation_fee_bps:
        assumptions.append("强平另计清算费，损失合计不超过该仓位的保证金")
    if position_intents is not None:
        assumptions.append(
            "仓位意图按目标敞口下单：同向加仓、分批减仓与结构止损都在下一根开盘撮合，"
            "每笔订单分别计费；结构止损按K线极值触发，跳空按开盘价成交；"
            "敞口来自每个意图的 targetExposurePct（不由 allocation_pct 决定）"
        )
        if max_portfolio_risk_pct:
            assumptions.append(
                f"总风险预算为权益的 {float(max_portfolio_risk_pct):g}%"
                "（入场价到结构止损的距离×数量），超限的开仓/加仓被拒绝并记录原因"
            )

    intent_summary: dict = {}
    if position_intents is not None:
        filled = [item for item in intent_records if item.get("status") == "filled"]
        increased = [item for item in filled if item.get("action") == "increase"]
        reduced = [item for item in intent_records if item.get("status") == "reduced"]
        exited = [item for item in intent_records if item.get("status") == "exited"]
        stopped = [item for item in intent_records if item.get("status") == "stopped"]
        cancelled = [item for item in intent_records if item.get("status") == "cancelled"]
        order_fees = [
            {
                "orderId": order.order_id,
                "purpose": order.purpose,
                "side": order.side,
                "orderType": order.order_type,
                "reason": order.reason,
                "status": order.status,
                "quantity": round(order.quantity, 8),
                "filledQuantity": round(order.filled_quantity, 8),
                "avgFillPrice": round(order.avg_fill_price, 8),
                "fee": round(sum(float(fill.get("fee") or 0.0) for fill in order.fills), 8),
                "feeKind": order.fee_kind,
                "fills": len(order.fills),
                "createdTime": order.created_time,
                "filledTime": order.filled_time,
            }
            for order in orders
        ]
        intent_summary = {
            "model": "intent",
            "intents": len(intent_records) + len(rejected_intents),
            "opens": len([item for item in filled if item.get("action") == "open"]),
            "increases": len(increased),
            "reduces": len(reduced),
            "exits": len(exited),
            "stops": len(stopped),
            "cancels": len(cancelled),
            "ignored": len([item for item in intent_records if item.get("status") == "ignored"]),
            "unfilled": len([item for item in intent_records if item.get("status") == "unfilled"]),
            "rejected": len(rejected_intents),
            "rejectedReasons": [item.get("note") for item in rejected_intents],
            "maxRiskCarried": round(max_risk_seen, 6),
            # `maxRiskCarried` is marked at each close against the stop, so with the
            # carried-risk constraint on it sits at the budget plus whatever the next
            # bar moved against the position before the correction filled - one bar, but
            # a bar can be large. Measured on real hourly stock perps at a 1% budget:
            # 234.2 unconstrained against 186.2 constrained (MSFT), 346.8 against 326.4
            # (TSLA). `riskAlerts` counts the bars that crossed; an alert does not always
            # become an order, because the next open may already be back inside.
            "riskBudgetEnforced": bool(enforce_open_risk and max_portfolio_risk_pct),
            "riskAlerts": risk_alerts,
            "riskReductions": risk_reductions,
            "riskExits": risk_exits,
            "maxPortfolioRiskPct": max_portfolio_risk_pct,
            # The reference budget, stated on initial capital so it does not move as
            # equity does; both live checks size against the current equity instead.
            "riskBudget": (
                round(config.initial_capital * float(max_portfolio_risk_pct) / 100, 6)
                if max_portfolio_risk_pct
                else None
            ),
            "orderFees": order_fees,
            "orderFeeTotal": round(sum(item["fee"] for item in order_fees), 6),
            "records": intent_records,
        }
        warnings.append(
            f"已启用结构化仓位意图模型（阶段 D）：{intent_summary['opens']} 次开仓、"
            f"{intent_summary['increases']} 次加仓、{intent_summary['reduces']} 次分批减仓、"
            f"{intent_summary['exits']} 次离场、{intent_summary['stops']} 次结构止损、"
            f"{intent_summary['rejected']} 个意图被拒绝；"
            "每笔订单的费用在 orderFees 里逐笔列出，total_fees 仍是全场合计"
        )
        if intent_summary["rejected"]:
            warnings.append(
                f"有 {intent_summary['rejected']} 个意图被拒绝，原因已记录："
                f"{intent_summary['rejectedReasons'][0]}"
            )
        if intent_summary["riskAlerts"]:
            warnings.append(
                f"风险预算同时约束持仓浮动风险：{intent_summary['riskAlerts']} 根K线收盘时"
                f"浮动风险越过预算，触发 {intent_summary['riskReductions']} 次风控减仓、"
                f"{intent_summary['riskExits']} 次风控整笔平仓（都在下一根开盘执行，"
                "所以 maxRiskCarried 最多超出预算一根K线的逆向波动）；"
                "enforce_open_risk=False 可关闭该约束以作对比"
            )

    order_stats = _order_stats(orders)  # counts are always the full population
    order_stats["total"] = len(orders)
    order_stats["stored"] = min(len(orders), MAX_ORDER_LOG)
    order_stats["truncated"] = len(orders) > MAX_ORDER_LOG
    if order_stats["truncated"]:
        warnings.append(
            f"订单日志只保留前 {MAX_ORDER_LOG:,} 条（本次共 {len(orders):,} 条），"
            f"成交明细以 trades 为准；统计计数仍是全量"
        )

    return BacktestResult(
        config=asdict(config),
        instrument=instrument or {},
        initial_capital=config.initial_capital,
        final_equity=round(equity, 6),
        net_return_pct=round((equity / config.initial_capital - 1) * 100, 6),
        max_drawdown_pct=round(max_drawdown, 6),
        win_rate_pct=round(len(winners) / len(trades) * 100, 4) if trades else 0.0,
        profit_factor=(round(gross_wins / gross_losses, 4) if gross_losses else (None if gross_wins else 0.0)),
        total_fees=round(total_fees, 6),
        total_funding=round(total_funding, 6),
        trades=trades,
        equity_curve=equity_curve,
        warnings=warnings,
        assumptions=assumptions,
        data_quality={
            "bars": len(ordered),
            "from": int(ordered[0]["ts"]),
            "to": int(last["ts"]),
            "interval": interval,
            "intervalMs": INTERVAL_MS.get(interval or "", None),
            "thinBars": sum(1 for flag in thin if flag),
            "fundingPoints": len(funding_rows),
            "fundingSettlements": len(funding_settlements),
            "markBars": len(mark_series),
            "markSource": "bybit:mark-price-kline" if mark_series else "bar_close_fallback",
            "latencyBars": int(config.latency_bars),
            "partialFill": config.partial_fill,
            "maxParticipation": float(config.max_participation),
            "unfilledOrders": unfilled_orders,
            "unfilledNotional": round(unfilled_notional, 6),
            "unfilledQuantity": round(unfilled_quantity, 8),
            "maxFillDelayBars": max_fill_delay,
            "orders": order_stats,
            "makerFill": config.maker_fill,
            "makerFeeBps": _maker_fee_bps(config),
            "protection": {
                "stopLossPct": config.stop_loss_pct,
                "takeProfitPct": config.take_profit_pct,
                "trailingStopPct": config.trailing_stop_pct,
                "barPath": config.bar_path,
                "stopExits": stop_exits,
            },
            "liquidationFeeBps": float(config.liquidation_fee_bps),
            "liquidationFees": round(total_liquidation_fees, 6),
            "higherTimeframes": {
                name: {
                    "bars": len((higher_timeframes or {}).get(name) or []),
                    "visibleBars": sum(1 for row in view if row is not None),
                    "firstVisibleIndex": next((i for i, row in enumerate(view) if row is not None), None),
                    "rule": "base.ts >= higher.ts + 高周期长度（高周期K线收盘后才可见）",
                }
                for name, view in aligned_views.items()
            },
            "dataProxies": proxies,
            "asOf": "严格 as-of：第 i 根只用 ts<=t_i 的信息，回测长度变化不会改变此前任何一根的决策",
            "riskTiers": len(risk_profile.tiers) if risk_profile else 0,
            "riskSource": risk_profile.source if risk_profile else None,
            "riskSyncedAt": risk_profile.synced_at if risk_profile else None,
            "maxLeverageAllowed": risk_profile.max_leverage if risk_profile else None,
            **({"positionIntents": intent_summary} if intent_summary else {}),
        },
        execution_model=_execution_model(
            config,
            unfilled_orders,
            unfilled_notional,
            unfilled_quantity=unfilled_quantity,
            max_fill_delay=max_fill_delay,
            stop_exits=stop_exits,
        ),
        orders=orders[:MAX_ORDER_LOG],
        data_proxies=proxies,
        total_liquidation_fees=round(total_liquidation_fees, 6),
        risk={
            "tiered": bool(risk_profile and risk_profile.tiers),
            "maxLeverageAllowed": risk_profile.max_leverage if risk_profile else None,
            "minMaintenanceMarginRate": risk_profile.min_maintenance_margin_rate if risk_profile else None,
            "liquidations": liquidations,
            "fundingSettlements": funding_settlements,
            "tradedTiers": sorted({t.risk_tier_id for t in trades if t.risk_tier_id is not None}),
        },
    )


def _order_stats(orders: list[Order]) -> dict:
    """Order-log summary: the counts a reader would otherwise have to compute."""
    by_status: dict[str, int] = {}
    for order in orders:
        by_status[order.status] = by_status.get(order.status, 0) + 1
    fills = [fill for order in orders for fill in order.fills]
    return {
        "created": len(orders),
        "byStatus": by_status,
        "entries": sum(1 for order in orders if order.purpose == "entry"),
        "exits": sum(1 for order in orders if order.purpose == "exit"),
        "fills": len(fills),
        "makerFills": sum(1 for fill in fills if fill["feeKind"] == "maker"),
        "takerFills": sum(1 for fill in fills if fill["feeKind"] == "taker"),
        "maxFillDelayBars": max((order.bars_to_fill for order in orders), default=0),
        "unfilledQuantity": round(sum(order.unfilled_quantity for order in orders), 8),
    }


def _intrabar_protection(
    position: _Position,
    *,
    bar_open: float,
    high: float,
    low: float,
    config: BacktestConfig,
    liquidation_on: bool,
) -> tuple[str, float] | None:
    """Which protective order this bar would have hit, and at what price.

    One bar cannot say whether the high or the low came first, so the order is
    decided by `bar_path`: `conservative` (the default) assumes the adverse leg
    happened first - a stop and a take profit in the same bar is a stop loss. A
    gap through the level fills at the open, which is worse than the level for a
    stop and better for a take profit.
    """
    long = position.direction == 1
    levels: list[tuple[float, str]] = []
    if config.stop_loss_pct:
        pct = float(config.stop_loss_pct) / 100
        levels.append((position.entry_price * (1 - pct if long else 1 + pct), "stop_loss"))
    if config.trailing_stop_pct:
        pct = float(config.trailing_stop_pct) / 100
        best = position.best_price or position.entry_price
        levels.append((best * (1 - pct if long else 1 + pct), "trailing_stop"))
    stop: float | None = None
    stop_reason = "stop_loss"
    if levels:
        stop, stop_reason = max(levels) if long else min(levels)
    take: float | None = None
    if config.take_profit_pct:
        pct = float(config.take_profit_pct) / 100
        take = position.entry_price * (1 + pct if long else 1 - pct)

    hit_stop = stop is not None and ((long and low <= stop) or (not long and high >= stop))
    if hit_stop and liquidation_on and position.liq_price:
        # Whichever level is closer to the market takes the position first: a stop
        # that sits beyond the liquidation price never gets a chance to fill.
        tighter = (long and stop > position.liq_price) or (not long and stop < position.liq_price)
        if not tighter:
            hit_stop = False
    hit_take = take is not None and ((long and high >= take) or (not long and low <= take))

    if hit_stop and hit_take and config.bar_path == "optimistic":
        return "take_profit", (max(bar_open, take) if long else min(bar_open, take))
    if hit_stop:
        return stop_reason, (min(bar_open, stop) if long else max(bar_open, stop))
    if hit_take:
        return "take_profit", (max(bar_open, take) if long else min(bar_open, take))
    return None


def _margin_ratio(
    position: _Position | None,
    mark: float,
    maintenance_margin_rate: float,
    maintenance_margin: float = 0.0,
) -> float | None:
    """Maintenance margin as a fraction of remaining margin; None when flat.

    The requirement is the position's own rung, so a position that grew into a
    stricter rung reports the higher ratio rather than the entry-time one.
    """
    if position is None:
        return None
    if position.direction == 1:
        pnl = position.quantity * (mark - position.entry_price)
    else:
        pnl = position.quantity * (position.entry_price - mark)
    remaining = position.margin + pnl
    if remaining <= 0:
        return None
    required = maintenance_margin
    if required <= 0:
        required = abs(position.quantity * mark) * maintenance_margin_rate
    return round(required / remaining, 6)


def _close(
    equity: float,
    position: _Position,
    bar: dict,
    exit_price: float,
    index: int,
    config: BacktestConfig,
    reason: str,
    *,
    order: Order | None = None,
) -> tuple[float, BacktestTrade]:
    """Realise a position. Returns (new equity, trade)."""
    exit_price = _round_to(exit_price, config.tick_size)
    exit_fee = position.quantity * exit_price * (config.fee_bps / 10_000)
    gross = position.direction * position.quantity * (exit_price - position.entry_price)
    liquidation = reason == "liquidation"
    # A liquidation fee is charged on top of the trading fee and recorded apart
    # from it, so a liquidation's cost is never hidden inside `fees`.
    liquidation_fee = (
        position.quantity * exit_price * (config.liquidation_fee_bps / 10_000) if liquidation else 0.0
    )
    net = gross - exit_fee - position.entry_fee - position.funding_paid - liquidation_fee
    if liquidation:
        # A liquidation cannot hand back more than the posted margin.
        net = max(net, -position.margin)
        # Entry fees and funding have already left equity. Apply the remaining
        # delta needed to make the full-lifecycle loss equal to the capped net.
        equity += net + position.entry_fee + position.funding_paid
    else:
        equity += gross - exit_fee
    trade = BacktestTrade(
        direction="多" if position.direction == 1 else "空",
        entry_time=position.entry_time,
        exit_time=int(bar["ts"]),
        entry_price=round(position.entry_price, 8),
        exit_price=round(exit_price, 8),
        quantity=round(position.quantity, 8),
        notional=round(position.notional, 4),
        gross_pnl=round(gross, 6),
        funding_paid=round(position.funding_paid, 6),
        fees=round(exit_fee + position.entry_fee, 6),
        net_pnl=round(net, 6),
        return_pct=round(net / config.initial_capital * 100, 6),
        bars_held=max(0, index - position.entry_index),
        exit_reason=reason,
        liquidated=liquidation,
        thin_entry=position.thin_entry,
        risk_tier_id=position.risk_tier_id,
        maintenance_margin_rate=position.maintenance_margin_rate,
        entry_fills=position.fill_count,
        entry_delay_bars=max(0, position.last_fill_index - position.entry_index),
        exit_fee=round(exit_fee, 6),
        liquidation_fee=round(liquidation_fee, 6),
    )
    if order is not None:
        order.fills.append(
            {
                "index": index,
                "time": int(bar["ts"]),
                "quantity": round(position.quantity, 8),
                "price": round(exit_price, 8),
                "fee": round(exit_fee, 8),
                "feeKind": "taker",
                "slippageBps": 0.0,
            }
        )
        order.status = "filled"
        order.filled_quantity = position.quantity
        order.unfilled_quantity = 0.0
        order.avg_fill_price = exit_price
        order.filled_index = index
        order.filled_time = int(bar["ts"])
        order.bars_to_fill = max(0, index - order.created_index)
    return equity, trade

def _close_partial(
    equity: float,
    position: _Position,
    bar: dict,
    exit_price: float,
    index: int,
    config: BacktestConfig,
    reason: str,
    *,
    quantity: float,
    order: Order | None = None,
) -> tuple[float, BacktestTrade]:
    """Realise part of a position and leave the rest open.

    The closed share carries its share of the entry fee, of the funding already paid
    and of the notional; the remainder keeps its size-weighted entry price, which a
    partial close does not change. The margin release is proportional, so the
    liquidation price of what is left is recomputed rather than left stale.
    """
    quantity = min(float(quantity), position.quantity)
    if quantity <= 0:
        raise ValueError("分批平仓数量必须大于 0")
    share = quantity / position.quantity if position.quantity else 0.0
    closed_entry_fee = position.entry_fee * share
    closed_funding = position.funding_paid * share
    closed_notional = position.notional * share
    closed_margin = position.margin * share

    exit_price = _round_to(exit_price, config.tick_size)
    exit_fee = quantity * exit_price * (config.fee_bps / 10_000)
    gross = position.direction * quantity * (exit_price - position.entry_price)
    net = gross - exit_fee - closed_entry_fee - closed_funding
    equity += gross - exit_fee

    trade = BacktestTrade(
        direction="多" if position.direction == 1 else "空",
        entry_time=position.entry_time,
        exit_time=int(bar["ts"]),
        entry_price=round(position.entry_price, 8),
        exit_price=round(exit_price, 8),
        quantity=round(quantity, 8),
        notional=round(closed_notional, 4),
        gross_pnl=round(gross, 6),
        funding_paid=round(closed_funding, 6),
        fees=round(exit_fee + closed_entry_fee, 6),
        net_pnl=round(net, 6),
        return_pct=round(net / config.initial_capital * 100, 6),
        bars_held=max(0, index - position.entry_index),
        exit_reason=reason,
        liquidated=False,
        thin_entry=position.thin_entry,
        risk_tier_id=position.risk_tier_id,
        maintenance_margin_rate=position.maintenance_margin_rate,
        entry_fills=position.fill_count,
        entry_delay_bars=max(0, position.last_fill_index - position.entry_index),
        exit_fee=round(exit_fee, 6),
        liquidation_fee=0.0,
    )

    position.quantity -= quantity
    position.notional -= closed_notional
    position.entry_fee -= closed_entry_fee
    position.funding_paid -= closed_funding
    position.margin -= closed_margin
    if position.quantity <= 1e-12:
        position.quantity = 0.0
    else:
        position.liq_price = liquidation_price(
            position.direction,
            position.entry_price,
            quantity=position.quantity,
            margin=position.margin,
            mmr=position.maintenance_margin_rate,
            mm_deduction=position.mm_deduction,
        )

    if order is not None:
        order.fills.append(
            {
                "index": index,
                "time": int(bar["ts"]),
                "quantity": round(quantity, 8),
                "price": round(exit_price, 8),
                "fee": round(exit_fee, 8),
                "feeKind": "taker",
                "slippageBps": 0.0,
            }
        )
        order.filled_quantity += quantity
        order.unfilled_quantity = max(0.0, order.quantity - order.filled_quantity)
        order.avg_fill_price = exit_price
        order.filled_index = index
        order.filled_time = int(bar["ts"])
        order.bars_to_fill = max(0, index - order.created_index)
        order.status = "filled" if order.unfilled_quantity <= 1e-12 else "partially_filled"
    return equity, trade
