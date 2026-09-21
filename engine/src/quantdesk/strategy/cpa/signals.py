"""From confirmed phases to signals, and the analysis entry point.

Two responsibilities, both small:

* `events_for` maps a phase series onto the engine's event contract - `1` long, `-1`
  short, `0` flat, `None` hold - placed at the bar the phase was confirmed on, so the
  engine fills at the *next* bar's open. Nothing here reaches into the next bar;
* `analyze` produces the whole reading: one interval's phases, the higher-timeframe
  context each bar was allowed to see, and the metadata a result must cite.

Position management is deliberately first-stage: one position, no scaling. An
`ema_crossback` or `base_n_break` that arrives while a position is already open is
recorded as an add-on *candidate* and produces no event, which is stated in the
series warnings rather than silently dropped. The structured position-intent model is
a later stage; until it exists the API says so.
"""

from __future__ import annotations

from typing import Any, Sequence

from ...backtest.engine import PositionIntent
from ..cpa import indicators as ind
from ..cpa import state_machine
from ..cpa.defaults import (
    HIGHER_TIMEFRAME_MAP,
    INTENT_POSITION_NOTICE,
    OBSERVATION_PHASES,
    PARAMETER_VERSION,
    POSITION_MODELS,
    STRATEGY_NAME,
    defaults_for,
)
from ..cpa.models import PhaseRecord, PhaseSeries
from ..cpa.detector import trend_view

SIMPLE_POSITION_NOTICE = (
    "当前回测尚未模拟 CPA 分批建仓与分批减仓，收益结果属于单仓位简化版本。"
)

# Events the engine understands, plus the hold value.
LONG, SHORT, FLAT, HOLD = 1, -1, 0, None


def events_for(
    bars: Sequence[dict],
    parameters: dict[str, Any],
    *,
    records: list[PhaseRecord] | None = None,
    higher_trends: Sequence[str | None] | None = None,
) -> list[int | None]:
    """Map confirmed phases to the engine's event series.

    `higher_trends`, when given, is the management timeframe's trend as of each bar
    (already aligned by the caller). With `requireHigherTimeframe` on, an entry is
    refused while that trend is explicitly bearish, and also while it is unknown -
    "not enough data" must not be read as permission.
    """
    series = records if records is not None else state_machine.run(bars, parameters)
    entry_stages = _entry_stages(parameters)
    side_mode = str(parameters.get("sideMode") or "long_only")
    exit_on_exhaustion = bool(parameters.get("exitOnExhaustion", True))
    require_higher = bool(parameters.get("requireHigherTimeframe", False))

    events: list[int | None] = [HOLD] * len(series)
    position = 0  # 1 long, -1 short, 0 flat
    for index, record in enumerate(series):
        # Exhaustion is an observation rather than a confirmed entry phase, but it
        # still acts at the bar where it is observed when the explicit exit switch is
        # on. Processing it inline preserves the position state at this exact bar.
        if (
            exit_on_exhaustion
            and record.phase == "exhaustion_extension"
            and record.status == "candidate"
            and position == 1
        ):
            events[index] = FLAT
            position = 0
            continue
        if record.status != "confirmed":
            continue
        trend = None if higher_trends is None else higher_trends[index]
        if record.phase in ("wedge_pop", "ema_crossback", "base_n_break"):
            if record.phase not in entry_stages:
                continue
            if require_higher and trend != "bullish":
                continue
            if position <= 0:
                events[index] = LONG
                position = 1
            # Already long: an add-on candidate, not an event. First stage keeps one
            # position, so nothing is emitted and the intent stays in the phase record.
            continue
        if record.phase == "exhaustion_extension":
            # Observations never act on their own; the exit switch is explicit.
            continue
        if record.phase == "wedge_drop":
            if position >= 0:
                if position > 0:
                    events[index] = FLAT
                position = 0
            if side_mode == "symmetric":
                events[index] = SHORT
                position = -1
            continue
        if record.phase in ("downside_ema_crossback", "downside_base_n_break"):
            if side_mode != "symmetric":
                continue
            if require_higher and trend is not None and trend == "bullish":
                continue
            if position >= 0:
                events[index] = SHORT
                position = -1
            continue

    return events


def _entry_stages(parameters: dict[str, Any]) -> tuple[str, ...]:
    raw = parameters.get("entryStages") or ["wedge_pop", "ema_crossback", "base_n_break"]
    if isinstance(raw, str):
        raw = [item.strip() for item in raw.split(",") if item.strip()]
    return tuple(raw)


def position_notice(parameters: dict[str, Any]) -> str:
    """The honest one-liner for the mode this run is in."""
    model = str((parameters or {}).get("positionModel") or "single")
    return INTENT_POSITION_NOTICE if model == "intent" else SIMPLE_POSITION_NOTICE


def intents_for(
    bars: Sequence[dict],
    parameters: dict[str, Any],
    *,
    records: list[PhaseRecord] | None = None,
    higher_trends: Sequence[str | None] | None = None,
) -> list[PositionIntent | None]:
    """Translate confirmed phases into structured position intents.

    What each phase asks for, and why:

    * `wedge_pop` -> `open` at the initial exposure, with the setup's structure low as
      the stop. It is the phase that starts a cycle, so it is what starts a position;
    * `ema_crossback` / `base_n_break` -> `open` when flat, `increase` when a position
      is already open. This is the "add-on candidate" the single-position version could
      only record; here it becomes an order;
    * `exhaustion_extension` -> `exit`, or a `reduce` when `exitOnExhaustion` is off
      (keep a runner at `reduceExposurePct` of the target);
    * `wedge_drop` -> `exit`;
    * downside phases -> a short `open`, and only under `sideMode=symmetric` - the same
      gate the event path uses, so the two models cannot disagree about direction;
    * a broken pivot -> `cancel` on the fill bar (drop the resting remainder) and
      `exit` afterwards.

    Intents are aligned with the bars and read with the engine's usual latency, so an
    intent on bar `i` fills at bar `i + 1`'s open. The generator tracks its own belief
    about the position purely to decide open-vs-increase; it never assumes a fill price.
    """
    series = records if records is not None else state_machine.run(bars, parameters)
    entry_stages = _entry_stages(parameters)
    side_mode = str(parameters.get("sideMode") or "long_only")
    exit_on_exhaustion = bool(parameters.get("exitOnExhaustion", True))
    require_higher = bool(parameters.get("requireHigherTimeframe", False))
    pivot_failure = bool(parameters.get("pivotFailureExit", True))
    initial = float(parameters.get("initialExposurePct") or 50.0)
    add_step = float(parameters.get("addExposurePct") or 25.0)
    keep_ratio = float(parameters.get("reduceExposurePct") or 50.0)
    model = str(parameters.get("positionModel") or "single")
    if model not in POSITION_MODELS:
        raise ValueError(f"positionModel 只能是 {' 或 '.join(POSITION_MODELS)}：{model}")

    out: list[PositionIntent | None] = [None] * len(series)
    position = 0            # 1 long, -1 short, 0 flat (the generator's own belief)
    target_pct = 0.0
    pivot: float | None = None
    open_index: int | None = None
    for index, record in enumerate(series):
        trend = None if higher_trends is None else higher_trends[index]
        close = float(bars[index]["close"])
        phase, status = record.phase, record.status

        # A pivot that failed right after the entry: the resting remainder is dropped
        # before it can fill into a broken setup. Later failures close the position.
        if pivot_failure and pivot is not None and open_index is not None and position != 0:
            broke = close < pivot if position == 1 else close > pivot
            if broke:
                if index == open_index + 1:
                    out[index] = PositionIntent(
                        action="cancel", direction="long" if position == 1 else "short",
                        stage="pivot_failure", stop_price=pivot,
                        reason=f"枢轴 {pivot:.4f} 在入场后失效，撤销剩余挂单",
                    )
                else:
                    out[index] = PositionIntent(
                        action="exit", direction="long" if position == 1 else "short",
                        stage="structure_exit", stop_price=pivot,
                        reason=f"收盘跌破结构失效价 {pivot:.4f}",
                    )
                    position = 0
                    target_pct = 0.0
                    pivot = None
                    open_index = None
                continue

        if status == "confirmed" and phase in ("wedge_pop", "ema_crossback", "base_n_break"):
            if phase not in entry_stages:
                continue
            if require_higher and trend != "bullish":
                continue
            reason = "；".join(record.reasons[:2]) or f"{phase} 确认"
            setup_stop = record.invalidation_price or record.pivot_price or pivot
            if position <= 0:
                target_pct = min(100.0, initial)
                out[index] = PositionIntent(
                    action="open", direction="long", target_exposure_pct=target_pct,
                    stage=phase, stop_price=setup_stop, reason=reason,
                )
                position = 1
                pivot = setup_stop
                open_index = index
            else:
                target_pct = min(100.0, target_pct + add_step)
                out[index] = PositionIntent(
                    action="increase", direction="long", target_exposure_pct=target_pct,
                    stage=phase, stop_price=setup_stop or pivot,
                    reason=f"加仓候选：{reason}",
                )
                pivot = setup_stop or pivot
                open_index = index
            continue

        if phase == "exhaustion_extension":
            if position <= 0:
                continue
            reason = "；".join(record.reasons[:2]) or "延伸衰竭"
            if exit_on_exhaustion:
                out[index] = PositionIntent(
                    action="exit", direction="long" if position == 1 else "short",
                    stage=phase, stop_price=pivot, reason=f"衰竭离场：{reason}",
                )
                position = 0
                target_pct = 0.0
                pivot = None
                open_index = None
            else:
                target_pct = max(0.0, target_pct * keep_ratio / 100.0)
                out[index] = PositionIntent(
                    action="reduce", direction="long" if position == 1 else "short",
                    target_exposure_pct=target_pct, stage=phase, stop_price=pivot,
                    reason=f"衰竭分批减仓，保留 {keep_ratio:g}%：{reason}",
                )
            continue

        if status == "confirmed" and phase == "wedge_drop":
            if position > 0:
                out[index] = PositionIntent(
                    action="exit", direction="long", stage=phase, stop_price=pivot,
                    reason="；".join(record.reasons[:2]) or "楔形下跌确认",
                )
                position = 0
                target_pct = 0.0
                pivot = None
                open_index = None
                if side_mode != "symmetric":
                    continue
            if side_mode == "symmetric" and position == 0:
                target_pct = min(100.0, initial)
                out[index] = PositionIntent(
                    action="open", direction="short", target_exposure_pct=target_pct,
                    stage=phase, stop_price=record.invalidation_price or record.pivot_price,
                    reason="；".join(record.reasons[:2]) or "楔形下跌确认",
                )
                position = -1
                pivot = record.invalidation_price or record.pivot_price
                open_index = index
            continue

        if status == "confirmed" and phase in ("downside_ema_crossback", "downside_base_n_break"):
            if side_mode != "symmetric":
                continue
            if require_higher and trend == "bullish":
                continue
            if position >= 0:
                target_pct = min(100.0, initial)
                out[index] = PositionIntent(
                    action="open", direction="short", target_exposure_pct=target_pct,
                    stage=phase, stop_price=record.invalidation_price or record.pivot_price,
                    reason="；".join(record.reasons[:2]) or f"{phase} 确认",
                )
                position = -1
                pivot = record.invalidation_price or record.pivot_price
                open_index = index
            else:
                target_pct = min(100.0, target_pct + add_step)
                out[index] = PositionIntent(
                    action="increase", direction="short", target_exposure_pct=target_pct,
                    stage=phase, stop_price=record.invalidation_price or pivot,
                    reason=f"下行加仓候选：{'；'.join(record.reasons[:1])}",
                )
                pivot = record.invalidation_price or pivot
                open_index = index
            continue

    return out


def resolve_parameters(
    supplied: dict[str, Any] | None,
    *,
    asset_class: str,
    interval: str,
) -> dict[str, Any]:
    """Asset-class and interval defaults, overridden by what the caller supplied."""
    parameters = defaults_for(asset_class, interval)
    for key, value in (supplied or {}).items():
        if value is None:
            continue
        parameters[key] = value
    parameters["entryStages"] = list(_entry_stages(parameters))
    return parameters


def higher_intervals_for(interval: str) -> tuple[str, str]:
    return HIGHER_TIMEFRAME_MAP.get(interval, ("", ""))


def analyze(
    *,
    symbol: str,
    display_symbol: str,
    interval: str,
    product_type: str,
    bars: list[dict],
    parameters: dict[str, Any] | None = None,
    management_bars: list[dict] | None = None,
    background_bars: list[dict] | None = None,
    snapshot_hash: str = "",
    data_version: str = "",
) -> PhaseSeries:
    """The complete CPA reading for one symbol and interval.

    `bars` must be closed bars in ascending time order. The higher-interval series are
    aligned through `backtest.engine.align_higher_timeframe`, so a base bar only ever
    sees a higher bar that had already closed - the one place where a multi-timeframe
    study usually leaks the future.
    """
    resolved = resolve_parameters(parameters, asset_class=product_type, interval=interval)
    management_interval, background_interval = higher_intervals_for(interval)
    series = PhaseSeries(
        symbol=symbol,
        display_symbol=display_symbol,
        interval=interval,
        product_type=product_type,
        snapshot_hash=snapshot_hash,
        data_version=data_version,
        parameter_version=PARAMETER_VERSION,
        parameters=resolved,
        higher_intervals=(management_interval, background_interval),
    )
    minimum = int(resolved.get("minBars") or 60)
    if len(bars) < minimum:
        series.insufficient = True
        series.insufficient_reason = (
            f"{interval} 只有 {len(bars)} 根已收盘K线，少于 CPA 需要的 {minimum} 根；"
            "不输出阶段结论"
        )
        series.warnings.append(series.insufficient_reason)
        return series

    records = state_machine.run(bars, resolved)
    management_view = _align(bars, management_bars, management_interval, resolved)
    background_view = _align(bars, background_bars, background_interval, resolved)
    for index, record in enumerate(records):
        record.higher = management_view[index]
        record.background = background_view[index]
    series.records = records

    if management_interval:
        if management_bars:
            series.warnings.append(
                f"管理周期 {management_interval} 已按收盘对齐接入：低周期只能看到当时已收盘的高周期K线"
            )
        else:
            series.warnings.append(
                f"管理周期 {management_interval} 数据不足或未提供：背景显示为不可用，"
                "开启 requireHigherTimeframe 时不会入场"
            )
    if background_interval and not background_bars:
        series.warnings.append(f"背景周期 {background_interval} 未提供，背景不足")
    series.warnings.append(SIMPLE_POSITION_NOTICE)
    return series


def _align(
    base: list[dict],
    higher: list[dict] | None,
    higher_interval: str,
    parameters: dict[str, Any],
) -> list[Any]:
    """Per-base-bar view of a higher interval, or an all-unavailable placeholder."""
    from ...backtest.engine import align_higher_timeframe
    from ...config.instruments import INTERVAL_MS
    from .models import HigherTimeframeView

    if not higher or not higher_interval:
        placeholder = HigherTimeframeView(
            interval=higher_interval, available=False,
            reason="高周期数据不足" if higher_interval else "未配置高周期",
        )
        return [placeholder for _ in base]
    step = INTERVAL_MS.get(higher_interval)
    if not step:
        placeholder = HigherTimeframeView(
            interval=higher_interval, available=False, reason="未知的高周期长度"
        )
        return [placeholder for _ in base]
    visible = align_higher_timeframe(base, higher, step)
    # A phase reading for the higher interval, computed once on its own closed bars.
    higher_closes = [float(item["close"]) for item in higher]
    higher_fast = ind.ema(higher_closes, int(parameters["emaFast"]))
    higher_slow = ind.ema(higher_closes, int(parameters["emaSlow"]))
    higher_long = ind.sma(higher_closes, int(parameters["longSma"]))
    views: list[Any] = []
    cache: dict[int, Any] = {}
    for row in visible:
        if row is None:
            views.append(
                HigherTimeframeView(
                    interval=higher_interval, available=False,
                    reason=f"{higher_interval} 尚未收盘，本根不参与判断",
                )
            )
            continue
        stamp = int(row["ts"])
        if stamp not in cache:
            cut = [item for item in higher if int(item["ts"]) <= stamp]
            cache[stamp] = trend_view(
                cut, higher_interval,
                ema_fast=higher_fast[:len(cut)], ema_slow=higher_slow[:len(cut)],
                long_sma=higher_long[:len(cut)],
            )
        views.append(cache[stamp])
    return views


def describe() -> dict[str, Any]:
    """What the API and the strategy catalogue publish about this rule set."""
    return {
        "id": "cpa_cycle",
        "name": STRATEGY_NAME,
        "parameterVersion": PARAMETER_VERSION,
        "simplePositionNotice": SIMPLE_POSITION_NOTICE,
        "intentPositionNotice": INTENT_POSITION_NOTICE,
        "positionModels": list(POSITION_MODELS),
        "positionNoticeByModel": {
            "single": SIMPLE_POSITION_NOTICE,
            "intent": INTENT_POSITION_NOTICE,
        },
        "observationPhases": list(OBSERVATION_PHASES),
        "higherTimeframeMap": {key: list(value) for key, value in HIGHER_TIMEFRAME_MAP.items()},
        "attribution": (
            "概念来源：Oliver Kell 公开描述的 Cycle of Price Action；阈值为 QuantDesk "
            "研究参数，不是作者原始规则，也不代表作者本人的交易方法。"
        ),
    }
