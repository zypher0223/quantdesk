"""Driving a campaign: one round at a time, against the engine's own numbers.

A round is four steps and no more:

1. ask the provider what it wants to try (`agent.propose`, or `agent.reflect` once
   there is something to reflect on), handing it the campaign's frozen factors and the
   train/validation summaries the protocol allows - never the test segment;
2. store the proposals, after the engine re-checks every one against the provider's
   declared space (`PluginRegistry.check_proposals`);
3. evaluate each proposal on the train and validation windows, using the engine's own
   backtest on the engine's own stored bars, and record what it scored;
4. stop when the budget, the rounds or the provider say so.

What this module deliberately is not: it does not decide what "good" means (that is
the pre-registered `success_criteria` and, at the end, the campaign statistics), it
does not promote anything (D3: a human does), and it never touches the test window
(that is a one-shot, human-approved act in `campaigns.unseal_test`).

The evaluator is injected. The default one runs a real backtest per symbol; tests
pass a stub, which is how the orchestration can be tested without ten minutes of
number crunching.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import campaigns
from .campaigns import CampaignError
from .plugins import PluginError, PluginManager, PluginRegistry
from .plugins.protocol import (
    AgentBudget,
    AgentProposeRequest,
    AgentReflectRequest,
    AgentTrialSummary,
)

# Protocol groups: the engine's own instrument groups are about product type, the
# protocol's are about which cross-section a signal belongs to.
GROUP_TO_PROTOCOL = {"stock": "equity", "leveraged_etf": "equity", "crypto": "crypto"}


@dataclass(frozen=True)
class Segment:
    """One evaluation window, in milliseconds, closed at the start and the end."""

    name: str
    start_ts: int
    end_ts: int


def segments_of(campaign: dict[str, Any], *, include_test: bool = False) -> list[Segment]:
    windows = campaign["windows"]
    names = ["train", "validation"] + (["test"] if include_test else [])
    out = []
    for name in names:
        pair = windows.get(name) or []
        if len(pair) == 2 and pair[0] and pair[1]:
            out.append(Segment(name, int(pair[0]), int(pair[1])))
    return out


def provider_for(campaign: dict[str, Any], manager: PluginManager) -> str:
    """The provider this campaign was registered against, or the enabled one."""
    provider = campaign.get("provider") or ""
    if provider:
        return provider
    for record in manager.discover()[0]:
        if record.enabled and "strategy_agent" in record.manifest.capabilities:
            return record.manifest.id
    raise CampaignError(
        "没有启用的 strategy_agent 插件；战役需要一个能提案的提供者", status=409
    )


def _budget(campaign: dict[str, Any]) -> AgentBudget:
    raw = campaign["budget"] or {}
    return AgentBudget(
        proposals=int(raw.get("proposalsPerRound") or campaigns.MAX_PROPOSALS_PER_ROUND),
        deadlineMs=int(raw.get("roundDeadlineMs") or campaigns.MAX_ROUND_MS),
    )


def _trial_summaries(trials: list[dict[str, Any]]) -> list[AgentTrialSummary]:
    """Only the segments a search may look at - enforced by the model, not by this loop."""
    summaries = []
    for item in trials:
        if item["segment"] not in ("train", "validation"):
            continue
        summaries.append(
            AgentTrialSummary(
                proposalId=str(item["proposalId"])[:64],
                segment=item["segment"],
                sharpe=item.get("sharpe"),
                returnPct=item.get("returnPct"),
                maxDrawdownPct=item.get("maxDrawdownPct"),
                trades=item.get("trades"),
                verdict=str(item.get("verdict") or "")[:40],
                reason=str(item.get("reason") or "")[:400],
            )
        )
    return summaries


def _space(campaign: dict[str, Any]) -> list[str]:
    return [str(item["factorId"]) for item in campaign["factorSpace"]]


def manifest_for(manager: Any, provider: str, campaign: dict[str, Any]) -> Any:
    """The provider's declared boundaries, asked for *this campaign's* space.

    The engine then checks every proposal against this manifest, so a provider that
    proposes outside the campaign's frozen factors is refused by name rather than
    quietly narrowing the campaign to whatever it felt like searching.
    """
    from .plugins.protocol import AgentManifestResult

    response = manager.invoke(
        provider,
        "agent.manifest",
        {"factorIds": _space(campaign), "group": GROUP_TO_PROTOCOL[campaign["group"]]},
        capability="strategy_agent",
    )
    return AgentManifestResult.model_validate(response["result"])


def run_round(
    db: Any,
    uid: str,
    *,
    home: Path,
    evaluate: Callable[[dict[str, Any], dict[str, Any], Segment], dict[str, Any]] | None = None,
    progress: Callable[[float, str], None] | None = None,
    manager: Any | None = None,
) -> dict[str, Any]:
    """Run the campaign's next round and return what happened."""
    def report(fraction: float, label: str) -> None:
        if progress is not None:
            progress(fraction, label)

    campaign = campaigns.get_campaign(db, uid)
    if campaign["status"] in campaigns.TERMINAL:
        raise CampaignError(
            f"战役已结束（{campaign['status']}：{campaign['stopReason'] or '无原因'}）", status=409
        )
    round_number = campaign["roundsUsed"] + 1
    # The provider's own limit is the round's deadline; the engine's clock is what
    # decides whether it was kept.
    started = time.time()
    campaigns.start_round(db, uid, round_number=round_number)
    report(0.02, f"第 {round_number} 轮开始")

    # The manager is injectable so the orchestration can be tested without a plugin
    # installed in a temporary home; production always builds it from the real one.
    manager = manager or PluginManager(home)
    registry = PluginRegistry(manager)
    provider = provider_for(campaign, manager)
    manifest = manifest_for(manager, provider, campaign)
    report(0.08, f"已取得 {provider} 的提案边界")

    prior = campaigns.agent_visible_trials(db, uid)
    summaries = _trial_summaries(prior)
    budget = _budget(campaign)
    snapshot = campaign["snapshotHash"]
    if round_number == 1:
        request = AgentProposeRequest(
            campaignId=uid, round=1, snapshotHash=snapshot, universe=campaign["universe"],
            interval=campaign["interval"], group=GROUP_TO_PROTOCOL[campaign["group"]],
            factorIds=_space(campaign), dataProfile={"horizonBars": campaign["horizonBars"]},
            priorTrials=summaries, budget=budget,
        )
        result = registry.agent_propose(provider, request, manifest)
        reflection = ""
    else:
        reflect_request = AgentReflectRequest(
            campaignId=uid, round=round_number - 1, snapshotHash=snapshot,
            universe=campaign["universe"], interval=campaign["interval"],
            group=GROUP_TO_PROTOCOL[campaign["group"]], factorIds=_space(campaign),
            dataProfile={"horizonBars": campaign["horizonBars"]},
            trials=summaries, budget=budget,
            remainingRounds=max(0, int(campaign["budget"].get("maxRounds", 1)) - round_number + 1),
        )
        reflected = registry.agent_reflect(provider, reflect_request, manifest)
        reflection = reflected.reflection
        result = reflected
    proposals = [item.model_dump() for item in result.proposals]
    report(0.15, f"{provider} 提出 {len(proposals)} 个候选")
    if not proposals:
        campaigns.finish(db, uid, status="completed",
                         reason=result.stopReason or "提供者没有给出更多候选")
        return {"round": round_number, "proposals": 0, "trials": [], "reflection": reflection,
                "stopped": True, "stopReason": result.stopReason}

    stored = campaigns.record_proposals(db, uid, round_number=round_number, proposals=proposals)
    report(0.2, f"第 {round_number} 轮 {len(stored)} 个提案入库")

    evaluator = evaluate or evaluate_proposal
    trials: list[dict[str, Any]] = []
    segments = segments_of(campaign)
    total = max(1, len(stored) * len(segments))
    for index, proposal in enumerate(proposals):
        for segment in segments:
            done = index * len(segments) + segments.index(segment)
            report(0.2 + 0.75 * (done / total),
                   f"第 {round_number} 轮 {proposal['proposalId']} 在 {segment.name} 上评估")
            try:
                metrics = evaluator(db, campaign, proposal, segment)
            except Exception as exc:  # one bad candidate must not stop the round
                metrics = {"verdict": "failed", "reason": f"{type(exc).__name__}: {exc}"[:400]}
            campaigns.record_trial(
                db, uid, proposal_uid=proposal["proposalId"], segment=segment.name,
                run_id=str(metrics.get("runId") or ""),
                sharpe=metrics.get("sharpe"), return_pct=metrics.get("returnPct"),
                max_drawdown_pct=metrics.get("maxDrawdownPct"), trades=metrics.get("trades"),
                verdict=str(metrics.get("verdict") or ""), reason=str(metrics.get("reason") or ""),
            )
            trials.append({"proposalId": proposal["proposalId"], "segment": segment.name, **metrics})

    elapsed_ms = int((time.time() - started) * 1000)
    rounded = campaigns.get_campaign(db, uid)
    limit_ms = int(campaign["budget"].get("roundDeadlineMs") or campaigns.MAX_ROUND_MS)
    stop_reason = ""
    if elapsed_ms > limit_ms:
        stop_reason = f"本轮耗时 {elapsed_ms} 毫秒超过单轮时限 {limit_ms} 毫秒（D2）"
        campaigns.finish(db, uid, status="budget_limited", reason=stop_reason)
    elif rounded["roundsUsed"] >= int(campaign["budget"].get("maxRounds") or campaigns.MAX_ROUNDS):
        stop_reason = f"已达轮数上限 {campaign['budget'].get('maxRounds')}"
        campaigns.finish(db, uid, status="completed", reason=stop_reason)
    elif result.stopReason:
        stop_reason = result.stopReason
        campaigns.finish(db, uid, status="completed", reason=stop_reason)
    report(1.0, f"第 {round_number} 轮完成（{elapsed_ms} 毫秒）")
    return {
        "round": round_number,
        "proposals": len(stored),
        "trials": trials,
        "reflection": reflection,
        "elapsedMs": elapsed_ms,
        "stopped": bool(stop_reason),
        "stopReason": stop_reason,
        "provider": provider,
        "agentVersion": manifest.agentVersion,
    }


def ensure_test_trial(
    db: Any,
    uid: str,
    *,
    approved_by: str,
    home: Path,
    evaluate: Callable[[dict[str, Any], dict[str, Any], Segment], dict[str, Any]] | None = None,
    manager: Any | None = None,
) -> dict[str, Any]:
    """Run the out-of-sample window once, for the proposal the search selected.

    The search itself never touches this window; it is opened here, by a human's
    approval, for exactly one proposal, exactly once. Idempotent by construction: if a
    test trial with a stored run already exists, that is what comes back - the segment
    must not be evaluated twice, or it stops being out of sample.

    The selected proposal is the one with the best validation Sharpe (ties broken by
    the order they were recorded), and the rule is written into the returned payload so
    a reader can see how the choice was made rather than inferring it.
    """
    campaign = campaigns.get_campaign(db, uid)
    if campaign["status"] not in campaigns.TERMINAL:
        raise CampaignError(
            f"战役还在 {campaign['status']}：搜索结束后才能开封测试段并判决", status=409
        )
    from .campaign_stats import stored_verdict

    # Already judged: there is nothing left to run, and re-running the test segment
    # after a verdict would turn an out-of-sample window into a reusable one.
    judged = stored_verdict(db, uid)
    if judged is not None:
        return {"proposalId": judged.get("proposalId") or "", "alreadyJudged": True,
                "verdict": judged.get("verdict"), "runId": judged.get("runId")}
    # Reading the test segment is itself refused while it is sealed, so the lookup only
    # happens once the window has been opened.
    if not campaign["testSealed"]:
        # Any test trial counts, not only one that happens to carry a run id: the
        # window is measured once, and a missing run id is a data problem to report
        # rather than a licence to measure it again.
        existing = [
            trial for trial in campaigns.trials_for(db, uid, include_test=True, limit=1000)
            if trial["segment"] == "test"
        ]
        if existing:
            return {"proposalId": existing[0]["proposalId"], "alreadyRun": True, **existing[0]}
    else:
        campaigns.unseal_test(db, uid, approved_by=approved_by)
    validation = [
        trial for trial in campaigns.trials_for(db, uid, limit=1000)
        if trial["segment"] == "validation"
    ]
    measurable = [trial for trial in validation if trial["sharpe"] is not None]
    if not measurable:
        raise CampaignError(
            "验证段没有任何 Sharpe 读数：没有可选出的提案，测试段开封也没有意义", status=409
        )
    best = max(measurable, key=lambda trial: float(trial["sharpe"]))
    proposal = next(
        (item for item in campaigns.detail(db, uid)["proposals"]
         if item["proposalId"] == best["proposalId"]),
        None,
    )
    if proposal is None:
        raise CampaignError(f"提案 {best['proposalId']} 不在战役里", status=404)
    segment = next(
        (item for item in segments_of(campaign, include_test=True) if item.name == "test"), None
    )
    if segment is None:
        raise CampaignError("战役没有测试段窗口", status=409)
    evaluator = evaluate or evaluate_proposal
    metrics = evaluator(db, campaign, proposal, segment)
    campaigns.record_trial(
        db, uid, proposal_uid=proposal["proposalId"], segment="test",
        run_id=str(metrics.get("runId") or ""),
        sharpe=metrics.get("sharpe"), return_pct=metrics.get("returnPct"),
        max_drawdown_pct=metrics.get("maxDrawdownPct"), trades=metrics.get("trades"),
        verdict=str(metrics.get("verdict") or ""),
        reason=f"一次性开封后评估（{metrics.get('reason') or ''}）"[:400],
    )
    return {
        "proposalId": proposal["proposalId"],
        "alreadyRun": False,
        "selectionRule": "验证段 Sharpe 最高的提案（并列取先记录的）",
        "runId": metrics.get("runId"),
        "sharpe": metrics.get("sharpe"),
        "returnPct": metrics.get("returnPct"),
        "maxDrawdownPct": metrics.get("maxDrawdownPct"),
        "trades": metrics.get("trades"),
    }


# --------------------------------------------------------------------------
# The default evaluator: a real backtest on real stored bars
# --------------------------------------------------------------------------

def annualisation(interval: str) -> float:
    """Bars per year, used to annualise a Sharpe computed on bar returns."""
    return {"15m": 4 * 24 * 365, "1h": 24 * 365, "4h": 6 * 365, "1d": 365,
            "1w": 52}.get(interval, 24 * 365)


def sharpe_of(equity: list[float], interval: str) -> float | None:
    """Annualised Sharpe from an equity curve, with the formula written down.

    Mean bar return over its standard deviation, scaled by the square root of the
    bars in a year. Nothing is risk-adjusted beyond that: no risk-free rate, because
    a perp has no natural one, and no smoothing.
    """
    if len(equity) < 3:
        return None
    returns = [
        equity[index] / equity[index - 1] - 1.0
        for index in range(1, len(equity))
        if equity[index - 1]
    ]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    deviation = math.sqrt(variance)
    if deviation == 0:
        return None
    return mean / deviation * math.sqrt(annualisation(interval))


INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
               "1w": 604_800_000}
# The engine's own ceiling on a backtest; a window longer than this is refused by the
# bar cap rather than silently truncated here.
MAX_WINDOW_BARS = 100_000


def bars_for_window(window: Segment, interval: str, horizon: int) -> int:
    """How many bars to read so a window is actually covered.

    Reading "the last N bars" and then filtering by the window is how the first real
    campaign round produced four empty trials: 240 hourly bars reach back ten days,
    while the window it was asked about was three months wide. The count has to come
    from the window's own length.
    """
    step = INTERVAL_MS.get(interval, 3_600_000)
    # From the window's *start* to now, not its own span: a training window that ended
    # months ago is still that far back from the newest bar, and reading only the span
    # returned bars that all fell after it.
    newest = max(int(time.time() * 1000), int(window.end_ts))
    needed = (newest - int(window.start_ts)) // step + 1 + max(0, int(horizon)) + 60
    return int(min(MAX_WINDOW_BARS, max(60, needed)))


def _factor_series(db: Any, symbol: str, interval: str, window: Segment,
                   factor_ids: list[str], bars: int) -> tuple[list[dict], dict[str, list[float | None]]]:
    """Factor values and the bars they were computed on, inside one window."""
    from .factors import _series_for, manager as factor_manager
    from .plugins import PluginRegistry as _Registry
    from .plugins.protocol import FactorComputeRequest

    manager_ = factor_manager()
    provider = None
    for record in manager_.discover()[0]:
        if record.enabled and "factor_provider" in record.manifest.capabilities:
            provider = record.manifest.id
            break
    if provider is None:
        raise CampaignError("没有启用的 factor_provider，无法评估提案", status=409)
    payload, _provenance, version = _series_for(db, symbol, interval, bars)
    inside = [bar for bar in payload if window.start_ts <= int(bar["time"]) <= window.end_ts]
    if len(inside) < 30:
        oldest = int(payload[0]["time"]) if payload else 0
        detail = (
            f"本地最早只到 {oldest}，窗口从 {window.start_ts} 开始"
            if oldest and oldest > window.start_ts
            else f"读入 {len(payload)} 根，落在窗口内 {len(inside)} 根"
        )
        raise CampaignError(
            f"{symbol} 在 {window.name} 窗口内只有 {len(inside)} 根K线，无法评估（{detail}）",
            status=409,
        )
    response = _Registry(manager_).compute_factors(
        provider,
        FactorComputeRequest(symbol=symbol, timeframe=interval, snapshotHash=version,
                             factorIds=factor_ids, candles=inside),
    )
    series = {entry.factorId: [point.value for point in entry.values] for entry in response.series}
    return inside, series


def evaluate_proposal(
    db: Any, campaign: dict[str, Any], proposal: dict[str, Any], segment: Segment
) -> dict[str, Any]:
    """One proposal on one window: signals from the factor, engine's backtest on top.

    The rule is deliberately crude and stated: a symbol is held long when the mean of
    the proposal's factor signs is above the entry threshold, short when below the
    exit threshold, flat in between. The point of a campaign is to find out whether the
    *factor* carries information, not to showcase an execution trick.
    """
    from .backtest.engine import BacktestConfig, run_backtest

    from .config.instruments import require_instrument

    factor_ids = [str(item) for item in proposal.get("factorIds") or []]
    if not factor_ids:
        return {"verdict": "failed", "reason": "提案没有因子"}
    parameters = proposal.get("parameters") or {}
    entry = float(parameters.get("entryThreshold") or 0.0)
    exit_threshold = float(parameters.get("exitThreshold") or -0.5)
    horizon = int(campaign["horizonBars"])
    per_symbol: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for symbol in campaign["universe"]:
        spec = require_instrument(symbol)
        try:
            bars, series = _factor_series(
                db, symbol, campaign["interval"], segment, factor_ids,
                bars_for_window(segment, campaign["interval"], horizon),
            )
        except CampaignError as exc:
            # A symbol whose stored history does not reach back into this window is
            # reported and left out: the group is evaluated on the symbols that have
            # the data, rather than the whole segment failing because one listing is
            # younger than the others.
            skipped.append({"symbol": spec.venue_symbol, "reason": str(exc)[:200]})
            continue
        if not series:
            skipped.append({"symbol": spec.venue_symbol, "reason": "没有因子取值"})
            continue
        # The engine's own shapes: bars keyed by `ts`, and one signal per bar where
        # 1 is long, -1 is short, 0 is flat and None is "no opinion, hold".
        engine_bars = [
            {
                "ts": int(bar["time"]), "open": float(bar["open"]), "high": float(bar["high"]),
                "low": float(bar["low"]), "close": float(bar["close"]),
                "volume": float(bar.get("volume") or 0.0),
            }
            for bar in bars
        ]
        events: list[int | None] = []
        for index in range(len(bars)):
            values = [values_[index] for values_ in series.values()
                      if index < len(values_) and values_[index] is not None]
            if not values:
                events.append(None)
                continue
            score = sum(1 if value > 0 else (-1 if value < 0 else 0) for value in values) / len(values)
            events.append(1 if score > entry else (-1 if score < exit_threshold else 0))
        if all(event is None for event in events):
            continue
        config = BacktestConfig(strategy_id="factor_sign", allocation_pct=100.0,
                                include_funding=True, include_liquidation=True)
        result = run_backtest(engine_bars, config, signal_events=events,
                              interval=campaign["interval"])
        per_symbol.append(
            {
                "symbol": spec.venue_symbol,
                "sharpe": sharpe_of([float(point["equity"]) for point in result.equity_curve],
                                    campaign["interval"]),
                "returnPct": result.net_return_pct,
                "maxDrawdownPct": result.max_drawdown_pct,
                "trades": len(result.trades),
                # Kept so the trial can be stored as a run whose curve the
                # campaign-level PBO reads instead of recomputing returns.
                "curve": [
                    {"time": int(point["time"]), "equity": float(point["equity"])}
                    for point in result.equity_curve
                ],
            }
        )
    if not per_symbol:
        reasons = "；".join(f"{item['symbol']}: {item['reason'][:40]}" for item in skipped[:3])
        return {"verdict": "inconclusive",
                "reason": f"{segment.name} 窗口内没有任何标的可评估（{reasons}）",
                "perSymbol": [], "skipped": skipped}
    sharpes = [item["sharpe"] for item in per_symbol if item["sharpe"] is not None]
    reason = f"{len(per_symbol)} 个标的等权平均（{segment.name}）"
    if skipped:
        reason += f"；跳过 {len(skipped)} 个：{', '.join(item['symbol'] for item in skipped[:4])}"
    aggregate = {
        "sharpe": sum(sharpes) / len(sharpes) if sharpes else None,
        "returnPct": sum(item["returnPct"] for item in per_symbol) / len(per_symbol),
        "maxDrawdownPct": min(item["maxDrawdownPct"] for item in per_symbol),
        "trades": sum(item["trades"] for item in per_symbol),
        "verdict": "measured" if sharpes else "inconclusive",
        "reason": reason,
        "perSymbol": per_symbol,
        "skipped": skipped,
    }
    # The trial is recorded as a *run* as well, because the campaign-level PBO reads
    # the engine's stored equity curves rather than recomputing anything. Without a
    # run row the statistics would have to re-derive returns, which is the one thing
    # this integration refuses to do.
    try:
        aggregate["runId"] = _store_trial_run(db, campaign, proposal, segment, per_symbol, aggregate)
    except Exception as exc:  # noqa: BLE001 - statistics are optional, the trial is not
        aggregate["runStoreError"] = f"{type(exc).__name__}: {exc}"[:200]
    return aggregate


def _store_trial_run(
    db: Any, campaign: dict[str, Any], proposal: dict[str, Any], segment: Segment,
    per_symbol: list[dict[str, Any]], aggregate: dict[str, Any],
) -> str:
    """Persist this trial's per-symbol results as a finished run, and return its id.

    The row is written through the queue's own `submit`/`finish` pair so it is the
    same shape as any other backtest run: the equity curve lands in `result_json` and
    the artifacts, which is exactly where `campaign_stats.stored_equity` looks.
    """
    from .backtest_runs import RunQueue

    queue = RunQueue(db)
    body = {
        "symbol": campaign["universe"][0],
        "timeframe": campaign["interval"],
        "strategyId": f"agent-campaign:{proposal['proposalId']}",
        "allocationPct": 100.0,
        "includeFunding": True,
        "includeLiquidation": True,
    }
    label = f"campaign {campaign['uid']} {proposal['proposalId']} {segment.name}"
    run = queue.submit("backtest", body, label=label, deduplicate=False)
    return str(_finish_trial_run(queue, int(run["id"]), per_symbol, aggregate, segment))


def _finish_trial_run(
    queue: Any, run_id: int, per_symbol: list[dict[str, Any]], aggregate: dict[str, Any],
    segment: Segment,
) -> int:
    """Write the result the statistics read: one equity curve per symbol plus a headline."""
    started = int(time.time() * 1000)
    # One equal-weight portfolio curve, not the symbols interleaved: every symbol
    # starts from the same capital, so the mean equity at each timestamp is what an
    # equal-weight book would have been worth - and a single series is what a PBO
    # needs to difference into returns.
    totals: dict[int, list[float]] = {}
    for item in per_symbol:
        for point in item.get("curve") or []:
            totals.setdefault(int(point["time"]), []).append(float(point["equity"]))
    curve = [
        {"time": stamp, "equity": sum(values) / len(values)}
        for stamp, values in sorted(totals.items())
    ]
    result = {
        "kind": "campaign-trial",
        "segment": segment.name,
        "bars": len(curve),
        "equity_curve": curve,
        "net_return_pct": aggregate.get("returnPct"),
        "max_drawdown_pct": aggregate.get("maxDrawdownPct"),
        "trades": aggregate.get("trades"),
        "sharpe": aggregate.get("sharpe"),
        "per_symbol": per_symbol,
        "window": {"start": segment.start_ts, "end": segment.end_ts},
    }
    queue.finish(run_id, result, started)
    return run_id
