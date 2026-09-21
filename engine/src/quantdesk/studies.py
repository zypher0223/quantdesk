"""Formal studies: the request shape and the one implementation of each.

A single backtest, a parameter search with walk-forward, and a portfolio are the
three formal studies QuantDesk runs. They answer to the same gate, read the same
local history and return the same provenance envelope, so they are implemented
once, here.

Two callers use this module:

* the API layer, which runs a *small* study inline and refuses a large one (see
  `sync_cost` and `sync_budget`), and
* the background run queue, which runs whatever it is given and records the
  result, the progress and the failure.

Neither owns the computation; a discrepancy between a quick run and a queued run
of the same request would be a bug by construction if it lived in two places.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable

from pydantic import BaseModel, Field, field_validator

from .backtest import (
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_TAKER_FEE_BPS,
    BacktestConfig,
    run_backtest,
)
from .config.instruments import TIMEFRAMES, require_instrument
from .config.settings import load_app_config, quantdesk_home
from .datahub.db import Database
from .datahub.venue import INTERVAL_MS
from .paper import PaperConfig
from .paper.engine import DEFAULT_MAINTENANCE_MARGIN_RATE
from .plugins import PluginError, PluginManager
from .strategy import StrategyRegistry

MAX_CANDLES = 1_000  # 兜底默认：config.toml 里 [backtest] max_bars 没写时用它
# 硬顶：任何本地配置都不允许把单次回测的K线上限抬到这条线以上。
ABSOLUTE_MAX_CANDLES = 100_000
MIN_CANDLES = 30


def max_backtest_bars() -> int:
    """单次回测/验证允许的K线上限，从 config.toml 的 [backtest] max_bars 读取。

    读取发生在校验时而不是导入时，改配置后的下一次请求就生效；超限的请求会被
    直接拒绝并给出可操作的信息，绝不静默截断K线（截断会让用户在不知情的情况下
    拿到一段更短的样本）。
    """
    try:
        section = load_app_config().backtest or {}
        value = int(section.get("max_bars", MAX_CANDLES))
    except Exception:  # noqa: BLE001 - 配置坏了不能让所有请求都失败
        value = MAX_CANDLES
    return max(MIN_CANDLES, min(value, ABSOLUTE_MAX_CANDLES))


def _bar_cap_error(value: int) -> str:
    limit = max_backtest_bars()
    return (
        f"请求 {value} 根K线，超过当前上限 {limit} 根；"
        f"可在 config.toml 的 [backtest] max_bars 调整（硬顶 {ABSOLUTE_MAX_CANDLES} 根），"
        f"或改用后台队列跑更长的样本"
    )


def execution_knobs(request) -> dict:
    """事件驱动执行层的可选开关，供三次建配置的地方共用。

    `getattr` 是为了兼容测试里那些只实现了一部分字段的假请求对象：缺字段时回落到
    引擎的默认值（挂单关闭、保护性委托关闭、清算费 0），也就是历史行为。
    """
    return {
        "maker_fee_bps": getattr(request, "makerFeeBps", None),
        "maker_fill": str(getattr(request, "makerFill", "never") or "never"),
        "maker_order_bars": int(getattr(request, "makerOrderBars", 1) or 1),
        "stop_loss_pct": getattr(request, "stopLossPct", None),
        "take_profit_pct": getattr(request, "takeProfitPct", None),
        "trailing_stop_pct": getattr(request, "trailingStopPct", None),
        "bar_path": str(getattr(request, "barPath", "conservative") or "conservative"),
        "liquidation_fee_bps": float(getattr(request, "liquidationFeeBps", 0.0) or 0.0),
    }


def _configured_bar_cap(value: int) -> int:
    if value > max_backtest_bars():
        raise ValueError(_bar_cap_error(value))
    return value


# How much work a study may do before it has to go to the background queue.
# One "unit" is one bar evaluated by one backtest, so a 1,000-bar single run is
# 1,000 units and a 3,000-bar 25-candidate walk-forward is over a million. The
# ceiling is where a synchronous request stops being an interaction: on this
# machine ~60k units take a couple of seconds, and anything much above that is a
# browser timeout waiting to happen.
SYNC_UNIT_BUDGET = 60_000


class StudyError(Exception):
    """A study that cannot start, with the reason the caller has to show.

    `kind` is stored with a failed queued run (`not_ready`, `invalid`,
    `internal`); `status` is the HTTP status the API layer answers with; `detail`
    is the structured body (the readiness verdict) when there is one.
    """

    def __init__(self, kind: str, message: str, *, status: int = 422,
                 detail: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.detail = detail


class CandleInput(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class BacktestRequest(BaseModel):
    symbol: str
    timeframe: str = "1h"
    strategyId: str = Field("ma_cross", min_length=2, max_length=160)
    strategyParams: dict = Field(default_factory=dict)
    fastPeriod: int = Field(9, ge=2, le=200)
    slowPeriod: int = Field(21, ge=3, le=500)
    direction: str = "both"
    initialCapital: float = Field(10_000, gt=0)
    allocationPct: float = Field(50, gt=0, le=100)
    feeBps: float | None = None
    slippageBps: float | None = None
    leverage: float = Field(1, ge=1, le=200)
    maintenanceMarginRate: float = Field(DEFAULT_MAINTENANCE_MARGIN_RATE, gt=0, lt=1)
    includeFunding: bool = True
    includeLiquidation: bool = True
    fillOnThin: str = "skip"
    allowDegraded: bool = False
    needsOpenInterest: bool = False
    slippageModel: str = "fixed"
    impactCoefficient: float = Field(0.1, ge=0, le=5)
    maxParticipation: float = Field(1.0, gt=0, le=1)
    # Execution: how long after the signal the fill happens, and what the book
    # does with an order bigger than its participation cap.
    latencyBars: int = Field(0, ge=0, le=10)
    partialFill: str = "ignore"
    # Order lifecycle: maker entry, protection levels, liquidation fee, and which
    # leg of a one-bar stop/take-profit collision is assumed to have happened first.
    makerFeeBps: float | None = Field(None, ge=0, le=200)
    makerFill: str = "never"
    makerOrderBars: int = Field(1, ge=1, le=100)
    stopLossPct: float | None = Field(None, gt=0, lt=100)
    takeProfitPct: float | None = Field(None, gt=0, lt=100)
    trailingStopPct: float | None = Field(None, gt=0, lt=100)
    barPath: str = "conservative"
    liquidationFeeBps: float = Field(0.0, ge=0, le=200)
    useRiskTiers: bool = True
    useMarkPrice: bool = True
    bars: int = Field(600, ge=MIN_CANDLES)
    candles: list[CandleInput] | None = None

    @field_validator("bars")
    @classmethod
    def _bars_within_config(cls, value: int) -> int:
        return _configured_bar_cap(value)


class ValidationRequest(BaseModel):
    symbol: str
    timeframe: str = "1h"
    strategyId: str = Field("ma_cross", min_length=2, max_length=160)
    bars: int = Field(1500, ge=MIN_CANDLES)
    allowDegraded: bool = False
    includeFunding: bool = True
    includeLiquidation: bool = True
    needsOpenInterest: bool = False
    train: float = Field(0.6, gt=0, lt=1)
    validation: float = Field(0.2, gt=0, lt=1)
    fastGrid: list[int] = Field(default_factory=lambda: [5, 9, 20], min_length=1, max_length=8)
    slowGrid: list[int] = Field(default_factory=lambda: [21, 50, 100], min_length=1, max_length=8)
    # The generic grid: {parameter: [values]}. Kept alongside the two double-MA grids so
    # an existing request keeps working unchanged, while any strategy (CPA included) can
    # be searched over its own parameters. Values are `Any` on purpose: CPA's entry
    # stages and side mode are strings (["wedge_pop"], "symmetric"), and a numeric-only
    # grid made those parameters unsearchable - the whole point of a generic grid.
    parameterGrid: dict[str, list[Any]] = Field(default_factory=dict)
    strategyParams: dict = Field(default_factory=dict)
    walkForwardWindows: int = Field(4, ge=1, le=12)
    initialCapital: float = Field(10_000, gt=0)
    allocationPct: float = Field(50, gt=0, le=100)
    leverage: float = Field(1, ge=1, le=200)
    direction: str = "both"
    useRiskTiers: bool = True
    useMarkPrice: bool = True
    slippageModel: str = "fixed"
    latencyBars: int = Field(0, ge=0, le=10)
    partialFill: str = "ignore"
    makerFeeBps: float | None = Field(None, ge=0, le=200)
    makerFill: str = "never"
    makerOrderBars: int = Field(1, ge=1, le=100)
    stopLossPct: float | None = Field(None, gt=0, lt=100)
    takeProfitPct: float | None = Field(None, gt=0, lt=100)
    trailingStopPct: float | None = Field(None, gt=0, lt=100)
    barPath: str = "conservative"
    liquidationFeeBps: float = Field(0.0, ge=0, le=200)

    @field_validator("bars")
    @classmethod
    def _bars_within_config(cls, value: int) -> int:
        return _configured_bar_cap(value)


class PortfolioRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1, max_length=17)
    weights: dict[str, float] | None = None
    timeframe: str = "1h"
    strategyId: str = Field("ma_cross", min_length=2, max_length=160)
    strategyParams: dict = Field(default_factory=dict)
    bars: int = Field(600, ge=MIN_CANDLES)
    initialCapital: float = Field(10_000, gt=0)
    allocationPct: float = Field(50, gt=0, le=100)
    leverage: float = Field(1, ge=1, le=200)
    direction: str = "both"
    useRiskTiers: bool = True
    useMarkPrice: bool = True
    allowDegraded: bool = False
    includeFunding: bool = True
    includeLiquidation: bool = True
    needsOpenInterest: bool = False
    slippageModel: str = "fixed"
    latencyBars: int = Field(0, ge=0, le=10)
    partialFill: str = "ignore"
    makerFeeBps: float | None = Field(None, ge=0, le=200)
    makerFill: str = "never"
    makerOrderBars: int = Field(1, ge=1, le=100)
    stopLossPct: float | None = Field(None, gt=0, lt=100)
    takeProfitPct: float | None = Field(None, gt=0, lt=100)
    trailingStopPct: float | None = Field(None, gt=0, lt=100)
    barPath: str = "conservative"
    liquidationFeeBps: float = Field(0.0, ge=0, le=200)

    @field_validator("bars")
    @classmethod
    def _bars_within_config(cls, value: int) -> int:
        return _configured_bar_cap(value)


class FactorRequest(BaseModel):
    """One queued factor computation over stored history.

    A wide request is minutes of CPU, so it goes through the run queue like any
    other long study: one submission, visible progress, a stored result.
    """

    symbol: str
    interval: str = "1h"
    bars: int = Field(2_000, ge=30, le=200_000)
    factorIds: list[str] | None = Field(None, max_length=64)
    parameters: dict = Field(default_factory=dict)


class CampaignRoundRequest(BaseModel):
    """One round of an agent campaign, run in the worker rather than in a request.

    A round is a dozen backtests: too much for an HTTP handler, which is why it is a
    queued study like any other. It reads its own campaign row, so a resumed worker
    needs nothing but the uid.
    """

    campaign: str = Field(..., min_length=1, max_length=64)


class CpaAblationRequest(BaseModel):
    """One ablation sweep over a group of contracts, run in the worker.

    A sweep is dozens of backtests, so it is a queued study like any other rather than
    an HTTP handler that blocks for minutes.
    """

    group: str = Field("stock", min_length=1, max_length=32)
    interval: str = Field("1h", max_length=8)
    bars: int = Field(1200, ge=120, le=20_000)
    allowDegraded: bool = False


REQUEST_MODELS: dict[str, type[BaseModel]] = {
    "backtest": BacktestRequest,
    "validate": ValidationRequest,
    "portfolio": PortfolioRequest,
    "factors": FactorRequest,
    "campaign": CampaignRoundRequest,
    "cpa_ablation": CpaAblationRequest,
}

Progress = Callable[[float, str], None]


def open_db() -> Database:
    """The local database, or a study error that says why it cannot be opened."""
    home = quantdesk_home()
    path = home / "quantdesk.db"
    try:
        return Database(path)
    except Exception as exc:  # noqa: BLE001 - sqlite raises OperationalError
        raise StudyError(
            "internal",
            f"无法打开本地数据库 {path}：{exc}。"
            "该目录对运行网关的账号不可写。把 QUANTDESK_HOME 指向可写目录后重启网关，或修正目录权限。",
            status=503,
        ) from exc


def paper_defaults(spec, *, home: Path | None = None) -> tuple[float, float]:
    """Per-symbol slippage: TradFi stock perps are thinner than crypto majors."""
    paper = load_app_config(home).paper or {}
    if spec.is_crypto:
        return (
            float(paper.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)),
            float(paper.get("taker_fee_bps", DEFAULT_TAKER_FEE_BPS)),
        )
    return (
        float(paper.get("stock_perp_slippage_bps", paper.get("slippage_bps", DEFAULT_SLIPPAGE_BPS))),
        float(paper.get("taker_fee_bps", DEFAULT_TAKER_FEE_BPS)),
    )


def research_options(request) -> dict:
    """The data knobs a formal study carries, read the same way for all three."""
    return {
        "include_funding": bool(getattr(request, "includeFunding", True)),
        "use_mark_price": bool(getattr(request, "useMarkPrice", True)),
        "include_liquidation": bool(getattr(request, "includeLiquidation", True)),
        "use_risk_tiers": bool(getattr(request, "useRiskTiers", True)),
        "needs_open_interest": bool(getattr(request, "needsOpenInterest", False)),
    }


def instrument_of(symbol: str):
    try:
        return require_instrument(symbol)
    except ValueError as exc:
        raise StudyError("invalid", "该合约不在固定合约池内") from exc


def check_timeframe(timeframe: str) -> None:
    if timeframe not in TIMEFRAMES:
        raise StudyError("invalid", f"不支持的周期：{timeframe}；仅支持 {', '.join(TIMEFRAMES)}")


def instrument_meta(db: Database, spec) -> dict:
    """Stored venue metadata; a backtest never performs an implicit network read."""
    meta = {
        "displaySymbol": spec.display_symbol,
        "venueSymbol": spec.venue_symbol,
        "productType": spec.product_type,
        "riskClass": spec.risk_class,
    }
    stored = db.load_instrument_meta("bybit", spec.venue_symbol) or {}
    raw = stored.get("raw_json") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    leverage = raw.get("leverageFilter", {}) if isinstance(raw, dict) else {}
    meta.update({
        "status": stored.get("status"),
        "tickSize": stored.get("tick_size"),
        "qtyStep": stored.get("qty_step"),
        "minOrderQty": None,
        "minNotionalValue": stored.get("min_notional"),
        "maxLeverage": float(leverage.get("maxLeverage", 0) or 0) or None,
        "fundingInterval": stored.get("funding_interval_hours"),
        "metadataCollectedAt": stored.get("collected_ts"),
    })
    return meta


def load_or_refuse(db: Database, spec, request, *, action: str, from_ts: int | None = None,
                   to_ts: int | None = None, risk_book=None):
    """Read a contract's local history through the gate, or explain the refusal.

    This is the only way a formal study gets its prices. Nothing here reaches the
    venue: Bybit is where the bars came from, not something a study fetches.
    """
    from .datahub.readiness import DataNotReady, load_research_data, require_ready

    data = load_research_data(
        db,
        spec,
        interval=request.timeframe,
        bars=request.bars,
        from_ts=from_ts,
        to_ts=to_ts,
        risk_book=risk_book,
        **research_options(request),
    )
    try:
        require_ready(data, allow_degraded=bool(getattr(request, "allowDegraded", False)), action=action)
    except DataNotReady as exc:
        detail = exc.as_detail()
        raise StudyError("not_ready", detail.get("detail") or "数据未就绪，已阻止正式研究",
                         status=409, detail=detail) from exc
    if not data.candles:
        raise StudyError("invalid", f"{spec.venue_symbol} 在该区间没有本地K线，请先回填历史")
    return data


def load_history_supporting(db: Database, spec, request, candles: list[dict]):
    """Pin the stored funding and marks for a range supplied by the caller."""
    from .risk import RiskBook
    from .datahub.view import read_history

    snapshot = read_history(
        db,
        symbol=spec.venue_symbol,
        interval=request.timeframe,
        bars=len(candles),
        display_symbol=spec.display_symbol,
        product_type=spec.product_type,
        from_ts=int(candles[0]["ts"]),
        to_ts=int(candles[-1]["ts"]),
        with_funding=True,
        with_marks=request.useMarkPrice,
    )
    profile = RiskBook(db).cached(spec.venue_symbol) if request.useRiskTiers else None
    if profile is not None and not profile.tiers:
        profile = None
    return snapshot, profile


def study_envelope(spec, request, research, config, candles, history, source: str) -> dict:
    """The provenance block every formal study returns, in one shape."""
    from .strategy.validation import build_provenance

    provenance = build_provenance(
        strategy_id=config.strategy_id,
        parameters=dict(config.strategy_params or {}),
        candles=candles,
        config=config,
        symbol=spec.venue_symbol,
        interval=request.timeframe,
        risk_profile=research.risk_profile if research is not None else None,
        data_source=source,
    ).as_dict()
    if research is not None:
        envelope = research.envelope(
            cost_model=provenance.get("cost_model") or {},
            strategy_version=strategy_version_block(provenance),
        )
        envelope["versions"]["readHistory"] = history.version
        return envelope
    # An uploaded sample is not gated - its bars came from the caller - but it
    # still says exactly what it read and what it cost.
    return {
        "readRange": {
            "interval": request.timeframe,
            "fromTs": int(candles[0]["ts"]),
            "toTs": int(candles[-1]["ts"]),
            "bars": len(candles),
            "expectedBars": len(candles),
        },
        "versions": {"readHistory": history.version, "upload": provenance["data_hash"]},
        "readiness": None,
        "dataReady": True,
        "degraded": False,
        "missingData": [],
        "dataImpacts": [],
        "costModel": provenance.get("cost_model") or {},
        "strategyVersion": strategy_version_block(provenance),
    }


def strategy_version_block(provenance: dict) -> dict:
    return {
        "strategyId": provenance["strategy_id"],
        "parameters": provenance["parameters"],
        "engine": provenance["engine"],
        "engineVersion": provenance["engine_version"],
    }


def cost_model(request, *, fee_bps: float, slippage_bps: float, meta: dict) -> dict:
    """Every assumption that produced the numbers, so a result can be re-read."""
    return {
        "feeBps": float(fee_bps),
        "slippageBps": float(slippage_bps),
        "slippageModel": str(getattr(request, "slippageModel", "fixed")),
        "impactCoefficient": float(getattr(request, "impactCoefficient", 0.0) or 0.0),
        "includeFunding": bool(getattr(request, "includeFunding", True)),
        "includeLiquidation": bool(getattr(request, "includeLiquidation", True)),
        "fillOnThin": str(getattr(request, "fillOnThin", "skip")),
        "latencyBars": int(getattr(request, "latencyBars", 0) or 0),
        "partialFill": str(getattr(request, "partialFill", "ignore") or "ignore"),
        "makerFeeBps": getattr(request, "makerFeeBps", None),
        "makerFill": str(getattr(request, "makerFill", "never") or "never"),
        "makerOrderBars": int(getattr(request, "makerOrderBars", 1) or 1),
        "stopLossPct": getattr(request, "stopLossPct", None),
        "takeProfitPct": getattr(request, "takeProfitPct", None),
        "trailingStopPct": getattr(request, "trailingStopPct", None),
        "barPath": str(getattr(request, "barPath", "conservative") or "conservative"),
        "liquidationFeeBps": float(getattr(request, "liquidationFeeBps", 0.0) or 0.0),
        "initialCapital": float(getattr(request, "initialCapital", 0.0)),
        "allocationPct": float(getattr(request, "allocationPct", 0.0)),
        "leverage": float(getattr(request, "leverage", 1.0)),
        "tickSize": meta.get("tickSize"),
        "qtyStep": meta.get("qtyStep"),
    }


# -- strategy identity -------------------------------------------------------


def strategy_code_hash(strategy_id: str) -> str:
    """A hash of the implementation behind a strategy id.

    For a built-in rule the source of its module is hashed, so editing the rule
    changes the version. A plugin strategy is named by its plugin and the version
    the plugin reports, which is what the manifest pins.
    """
    if strategy_id.startswith("plugin:"):
        parts = strategy_id.split(":", 2)
        return f"plugin:{parts[1] if len(parts) > 1 else '?'}"
    try:
        from .strategy import rules as rules_module
        from .strategy.registry import BUILTIN_STRATEGIES

        described = {item.id: item for item in BUILTIN_STRATEGIES}
        item = described.get(strategy_id)
        payload = {
            "id": strategy_id,
            "module": getattr(rules_module, "__file__", ""),
            "description": item.description if item else "",
        }
        source = ""
        module_file = getattr(rules_module, "__file__", "")
        if module_file:
            with open(module_file, "rb") as handle:
                source = hashlib.sha256(handle.read()).hexdigest()[:16]
        payload["sourceHash"] = source
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]
    except Exception:  # noqa: BLE001 - identity must not be able to fail a study
        return "unknown"


def strategy_identity(strategy_id: str, parameters: dict | None = None) -> dict:
    """The version of a strategy: id, parameters, engine and implementation hash."""
    code_hash = strategy_code_hash(strategy_id)
    material = json.dumps(
        {
            "strategyId": strategy_id,
            "parameters": parameters or {},
            "engine": "quantdesk.backtest.run_backtest",
            "engineVersion": "2",
            "codeHash": code_hash,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return {
        "strategyId": strategy_id,
        "parameters": dict(parameters or {}),
        "engine": "quantdesk.backtest.run_backtest",
        "engineVersion": "2",
        "codeHash": code_hash,
        "version": hashlib.sha256(material.encode()).hexdigest()[:16],
        "source": "plugin" if strategy_id.startswith("plugin:") else "builtin",
        "createdTs": int(time.time() * 1000),
    }


def record_strategy_version(db: Database, identity: dict) -> None:
    """Pin a strategy version so a later run can cite the same one."""
    from .datahub.db import Database as _Database  # noqa: F401 - type clarity

    db.execute(
        "INSERT OR IGNORE INTO strategy_versions "
        "(strategy_id, version, parameters_json, engine, engine_version, code_hash, source, created_ts) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            identity["strategyId"], identity["version"],
            json.dumps(identity.get("parameters") or {}, ensure_ascii=False),
            identity.get("engine"), identity.get("engineVersion"),
            identity.get("codeHash"), identity.get("source"), identity["createdTs"],
        ),
    )


# -- synchronous budgets -----------------------------------------------------


def sync_cost(kind: str, request) -> int:
    """How much work a study implies, in bar-evaluations.

    The number is the honest size of the job: a candidate search evaluates every
    candidate on two segments, and a walk-forward run repeats that search once per
    window. A portfolio multiplies a single run by its members.
    """
    if kind == "backtest":
        return int(getattr(request, "bars", 0) or 0)
    if kind == "validate":
        candidates = max(1, len(_parameter_combinations(request)))
        windows = max(1, int(request.walkForwardWindows))
        # Two segments per candidate, plus one search per walk-forward window.
        return int(request.bars) * candidates * (2 + windows)
    if kind == "portfolio":
        return int(getattr(request, "bars", 0) or 0) * max(1, len(request.symbols))
    return 0


def auto_queue(kind: str, request, *, label: str = "") -> tuple[dict, int] | None:
    """Queue a study that is too large to answer inline.

    The operator asked for a result, not for a refusal: when the work does not fit
    in a request, the same request goes to the run queue and the caller gets the
    run to watch. Returns `(run, cost)` when it queued the study, and None when the
    request fits and should run inline.
    """
    cost = sync_cost(kind, request)
    if cost <= SYNC_UNIT_BUDGET:
        return None
    from .backtest_runs import get_run_queue

    body = request.model_dump(mode="json") if hasattr(request, "model_dump") else dict(request)
    return get_run_queue().submit(kind, body, label=label), cost


def queued_response(run: dict, cost: int) -> dict:
    """The body a caller gets when their study was moved to the queue."""
    return {
        "queued": True,
        "run": run,
        "reason": (
            f"该研究预计 {int(cost):,} 单位工作量，超过同步上限 "
            f"{SYNC_UNIT_BUDGET:,} 单位，已自动转入后台队列"
        ),
        "syncCost": int(cost),
        "syncBudget": SYNC_UNIT_BUDGET,
        "detail": "结果与进度在结果中心查看；同一请求不会重复排队",
    }


def require_sync_budget(kind: str, request, *, budget: int = SYNC_UNIT_BUDGET) -> int:
    """Refuse a study too large for an HTTP request, and say where to run it.

    The queue exists because a study that takes minutes cannot be an interaction:
    the browser gives up, the operator sees nothing, and no record is kept. A
    request that exceeds the budget is not rejected - it is redirected.
    """
    cost = sync_cost(kind, request)
    if cost > budget:
        raise StudyError(
            "too_large",
            f"该研究预计 {cost:,} 单位工作量，超过同步上限 {budget:,} 单位。"
            "请改用结果中心的后台运行（POST /api/backtest/runs），它会记录进度、结果与失败原因。",
            status=409,
            detail={
                "title": "该研究必须放到后台队列",
                "detail": f"预计工作量 {cost:,} 单位，同步上限 {budget:,} 单位",
                "action": "改用结果中心的后台运行（POST /api/backtest/runs），或用默认行为自动转入后台",
                "syncCost": cost,
                "syncBudget": budget,
                "autoQueueAvailable": True,
            },
        )
    return cost


# -- the three studies -------------------------------------------------------


def run_single(db: Database, request: BacktestRequest, *, progress: Progress | None = None) -> dict:
    """One strategy over one contract's stored history."""
    spec = instrument_of(request.symbol)
    check_timeframe(request.timeframe)
    if progress:
        progress(0.05, "读取本地历史并过数据门禁")

    if request.candles:
        candles = [
            {"ts": row.time, "open": row.open, "high": row.high, "low": row.low,
             "close": row.close, "volume": row.volume}
            for row in request.candles
        ]
        source = "upload"
        research = None
        history, risk_profile = load_history_supporting(db, spec, request, candles)
    else:
        research = load_or_refuse(
            db, spec, request,
            action="补齐数据（fetch history / 历史数据页的矩阵回填）后重试，或显式允许降级模式",
        )
        candles = research.candles
        source = "bybit-local"
        history = research.history
        risk_profile = research.risk_profile

    candles = sorted(candles, key=lambda row: int(row["ts"]))
    if len(candles) < MIN_CANDLES:
        raise StudyError("invalid", f"有效K线只有 {len(candles)} 根，至少需要 {MIN_CANDLES} 根")

    meta = instrument_meta(db, spec)
    venue_cap = meta.get("maxLeverage")
    if venue_cap and request.leverage > float(venue_cap):
        raise StudyError(
            "invalid",
            f"{spec.venue_symbol} 交易所允许的最高杠杆是 {float(venue_cap):g}x，请求 {request.leverage:g}x",
        )
    slippage_default, fee_default = paper_defaults(spec)
    last_close = float(candles[-1]["close"])
    min_notional = max(
        float(meta.get("minNotionalValue") or 0),
        float(meta.get("minOrderQty") or 0) * last_close,
        1.0,
    )

    strategy_parameters = dict(request.strategyParams)
    if request.strategyId == "ma_cross":
        strategy_parameters.setdefault("fastPeriod", request.fastPeriod)
        strategy_parameters.setdefault("slowPeriod", request.slowPeriod)

    config = BacktestConfig(
        strategy_id=request.strategyId,
        strategy_params=strategy_parameters,
        fast_period=request.fastPeriod,
        slow_period=request.slowPeriod,
        direction=request.direction,
        initial_capital=request.initialCapital,
        allocation_pct=request.allocationPct,
        fee_bps=request.feeBps if request.feeBps is not None else fee_default,
        slippage_bps=request.slippageBps if request.slippageBps is not None else slippage_default,
        leverage=request.leverage,
        maintenance_margin_rate=request.maintenanceMarginRate,
        include_funding=request.includeFunding,
        include_liquidation=request.includeLiquidation,
        fill_on_thin=request.fillOnThin,
        slippage_model=request.slippageModel,
        impact_coefficient=request.impactCoefficient,
        latency_bars=int(getattr(request, "latencyBars", 0) or 0),
        partial_fill=str(getattr(request, "partialFill", "ignore") or "ignore"),
        max_participation=float(getattr(request, "maxParticipation", 1.0) or 1.0),
        tick_size=meta.get("tickSize"),
        qty_step=meta.get("qtyStep"),
        min_order_notional=min_notional,
        **execution_knobs(request),
    )

    # CPA's higher-timeframe view must be the same object used by signal
    # generation and by the result attachment. Previously it was computed only after
    # the backtest, so requireHigherTimeframe never affected actual orders.
    cpa_phases = None
    cpa_records = None
    cpa_higher_trends = None
    if request.strategyId == "cpa_cycle":
        from .strategy import cpa

        management_interval, background_interval = cpa.higher_intervals_for(request.timeframe)
        management = None if source == "upload" else _higher_bars(db, spec, management_interval)
        background = (
            management if background_interval == management_interval
            else (None if source == "upload" else _higher_bars(db, spec, background_interval))
        )
        cpa_phases = cpa.analyze(
            symbol=spec.venue_symbol, display_symbol=spec.display_symbol,
            interval=request.timeframe, product_type=spec.product_type, bars=candles,
            parameters=strategy_parameters, management_bars=management,
            background_bars=background, snapshot_hash=str(history.version or ""),
            data_version=str(history.version or ""),
        )
        cpa_records = cpa_phases.records
        cpa_higher_trends = [
            row.higher.trend if row.higher is not None and row.higher.available else None
            for row in cpa_records
        ]

    if progress:
        progress(0.3, f"生成信号并回放 {len(candles):,} 根K线")
    try:
        from .strategy.registry import cpa_intents, wants_intent_model

        # One run, one signal path. `positionModel=intent` swaps the event series for
        # the structured intent series; everything else about the run is unchanged, so
        # the two models are comparable on the same data, costs and execution config.
        intent_mode = wants_intent_model(request.strategyId, strategy_parameters)
        strategy_warnings: list[str] = []
        intents = None
        events = None
        resolved_cpa: dict = {}
        if intent_mode:
            # Resolve once, from the same asset class and interval the intent generator
            # uses. Reading the raw request here would miss every CPA default - the run
            # would silently execute without the risk budget the catalogue advertises.
            from .strategy.cpa import resolve_parameters as resolve_cpa_parameters

            resolved_cpa = resolve_cpa_parameters(
                strategy_parameters, asset_class=spec.product_type, interval=request.timeframe
            )
            intents = cpa_intents(
                candles, resolved_cpa,
                asset_class=spec.product_type, interval=request.timeframe,
                records=cpa_records, higher_trends=cpa_higher_trends,
            )
        else:
            registry = StrategyRegistry(PluginManager(quantdesk_home()))
            events, strategy_warnings = registry.generate(
                request.strategyId,
                candles,
                symbol=spec.venue_symbol,
                timeframe=request.timeframe,
                parameters=strategy_parameters,
                asset_class=spec.product_type,
                records=cpa_records,
                higher_trends=cpa_higher_trends,
            )
        result = run_backtest(
            candles,
            config,
            funding=history.funding,
            marks=history.marks,
            risk_profile=risk_profile,
            instrument=meta,
            interval=request.timeframe,
            signal_events=events,
            position_intents=intents,
            max_portfolio_risk_pct=(
                float(resolved_cpa.get("maxPortfolioRiskPct") or 0) or None
                if intent_mode
                else None
            ),
            # 同一份预算也约束持仓的浮动风险。参数默认开启，关掉只用于对比；
            # 事件路径（position_intents 为 None）下引擎根本不会读它。
            enforce_open_risk=bool(resolved_cpa.get("enforceOpenRisk", True)),
            strategy_warnings=strategy_warnings,
        )
    except (ValueError, PluginError) as exc:
        raise StudyError("invalid", str(exc)) from exc

    result.data_quality["source"] = source
    result.data_quality["venueSymbol"] = spec.venue_symbol
    if research is not None:
        research.apply_to(result)
    payload = result.as_dict()
    payload["riskProfile"] = risk_profile.as_dict() if risk_profile is not None else None
    payload["snapshotVersion"] = history.version
    payload["history"] = history.provenance()
    payload.update(study_envelope(spec, request, research, config, candles, history, source))
    if request.strategyId == "cpa_cycle":
        # This is the exact phase context that generated the orders above.
        from .strategy import cpa

        phases = cpa_phases
        assert phases is not None
        payload["cpaPhases"] = phases.as_dict(limit=2000)
        position_model = str(phases.parameters.get("positionModel") or "single")
        order_report = (result.data_quality.get("positionIntents") or {})
        payload["cpaMeta"] = {
            "parameterVersion": phases.parameter_version,
            "dataVersion": phases.data_version,
            "higherIntervals": list(phases.higher_intervals),
            "warnings": list(phases.warnings),
            "positionModel": position_model,
            # The notice follows the mode: telling a reader "simplified single position"
            # about a run that filled adds and partial exits would understate it, and
            # the reverse would overstate a backtest as live trading.
            "simplePositionNotice": cpa.position_notice(phases.parameters),
        }
        if order_report:
            payload["cpaPosition"] = {
                key: value for key, value in order_report.items() if key != "records"
            }
            payload["cpaOrders"] = {
                "model": "intent",
                "parameterVersion": phases.parameter_version,
                "dataVersion": phases.data_version,
                "orders": order_report.get("orderFees") or [],
                "records": order_report.get("records") or [],
                "maxRiskCarried": order_report.get("maxRiskCarried"),
                "riskBudget": order_report.get("riskBudget"),
                "rejected": order_report.get("rejected"),
                "rejectedReasons": order_report.get("rejectedReasons") or [],
            }
    if progress:
        progress(0.95, "整理结果与引用版本")
    return payload


def _higher_bars(db: Database, spec: Any, interval: str) -> list[dict] | None:
    """Higher-timeframe bars for the CPA attachment, or None when unavailable."""
    if not interval:
        return None
    from .datahub.view import read_history

    try:
        slice_ = read_history(db, symbol=spec.venue_symbol, interval=interval, bars=400,
                              display_symbol=spec.display_symbol, product_type=spec.product_type,
                              with_funding=False, with_marks=False)
    except Exception:  # noqa: BLE001 - a missing backdrop is reported, not fatal
        return None
    return [
        {"ts": int(row["ts"]), "open": float(row["open"]), "high": float(row["high"]),
         "low": float(row["low"]), "close": float(row["close"]),
         "volume": float(row.get("volume") or 0.0)}
        for row in slice_.bars
    ]


def _parameter_grid(request: Any) -> dict[str, list[Any]]:
    """The grid as a cross product, which is the shape the search engine expects.

    `parameterGrid` when given, otherwise the two double-MA lists - so an existing
    request produces exactly the grid it used to, and a CPA request can search its own
    thresholds without a new request model.
    """
    generic = {key: list(values) for key, values in (getattr(request, "parameterGrid", {}) or {}).items()}
    if generic:
        return generic
    if getattr(request, "strategyId", "") == "ma_cross":
        return {"fastPeriod": list(request.fastGrid), "slowPeriod": list(request.slowGrid)}
    return {}


def _parameter_combinations(request: Any) -> list[dict[str, Any]]:
    """The parameter sets a validation searches, from the generic grid or the MA grids.

    `parameterGrid` wins when present: it is the strategy's own parameters, and the
    two double-MA grids only exist because they were the first strategy's shape. A
    request that still sends `fastGrid`/`slowGrid` gets exactly the combinations it
    used to get.
    """
    import itertools

    base = dict(getattr(request, "strategyParams", {}) or {})
    generic = {key: list(values) for key, values in (getattr(request, "parameterGrid", {}) or {}).items()}
    if generic:
        keys = sorted(generic)
        combinations = []
        for values in itertools.product(*(generic[key] for key in keys)):
            combination = dict(base)
            combination.update(dict(zip(keys, values)))
            combinations.append(combination)
        return combinations
    if getattr(request, "strategyId", "") == "ma_cross":
        return [
            {**base, "fastPeriod": fast, "slowPeriod": slow}
            for fast in request.fastGrid for slow in request.slowGrid if slow > fast
        ]
    return [base]


def run_validation(db: Database, request: ValidationRequest, *, progress: Progress | None = None) -> dict:
    """Train/validation/test split, walk-forward, overfit and leakage checks."""
    spec = instrument_of(request.symbol)
    check_timeframe(request.timeframe)
    if request.train + request.validation >= 1:
        raise StudyError("invalid", "训练与验证比例之和必须小于 1，给测试段留出数据")
    if progress:
        progress(0.05, "读取本地历史并过数据门禁")

    research = load_or_refuse(
        db, spec, request,
        action="补齐数据（fetch history / 历史数据页的矩阵回填）后重试，或显式允许降级模式",
    )
    candles = research.candles
    meta = instrument_meta(db, spec)
    slippage_default, fee_default = paper_defaults(spec)
    last_close = float(candles[-1]["close"])
    interval_ms = INTERVAL_MS.get(request.timeframe)

    from .risk import RiskBook  # noqa: F401 - kept for parity with the API path
    from .strategy.registry import generate_builtin_events, generate_events
    from .strategy.validation import (
        build_provenance,
        leakage_report,
        run_walk_forward,
        search_parameters,
        split_segments,
    )

    profile = research.risk_profile
    funding = research.funding
    marks = research.marks
    # What the search actually iterates: the strategy's own grid when it has one, the
    # two double-MA lists otherwise (see `_parameter_combinations`). The warmup has to
    # come from the parameters being searched, not from the double-MA lists, or a CPA
    # study would size its warmup from periods it never uses.
    search = _parameter_combinations(request)
    grid = _parameter_grid(request)
    longest = max(
        (int(value) for combination in search for key, value in combination.items()
         if key in ("slowPeriod", "emaSlow", "longSma", "pivotLookback", "contractionWindow",
                    "volumeWindow", "minBars")),
        default=request.slowGrid[-1] if request.slowGrid else 21,
    )
    warmup = longest + 2
    segments = split_segments(
        candles, train=request.train, validation=request.validation, warmup=warmup
    )
    config = BacktestConfig(
        strategy_id=request.strategyId,
        direction=request.direction,
        initial_capital=request.initialCapital,
        allocation_pct=request.allocationPct,
        fee_bps=fee_default,
        slippage_bps=slippage_default,
        leverage=request.leverage,
        slippage_model=request.slippageModel,
        latency_bars=int(getattr(request, "latencyBars", 0) or 0),
        partial_fill=str(getattr(request, "partialFill", "ignore") or "ignore"),
        tick_size=meta.get("tickSize"),
        qty_step=meta.get("qtyStep"),
        min_order_notional=max(float(meta.get("minOrderQty") or 0) * last_close, 1.0),
        **execution_knobs(request),
    )

    validation_management = validation_background = None
    if request.strategyId == "cpa_cycle":
        from .strategy import cpa

        management_interval, background_interval = cpa.higher_intervals_for(request.timeframe)
        validation_management = _higher_bars(db, spec, management_interval)
        validation_background = (
            validation_management if background_interval == management_interval
            else _higher_bars(db, spec, background_interval)
        )

    def source(series, parameters):
        if request.strategyId != "cpa_cycle":
            return generate_events(
                series, request.strategyId, parameters,
                asset_class=spec.product_type, interval=request.timeframe,
            )
        phase_series = cpa.analyze(
            symbol=spec.venue_symbol, display_symbol=spec.display_symbol,
            interval=request.timeframe, product_type=spec.product_type, bars=series,
            parameters=parameters, management_bars=validation_management,
            background_bars=validation_background,
        )
        higher_trends = [
            row.higher.trend if row.higher is not None and row.higher.available else None
            for row in phase_series.records
        ]
        return generate_events(
            series, request.strategyId, parameters,
            asset_class=spec.product_type, interval=request.timeframe,
            records=phase_series.records, higher_trends=higher_trends,
        )

    grid = _parameter_combinations(request)
    candidates = max(1, len(_parameter_combinations(request)))
    windows = max(1, int(request.walkForwardWindows))

    def on_candidate(index: int, total: int) -> None:
        if progress:
            progress(0.1 + 0.3 * (index / max(1, total)), f"参数搜索 {index}/{total} 组合")

    def on_window(index: int, total: int) -> None:
        if progress:
            progress(0.4 + 0.5 * (index / max(1, total)), f"滚动窗口 {index}/{total}")

    search = search_parameters(
        candles, config, grid, signal_source=source,
        train=segments[0], validation=segments[1], test=segments[2],
        funding=funding, marks=marks, risk_profile=profile, interval=request.timeframe,
        interval_ms=interval_ms, product_type=spec.product_type,
        on_candidate=on_candidate,
    )
    walk = run_walk_forward(
        candles, config, signal_source=source, grid=grid,
        windows=windows, train_fraction=0.5, warmup=warmup,
        funding=funding, marks=marks, risk_profile=profile, interval=request.timeframe,
        interval_ms=interval_ms, product_type=spec.product_type,
        on_window=on_window,
    )
    if progress:
        progress(0.92, "泄漏检查与引用版本")
    best = (search["best"] or {}).get("parameters") or {}
    signals = source(candles, best) if best else None
    leakage = leakage_report(
        candles, signal_source=source, parameters=best, interval_ms=interval_ms or 0, signals=signals
    )
    provenance = build_provenance(
        strategy_id=request.strategyId, parameters=best, candles=candles, config=config,
        symbol=spec.venue_symbol, interval=request.timeframe, risk_profile=profile,
        data_source="bybit-local",
    )
    # The search scores segments; a reader still wants the whole sample for the
    # parameters it selected - the curve, the trades, and what they cost. It is one
    # more backtest, not another search, and it is labelled as the selected-parameter
    # run so it is never mistaken for the out-of-sample figure.
    selected = _selected_run(
        candles, config, best, source=source, funding=funding, marks=marks,
        risk_profile=profile, meta=meta, interval=request.timeframe,
    )
    return {
        "symbol": spec.venue_symbol,
        "displaySymbol": spec.display_symbol,
        "interval": request.timeframe,
        "bars": len(candles),
        "segments": [segment.as_dict() for segment in segments],
        "parameterSearch": search,
        "walkForward": walk,
        "leakage": leakage,
        "selectedRun": selected,
        # One strategy, one cost model, one execution model for the whole study.
        "executionModel": (selected or {}).get("executionModel") or {},
        # The selected run's warnings - latency, a trimmed order, missing funding -
        # affect how the whole study reads, so they are repeated here rather than
        # buried inside the run they came from.
        "executionWarnings": list((selected or {}).get("warnings") or []),
        "provenance": provenance.as_dict(),
        "riskProfile": profile.as_dict() if profile is not None else None,
        **study_envelope(spec, request, research, config, candles, research.history, "bybit-local"),
    }


def _selected_run(candles: list[dict], config: BacktestConfig, parameters: dict, *,
                  source, funding, marks, risk_profile, meta: dict, interval: str) -> dict | None:
    """One whole-sample backtest of the parameters the validation selected."""
    if not parameters:
        return None
    from dataclasses import replace

    try:
        events = source(candles, parameters)
        result = run_backtest(
            candles,
            replace(config, strategy_params=parameters),
            funding=funding,
            marks=marks,
            risk_profile=risk_profile,
            instrument=meta,
            interval=interval,
            signal_events=events,
        )
    except Exception as exc:  # noqa: BLE001 - a missing curve must not fail the study
        return {"parameters": parameters, "error": f"{type(exc).__name__}: {exc}"}
    payload = result.as_dict()
    return {
        "parameters": parameters,
        # The execution model of the run these numbers came from: a validation
        # result is a backtest's numbers plus statistics about them, and the
        # assumptions that produced them must travel with the whole study.
        "executionModel": payload.get("execution_model") or {},
        "dataQuality": payload.get("data_quality") or {},
        "metrics": {
            key: payload.get(key)
            for key in ("initial_capital", "final_equity", "net_return_pct", "max_drawdown_pct",
                        "win_rate_pct", "profit_factor", "total_fees", "total_funding")
        },
        "equityCurve": payload.get("equity_curve") or [],
        "trades": payload.get("trades") or [],
        "warnings": payload.get("warnings") or [],
        "note": "选定参数在整段样本上的表现；判定仍以样本外测试段为准",
    }


def run_portfolio(db: Database, request: PortfolioRequest, *, progress: Progress | None = None) -> dict:
    """Run one strategy across several contracts and combine the books."""
    specs = [instrument_of(item) for item in request.symbols]
    check_timeframe(request.timeframe)
    if progress:
        progress(0.05, "对齐组合成员的公共区间")

    from .datahub.readiness import common_window, portfolio_readiness

    from_ts, to_ts, member_ranges = common_window(
        db, venue="bybit", symbols=[spec.venue_symbol for spec in specs],
        interval=request.timeframe, bars=request.bars,
        require_marks=bool(getattr(request, "useMarkPrice", True)
                           or getattr(request, "includeLiquidation", True)),
    )
    members = {}
    for index, spec in enumerate(specs):
        if progress:
            progress(0.05 + 0.15 * (index / max(1, len(specs))),
                     f"成员门禁 {index + 1}/{len(specs)}：{spec.venue_symbol}")
        members[spec.venue_symbol] = load_or_refuse(
            db, spec, request, action="补齐组合成员的历史数据后重试，或显式允许降级模式",
            from_ts=from_ts, to_ts=to_ts,
        )

    from .strategy.metrics import benchmark_buy_and_hold
    from .strategy.registry import generate_builtin_events, generate_events
    from .strategy.validation import run_portfolio, sessions_per_year

    interval_ms = INTERVAL_MS.get(request.timeframe)
    samples: dict[str, dict] = {}
    benchmarks: dict[str, Any] = {}
    envelopes: dict[str, dict] = {}
    for index, spec in enumerate(specs):
        if progress:
            progress(0.2 + 0.7 * (index / max(1, len(specs))),
                     f"回放成员 {index + 1}/{len(specs)}：{spec.venue_symbol}")
        research = members[spec.venue_symbol]
        candles = research.candles
        meta = instrument_meta(db, spec)
        slippage_default, fee_default = paper_defaults(spec)
        profile = research.risk_profile
        funding = research.funding
        marks = research.marks
        parameters = dict(request.strategyParams)
        events = generate_events(candles, request.strategyId, parameters,
                                 asset_class=spec.product_type, interval=request.timeframe)
        config = BacktestConfig(
            strategy_id=request.strategyId,
            strategy_params=parameters,
            direction=request.direction,
            initial_capital=request.initialCapital,
            allocation_pct=request.allocationPct,
            fee_bps=fee_default,
            slippage_bps=slippage_default,
            leverage=request.leverage,
            slippage_model=str(getattr(request, "slippageModel", "fixed") or "fixed"),
            latency_bars=int(getattr(request, "latencyBars", 0) or 0),
            partial_fill=str(getattr(request, "partialFill", "ignore") or "ignore"),
            max_participation=float(getattr(request, "maxParticipation", 1.0) or 1.0),
            tick_size=meta.get("tickSize"),
            qty_step=meta.get("qtyStep"),
            min_order_notional=max(float(meta.get("minOrderQty") or 0) * float(candles[-1]["close"]), 1.0),
            **execution_knobs(request),
        )
        result = run_backtest(
            candles, config, funding=funding, marks=marks, risk_profile=profile,
            instrument=meta, interval=request.timeframe, signal_events=events,
        )
        payload = result.as_dict()
        payload["productType"] = spec.product_type
        envelopes[spec.venue_symbol] = study_envelope(
            spec, request, research, config, candles, research.history, "bybit-local"
        )
        samples[spec.venue_symbol] = payload
        benchmarks[spec.venue_symbol] = benchmark_buy_and_hold(
            candles,
            initial_capital=request.initialCapital,
            interval_ms=interval_ms,
            calendar_days_per_year=sessions_per_year(spec.product_type),
            fee_bps=fee_default,
        )
    if progress:
        progress(0.92, "合并组合账本与覆盖度")
    combined = run_portfolio(
        samples,
        weights=request.weights,
        initial_capital=request.initialCapital,
        interval_ms=interval_ms,
        benchmarks=benchmarks,
    )
    coverage = portfolio_readiness(members, member_ranges, from_ts, to_ts)
    for note in coverage["notes"]:
        combined.setdefault("warnings", [])
        if note not in combined["warnings"]:
            combined["warnings"].append(note)
    member_missing = sorted({item for data in members.values() for item in data.readiness.missing})
    member_impacts = [
        f"{symbol}：{impact}"
        for symbol, data in sorted(members.items())
        for impact in data.readiness.impacts
    ]
    combined["missingData"] = sorted(set(combined.get("missingData") or []) | set(member_missing))
    combined["dataImpacts"] = member_impacts + list(coverage["notes"])
    if not coverage["comparable"]:
        combined["degraded"] = True
        combined["missingData"] = sorted({*combined["missingData"], "portfolioComparability"})
    combined["memberVersions"] = {symbol: data.versions for symbol, data in members.items()}
    combined["portfolioCoverage"] = coverage
    combined["members"] = envelopes
    combined["strategyVersion"] = strategy_version_block({
        "strategy_id": request.strategyId,
        "parameters": dict(request.strategyParams or {}),
        "engine": "quantdesk.backtest.run_backtest",
        "engine_version": "2",
    })
    combined["costModel"] = {
        "perMember": {symbol: envelope.get("costModel") or {} for symbol, envelope in envelopes.items()}
    }
    member_models = {symbol: sample.get("execution_model") or {} for symbol, sample in samples.items()}
    distinct = {json.dumps(model, sort_keys=True, ensure_ascii=False) for model in member_models.values()}
    combined["executionModel"] = (
        next(iter(member_models.values())) if len(distinct) == 1 and member_models
        else {"perMember": member_models}
    )
    combined["degraded"] = bool(combined.get("degraded") or any(d.degraded for d in members.values()))
    combined["dataReady"] = not combined["degraded"]
    from .datahub.readiness import read_range_of

    combined["readRange"] = read_range_of(
        request.timeframe, from_ts, to_ts,
        (int(to_ts) - int(from_ts)) // max(1, INTERVAL_MS.get(request.timeframe, 1)) + 1,
    )
    return combined


def _run_factor_study(db: Database, request: FactorRequest, *,
                      progress: Progress | None = None) -> dict:
    """Factors are computed by their own module; this is the queue's door to it.

    The import is inside the call because the factor service imports this module
    for `StudyError`: a top-level import would be a cycle, and an import-order
    dependency would decide whether a queued factor run works.
    """
    from .factors import run_factors

    return run_factors(db, request, progress=progress)


def _run_campaign_round(
    db: Database, request: "CampaignRoundRequest", *, progress: Progress | None = None
) -> dict:
    """Run the campaign's next round. The campaign owns its own budget and stopping."""
    from .agent_campaign import run_round

    home = db.path.parent if getattr(db, "path", None) is not None else quantdesk_home()
    return run_round(db, request.campaign, home=home, progress=progress)


def _run_cpa_ablation(db: Database, request: "CpaAblationRequest", *,
                      progress: Progress | None = None) -> dict:
    """Every CPA variant against every contract of one group."""
    from .strategy.cpa.study import run_ablation

    return run_ablation(db, group=request.group, interval=request.interval,
                        bars=request.bars, allow_degraded=request.allowDegraded,
                        progress=progress)


STUDIES: dict[str, Callable[..., dict]] = {
    "backtest": run_single,
    "validate": run_validation,
    "portfolio": run_portfolio,
    "factors": _run_factor_study,
    "campaign": _run_campaign_round,
    "cpa_ablation": _run_cpa_ablation,
}


def run_study(db: Database, kind: str, request: BaseModel, *, progress: Progress | None = None) -> dict:
    """Run one study of any kind; the queue and the API both come through here."""
    runner = STUDIES.get(kind)
    if runner is None:
        raise StudyError("invalid", f"不支持的研究类型：{kind}；可用：{', '.join(STUDIES)}")
    return runner(db, request, progress=progress)


def parse_request(kind: str, body: dict) -> BaseModel:
    """Parse a stored request body back into its typed model."""
    model = REQUEST_MODELS.get(kind)
    if model is None:
        raise StudyError("invalid", f"不支持的研究类型：{kind}")
    try:
        return model.model_validate(body)
    except Exception as exc:  # noqa: BLE001 - pydantic raises ValidationError
        raise StudyError("invalid", f"请求参数无效：{exc}") from exc
