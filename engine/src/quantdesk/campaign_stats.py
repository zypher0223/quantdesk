"""Campaign statistics and the Gate-C verdict: DSR, proposal-dimension PBO, one-shot judgement.

Three things live here, and all three exist to stop a campaign from grading its own
homework:

* **Deflated Sharpe ratio (DSR)** - Bailey & Lopez de Prado, "The Deflated Sharpe
  Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality"
  (2014). The trial count `N` is the whole point: a Sharpe that is impressive for one
  try is ordinary for the best of fifty. So there is no fallback that invents an `N`
  or a spread - if `N` is missing or there is only one usable trial Sharpe, the
  result is `available: false` with a reason, never a number.
* **Probability of backtest overfitting (PBO)**, at the *proposal* dimension, by
  combinatorially symmetric cross-validation (CSCV): Bailey, Borwein, Lopez de Prado
  & Zhu, "The Probability of Backtest Overfitting" (2015). The columns of the matrix
  are the campaign's proposals, not a strategy's parameter grid - the campaign's
  search space *is* the proposal list, so that is the dimension where selection bias
  is created here. The result says so in `method` so nobody reads it as a
  parameter-level PBO.
* **The Gate-C verdict** - unseal the test window once, evaluate the pre-registered
  criteria against it once, store the verdict, and return the stored row for every
  later call. Test-segment numbers therefore cannot be produced twice, cannot be
  produced before the unsealing is approved, and cannot turn "we could not compute
  it" into a pass.

Where the numbers come from: `agent_trials.run_id` -> the engine's own stored
`backtest_runs.result_json` (falling back to the `equity` artifact) -> differencing
that equity curve. Returns are *not* recomputed here and no P&L is re-derived: the
engine already ran the backtest, this module only reads what it stored and slices it
to the campaign window that is allowed to be seen.

Every approximation is named in the returned `method`/`note`/`approximations` fields
rather than folded silently into a number.
"""

from __future__ import annotations

import json
import math
import random
import re
import sqlite3
import time
from itertools import combinations
from typing import Any, Iterable, Sequence

from . import campaigns
from .campaigns import CampaignError

# Euler-Mascheroni constant, as used in the expected-maximum-Sharpe expression.
EULER_MASCHERONI = 0.5772156649015329
# CSCV block count. Even, because half the blocks rank and half are ranked.
DEFAULT_BLOCKS = 8
DEFAULT_MAX_COMBINATIONS = 252
MIN_BLOCK_OBSERVATIONS = 5
MIN_SERIES_POINTS = 5
VERDICTS = ("pass", "fail", "inconclusive")
SEGMENT_LABELS = {"train": "训练段", "validation": "验证段", "test": "测试段"}

# Bars per year, mirroring `agent_campaign.annualisation` (asserted equal in the
# tests, so the two cannot drift apart silently). Kept local so this module does not
# have to import the plugin layer through `agent_campaign`.
ANNUALISATION = {"15m": 4 * 24 * 365, "1h": 24 * 365, "4h": 6 * 365, "1d": 365, "1w": 52}
DEFAULT_ANNUALISATION = 24 * 365


def annualisation(interval: str) -> float:
    return float(ANNUALISATION.get(interval, DEFAULT_ANNUALISATION))


# --------------------------------------------------------------------------------
# Small statistics: written out rather than imported, so the formula in the report
# is the formula in the code.
# --------------------------------------------------------------------------------


def _finite(values: Iterable[Any]) -> list[float]:
    out: list[float] = []
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            out.append(float(value))
    return out


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _sample_stdev(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def _skewness(values: Sequence[float]) -> float | None:
    """Sample skewness, m3 / m2**1.5."""
    if len(values) < 3:
        return None
    mean = _mean(values)
    m2 = sum((value - mean) ** 2 for value in values) / len(values)
    if m2 <= 0:
        return None
    m3 = sum((value - mean) ** 3 for value in values) / len(values)
    return m3 / (m2 ** 1.5)


def _kurtosis(values: Sequence[float]) -> float | None:
    """Kurtosis *not* in excess: the normal distribution is 3.0, as the paper's gamma4.

    (`plugins/vibe-factors` uses the excess convention and therefore subtracts a
    different amount inside the same variance term; that plugin is frozen and is not
    what this module calls.)
    """
    if len(values) < 4:
        return None
    mean = _mean(values)
    m2 = sum((value - mean) ** 2 for value in values) / len(values)
    if m2 <= 0:
        return None
    m4 = sum((value - mean) ** 4 for value in values) / len(values)
    return m4 / (m2 * m2)


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


_A = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
      1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
_B = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
      6.680131188771972e01, -1.328068155288572e01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
      -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
      3.754408661907416e00)
_P_LOW = 0.02425


def normal_ppf(probability: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation + one Halley step)."""
    if not 0.0 < probability < 1.0:
        raise ValueError("normal_ppf needs a probability strictly inside (0, 1)")
    p = probability
    if p < _P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / (
            (((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    elif p <= 1.0 - _P_LOW:
        q = p - 0.5
        r = q * q
        x = (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q / (
            ((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / (
            (((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    error = normal_cdf(x) - p
    u = error * math.sqrt(2.0 * math.pi) * math.exp(x * x / 2.0)
    return x - u / (1.0 + x * u / 2.0)


def per_bar_sharpe(returns: Sequence[float]) -> float | None:
    """Mean over standard deviation of *per-observation* returns, un-annualised.

    The DSR is a statement about the sampling distribution of the Sharpe estimator,
    whose variance depends on the number of observations, so the ratio and the
    moments must be in the same per-observation units.
    """
    values = _finite(returns)
    if len(values) < 2:
        return None
    deviation = _sample_stdev(values)
    if deviation is None or deviation == 0:
        return None
    return _mean(values) / deviation


# --------------------------------------------------------------------------------
# Deflated Sharpe ratio
# --------------------------------------------------------------------------------


def deflated_sharpe(
    *,
    observed_sharpe: float | None,
    trial_sharpes: Sequence[float] | None,
    trials: int | None,
    sample_length: int | None = None,
    skew: float | None = None,
    kurtosis: float | None = None,
    annualisation_factor: float | None = None,
    extra_note: str = "",
    extra_approximations: Sequence[str] = (),
) -> dict[str, Any]:
    """DSR of one observed Sharpe against the best-of-`N` trials null.

    All Sharpe inputs are per-observation. Refusals are explicit: no `N`, `N == 1`,
    fewer than two trial Sharpes, no sample length, or a non-positive variance term
    all return `available: false` with a reason, because a deflated number computed
    from a guessed trial count is worse than no number.
    """
    approximations = list(extra_approximations)
    method = "bailey-lopez-de-prado-2014-dsr"
    result: dict[str, Any] = {
        "available": False,
        "deflatedSharpe": None,
        "expectedMaxSharpe": None,
        "observedSharpe": observed_sharpe,
        "observedSharpeAnnualised": (
            observed_sharpe * math.sqrt(annualisation_factor)
            if observed_sharpe is not None and annualisation_factor
            else None
        ),
        "trials": int(trials) if trials else 0,
        "sharpeSpread": None,
        "sampleLength": sample_length,
        "skew": skew,
        "kurtosis": kurtosis,
        "method": method,
        "note": extra_note,
        "approximations": approximations,
        "reason": "",
    }

    spread_values = _finite(trial_sharpes or [])
    if trials is None or int(trials) <= 1:
        result["reason"] = (
            "没有试验次数 N（N<=1）：多重检验的修正量由尝试次数决定，没有 N 就没有 DSR，"
            "不猜一个数字"
        )
        return result
    if len(spread_values) < 2:
        result["reason"] = (
            f"只有 {len(spread_values)} 个可用的试验 Sharpe，算不出尝试之间的离散度，DSR 不可用"
        )
        return result
    if observed_sharpe is None or not math.isfinite(float(observed_sharpe)):
        result["reason"] = "缺少被检验的 Sharpe（入选提案还没有可用的收益序列），DSR 不可用"
        return result
    if sample_length is None or int(sample_length) < 2:
        result["reason"] = "缺少样本长度 T（既没有权益曲线也没有可核对的 bar 数），DSR 不可用"
        return result

    spread = _sample_stdev(spread_values)
    if spread is None or spread == 0:
        result["reason"] = "试验 Sharpe 的离散度为 0，DSR 的基准（期望最大 Sharpe）无法定义"
        return result

    trials_count = int(trials)
    expected_max = spread * (
        (1.0 - EULER_MASCHERONI) * normal_ppf(1.0 - 1.0 / trials_count)
        + EULER_MASCHERONI * normal_ppf(1.0 - 1.0 / (trials_count * math.e))
    )
    skew_value = 0.0 if skew is None else float(skew)
    kurtosis_value = 3.0 if kurtosis is None else float(kurtosis)
    sharpe = float(observed_sharpe)
    variance_term = 1.0 - skew_value * sharpe + ((kurtosis_value - 1.0) / 4.0) * sharpe * sharpe
    if variance_term <= 0:
        result["reason"] = (
            "偏度/峰度让方差展开项非正，DSR 的标准化在数学上不成立，不给数字"
        )
        return result

    z = (sharpe - expected_max) * math.sqrt(int(sample_length) - 1) / math.sqrt(variance_term)
    result.update(
        {
            "available": True,
            "deflatedSharpe": normal_cdf(z),
            "expectedMaxSharpe": expected_max,
            "expectedMaxSharpeAnnualised": (
                expected_max * math.sqrt(annualisation_factor) if annualisation_factor else None
            ),
            "sharpeSpread": spread,
            "sampleLength": int(sample_length),
            "skew": skew_value,
            "kurtosis": kurtosis_value,
            "zScore": z,
            "reason": "",
        }
    )
    if "moments=normal-approximation" in approximations:
        result["method"] = method + "+normal-moments"
    if "sampleLength=window-approximation" in approximations:
        result["method"] = result["method"] + "+window-T"
    return result


# --------------------------------------------------------------------------------
# Stored equity curves: the only source of returns
# --------------------------------------------------------------------------------


def _as_millis(value: Any) -> int | None:
    """Campaign windows are epoch milliseconds; accept second-stamped curves too."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if abs(number) < 1e11:  # seconds, not milliseconds
        number *= 1000.0
    return int(number)


def _point(entry: Any) -> tuple[int, float] | None:
    if isinstance(entry, dict):
        stamp = _as_millis(
            entry.get("time", entry.get("ts", entry.get("t", entry.get("timestamp"))))
        )
        equity = entry.get("equity", entry.get("value", entry.get("v", entry.get("balance"))))
    elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
        stamp = _as_millis(entry[0])
        equity = entry[1]
    else:
        return None
    if stamp is None or isinstance(equity, bool) or not isinstance(equity, (int, float)):
        return None
    if not math.isfinite(float(equity)):
        return None
    return stamp, float(equity)


def _curve_of(payload: Any) -> list[tuple[int, float]]:
    """Pull an equity curve out of whatever shape the stored result used."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return []
    if not isinstance(payload, dict):
        return []
    nested = []
    for container in ("selectedRun", "portfolio", "backtest"):
        inner = payload.get(container)
        if isinstance(inner, dict):
            nested.extend([inner.get("equityCurve"), inner.get("equity_curve")])
    candidates = [payload.get("equity_curve"), payload.get("equityCurve"), *nested]
    for candidate in candidates:
        if not isinstance(candidate, list):
            continue
        points = [point for point in (_point(entry) for entry in candidate) if point]
        if len(points) >= 2:
            points.sort(key=lambda item: item[0])
            return points
    return []


def stored_bars(db: Any, run_id: str | int) -> int | None:
    """The bar count the run itself recorded, if it did."""
    row = None
    try:
        rows = db.query(
            "SELECT result_json, summary_json FROM backtest_runs WHERE id=?", (int(run_id),)
        )
        row = rows[0] if rows else None
    except (TypeError, ValueError):
        return None
    if row is None:
        return None
    for column in ("result_json", "summary_json"):
        try:
            payload = json.loads(row.get(column) or "{}")
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        for key in ("bars", "barCount"):
            value = payload.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        for container in ("readRange", "range", "coverage", "summary"):
            inner = payload.get(container)
            if isinstance(inner, dict):
                value = inner.get("bars", inner.get("barCount"))
                if isinstance(value, (int, float)) and value > 0:
                    return int(value)
    return None


def stored_equity(db: Any, run_id: str | int) -> tuple[list[tuple[int, float]], str]:
    """The equity curve the engine already stored for this run, and where it came from.

    Read path: `backtest_runs.result_json`, then the `equity`/`selectedEquity`
    artifact. Never a re-run: the point is to read the engine's own numbers, not to
    produce new ones.
    """
    if run_id in (None, ""):
        return [], "没有 run_id"
    try:
        numeric = int(run_id)
    except (TypeError, ValueError):
        return [], f"run_id 不是数字：{run_id!r}"
    rows = db.query("SELECT id, status, result_json FROM backtest_runs WHERE id=?", (numeric,))
    if not rows:
        return [], f"run {numeric} 不在 backtest_runs 里"
    points = _curve_of(rows[0].get("result_json"))
    if points:
        return points, "backtest_runs.result_json"
    artifacts = db.query(
        "SELECT name, payload FROM backtest_artifacts WHERE run_id=? AND name IN "
        "('equity','selectedEquity') ORDER BY CASE name WHEN 'equity' THEN 0 ELSE 1 END, id",
        (numeric,),
    )
    for artifact in artifacts:
        points = _curve_of(artifact.get("payload"))
        if points:
            return points, f"backtest_artifacts.{artifact['name']}"
    return [], f"run {numeric} 既没有 result_json 权益曲线也没有 equity 工件"


def as_points(curve: Sequence[Any]) -> list[tuple[int, float]]:
    """Accept either `(stamp, equity)` pairs or the stored `{time, equity}` shape."""
    return [point for point in (_point(entry) for entry in curve) if point]


def curve_in_window(
    curve: Sequence[Any], window: Sequence[int]
) -> list[tuple[int, float]]:
    """The part of a curve inside a campaign window, ends included."""
    if not window or len(window) < 2 or window[0] is None or window[1] is None:
        return []
    start, end = int(window[0]), int(window[1])
    points = as_points(curve)
    points.sort(key=lambda item: item[0])
    return [point for point in points if start <= point[0] <= end]


def returns_of(points: Sequence[Any]) -> tuple[list[float], list[int]]:
    """Per-observation returns by differencing the stored equity curve."""
    rows = as_points(points)
    returns: list[float] = []
    stamps: list[int] = []
    for index in range(1, len(rows)):
        previous = rows[index - 1][1]
        if previous == 0:
            continue
        returns.append(rows[index][1] / previous - 1.0)
        stamps.append(rows[index][0])
    return returns, stamps


def segment_metrics(
    points: Sequence[Any], interval: str
) -> dict[str, Any]:
    """Sharpe / return / drawdown of one window, from the stored curve only."""
    rows = as_points(points)
    returns, _ = returns_of(rows)
    factor = annualisation(interval)
    sharpe = per_bar_sharpe(returns)
    peak = None
    max_drawdown = 0.0
    for _, equity in rows:
        peak = equity if peak is None or equity > peak else peak
        if peak:
            max_drawdown = min(max_drawdown, equity / peak - 1.0)
    total = None
    if len(rows) >= 2 and rows[0][1]:
        total = (rows[-1][1] / rows[0][1] - 1.0) * 100.0
    return {
        "bars": len(rows),
        "returnPct": total,
        "sharpe": sharpe * math.sqrt(factor) if sharpe is not None else None,
        "perBarSharpe": sharpe,
        "maxDrawdownPct": max_drawdown * 100.0 if rows else None,
        "available": sharpe is not None,
    }


# --------------------------------------------------------------------------------
# Probability of backtest overfitting, proposal dimension
# --------------------------------------------------------------------------------


def _rank_omega(out_of_sample: Sequence[float], chosen: int) -> float:
    """Where the in-sample winner landed out of sample, as a fraction in (0, 1].

    Ascending: 1.0 is the best of the field, 0.5 is the median. Ties share the middle
    of their span, which keeps the statistic honest when two proposals are
    indistinguishable rather than letting the sort order decide.
    """
    value = out_of_sample[chosen]
    worse = sum(1 for other in out_of_sample if other < value)
    tied = sum(1 for other in out_of_sample if other == value) - 1
    rank = worse + (tied + 1) / 2.0
    return rank / len(out_of_sample)


def proposal_pbo(
    series: dict[str, Sequence[float]],
    *,
    blocks: int = DEFAULT_BLOCKS,
    seed: int = 0,
    max_combinations: int = DEFAULT_MAX_COMBINATIONS,
) -> dict[str, Any]:
    """CSCV over aligned per-proposal return series; columns are proposals.

    Split the common timeline into `blocks` contiguous blocks, take every balanced
    half as the in-sample set, rank the proposals there, and ask how often the
    in-sample winner falls to or below the out-of-sample median. Pure noise gives
    about 0.5; a set where one proposal really is better gives something near 0.
    """
    result: dict[str, Any] = {
        "available": False,
        "pbo": None,
        "proposals": 0,
        "skipped": {},
        "blocks": 0,
        "combinations": 0,
        "observations": 0,
        "droppedObservations": 0,
        "medianOosRank": None,
        "meanOosRank": None,
        "selectionFrequency": {},
        "method": "cscv-proposal-dimension",
        "note": (
            "按提案维度做的 PBO（矩阵的列是提案，不是某个策略的参数网格）："
            "战役的搜索空间就是提案列表，选择偏差也正是在这一维产生的。"
        ),
        "reason": "",
    }
    usable: dict[str, list[float]] = {}
    skipped: dict[str, str] = {}
    for proposal_id, values in series.items():
        cleaned = _finite(values)
        if len(cleaned) != len(values):
            skipped[proposal_id] = "序列里有非有限值"
            continue
        if len(cleaned) < MIN_SERIES_POINTS:
            skipped[proposal_id] = f"可用观测只有 {len(cleaned)} 个，少于 {MIN_SERIES_POINTS} 个"
            continue
        usable[proposal_id] = cleaned
    result["skipped"] = skipped
    result["proposals"] = len(usable)
    if len(usable) < 2:
        result["reason"] = (
            f"可用收益序列的提案只有 {len(usable)} 个，PBO 至少需要 2 个（其余在 skipped 里写明了原因）"
        )
        return result

    length = min(len(values) for values in usable.values())
    if any(len(values) != length for values in usable.values()):
        result["note"] += " 各提案序列长度不一致，已统一截到最短的公共长度。"
        usable = {key: values[:length] for key, values in usable.items()}

    usable_blocks = int(blocks)
    if usable_blocks % 2:
        usable_blocks -= 1
    while usable_blocks >= 2 and length // usable_blocks < MIN_BLOCK_OBSERVATIONS:
        usable_blocks -= 2
    if usable_blocks < 2:
        result["reason"] = (
            f"公共观测 {length} 个，切不出每块至少 {MIN_BLOCK_OBSERVATIONS} 个观测的两块以上，PBO 不可用"
        )
        return result

    per_block = length // usable_blocks
    kept = per_block * usable_blocks
    result["blocks"] = usable_blocks
    result["observations"] = kept
    result["droppedObservations"] = length - kept

    identifiers = sorted(usable)
    block_means = {
        proposal_id: [
            _mean(usable[proposal_id][index * per_block:(index + 1) * per_block])
            for index in range(usable_blocks)
        ]
        for proposal_id in identifiers
    }
    all_splits = list(combinations(range(usable_blocks), usable_blocks // 2))
    if len(all_splits) > int(max_combinations):
        generator = random.Random(int(seed))
        chosen_splits = sorted(generator.sample(range(len(all_splits)), int(max_combinations)))
        combinations_used = [all_splits[index] for index in chosen_splits]
        result["note"] += f" 组合数 {len(all_splits)} 超过上限，按固定种子取了 {len(combinations_used)} 个。"
    else:
        combinations_used = all_splits
    result["combinations"] = len(combinations_used)
    result["combinationsAvailable"] = len(all_splits)

    overfit = 0
    omegas: list[float] = []
    selection = {proposal_id: 0 for proposal_id in identifiers}
    for in_sample in combinations_used:
        in_set = set(in_sample)
        out_of_sample = [index for index in range(usable_blocks) if index not in in_set]
        in_scores = [
            _mean([block_means[proposal_id][index] for index in in_sample])
            for proposal_id in identifiers
        ]
        best = max(range(len(identifiers)), key=lambda index: in_scores[index])
        selection[identifiers[best]] += 1
        out_scores = [
            _mean([block_means[proposal_id][index] for index in out_of_sample])
            for proposal_id in identifiers
        ]
        omega = _rank_omega(out_scores, best)
        omegas.append(omega)
        if omega <= 0.5:
            overfit += 1

    ordered = sorted(omegas)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2.0
    )
    result.update(
        {
            "available": True,
            "pbo": overfit / len(combinations_used),
            "medianOosRank": median,
            "meanOosRank": _mean(omegas),
            "selectionFrequency": {
                proposal_id: selection[proposal_id] / len(combinations_used)
                for proposal_id in identifiers
            },
            "reason": "",
        }
    )
    return result


def aligned_series(
    per_proposal: dict[str, Sequence[tuple[int, float]]]
) -> tuple[dict[str, list[float]], dict[str, str]]:
    """Align proposal return series on the timestamps *all* of them share."""
    usable = {
        key: points for key, points in per_proposal.items() if len(points) >= 2
    }
    skipped = {
        key: f"窗口内只有 {len(points)} 个权益点" for key, points in per_proposal.items() if key not in usable
    }
    if len(usable) < 2:
        return {}, skipped
    common: set[int] | None = None
    for points in usable.values():
        stamps = {stamp for stamp, _ in points}
        common = stamps if common is None else (common & stamps)
    common = common or set()
    if len(common) < MIN_SERIES_POINTS:
        for key in usable:
            skipped[key] = f"共同时间戳只有 {len(common)} 个"
        return {}, skipped
    axis = sorted(common)
    aligned: dict[str, list[float]] = {}
    for key, points in usable.items():
        lookup = {stamp: value for stamp, value in points}
        returns: list[float] = []
        for index in range(1, len(axis)):
            previous, current = lookup[axis[index - 1]], lookup[axis[index]]
            returns.append(current / previous - 1.0 if previous else 0.0)
        aligned[key] = returns
    return aligned, skipped


# --------------------------------------------------------------------------------
# Campaign-level view
# --------------------------------------------------------------------------------


def _pick_proposal(
    trials: Sequence[dict[str, Any]], prefer: Sequence[str], *, fallback: bool = True
) -> dict[str, Any] | None:
    """The best trial in the preferred segments, by the Sharpe it recorded.

    `fallback=False` refuses to look outside the preferred segments, which is what
    keeps a test-segment Sharpe from being reported as the validation one when a
    campaign happens to have no validation trials.
    """
    for segment in prefer:
        pool = [item for item in trials if item["segment"] == segment and item["sharpe"] is not None]
        if pool:
            return max(pool, key=lambda item: float(item["sharpe"]))
    if not fallback:
        return None
    pool = [item for item in trials if item["sharpe"] is not None]
    return max(pool, key=lambda item: float(item["sharpe"])) if pool else None


def _campaign_id(db: Any, uid: str) -> int:
    rows = db.query("SELECT id FROM agent_campaigns WHERE campaign_uid=?", (uid,))
    if not rows:
        raise CampaignError(f"没有这个战役：{uid}", status=404)
    return int(rows[0]["id"])


def _proposal_runs(
    db: Any, campaign: dict[str, Any], segment: str
) -> dict[str, Any]:
    """Every proposal's curve inside one segment window, read from its trial's run."""
    rows = db.query(
        "SELECT p.proposal_uid, t.run_id FROM agent_trials t "
        "JOIN agent_proposals p ON p.id = t.proposal_id "
        "WHERE t.campaign_id = (SELECT id FROM agent_campaigns WHERE campaign_uid=?) "
        "AND t.segment=? ORDER BY t.id",
        (campaign["uid"], segment),
    )
    curves: dict[str, list[tuple[int, float]]] = {}
    sources: dict[str, str] = {}
    failures: dict[str, str] = {}
    for row in rows:
        proposal_id = row["proposal_uid"]
        if proposal_id in curves:
            continue
        if not row["run_id"]:
            failures[proposal_id] = "该提案的试验没有 run_id，取不到引擎存下来的收益序列"
            continue
        curve, source = stored_equity(db, row["run_id"])
        if not curve:
            failures[proposal_id] = source
            continue
        windowed = curve_in_window(curve, campaign["windows"][segment])
        if len(windowed) < 2:
            failures[proposal_id] = f"{segment} 窗口内没有权益点"
            continue
        curves[proposal_id] = windowed
        sources[proposal_id] = source
    return {"curves": curves, "sources": sources, "failures": failures}


def campaign_stats(
    db: Any,
    uid: str,
    *,
    include_test: bool = False,
    observed_segment: str | None = None,
    observed_run_id: str = "",
    blocks: int = DEFAULT_BLOCKS,
    seed: int | None = None,
    max_combinations: int = DEFAULT_MAX_COMBINATIONS,
) -> dict[str, Any]:
    """DSR and proposal-dimension PBO for a campaign - readable before unsealing.

    Before the seal is broken nothing here can see a test-segment value: the search
    trials come from `trials_for(include_test=False)`, every curve is sliced to a
    train/validation window, and `observed_segment="test"` is refused outright. The
    numbers are therefore the same ones the campaign was allowed to look at.
    """
    campaign = campaigns.get_campaign(db, uid)
    if observed_segment is not None and observed_segment not in ("train", "validation", "test"):
        raise CampaignError(f"观测分段只能是 train/validation/test：{observed_segment}", status=422)
    if observed_segment == "test" and campaign["testUnsealedTs"] is None:
        raise CampaignError(
            "测试段尚未开封（Gate-C）：开封前不计算任何测试段统计", status=409
        )
    search_trials = campaigns.trials_for(db, uid, include_test=False)
    visible_trials = campaigns.trials_for(db, uid, include_test=True) if include_test else search_trials
    factor = annualisation(campaign["interval"])
    judge_segment = observed_segment or "validation"

    # Trial Sharpes are recorded annualised (`agent_campaign.sharpe_of`); the DSR
    # works in per-observation units, so they are converted back once, here.
    trial_sharpes = [
        float(item["sharpe"]) / math.sqrt(factor)
        for item in search_trials
        if item["sharpe"] is not None
    ]
    selected = _pick_proposal(visible_trials, (judge_segment,), fallback=False)
    # The observed Sharpe is read from a stored run: the one the caller named, or the
    # one the selected trial points at. A run that no trial points at yet (the operator
    # hands the test run straight to the verdict call) is still the engine's own curve.
    observed_run = str(observed_run_id or (selected["runId"] if selected else "") or "")
    approximations: list[str] = []
    notes: list[str] = []

    observed_metrics: dict[str, Any] = {"available": False, "source": ""}
    observed_sharpe: float | None = None
    observed_returns: list[float] = []
    sample_length: int | None = None
    skew: float | None = None
    kurtosis: float | None = None
    if observed_run:
        curve_points, source = stored_equity(db, observed_run)
        windowed = (
            curve_in_window(curve_points, campaign["windows"][judge_segment]) if curve_points else []
        )
        if len(windowed) >= MIN_SERIES_POINTS:
            observed_metrics = segment_metrics(windowed, campaign["interval"])
            observed_metrics["source"] = source
            observed_returns, _ = returns_of(windowed)
            observed_sharpe = per_bar_sharpe(observed_returns)
            sample_length = len(observed_returns)
            skew = _skewness(observed_returns)
            kurtosis = _kurtosis(observed_returns)
        else:
            notes.append(
                f"run {observed_run} 在 {judge_segment} 窗口里没有可用权益曲线（{source}）"
            )
    if observed_sharpe is None and selected is not None and selected["sharpe"] is not None:
        if not observed_run:
            notes.append("入选提案的试验没有 run_id：只能退回试验记录里的 Sharpe")
        observed_sharpe = float(selected["sharpe"]) / math.sqrt(factor)
        observed_metrics = {
            "available": True,
            "source": "trial-record",
            "runId": observed_run or None,
            "bars": None,
            "returnPct": selected["returnPct"],
            "sharpe": selected["sharpe"],
            "perBarSharpe": observed_sharpe,
            "maxDrawdownPct": selected["maxDrawdownPct"],
        }
        approximations.append("moments=normal-approximation")
        skew, kurtosis = None, None
        bars = stored_bars(db, observed_run) if observed_run else None
        span = campaign["windows"][judge_segment]
        if bars:
            sample_length = bars
        elif span and span[0] is not None and span[1] is not None:
            seconds = {
                "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800,
            }.get(campaign["interval"], 3600)
            sample_length = max(2, int((int(span[1]) - int(span[0])) / 1000 / seconds))
            approximations.append("sampleLength=window-approximation")
        notes.append(
            "偏度/峰度在取不到真实收益序列时按正态近似（偏度 0、峰度 3），已写进 method/approximations"
        )
    observed_metrics["runId"] = observed_run or None

    dsr = deflated_sharpe(
        observed_sharpe=observed_sharpe,
        trial_sharpes=trial_sharpes,
        trials=len(search_trials),
        sample_length=sample_length,
        skew=skew,
        kurtosis=kurtosis,
        annualisation_factor=factor,
        extra_note=" ".join(notes),
        extra_approximations=approximations,
    )
    dsr["observedSegment"] = judge_segment
    dsr["trialsBasis"] = (
        "N=搜索阶段的尝试数（train+validation 的 agent_trials）；测试段那一次是最终测量，不计入 N"
    )

    windows = campaign["windows"]
    validation_trials = [item for item in visible_trials if item["segment"] == "validation"]
    if not validation_trials:
        validation_trials = [item for item in search_trials if item["segment"] == "validation"]
    runs = _proposal_runs(db, campaign, "validation")
    aligned, align_skipped = aligned_series(runs["curves"])
    skipped = dict(runs["failures"])
    skipped.update({key: value for key, value in align_skipped.items() if key not in skipped})
    pbo = proposal_pbo(
        aligned, blocks=blocks, seed=campaign["seed"] if seed is None else seed,
        max_combinations=max_combinations,
    )
    pbo["skipped"] = {**skipped, **pbo["skipped"]}
    pbo["proposalsConsidered"] = len(validation_trials)
    pbo["segment"] = "validation"
    pbo["window"] = windows["validation"]
    pbo["sources"] = runs["sources"]

    return {
        "campaignId": uid,
        "group": campaign["group"],
        "interval": campaign["interval"],
        "horizonBars": campaign["horizonBars"],
        "status": campaign["status"],
        "testSealed": campaign["testSealed"],
        "testUnsealedTs": campaign["testUnsealedTs"],
        "visibility": {
            "includeTest": bool(include_test),
            "segments": sorted({item["segment"] for item in visible_trials}),
            "trialsInView": len(visible_trials),
            "searchTrials": len(search_trials),
            "testTrialsRecorded": max(0, campaign["trialsUsed"] - len(search_trials)),
        },
        "trials": len(search_trials),
        "trialsRecorded": campaign["trialsUsed"],
        "trialsWithSharpe": len(trial_sharpes),
        "selectedProposal": (
            {
                "proposalId": selected["proposalId"],
                "segment": selected["segment"],
                "sharpeAnnualised": selected["sharpe"],
                "runId": selected["runId"],
            }
            if selected is not None
            else None
        ),
        "observed": {
            "segment": judge_segment,
            **observed_metrics,
        },
        "deflatedSharpe": dsr,
        "pbo": pbo,
        "approximations": sorted(set(approximations)),
        "note": " ".join(notes),
    }


# --------------------------------------------------------------------------------
# Pre-registered criteria: parsed, then evaluated, never invented
# --------------------------------------------------------------------------------


_CLAUSE_SPLIT = re.compile(r"[;；\n]+|\s*且\s*|\s*并且\s*|\s*同时\s*|\s*以及\s*|\band\b|[,，、]")
_CLAUSE = re.compile(
    r"(?P<metric>[^<>=≤≥!%]*?)\s*(?P<operator>>=|<=|==|!=|≥|≤|>|<|=)\s*"
    r"(?P<value>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*(?P<percent>%|％)?"
)
_OPERATOR_ALIASES = {">=": ">=", "≥": ">=", "<=": "<=", "≤": "<=", "==": "==", "=": "==", "!=": "!=", ">": ">", "<": "<"}
_SEGMENT_PATTERNS = (
    ("train", ("训练段", "训练", "样本内", "in-sample", "insample", "train")),
    ("validation", ("验证段", "验证", "valid", "validation")),
    ("test", ("测试段", "测试", "样本外", "留出", "out-of-sample", "outofsample", "holdout", "oos", "test")),
)
_METRIC_ALIASES = {
    "sharpe": "sharpe", "夏普": "sharpe", "sharperatio": "sharpe", "夏普比率": "sharpe",
    "returnpct": "returnPct", "return": "returnPct", "netreturn": "returnPct",
    "净收益": "returnPct", "收益": "returnPct", "收益率": "returnPct",
    "maxdrawdownpct": "maxDrawdownPct", "maxdrawdown": "maxDrawdownPct",
    "drawdown": "maxDrawdownPct", "最大回撤": "maxDrawdownPct", "回撤": "maxDrawdownPct",
    "trades": "trades", "tradecount": "trades", "交易数": "trades", "交易次数": "trades",
    "deflatedsharpe": "deflatedSharpe", "dsr": "deflatedSharpe", "收缩夏普": "deflatedSharpe",
    "deflatedsharperatio": "deflatedSharpe",
    "pbo": "pbo", "过拟合概率": "pbo", "backtestoverfittingprobability": "pbo",
    "trials": "trials", "试验数": "trials", "试验次数": "trials", "n": "trials",
}
_GLOBAL_METRICS = ("deflatedSharpe", "pbo", "trials")


def _normalise_metric(text: str) -> str | None:
    key = re.sub(r"[\s_\-（）()：:的]", "", text or "").lower()
    if not key:
        return None
    if key in _METRIC_ALIASES:
        return _METRIC_ALIASES[key]
    for alias, metric in _METRIC_ALIASES.items():
        if len(alias) < 2:  # a one-letter alias would match anything
            continue
        if key.endswith(alias) or alias in key:
            return metric
    return None


def _segment_scope(text: str) -> str | None:
    lowered = (text or "").lower()
    # Chinese qualifiers first: "样本外" contains "样本" but not "样本内", and the
    # English patterns are checked after so "testing" cannot win over "验证段".
    for segment, patterns in _SEGMENT_PATTERNS:
        for pattern in patterns:
            if pattern in lowered:
                return segment
    return None


def parse_criteria(text: str) -> dict[str, Any]:
    """Turn the pre-registered sentence into clauses a machine can check.

    Unrecognised prose is kept verbatim in `unrecognised`, because a criteria string
    that cannot be parsed must end in `inconclusive`, not in a silent pass.
    """
    source = text or ""
    clauses: list[dict[str, Any]] = []
    unrecognised: list[str] = []
    for raw in _CLAUSE_SPLIT.split(source):
        piece = (raw or "").strip(" \t。.：:")
        if not piece:
            continue
        match = _CLAUSE.search(piece)
        metric = _normalise_metric(match.group("metric")) if match else None
        if not match or metric is None:
            unrecognised.append(piece)
            continue
        operator = _OPERATOR_ALIASES[match.group("operator")]
        value = float(match.group("value"))
        if match.group("percent"):
            value = value / 100.0
        clauses.append(
            {
                "metric": metric,
                "operator": operator,
                "value": value,
                "segment": _segment_scope(piece),
                "text": piece,
            }
        )
    return {
        "source": source,
        "clauses": clauses,
        "unrecognised": unrecognised,
        "recognisedCount": len(clauses),
    }


def _compare(actual: float, operator: str, expected: float) -> bool:
    if operator == ">=":
        return actual >= expected
    if operator == "<=":
        return actual <= expected
    if operator == ">":
        return actual > expected
    if operator == "<":
        return actual < expected
    if operator == "==":
        return math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12)
    if operator == "!=":
        return not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12)
    raise ValueError(operator)


def _metric_value(
    metric: str, scope: str, evidence: dict[str, Any]
) -> tuple[float | None, str]:
    if metric in _GLOBAL_METRICS:
        stats = evidence.get("stats") or {}
        if metric == "trials":
            value = stats.get("trials")
            return (float(value), "全局：搜索阶段试验数 N") if value else (None, "没有试验数")
        if metric == "deflatedSharpe":
            value = stats.get("deflatedSharpe")
            if value is None:
                return None, f"DSR 不可用：{stats.get('deflatedSharpeReason') or '缺少 N 或离散度'}"
            return float(value), "全局：收缩夏普 DSR"
        value = stats.get("pbo")
        if value is None:
            return None, f"PBO 不可用：{stats.get('pboReason') or '可用序列不足'}"
        return float(value), "全局：提案维度 PBO"
    segment = evidence.get("segments", {}).get(scope)
    if not segment or not segment.get("available"):
        return None, f"{scope} 段没有可用的收益序列"
    value = segment.get(metric)
    if value is None:
        return None, f"{scope} 段没有记录 {metric}"
    return float(value), f"{scope} 段（来源：{segment.get('source') or '试验记录'}）"


def evaluate_criteria(
    parsed: dict[str, Any], evidence: dict[str, Any], *, judged_segment: str
) -> dict[str, Any]:
    """Compare, clause by clause. Any failure fails; any hole is inconclusive."""
    outcomes: list[dict[str, Any]] = []
    for clause in parsed["clauses"]:
        scope = clause["segment"] or judged_segment
        actual, origin = _metric_value(clause["metric"], scope, evidence)
        if actual is None:
            outcomes.append({**clause, "actual": None, "status": "unevaluable", "origin": origin})
            continue
        outcomes.append(
            {
                **clause,
                "actual": actual,
                "status": "pass" if _compare(actual, clause["operator"], clause["value"]) else "fail",
                "origin": origin,
            }
        )
    failed = [item for item in outcomes if item["status"] == "fail"]
    missing = [item for item in outcomes if item["status"] == "unevaluable"]
    if not parsed["clauses"]:
        verdict, reason = "inconclusive", "预注册标准里没有可判定的数值条款，无法据此判定"
    elif failed:
        verdict = "fail"
        reason = "；".join(
            f"{item['text']} 不满足（实际 {item['actual']:.6g}）" for item in failed
        )
    elif missing:
        verdict = "inconclusive"
        reason = "；".join(f"{item['text']} 无法判定（{item['origin']}）" for item in missing)
        if parsed["unrecognised"]:
            reason += f"；另有无法解析的条款：{' / '.join(parsed['unrecognised'])}"
    else:
        verdict = "pass"
        reason = "所有预注册条款都满足"
    return {
        "verdict": verdict,
        "reason": reason,
        "outcomes": outcomes,
        "failed": [item["text"] for item in failed],
        "unevaluable": [item["text"] for item in missing],
        "unrecognised": list(parsed["unrecognised"]),
    }


# --------------------------------------------------------------------------------
# Gate-C: one unsealing, one test run, one stored verdict
# --------------------------------------------------------------------------------


def _verdict_row(row: Any) -> dict[str, Any]:
    try:
        evidence = json.loads(row["evidence_json"] or "{}")
    except ValueError:
        evidence = {}
    return {
        "campaignId": row["campaign_uid"],
        "proposalId": row["proposal_uid"],
        "segment": row["segment"],
        "group": row["group_name"],
        "interval": row["interval"],
        "horizonBars": int(row["horizon_bars"] or 0),
        "window": [row["window_start_ts"], row["window_end_ts"]],
        "runId": row["run_id"],
        "bars": row["bars"],
        "sharpe": row["sharpe"],
        "returnPct": row["return_pct"],
        "maxDrawdownPct": row["max_drawdown_pct"],
        "trades": row["trades"],
        "deflatedSharpe": row["deflated_sharpe"],
        "expectedMaxSharpe": row["expected_max_sharpe"],
        "pbo": row["pbo"],
        "trials": row["trials"],
        "verdict": row["verdict"],
        "reason": row["reason"],
        "criteria": row["criteria"],
        "evidence": evidence,
        "approvedBy": row["approved_by"],
        "createdTs": int(row["created_ts"]),
    }


def stored_verdict(
    db: Any, uid: str, *, proposal_uid: str = "", segment: str = ""
) -> dict[str, Any] | None:
    """The verdict already on record, if the campaign has been judged."""
    campaign = campaigns.get_campaign(db, uid)
    clauses = ["campaign_id = (SELECT id FROM agent_campaigns WHERE campaign_uid=?)"]
    params: list[Any] = [campaign["uid"]]
    if proposal_uid:
        clauses.append("proposal_uid = ?")
        params.append(proposal_uid)
    if segment:
        clauses.append("segment = ?")
        params.append(segment)
    rows = db.query(
        f"SELECT * FROM agent_verdicts WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT 1",
        tuple(params),
    )
    return _verdict_row(rows[0]) if rows else None


def verdicts_for(db: Any, uid: str, *, limit: int = 50) -> list[dict[str, Any]]:
    campaigns.get_campaign(db, uid)
    rows = db.query(
        "SELECT * FROM agent_verdicts WHERE campaign_id = "
        "(SELECT id FROM agent_campaigns WHERE campaign_uid=?) ORDER BY id DESC LIMIT ?",
        (uid, int(limit)),
    )
    return [_verdict_row(row) for row in rows]


def _segment_evidence(
    db: Any,
    campaign: dict[str, Any],
    trials: Sequence[dict[str, Any]],
    segment: str,
    *,
    run_override: str = "",
) -> dict[str, Any]:
    """One segment's numbers, from its own stored run, for the criteria to read."""
    pool = [item for item in trials if item["segment"] == segment]
    candidate = _pick_proposal(pool, (segment,)) if pool else None
    run_id = run_override or (candidate["runId"] if candidate else "")
    source_trial = candidate
    if not run_id:
        return {
            "available": False,
            "source": "none",
            "reason": f"{segment} 段没有 run_id",
            "sharpe": None,
            "returnPct": None,
            "maxDrawdownPct": None,
            "trades": source_trial["trades"] if source_trial else None,
        }
    curve, source = stored_equity(db, run_id)
    windowed = curve_in_window(curve, campaign["windows"][segment]) if curve else []
    if len(windowed) < MIN_SERIES_POINTS:
        return {
            "available": False,
            "source": source,
            "reason": f"{segment} 段窗口内权益点不足（{source}）",
            "sharpe": source_trial["sharpe"] if source_trial else None,
            "returnPct": source_trial["returnPct"] if source_trial else None,
            "maxDrawdownPct": source_trial["maxDrawdownPct"] if source_trial else None,
            "trades": source_trial["trades"] if source_trial else None,
        }
    metrics = segment_metrics(windowed, campaign["interval"])
    metrics["source"] = source
    metrics["runId"] = str(run_id)
    metrics["trades"] = source_trial["trades"] if source_trial else None
    metrics["reason"] = ""
    return metrics


def adjudicate(
    db: Any,
    uid: str,
    *,
    approved_by: str,
    proposal_uid: str = "",
    run_id: str = "",
    segment: str = "test",
    blocks: int = DEFAULT_BLOCKS,
    seed: int | None = None,
    max_combinations: int = DEFAULT_MAX_COMBINATIONS,
) -> dict[str, Any]:
    """Gate-C: unseal once, judge once, store the verdict, return the stored row after.

    Repeat calls are idempotent by construction - the stored row is looked up before
    the unsealing is even attempted, and the unique index on
    (campaign, proposal, segment) is the backstop. The test segment is therefore run
    at most once per campaign, which is the only thing that makes it out of sample.
    """
    if not str(approved_by).strip():
        raise CampaignError("开封测试段必须写明是谁批准的（Gate-C）", status=422)
    if segment not in ("train", "validation", "test"):
        raise CampaignError(f"判决分段只能是 train/validation/test：{segment}", status=422)
    campaign = campaigns.get_campaign(db, uid)

    # The stored row is looked up *before* the unsealing is attempted: the second
    # call must not try to open a window that is already open, and must not run the
    # test segment again.
    existing = stored_verdict(db, uid, proposal_uid=proposal_uid, segment=segment)
    if existing is not None:
        return {**existing, "idempotent": True, "alreadyJudged": True}

    search_trials = campaigns.trials_for(db, uid, include_test=False)
    candidate = None
    if proposal_uid:
        known = {item["proposalId"] for item in campaigns.detail(db, uid)["proposals"]}
        if proposal_uid not in known:
            raise CampaignError(f"提案不在这个战役里：{proposal_uid}", status=404)
        pool = [item for item in search_trials if item["proposalId"] == proposal_uid]
        candidate = _pick_proposal(pool, ("validation", "train"))
        if candidate is None:
            raise CampaignError(
                f"提案 {proposal_uid} 没有验证段证据，开封测试段之前先跑完训练/验证段", status=409
            )
    else:
        candidate = _pick_proposal(search_trials, ("validation", "train"))
        if candidate is None:
            raise CampaignError(
                "没有任何提案有验证段证据：没有可判定的对象，不开封测试段", status=409
            )
        proposal_uid = candidate["proposalId"]
    if candidate["segment"] != "validation":
        raise CampaignError(
            f"提案 {proposal_uid} 只有 {candidate['segment']} 段证据，Gate-C 要求验证段证据", status=409
        )

    if campaign["testUnsealedTs"] is None:
        campaign = campaigns.unseal_test(db, uid, approved_by=approved_by)
        unsealed_now = True
    else:
        unsealed_now = False

    all_trials = campaigns.trials_for(db, uid, include_test=True)
    test_pool = [item for item in all_trials if item["segment"] == segment]
    judged = _pick_proposal(test_pool, (segment,)) if test_pool else None
    judged_run = str(run_id or (judged["runId"] if judged else "") or "")
    test_evidence = _segment_evidence(
        db, campaign, all_trials, segment, run_override=judged_run
    )
    if judged and judged["trades"] is not None:
        test_evidence["trades"] = judged["trades"]
    if not test_evidence.get("available") and judged and judged["sharpe"] is not None:
        # A recorded test trial without a readable curve still carries the engine's
        # own numbers; use them rather than calling the segment empty.
        test_evidence = {
            **test_evidence,
            "available": True,
            "source": "trial-record",
            "sharpe": judged["sharpe"],
            "returnPct": judged["returnPct"],
            "maxDrawdownPct": judged["maxDrawdownPct"],
            "trades": judged["trades"],
            "approximations": ["moments=normal-approximation", "sampleLength=window-approximation"],
        }

    stats = campaign_stats(
        db, uid, include_test=True, observed_segment=segment, observed_run_id=judged_run,
        blocks=blocks, seed=seed, max_combinations=max_combinations,
    )
    validation_evidence = _segment_evidence(db, campaign, all_trials, "validation")
    train_evidence = _segment_evidence(
        db, campaign, [item for item in all_trials if item["segment"] == "train"], "train"
    )
    evidence = {
        "campaign": {
            "uid": uid, "group": campaign["group"], "interval": campaign["interval"],
            "horizonBars": campaign["horizonBars"], "status": campaign["status"],
            "provider": campaign["provider"], "seed": campaign["seed"],
            "roundsUsed": campaign["roundsUsed"], "proposalsUsed": campaign["proposalsUsed"],
            "snapshotHash": campaign["snapshotHash"],
        },
        "judged": {
            "proposalId": proposal_uid, "segment": segment,
            "window": campaign["windows"][segment],
            "runId": judged_run or None,
            "unsealedNow": unsealed_now,
            "unsealedTs": campaign["testUnsealedTs"],
            "selectionRule": "验证段 Sharps 最高的提案（并列取先记录的）",
        },
        "segments": {
            "train": train_evidence,
            "validation": validation_evidence,
            segment: test_evidence,
        },
        "stats": {
            "trials": stats["trials"],
            "trialsRecorded": stats["trialsRecorded"],
            "deflatedSharpe": stats["deflatedSharpe"].get("deflatedSharpe"),
            "deflatedSharpeReason": stats["deflatedSharpe"].get("reason"),
            "expectedMaxSharpe": stats["deflatedSharpe"].get("expectedMaxSharpe"),
            "pbo": stats["pbo"].get("pbo"),
            "pboReason": stats["pbo"].get("reason"),
            "pboSkipped": stats["pbo"].get("skipped"),
            "method": {
                "deflatedSharpe": stats["deflatedSharpe"].get("method"),
                "pbo": stats["pbo"].get("method"),
            },
            "approximations": stats["approximations"],
        },
    }

    parsed = parse_criteria(campaign["successCriteria"])
    outcome = evaluate_criteria(parsed, evidence, judged_segment=segment)
    criteria_verdict = outcome["verdict"]
    verdict = criteria_verdict
    reason = outcome["reason"]
    if not test_evidence.get("available"):
        verdict = "inconclusive"
        reason = (
            f"{segment} 段没有可用的收益序列（{test_evidence.get('reason') or '未知'}）："
            f"证据不足只能记 inconclusive，不能 pass"
        )
    elif criteria_verdict == "pass" and not stats["deflatedSharpe"].get("available"):
        reason = outcome["reason"] + "（注意：DSR 不可用，未被任何条款引用）"

    sentence = (
        f"在 {campaign['group']} / {campaign['interval']} / h{campaign['horizonBars']} 的"
        f"{SEGMENT_LABELS.get(segment, segment)}窗口 "
        f"[{campaign['windows'][segment][0]}, {campaign['windows'][segment][1]}] 上，"
        f"提案 {proposal_uid} 的年化 Sharpe 为 "
        f"{_fmt(test_evidence.get('sharpe'))}、收益 {_fmt(test_evidence.get('returnPct'))}%、"
        f"最大回撤 {_fmt(test_evidence.get('maxDrawdownPct'))}%；"
        f"DSR={_fmt(stats['deflatedSharpe'].get('deflatedSharpe'))}"
        f"（N={stats['trials']}）、PBO={_fmt(stats['pbo'].get('pbo'))}。"
        f"预注册标准：「{campaign['successCriteria']}」→ 判定 {verdict}：{reason}"
    )
    evidence["criteria"] = {
        "text": campaign["successCriteria"],
        "parsed": parsed,
        "outcome": outcome,
        "verdict": verdict,
        "criteriaVerdict": criteria_verdict,
        "sentence": sentence,
    }

    created = int(time.time() * 1000)
    try:
        db.execute(
            "INSERT INTO agent_verdicts (campaign_id, campaign_uid, proposal_uid, segment, "
            "group_name, interval, horizon_bars, window_start_ts, window_end_ts, run_id, bars, "
            "sharpe, return_pct, max_drawdown_pct, trades, deflated_sharpe, expected_max_sharpe, "
            "pbo, trials, verdict, reason, criteria, evidence_json, approved_by, created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _campaign_id(db, uid), uid, proposal_uid, segment,
                campaign["group"], campaign["interval"], int(campaign["horizonBars"]),
                campaign["windows"][segment][0], campaign["windows"][segment][1],
                judged_run or None, test_evidence.get("bars"),
                test_evidence.get("sharpe"), test_evidence.get("returnPct"),
                test_evidence.get("maxDrawdownPct"), test_evidence.get("trades"),
                stats["deflatedSharpe"].get("deflatedSharpe"),
                stats["deflatedSharpe"].get("expectedMaxSharpe"),
                stats["pbo"].get("pbo"), stats["trials"], verdict, reason,
                campaign["successCriteria"],
                json.dumps(evidence, ensure_ascii=False, default=str), approved_by, created,
            ),
        )
    except sqlite3.IntegrityError:
        # Someone judged this (campaign, proposal, segment) between the lookup and the
        # insert: theirs is the verdict of record, and this call did not run anything.
        stored = stored_verdict(db, uid, proposal_uid=proposal_uid, segment=segment)
        if stored is None:
            raise
        return {**stored, "idempotent": True, "alreadyJudged": True}

    # The verdict row carries the test numbers; recording the test trial as well makes
    # the campaign's own ledger show the one out-of-sample measurement. Only a still
    # running campaign accepts a trial (`record_trial` refuses a finished one), and a
    # refusal here must never cost us the verdict that is already stored.
    if judged is None:
        try:
            campaigns.record_trial(
                db, uid, proposal_uid=proposal_uid, segment=segment,
                run_id=judged_run, sharpe=test_evidence.get("sharpe"),
                return_pct=test_evidence.get("returnPct"),
                max_drawdown_pct=test_evidence.get("maxDrawdownPct"),
                trades=test_evidence.get("trades"),
                verdict=verdict, reason=f"Gate-C 判决 {verdict}",
            )
        except CampaignError:
            pass

    stored = stored_verdict(db, uid, proposal_uid=proposal_uid, segment=segment)
    if stored is None:  # pragma: no cover - the insert above commits before this
        raise CampaignError("判决写入后读不回来，数据库可能正在被别的进程改写", status=500)
    return {
        **stored,
        "idempotent": False,
        "alreadyJudged": False,
        "criteriaVerdict": criteria_verdict,
        "sentence": sentence,
        "unsealedNow": unsealed_now,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "不可用"
    if isinstance(value, (int, float)):
        return f"{value:.4f}"
    return str(value)
