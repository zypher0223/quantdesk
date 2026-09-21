"""Typed business messages exchanged with external QuantDesk plugins."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class PluginCandle(BaseModel):
    time: int = Field(gt=0)
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    turnover: float | None = Field(None, ge=0)

    @model_validator(mode="after")
    def validate_range(self):
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high 小于 OHLC 其他价格")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low 大于 OHLC 其他价格")
        return self


class DataCandlesRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=64)
    timeframe: Literal["15m", "1h", "4h", "1d", "1w"]
    startTime: int | None = Field(None, gt=0)
    endTime: int | None = Field(None, gt=0)
    limit: int = Field(400, ge=1, le=5000)

    @model_validator(mode="after")
    def validate_time_range(self):
        if self.startTime and self.endTime and self.startTime > self.endTime:
            raise ValueError("startTime 不能晚于 endTime")
        return self


class DataCandlesResult(BaseModel):
    source: str = Field(min_length=1, max_length=120)
    candles: list[PluginCandle]
    complete: bool = True
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_order(self):
        times = [item.time for item in self.candles]
        if times != sorted(set(times)):
            raise ValueError("candles 必须按 time 升序且不能重复")
        return self


class StrategyParameter(BaseModel):
    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=80)
    type: Literal["integer", "number", "boolean", "select"]
    default: int | float | bool | str
    minimum: float | None = None
    maximum: float | None = None
    options: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_shape(self):
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum 不能大于 maximum")
        if self.type == "select":
            if not self.options:
                raise ValueError("select 参数必须声明 options")
            if str(self.default) not in self.options:
                raise ValueError("select 参数的 default 必须在 options 中")
        if self.type == "boolean" and not isinstance(self.default, bool):
            raise ValueError("boolean 参数的 default 必须是布尔值")
        return self


class StrategyDescription(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    parameters: list[StrategyParameter] = Field(default_factory=list)


class StrategyDescribeResult(BaseModel):
    strategies: list[StrategyDescription]


class StrategyGenerateRequest(BaseModel):
    strategyId: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=64)
    timeframe: Literal["15m", "1h", "4h", "1d", "1w"]
    candles: list[PluginCandle] = Field(min_length=3, max_length=5000)
    parameters: dict[str, Any] = Field(default_factory=dict)


class StrategySignal(BaseModel):
    time: int = Field(gt=0)
    direction: Literal["long", "short", "flat"]
    strength: float = Field(0, ge=0, le=1)
    reason: str = Field(default="", max_length=500)


class StrategyGenerateResult(BaseModel):
    signals: list[StrategySignal]
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_signals(self):
        times = [item.time for item in self.signals]
        if len(times) != len(set(times)):
            raise ValueError("同一根K线只能返回一个信号")
        return self


class ResearchCollectRequest(BaseModel):
    """One research question about one contract.

    The mapping and the provider plan are decided by the engine and handed over,
    so an adapter never invents its own ticker translation or silently picks a
    provider the operator did not name.
    """

    symbol: str = Field(min_length=1, max_length=64)
    tradeDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    topics: list[str] = Field(default_factory=list, max_length=20)
    mapping: dict[str, Any] = Field(default_factory=dict)
    # Ordered provider plan for these topics: primary first, then the configured
    # fallbacks. Empty means no provider is configured, which is reported rather
    # than guessed.
    providers: list[str] = Field(default_factory=list, max_length=8)
    openbbBaseUrl: str = Field(default="", max_length=300)


class ResearchEvidence(BaseModel):
    """One external research reading, with the provenance that makes it auditable.

    v1 carried only key/label/value/source/observedAt. The optional fields below
    are what a point-in-time check needs: which provider and endpoint produced the
    reading, when it was published, and when it stops being current.
    """

    key: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,120}$")
    label: str = Field(min_length=1, max_length=120)
    value: Any
    source: str = Field(min_length=1, max_length=300)
    observedAt: str
    provider: str = Field(default="", max_length=64)
    endpoint: str = Field(default="", max_length=160)
    asOf: str = Field(default="", max_length=40)
    publishedAt: str = Field(default="", max_length=40)
    expiresAt: str = Field(default="", max_length=40)
    contentHash: str = Field(default="", max_length=128)
    pointInTime: bool = False
    warnings: list[str] = Field(default_factory=list)


class ResearchCollectResult(BaseModel):
    evidence: list[ResearchEvidence]
    warnings: list[str] = Field(default_factory=list)
    # Named so a caller can tell "the provider had nothing" from "the provider
    # was never asked": a missing reading must not be filled with a guess.
    unavailable: list[dict[str, Any]] = Field(default_factory=list)


class AnalyticsPosition(BaseModel):
    """One position as QuantDesk holds it, in the contract's own code."""

    symbol: str = Field(min_length=1, max_length=64)
    group: str = Field(default="", max_length=64)
    side: Literal["long", "short"]
    quantity: float
    entryPrice: float = Field(gt=0)
    markPrice: float = Field(gt=0)
    notional: float = Field(ge=0)
    margin: float = Field(ge=0)


class AnalyticsReturnPoint(BaseModel):
    time: int = Field(gt=0)
    value: float = Field(alias="return")

    model_config = {"populate_by_name": True}


class AnalyticsPortfolioRequest(BaseModel):
    asOf: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
    baseCurrency: str = Field(default="USDT", min_length=2, max_length=12)
    confidence: float = Field(0.95, gt=0, lt=1)
    positions: list[AnalyticsPosition] = Field(default_factory=list, max_length=200)
    returns: dict[str, list[AnalyticsReturnPoint]] = Field(default_factory=dict)
    # QuantDesk's market-data version, carried so a stored result can be tied back
    # to the exact snapshot it was computed from.
    marketSnapshotVersion: str = Field(default="", max_length=120)
    riskFreeRate: float = Field(default=0.0)


class AnalyticsRiskContribution(BaseModel):
    symbol: str = Field(min_length=1, max_length=64)
    value: float
    percentage: float


class AnalyticsCorrelation(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    matrix: list[list[float]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_square(self):
        size = len(self.symbols)
        if size and len(self.matrix) != size:
            raise ValueError("相关性矩阵行数与 symbols 数量不一致")
        for row in self.matrix:
            if len(row) != size:
                raise ValueError("相关性矩阵必须是方阵")
        return self


class AnalyticsMetrics(BaseModel):
    volatility: float | None = None
    var: float | None = None
    cvar: float | None = None
    maxDrawdown: float | None = None
    # Provider extras (Sharpe, beta, …) stay open so a provider addition does not
    # need a schema change; the named metrics above are the contract.
    extra: dict[str, Any] = Field(default_factory=dict)


class AnalyticsOptimization(BaseModel):
    method: str = Field(default="", max_length=80)
    weights: dict[str, float] = Field(default_factory=dict)
    expectedReturn: float | None = None
    expectedVolatility: float | None = None
    notes: list[str] = Field(default_factory=list)


class AnalyticsPortfolioResult(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    asOf: str = Field(min_length=1, max_length=40)
    # A refusal is a valid, structured answer: the engine reports it as a missing
    # analysis instead of treating it as a broken plugin.
    unavailable: str = Field(default="", max_length=400)
    metrics: AnalyticsMetrics = Field(default_factory=AnalyticsMetrics)
    riskContributions: list[AnalyticsRiskContribution] = Field(default_factory=list)
    correlation: AnalyticsCorrelation | None = None
    optimization: AnalyticsOptimization | None = None
    source: str = Field(default="", max_length=200)
    requestId: str = Field(default="", max_length=120)
    warnings: list[str] = Field(default_factory=list)


class AnalyticsScenarioRequest(BaseModel):
    asOf: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
    baseCurrency: str = Field(default="USDT", min_length=2, max_length=12)
    confidence: float = Field(0.95, gt=0, lt=1)
    positions: list[AnalyticsPosition] = Field(default_factory=list, max_length=200)
    scenario: str = Field(min_length=1, max_length=80)
    shocks: dict[str, float] = Field(default_factory=dict)
    marketSnapshotVersion: str = Field(default="", max_length=120)


class AnalyticsScenarioPosition(BaseModel):
    symbol: str = Field(min_length=1, max_length=64)
    pnl: float
    pnlPct: float | None = None
    shockedPrice: float | None = None


class AnalyticsScenarioResult(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    asOf: str = Field(min_length=1, max_length=40)
    scenario: str = Field(min_length=1, max_length=80)
    unavailable: str = Field(default="", max_length=400)
    equityBefore: float | None = None
    equityAfter: float | None = None
    equityChange: float | None = None
    equityChangePct: float | None = None
    positions: list[AnalyticsScenarioPosition] = Field(default_factory=list)
    marginUsageBefore: float | None = None
    marginUsageAfter: float | None = None
    breachesAccountRisk: bool = False
    breachedLimits: list[str] = Field(default_factory=list)
    source: str = Field(default="", max_length=200)
    requestId: str = Field(default="", max_length=120)
    warnings: list[str] = Field(default_factory=list)


class NotificationEvent(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,80}$")
    severity: Literal["info", "warning", "critical"] = "info"
    title: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=4000)
    occurredAt: str
    symbol: str | None = Field(None, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)


class NotifyResult(BaseModel):
    delivered: bool
    destination: str = Field(default="", max_length=160)
    messageId: str | None = Field(None, max_length=200)
    detail: str = Field(default="", max_length=500)


# -- v3: factor research and backtest validation ------------------------
#
# The boundary these messages encode: QuantDesk supplies closed candles and its
# own finished backtest, and receives factors and statistical diagnostics back.
# A factor provider never prices anything, and a validator never recomputes the
# P&L - the engine's numbers are the truth the diagnostics describe.

class FactorDefinition(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{2,120}$")
    name: str = Field(min_length=1, max_length=120)
    family: str = Field(default="", max_length=60)
    mode: Literal["time_series", "cross_sectional"] = "time_series"
    requiredFields: list[str] = Field(default_factory=list, max_length=20)
    warmupBars: int = Field(0, ge=0, le=5000)
    supportedTimeframes: list[str] = Field(default_factory=list, max_length=10)
    implementationVersion: str = Field(default="", max_length=80)
    # Which data this factor needs beyond price. `openbb` marks a point-in-time
    # fundamental/macro input, which carries a publication time the engine checks.
    sources: list[Literal["bybit", "openbb", "derived"]] = Field(default_factory=list, max_length=5)
    formulaHash: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=500)


class FactorCatalogResult(BaseModel):
    factors: list[FactorDefinition]
    providerVersion: str = Field(default="", max_length=120)
    warnings: list[str] = Field(default_factory=list)


class FactorCandle(BaseModel):
    """One closed bar. The engine passes only bars it has already closed."""

    time: int = Field(gt=0)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    turnover: float | None = None


class FactorComputeRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=64)
    timeframe: Literal["15m", "1h", "4h", "1d", "1w"]
    snapshotHash: str = Field(default="", max_length=120)
    factorIds: list[str] = Field(min_length=1, max_length=64)
    candles: list[FactorCandle] = Field(default_factory=list, max_length=20_000)
    funding: list[dict[str, Any]] = Field(default_factory=list, max_length=20_000)
    openInterest: list[dict[str, Any]] = Field(default_factory=list, max_length=20_000)
    # Point-in-time auxiliary readings, already filtered by the engine's gate.
    auxiliary: list[dict[str, Any]] = Field(default_factory=list, max_length=2_000)
    parameters: dict[str, Any] = Field(default_factory=dict)


class FactorValue(BaseModel):
    time: int = Field(gt=0)
    value: float | None = None


class FactorSeries(BaseModel):
    factorId: str = Field(min_length=1, max_length=120)
    values: list[FactorValue] = Field(default_factory=list, max_length=20_000)
    implementationVersion: str = Field(default="", max_length=80)


class FactorComputeResult(BaseModel):
    snapshotHash: str = Field(default="", max_length=120)
    series: list[FactorSeries] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ValidationTrade(BaseModel):
    entryTime: int
    exitTime: int
    direction: Literal["long", "short"]
    netPnl: float
    returnPct: float = 0.0
    barsHeld: int = 0


class ValidationEquityPoint(BaseModel):
    time: int
    equity: float


class ValidationAnalyzeRequest(BaseModel):
    """A finished QuantDesk backtest, handed over for diagnostics.

    The trades and equity curve are read-only inputs: a validator reports on them
    and cannot alter them.
    """

    runId: str = Field(min_length=1, max_length=120)
    seed: int = Field(42, ge=0)
    interval: Literal["15m", "1h", "4h", "1d", "1w"] = "1h"
    equityCurve: list[ValidationEquityPoint] = Field(default_factory=list, max_length=200_000)
    trades: list[ValidationTrade] = Field(default_factory=list, max_length=100_000)
    benchmark: dict[str, Any] = Field(default_factory=dict)
    tests: dict[str, Any] = Field(default_factory=dict)


class ValidationInterval(BaseModel):
    low: float | None = None
    high: float | None = None
    confidence: float = Field(0.95, gt=0, lt=1)


class ValidationPathRisk(BaseModel):
    """Trade-order sensitivity. Explicitly *not* a significance test."""

    simulations: int = 0
    drawdown: ValidationInterval = Field(default_factory=ValidationInterval)
    maxConsecutiveLosses: ValidationInterval = Field(default_factory=ValidationInterval)
    finalEquity: ValidationInterval = Field(default_factory=ValidationInterval)
    interpretation: str = Field(
        default="路径风险模拟：仅重排已有交易的顺序，用来说明权益路径与回撤对顺序的敏感度，不构成策略显著性检验",
        max_length=500,
    )


class ValidationBootstrap(BaseModel):
    method: str = Field(default="moving_block", max_length=60)
    blockSize: int = Field(0, ge=0)
    resamples: int = 0
    sharpe: ValidationInterval = Field(default_factory=ValidationInterval)
    returnPct: ValidationInterval = Field(default_factory=ValidationInterval)
    maxDrawdown: ValidationInterval = Field(default_factory=ValidationInterval)
    positiveSharpeProbability: float | None = None


class ValidationRandomization(BaseModel):
    """The actual null hypothesis: signals carry no information."""

    method: str = Field(default="", max_length=80)
    permutations: int = 0
    observedSharpe: float | None = None
    pValue: float | None = None
    nullSharpe: ValidationInterval = Field(default_factory=ValidationInterval)


class ValidationMultipleTesting(BaseModel):
    trials: int = Field(0, ge=0)
    factorCount: int = Field(0, ge=0)
    parameterCombinations: int = Field(0, ge=0)
    deflatedSharpe: float | None = None
    probabilityOfBacktestOverfitting: float | None = None
    method: str = Field(default="", max_length=120)
    # True only when the correction was actually applied to this result.
    applied: bool = False
    note: str = Field(default="", max_length=400)


class ValidationAnalyzeResult(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    runId: str = Field(default="", max_length=120)
    algorithmVersion: str = Field(default="", max_length=80)
    seed: int = Field(0, ge=0)
    samples: int = Field(0, ge=0)
    pathRisk: ValidationPathRisk = Field(default_factory=ValidationPathRisk)
    bootstrap: ValidationBootstrap = Field(default_factory=ValidationBootstrap)
    randomization: ValidationRandomization = Field(default_factory=ValidationRandomization)
    multipleTesting: ValidationMultipleTesting = Field(default_factory=ValidationMultipleTesting)
    tailLoss: ValidationInterval = Field(default_factory=ValidationInterval)
    equityPercentiles: dict[str, list[float]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    unavailable: str = Field(default="", max_length=400)


# -- v4: the strategy improvement agent --------------------------------------
#
# The boundary this version draws, and why it is drawn so tightly:
#
# * "self-improving" means "searching", and a search that can write its own
#   scoring is not a search. So a proposal is **data** - factor ids from the
#   allowlist, parameters inside declared ranges, a rule template by name - and
#   never code, a path, or a dependency to install;
# * the agent never sees a result it could optimise against: the trial summaries
#   it receives are train/validation only. The test segment is sealed until the
#   campaign ends, which is the whole reason a campaign can be trusted;
# * nothing here lets a plugin report performance. A proposal has a hypothesis and
#   an expected failure mode, not a return.
#
# `extra="forbid"` on the proposal types is deliberate: a plugin that tries to
# send `netPnl`, `equityCurve` or `sharpe` alongside a proposal is refused loudly
# rather than quietly having the field dropped.


class AgentProposalSpace(BaseModel):
    """What a provider says it is willing to propose, before it proposes."""

    factorIds: list[str] = Field(default_factory=list, max_length=512)
    parameters: dict[str, list[float]] = Field(default_factory=dict, max_length=32)
    ruleTemplates: list[str] = Field(default_factory=list, max_length=32)
    maxProposalsPerRound: int = Field(32, ge=1, le=256)
    maxRounds: int = Field(5, ge=1, le=50)


class AgentManifestResult(BaseModel):
    """`agent.manifest`: the provider's boundaries, checked before any proposal."""

    agentVersion: str = Field(min_length=1, max_length=120)
    mode: Literal["deterministic_search", "llm_assisted"] = "deterministic_search"
    proposalSpace: AgentProposalSpace = Field(default_factory=AgentProposalSpace)
    requires: list[str] = Field(default_factory=list, max_length=16)
    never: list[str] = Field(default_factory=list, max_length=16)
    providerVersion: str = Field(default="", max_length=120)
    warnings: list[str] = Field(default_factory=list, max_length=32)
    unavailable: str = Field(default="", max_length=400)


class AgentTrialSummary(BaseModel):
    """One evaluated proposal, as the agent is allowed to see it.

    `segment` is deliberately restricted to the two segments a search may look at.
    A provider asking for the test segment is sent a validation error, not a
    result: the out-of-sample window is opened once, by the engine, at the end.
    """

    proposalId: str = Field(min_length=1, max_length=64)
    segment: Literal["train", "validation"] = "validation"
    sharpe: float | None = None
    returnPct: float | None = None
    maxDrawdownPct: float | None = None
    trades: int | None = Field(None, ge=0)
    verdict: str = Field(default="", max_length=40)
    reason: str = Field(default="", max_length=400)


class AgentBudget(BaseModel):
    proposals: int = Field(32, ge=1, le=256)
    deadlineMs: int = Field(600_000, ge=1_000, le=3_600_000)


class AgentProposeRequest(BaseModel):
    """One round of proposals, with everything the agent may condition on."""

    campaignId: str = Field(min_length=1, max_length=64)
    round: int = Field(1, ge=1, le=50)
    snapshotHash: str = Field(default="", max_length=120)
    universe: list[str] = Field(default_factory=list, max_length=64)
    interval: Literal["15m", "1h", "4h", "1d", "1w"] = "1h"
    # Which group this campaign belongs to. Crypto and equity are never scored
    # against each other, and a cross-sectional factor needs the equity group.
    group: Literal["crypto", "equity", "mixed"] = "crypto"
    factorIds: list[str] = Field(default_factory=list, max_length=512)
    dataProfile: dict[str, Any] = Field(default_factory=dict)
    priorTrials: list[AgentTrialSummary] = Field(default_factory=list, max_length=256)
    budget: AgentBudget = Field(default_factory=AgentBudget)


class AgentProposal(BaseModel):
    """One candidate, as data. Nothing here is executable."""

    model_config = {"extra": "forbid"}

    proposalId: str = Field(min_length=1, max_length=64)
    kind: Literal["parameter_set", "factor_combo", "rule"] = "parameter_set"
    factorIds: list[str] = Field(default_factory=list, max_length=32)
    parameters: dict[str, float] = Field(default_factory=dict, max_length=32)
    rule: dict[str, Any] | None = None
    hypothesis: str = Field(min_length=1, max_length=400)
    expectedFailureMode: str = Field(default="", max_length=400)


class AgentProposeResult(BaseModel):
    proposals: list[AgentProposal] = Field(default_factory=list, max_length=256)
    warnings: list[str] = Field(default_factory=list, max_length=32)
    stopReason: str = Field(default="", max_length=200)


class AgentReflectRequest(BaseModel):
    """The round's outcome, fed back. Test-segment results never appear here.

    The campaign's context travels with the outcome, not only with the first
    proposal: without the frozen `factorIds` the provider would have to guess its own
    search space on every round after the first, and a proposal that left that space
    would be refused by the engine anyway. Reflection narrows an existing search; it
    does not get to widen it.
    """

    campaignId: str = Field(min_length=1, max_length=64)
    round: int = Field(1, ge=1, le=50)
    snapshotHash: str = Field(default="", max_length=120)
    universe: list[str] = Field(default_factory=list, max_length=64)
    interval: Literal["15m", "1h", "4h", "1d", "1w"] = "1h"
    group: Literal["crypto", "equity", "mixed"] = "crypto"
    factorIds: list[str] = Field(default_factory=list, max_length=512)
    dataProfile: dict[str, Any] = Field(default_factory=dict)
    trials: list[AgentTrialSummary] = Field(default_factory=list, max_length=256)
    budget: AgentBudget = Field(default_factory=AgentBudget)
    remainingRounds: int = Field(0, ge=0, le=50)


class AgentReflectResult(BaseModel):
    reflection: str = Field(default="", max_length=2000)
    proposals: list[AgentProposal] = Field(default_factory=list, max_length=256)
    stopReason: str = Field(default="", max_length=200)
    warnings: list[str] = Field(default_factory=list, max_length=32)
