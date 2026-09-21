"""Built-in and external strategy catalog with one event-series contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..plugins import PluginError, PluginManager, PluginRegistry, StrategyGenerateRequest


@dataclass(frozen=True)
class ParameterSpec:
    key: str
    label: str
    type: str
    default: int | float | bool | str
    minimum: float | None = None
    maximum: float | None = None
    options: tuple[str, ...] = ()
    # Added for the CPA parameters page: a unit and a sentence of help make a
    # threshold reviewable. Both default to empty so every existing strategy keeps
    # the exact catalogue entry it had.
    unit: str = ""
    help: str = ""


@dataclass(frozen=True)
class StrategySpec:
    id: str
    name: str
    description: str
    source: str = "builtin"
    plugin_id: str | None = None
    parameters: tuple[ParameterSpec, ...] = field(default_factory=tuple)
    # The rule-set version a result must cite. Empty for strategies whose rules have
    # no separate version (the built-ins), which keeps their catalogue entry unchanged.
    parameter_version: str = ""
    notices: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["parameters"] = [asdict(item) for item in self.parameters]
        value["notices"] = list(self.notices)
        value["parameterVersion"] = self.parameter_version
        return value


def _cpa_spec() -> StrategySpec:
    """The CPA strategy's catalogue entry, built from the rule set's own schema.

    The parameters are not duplicated here: they come from `cpa.PARAMETER_SPECS`, so a
    threshold cannot be published with one default and used with another.
    """
    from .cpa import PARAMETER_SPECS, PARAMETER_VERSION, SIMPLE_POSITION_NOTICE, STRATEGY_NAME

    parameters = tuple(
        ParameterSpec(
            key=spec["key"], label=spec["label"], type=spec["type"], default=spec["default"],
            minimum=spec.get("minimum"), maximum=spec.get("maximum"),
            options=tuple(spec.get("options") or ()), unit=spec.get("unit") or "",
            help=spec.get("help") or "",
        )
        for spec in PARAMETER_SPECS
    )
    return StrategySpec(
        id="cpa_cycle",
        name=STRATEGY_NAME,
        description=(
            "按价格周期阶段（反转延伸、楔形突破、均线回踩、平台突破、延伸衰竭、楔形下跌）"
            "识别并进行单仓位回测。概念来源于 Oliver Kell 公开描述的 Cycle of Price Action，"
            "阈值为 QuantDesk 研究参数。"
        ),
        parameters=parameters,
        parameter_version=PARAMETER_VERSION,
        notices=(SIMPLE_POSITION_NOTICE,),
    )


BUILTIN_STRATEGIES: tuple[StrategySpec, ...] = (
    StrategySpec(
        id="ma_cross",
        name="双均线交叉",
        description="快慢均线在收盘后交叉，下一根K线开盘成交。",
        parameters=(
            ParameterSpec("fastPeriod", "快线周期", "integer", 9, 2, 200),
            ParameterSpec("slowPeriod", "慢线周期", "integer", 21, 3, 500),
        ),
    ),
    StrategySpec(
        id="channel_breakout",
        name="价格通道突破",
        description="收盘价突破此前价格通道后入场，反向突破时退出或反手。",
        parameters=(ParameterSpec("lookback", "通道周期", "integer", 20, 5, 200),),
    ),
    StrategySpec(
        id="buy_hold",
        name="买入并持有（基准）",
        description="第一根已收盘K线开仓后一直持有，用作所有策略的对照基准。",
        parameters=(),
    ),
    _cpa_spec(),
    StrategySpec(
        id="rsi_reversal",
        name="RSI 反转",
        description="RSI 从超卖区回升做多，从超买区回落做空。",
        parameters=(
            ParameterSpec("period", "RSI周期", "integer", 14, 2, 100),
            ParameterSpec("oversold", "超卖线", "number", 30, 1, 49),
            ParameterSpec("overbought", "超买线", "number", 70, 51, 99),
        ),
    ),
)


def _integer(parameters: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(parameters.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"策略参数 {key} 必须是整数") from exc
    if not low <= value <= high:
        raise ValueError(f"策略参数 {key} 必须在 {low} 到 {high} 之间")
    return value


def _number(parameters: dict[str, Any], key: str, default: float, low: float, high: float) -> float:
    try:
        value = float(parameters.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"策略参数 {key} 必须是数字") from exc
    if not low <= value <= high:
        raise ValueError(f"策略参数 {key} 必须在 {low:g} 到 {high:g} 之间")
    return value


def _ma(values: list[float], period: int) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= period:
            total -= values[index - period]
        if index >= period - 1:
            output[index] = total / period
    return output


def _rsi(values: list[float], period: int) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return output
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    output[period] = 100.0 if average_loss == 0 else 100 - 100 / (1 + average_gain / average_loss)
    for index in range(period + 1, len(values)):
        average_gain = (average_gain * (period - 1) + gains[index - 1]) / period
        average_loss = (average_loss * (period - 1) + losses[index - 1]) / period
        output[index] = 100.0 if average_loss == 0 else 100 - 100 / (1 + average_gain / average_loss)
    return output


def generate_events(
    candles: list[dict],
    strategy_id: str,
    parameters: dict[str, Any],
    *,
    asset_class: str = "stock",
    interval: str = "",
    records: list[Any] | None = None,
    higher_trends: list[str | None] | None = None,
) -> list[int | None]:
    """The one place a strategy id becomes an event series.

    Everything that needs signals - backtest, validation, portfolio, CLI - comes
    through here. The report called this out as a correctness issue and it was real:
    when the validation and portfolio paths generated their own events, a strategy
    available in one study silently behaved differently in another.
    """
    if strategy_id == "cpa_cycle":
        from .cpa import events_for, resolve_parameters

        resolved = resolve_parameters(parameters, asset_class=asset_class, interval=interval)
        return events_for(candles, resolved, records=records, higher_trends=higher_trends)
    return generate_builtin_events(candles, strategy_id, parameters)


def cpa_intents(
    candles: list[dict],
    parameters: dict[str, Any],
    *,
    asset_class: str = "stock",
    interval: str = "",
    records: list[Any] | None = None,
    higher_trends: list[str | None] | None = None,
) -> list[Any]:
    """The CPA intent series for a run configured with `positionModel=intent`.

    Kept beside `generate_events` so both models come from the same resolved
    parameters: a run cannot search one parameter set and execute another.
    """
    from .cpa import intents_for, resolve_parameters

    resolved = resolve_parameters(parameters, asset_class=asset_class, interval=interval)
    if str(resolved.get("positionModel") or "single") != "intent":
        raise ValueError("cpa_intents 只在 positionModel=intent 时使用")
    return intents_for(candles, resolved, records=records, higher_trends=higher_trends)


def wants_intent_model(strategy_id: str, parameters: dict[str, Any] | None) -> bool:
    """Whether this run asked for the structured position model."""
    return strategy_id == "cpa_cycle" and str(
        (parameters or {}).get("positionModel") or "single"
    ) == "intent"


def generate_builtin_events(
    candles: list[dict], strategy_id: str, parameters: dict[str, Any]
) -> list[int | None]:
    """Return action events aligned with candles: 1 long, -1 short, 0 flat, None hold."""
    closes = [float(row["close"]) for row in candles]
    events: list[int | None] = [None] * len(candles)
    if strategy_id == "ma_cross":
        fast_period = _integer(parameters, "fastPeriod", 9, 2, 200)
        slow_period = _integer(parameters, "slowPeriod", 21, 3, 500)
        if slow_period <= fast_period:
            raise ValueError("慢线周期必须大于快线周期")
        fast, slow = _ma(closes, fast_period), _ma(closes, slow_period)
        for index in range(1, len(candles)):
            if None in (fast[index - 1], slow[index - 1], fast[index], slow[index]):
                continue
            if fast[index - 1] <= slow[index - 1] and fast[index] > slow[index]:
                events[index] = 1
            elif fast[index - 1] >= slow[index - 1] and fast[index] < slow[index]:
                events[index] = -1
        return events
    if strategy_id == "buy_hold":
        # The comparison baseline every ablation is measured against. The entry signal
        # is held for the first few bars rather than only the first: the engine reads
        # the signal of bar `i - 1 - latency` when it acts on bar `i`, so a lone event
        # on bar 0 asks it to enter on bar 1 - and a single-bar window is a fragile
        # place to put the baseline. Repeating it makes the entry unambiguous without
        # changing what the baseline means (one entry, held to the end).
        for index in range(min(3, len(candles))):
            events[index] = 1
        return events
    if strategy_id == "channel_breakout":
        lookback = _integer(parameters, "lookback", 20, 5, 200)
        for index in range(lookback + 1, len(candles)):
            upper = max(float(row["high"]) for row in candles[index - lookback:index])
            lower = min(float(row["low"]) for row in candles[index - lookback:index])
            previous_upper = max(float(row["high"]) for row in candles[index - lookback - 1:index - 1])
            previous_lower = min(float(row["low"]) for row in candles[index - lookback - 1:index - 1])
            if closes[index] > upper and closes[index - 1] <= previous_upper:
                events[index] = 1
            elif closes[index] < lower and closes[index - 1] >= previous_lower:
                events[index] = -1
        return events
    if strategy_id == "rsi_reversal":
        period = _integer(parameters, "period", 14, 2, 100)
        oversold = _number(parameters, "oversold", 30, 1, 49)
        overbought = _number(parameters, "overbought", 70, 51, 99)
        values = _rsi(closes, period)
        for index in range(period + 1, len(candles)):
            previous, current = values[index - 1], values[index]
            if previous is None or current is None:
                continue
            if previous <= oversold < current:
                events[index] = 1
            elif previous >= overbought > current:
                events[index] = -1
        return events
    if strategy_id == "cpa_cycle":
        # Reachable for callers that hold the raw function rather than the facade;
        # CPA resolves its own defaults, so it does not need the legacy branch above.
        from .cpa import events_for, resolve_parameters

        return events_for(candles, resolve_parameters(parameters, asset_class="stock", interval=""))
    raise ValueError(f"未知内置策略：{strategy_id}")


class StrategyRegistry:
    def __init__(self, manager: PluginManager):
        self.plugins = PluginRegistry(manager)

    def catalog(self) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        strategies = [item.as_dict() for item in BUILTIN_STRATEGIES]
        errors: list[dict[str, str]] = []
        for plugin in self.plugins.enabled("strategy"):
            try:
                described = self.plugins.describe_strategies(plugin)
                for item in described.strategies:
                    strategies.append(
                        {
                            **item.model_dump(),
                            "id": f"plugin:{plugin.manifest.id}:{item.id}",
                            "source": "plugin",
                            "plugin_id": plugin.manifest.id,
                        }
                    )
            except PluginError as exc:
                errors.append({"pluginId": plugin.manifest.id, "error": str(exc)})
        return strategies, errors

    def generate(
        self,
        strategy_id: str,
        candles: list[dict],
        *,
        symbol: str,
        timeframe: str,
        parameters: dict[str, Any],
        asset_class: str = "stock",
        records: list[Any] | None = None,
        higher_trends: list[str | None] | None = None,
    ) -> tuple[list[int | None], list[str]]:
        if not strategy_id.startswith("plugin:"):
            return generate_events(
                candles, strategy_id, parameters,
                asset_class=asset_class, interval=timeframe,
                records=records, higher_trends=higher_trends,
            ), []
        parts = strategy_id.split(":", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            raise ValueError("外部策略 ID 格式无效")
        plugin_id, external_id = parts[1], parts[2]
        result = self.plugins.generate_signals(
            plugin_id,
            StrategyGenerateRequest(
                strategyId=external_id,
                symbol=symbol,
                timeframe=timeframe,
                candles=[
                    {
                        "time": int(row["ts"]),
                        "open": row["open"],
                        "high": row["high"],
                        "low": row["low"],
                        "close": row["close"],
                        "volume": row.get("volume", 0),
                    }
                    for row in candles
                ],
                parameters=parameters,
            ),
        )
        by_time = {int(row["ts"]): index for index, row in enumerate(candles)}
        events: list[int | None] = [None] * len(candles)
        direction = {"long": 1, "short": -1, "flat": 0}
        for signal in result.signals:
            index = by_time.get(signal.time)
            if index is not None:
                events[index] = direction[signal.direction]
        return events, result.warnings

