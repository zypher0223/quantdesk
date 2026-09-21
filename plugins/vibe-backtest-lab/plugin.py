#!/usr/bin/env python3
"""Vibe-Trading Alpha Zoo as a QuantDesk factor provider.

What this plugin is: a JSON-RPC adapter (see PLUGIN_API.md) around the vendored
alpha implementations in `vendor/vibe-trading/`, restricted to the alphas that can
be computed honestly from what QuantDesk hands a factor provider.

It answers for two libraries, because a capability has exactly one owner:

* `vibezoo:*` — the vendored Vibe-Trading zoo (daily, cross-sectional alphas
  excluded by an empirical gate, see `factor_allowlist.json`);
* `vibe.*` — QuantDesk's own 28 time-series factors at `15m/1h/4h/1d`, carried in
  `quantdesk_factors.py`, which is a byte-for-byte copy of
  `plugins/vibe-factors/plugin.py` (a test re-hashes it on every run). Those ids
  were the only ones the engine's existing factor runs ever used, so taking over
  `factor_provider` without them would have broken every one of them.

Three boundaries are structural rather than promised in a README:

* **The catalogue is a build artifact.** `factor_allowlist.json` lists every factor
  this plugin will ever name, together with the sha256 of the module that computes
  it. At load the plugin re-hashes those modules and refuses to serve a factor whose
  file changed, so "allowlist" is a property of the bytes on disk.
* **The panel is built here from the request's candles only.** The vendor package
  has no network client, no data reader and no clock: a factor is a pure function
  of the panel it is handed, and every derived field (`amount`) is labelled as
  derived in the response warnings.
* **Nothing but a factor series goes back.** No prices, no returns, no P&L: the
  engine owns the numbers, a provider only describes them.

The zoo is daily by construction (its windows are in trading days), so those factors
are served at `1d` and refuse other intervals instead of being relabelled. The
`vibe.*` library keeps its own four intervals.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
import traceback
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent
VENDOR_ROOT = PLUGIN_ROOT / "vendor" / "vibe-trading"
ALLOWLIST_PATH = PLUGIN_ROOT / "factor_allowlist.json"
SOURCE_PATH = VENDOR_ROOT / "SOURCE.json"
QUANTDESK_LIBRARY = PLUGIN_ROOT / "quantdesk_factors.py"
QUANTDESK_PROVENANCE = PLUGIN_ROOT / "quantdesk_factors.provenance.json"

PROTOCOL = "2.0"
PLUGIN_VERSION = "0.2.0"
# The zoo is daily (its warmups are counted in trading days). The `vibe.*` library
# is the one that decides which of the four intervals it supports.
ZOO_INTERVALS = ("1d",)
ZOO_PREFIX = "vibezoo:"
# A factor's panel is built once per call from the candles in that call. The engine
# caps a request at 64 ids; this is the number this provider is willing to compute
# in one process before the caller should split the request.
MAX_FACTORS_PER_CALL = 32
# Everything the panel carries. `amount` is turnover, which QuantDesk derives as
# close × volume; a true `vwap` cannot be derived and is therefore never served.
PANEL_FIELDS = ("open", "high", "low", "close", "volume", "amount")


def log(message: str) -> None:
    """Diagnostics go to stderr; stdout carries exactly one response per request."""
    print(message, file=sys.stderr, flush=True)


class ProviderError(RuntimeError):
    """A request the provider refuses, with a reason an operator can act on."""


# --------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------

class Catalog:
    """The allowlisted factors, with their implementation bytes verified once."""

    def __init__(self) -> None:
        raw = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
        self.generated_at = raw.get("generatedAt", "")
        self.source = raw.get("source", {})
        self.stats = raw.get("stats", {})
        self.policy = raw.get("policy", {})
        self.factors: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        self.by_module: dict[str, dict[str, Any]] = {}
        tampered: list[str] = []
        for item in raw["factors"]:
            # `modulePath` is the import path from the vendor root (`src.factors....`).
            path = VENDOR_ROOT / (item["modulePath"].replace(".", "/") + ".py")
            if not path.is_file():
                tampered.append(f"{item['id']}（缺少模块 {item['modulePath']}）")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != item["moduleSha256"]:
                tampered.append(f"{item['id']}（sha256 与白名单不一致）")
                continue
            self.factors.append(item)
            self.by_id[item["id"]] = item
            self.by_module[item["modulePath"]] = item
        if tampered:
            raise ProviderError(
                "vendored 因子代码与 factor_allowlist.json 不一致，拒绝提供因子："
                + "；".join(tampered[:5])
            )
        if not self.factors:
            raise ProviderError("factor_allowlist.json 没有可用因子")

    @property
    def provider_version(self) -> str:
        commit = str(self.source.get("commit") or "")[:8]
        return f"{PLUGIN_VERSION}+vibe-trading-{str(self.source.get('describe') or commit)}"

    @property
    def vendor_commit(self) -> str:
        return str(self.source.get("commit") or "")

    def definition(self, item: dict[str, Any]) -> dict[str, Any]:
        """One factor as the v3 catalogue describes it."""
        requirement = "、".join(item["requiredFields"])
        latex = item.get("formulaLatex") or ""
        description = f"{item['name']}｜取自 Vibe-Trading {item['zoo']}｜需要 {requirement}"
        if latex:
            description += f"｜公式 {latex}"
        return {
            "id": f"vibezoo:{item['id']}",
            "name": item["name"][:120],
            "family": f"vibe-trading/{item['zoo']}",
            "mode": "time_series",
            "requiredFields": list(item["requiredFields"]),
            "warmupBars": int(item["warmupBars"]),
            "supportedTimeframes": list(ZOO_INTERVALS),
            "implementationVersion": item["moduleSha256"][:16],
            "sources": ["bybit", "derived"],
            "formulaHash": item["moduleSha256"],
            "description": description[:500],
        }

    def resolve(self, factor_id: str) -> dict[str, Any]:
        key = factor_id.split(":", 1)[1] if factor_id.startswith("vibezoo:") else factor_id
        item = self.by_id.get(key)
        if item is None:
            raise ProviderError(
                f"未知因子 {factor_id}；本提供者提供 {len(self.factors)} 个白名单因子，"
                "完整列表见 factor_allowlist.json"
            )
        return item


# --------------------------------------------------------------------------
# Computation
# --------------------------------------------------------------------------

_LIBRARY_MODULE: Any = None


def quantdesk_library():
    """Load QuantDesk's own factor library, copied verbatim from vibe-factors.

    Loaded from an explicit path rather than imported as a package: the plugin is
    copied into the install root on its own, and the file records its origin hash in
    `quantdesk_factors.provenance.json`. Its `main()` is guarded, so importing it
    never starts a JSON-RPC loop.
    """
    global _LIBRARY_MODULE
    if _LIBRARY_MODULE is None:
        if not QUANTDESK_LIBRARY.is_file():
            raise ProviderError(f"缺少 QuantDesk 因子库副本：{QUANTDESK_LIBRARY.name}")
        spec = importlib.util.spec_from_file_location("quantdesk_factor_library", QUANTDESK_LIBRARY)
        if spec is None or spec.loader is None:
            raise ProviderError(f"无法加载 {QUANTDESK_LIBRARY.name}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LIBRARY_MODULE = module
    return _LIBRARY_MODULE


def library_provenance() -> dict[str, Any]:
    try:
        return json.loads(QUANTDESK_PROVENANCE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _module_of(item: dict[str, Any]):
    if str(VENDOR_ROOT) not in sys.path:
        sys.path.insert(0, str(VENDOR_ROOT))
    return importlib.import_module(item["modulePath"])


def _panel(candles: list[dict[str, Any]], symbol: str, needed: set[str]):
    """A single-symbol panel, built only from the candles in this request.

    The zoo's operators are written against a wide frame (index = time, columns =
    symbols); one symbol is the honest width here, because the engine's v3 request
    carries one symbol's bars and a cross-sectional factor cannot be computed from
    it. That is why cross-sectional alphas are excluded from the allowlist rather
    than evaluated on a one-column frame, where `rank()` would be constant.
    """
    import pandas as pd

    times = pd.to_datetime([int(bar["time"]) for bar in candles], unit="ms", utc=True)
    close = pd.Series([float(bar["close"]) for bar in candles], index=times)
    volume = pd.Series([float(bar.get("volume") or 0.0) for bar in candles], index=times)

    def frame(values) -> "pd.DataFrame":
        return pd.DataFrame({symbol: values}, index=times, dtype="float64")

    panel: dict[str, Any] = {"close": frame(close)}
    if "open" in needed:
        panel["open"] = frame([float(bar["open"]) for bar in candles])
    if "high" in needed:
        panel["high"] = frame([float(bar["high"]) for bar in candles])
    if "low" in needed:
        panel["low"] = frame([float(bar["low"]) for bar in candles])
    if "volume" in needed:
        panel["volume"] = frame(volume)
    if "amount" in needed:
        amount, derived = [], False
        for bar in candles:
            turnover = bar.get("turnover")
            if turnover in (None, ""):
                amount.append(float(bar["close"]) * float(bar.get("volume") or 0.0))
                derived = True
            else:
                amount.append(float(turnover))
        panel["amount"] = frame(amount)
        if derived:
            panel["__amount_derived__"] = True
    return panel, times


def _series_from(result: Any, times, symbol: str) -> tuple[list[float | None], str]:
    """The computed column, aligned back onto the request's bars."""
    import numpy as np

    if not hasattr(result, "iloc"):
        raise ProviderError(f"因子返回了 {type(result).__name__}，不是数据框")
    if result.empty:
        raise ProviderError("因子返回了空数据框")
    warning = ""
    if result.index.equals(times):
        column = result.iloc[:, 0]
    elif len(result) == len(times):
        # A factor that rebuilt its frame without carrying our index. Positional
        # alignment is only safe because the panel was in bar order.
        column = result.iloc[:, 0]
        warning = "因子没有保留面板索引，已按K线顺序对齐"
    else:
        raise ProviderError(
            f"因子返回 {len(result)} 行，与请求的 {len(times)} 根K线不匹配"
        )
    values: list[float | None] = []
    for value in np.asarray(column.to_numpy(), dtype="float64"):
        values.append(None if not np.isfinite(value) else float(value))
    return values, warning


def compute(catalog: Catalog, params: dict[str, Any]) -> dict[str, Any]:
    symbol = str(params.get("symbol") or "")
    if not symbol:
        raise ProviderError("请求缺少 symbol")
    interval = str(params.get("timeframe") or "")
    candles = params.get("candles") or []
    ids = list(params.get("factorIds") or [])
    if not ids:
        raise ProviderError("请求没有指定 factorIds")
    if len(ids) > MAX_FACTORS_PER_CALL:
        raise ProviderError(
            f"一次请求最多计算 {MAX_FACTORS_PER_CALL} 个因子，收到 {len(ids)} 个；请分批调用"
        )
    if len(ids) != len(set(ids)):
        raise ProviderError("factorIds 有重复项")

    # Two libraries, one capability. `vibezoo:` ids are the vendored daily zoo;
    # everything else belongs to QuantDesk's own multi-interval library, which is
    # what every pre-existing factor run asked for.
    zoo_ids = [factor_id for factor_id in ids if factor_id.startswith(ZOO_PREFIX)]
    local_ids = [factor_id for factor_id in ids if not factor_id.startswith(ZOO_PREFIX)]
    series: list[dict[str, Any]] = []
    warnings: list[str] = []
    if zoo_ids:
        if interval not in ZOO_INTERVALS:
            raise ProviderError(
                f"zoo 因子只提供 {'/'.join(ZOO_INTERVALS)} 日频：收到 {interval or '未声明'}；"
                "zoo 的窗口以交易日计数，换算成日内周期会改变因子的定义"
            )
        zoo_series, zoo_warnings = _compute_zoo(catalog, params, zoo_ids, symbol, candles)
        series.extend(zoo_series)
        warnings.extend(zoo_warnings)
    if local_ids:
        local_series, local_warnings = _compute_local(params, local_ids)
        series.extend(local_series)
        warnings.extend(local_warnings)
    by_id = {entry["factorId"]: entry for entry in series}
    return {
        "snapshotHash": str(params.get("snapshotHash") or ""),
        "series": [by_id[factor_id] for factor_id in ids if factor_id in by_id],
        "warnings": warnings,
    }


def _compute_local(
    params: dict[str, Any], ids: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """QuantDesk's own factors, computed by the library copied from vibe-factors."""
    library = quantdesk_library()
    request = {**params, "factorIds": ids}
    result = library.compute(request)
    return list(result.get("series") or []), list(result.get("warnings") or [])


def _compute_zoo(
    catalog: Catalog, params: dict[str, Any], ids: list[str], symbol: str, candles: list
) -> tuple[list[dict[str, Any]], list[str]]:
    """The vendored zoo, on a single-symbol panel built from this request's bars."""
    items = [catalog.resolve(factor_id) for factor_id in ids]
    longest = max(int(item["warmupBars"]) for item in items)
    if len(candles) < longest + 1:
        raise ProviderError(
            f"K线不足：最长的因子需要 {longest} 根预热（共 {longest + 1} 根），"
            f"请求只有 {len(candles)} 根"
        )

    needed = {"close"}
    for item in items:
        needed.update(item["requiredFields"])
    panel, times = _panel(candles, symbol, needed)
    amount_derived = bool(panel.pop("__amount_derived__", False))

    series, warnings = [], []
    if amount_derived:
        warnings.append(
            "amount 用的是 QuantDesk 的成交额（收盘价×成交量）而不是逐笔成交额"
        )
    for item in items:
        try:
            module = _module_of(item)
            raw = module.compute(panel)
        except ProviderError:
            raise
        except Exception as exc:  # a factor crashing must not take the request down
            log(f"factor {item['id']} 失败：{traceback.format_exc()}")
            raise ProviderError(f"因子 {item['id']} 计算失败：{type(exc).__name__}: {exc}") from exc
        values, warning = _series_from(raw, times, symbol)
        if warning:
            warnings.append(f"{item['id']}：{warning}")
        series.append(
            {
                "factorId": f"vibezoo:{item['id']}",
                "values": [
                    {"time": int(bar["time"]), "value": value}
                    for bar, value in zip(candles, values)
                ],
                "implementationVersion": item["moduleSha256"][:16],
            }
        )
    known = sum(1 for entry in series for point in entry["values"] if point["value"] is not None)
    total = sum(len(entry["values"]) for entry in series)
    if known == 0:
        warnings.append("所有取值都是 null：预热或数据长度不足，没有用 0 填充")
    log(f"{symbol} {params.get('timeframe')}：zoo {len(items)} 个因子，{known}/{total} 个取值非空")
    return series, warnings


# --------------------------------------------------------------------------
# The self-improvement agent (protocol v4)
# --------------------------------------------------------------------------
#
# "Self-improving" here means *searching*, and the search is deliberately dull: a
# seeded enumeration over the campaign's frozen factor space, narrowed each round by
# what the previous round measured. Three properties matter more than cleverness:
#
# * **it cannot invent a factor**: every proposal draws its ids from the space the
#   campaign froze at registration, and the engine re-checks that (`check_proposals`)
#   against the manifest this plugin declares;
# * **it cannot see the answer**: it is handed train/validation summaries only. The
#   protocol's `AgentTrialSummary.segment` has no `test` member, so the sealed window
#   is not something this code chooses to respect - it is something it cannot name;
# * **it is reproducible**: the same campaign, round and prior trials produce the same
#   proposals, because the generator is seeded by them rather than by a clock.

AGENT_VERSION = "vibe-lab-agent/1"
# Thresholds a proposal may set, and the range each may take. Declared here and
# echoed in `agent.manifest` so the engine can refuse anything outside them.
AGENT_PARAMETERS: dict[str, list[float]] = {
    "entryThreshold": [0.0, 1.0],
    "exitThreshold": [-1.0, 0.0],
    "windowScale": [1, 4],
}
AGENT_RULE_TEMPLATES = ("sign_threshold", "mean_reversion")


def agent_manifest(catalog: Catalog, params: dict[str, Any]) -> dict[str, Any]:
    """The boundaries this provider will propose inside, before it proposes."""
    requested = [str(item) for item in (params.get("factorIds") or [])]
    known = [item for item in requested if item.split(":", 1)[-1] in catalog.by_id]
    local = [item for item in requested if item not in known and ":" not in item]
    factory = Path(__file__).resolve().parent / "quantdesk_factors.py"
    if not requested and params.get("allFactors"):
        # Only when someone explicitly asks for it: a campaign always names its space.
        known = [f"vibezoo:{item}" for item in catalog.by_id]
    return {
        "agentVersion": AGENT_VERSION,
        "mode": "deterministic_search",
        "proposalSpace": {
            # The space is the campaign's frozen list, restricted to what this
            # provider can actually compute; an empty request means "everything you
            # have that is daily", which is what a manual campaign would ask for.
            "factorIds": known or local,
            "parameters": AGENT_PARAMETERS,
            "ruleTemplates": list(AGENT_RULE_TEMPLATES),
            "maxProposalsPerRound": MAX_FACTORS_PER_CALL,
            "maxRounds": 5,
        },
        "requires": ["QuantDesk 提供的已收盘K线", "冻结的因子空间"],
        "never": ["自取行情", "读取测试段", "写入代码或依赖", "下单"],
        "providerVersion": catalog.provider_version,
        "warnings": [
            "确定性搜索：提案由（战役、轮次、已有试验）决定，同样输入必得同样提案",
            f"自研因子库副本：{'存在' if factory.is_file() else '缺失'}",
        ],
    }


def _seeded_order(items: list[str], *parts: Any) -> list[str]:
    """A stable shuffle: the same campaign and round always give the same order."""
    import random

    seed = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    rng = random.Random(int(seed[:16], 16))
    shuffled = list(items)
    rng.shuffle(shuffled)
    return shuffled


def _proposal_ids(round_number: int, index: int) -> str:
    return f"r{int(round_number):02d}-p{int(index):03d}"


def _hypothesis(kind: str, factor_id: str, parameters: dict[str, float], round_number: int) -> str:
    threshold = parameters.get("entryThreshold")
    if kind == "mean_reversion":
        return (
            f"第 {round_number} 轮：{factor_id} 处于极端低位时反弹，用 {-1 * float(parameters.get('exitThreshold', -0.5)):.2f} "
            "的滞后阈值控制换手"
        )
    return (
        f"第 {round_number} 轮：{factor_id} 的符号在 24 根视野内延续，"
        f"入场阈值 {float(threshold):.2f}"
    )


def _round_proposals(
    catalog: Catalog,
    params: dict[str, Any],
    *,
    round_number: int,
    prior_trials: list[dict[str, Any]],
    budget: dict[str, Any],
) -> tuple[list[dict[str, Any]], str, list[str]]:
    """One round's candidates, chosen from the space and the previous results."""
    space = agent_manifest(catalog, params)["proposalSpace"]
    factors = list(space["factorIds"])
    if not factors:
        raise ProviderError(
            "没有可提案的因子：请求里没有给出战役冻结的因子空间。"
            "propose 与 reflect 都必须带上 factorIds —— 代理只能收窄搜索空间，不能自己扩大它"
        )
    limit = max(1, min(int(budget.get("proposals") or MAX_FACTORS_PER_CALL), MAX_FACTORS_PER_CALL))
    campaign_id = str(params.get("campaignId") or "")
    # What the previous rounds measured, as a ranking. Only train/validation
    # summaries can appear here; the protocol has no test segment to leak.
    scored = [item for item in prior_trials if item.get("sharpe") is not None]
    scored.sort(key=lambda item: float(item["sharpe"]), reverse=True)
    best = scored[0]["proposalId"] if scored else ""
    order = _seeded_order(factors, campaign_id, round_number, best)
    warnings: list[str] = []
    if scored:
        warnings.append(
            f"上一轮 {len(scored)} 个有读数的提案中最好的是 {best}（Sharpe {float(scored[0]['sharpe']):.2f}）"
        )

    proposals: list[dict[str, Any]] = []
    index = 0
    # Round 1 asks about each factor once, at a neutral threshold. Later rounds keep
    # the best factor in play and spend the budget on its neighbourhood, which is the
    # cheapest honest form of "improvement": no new factors, no new code.
    plan: list[tuple[str, str, dict[str, float]]] = []
    for factor_id in order:
        plan.append((factor_id, "sign_threshold", {"entryThreshold": 0.0, "exitThreshold": -0.5}))
    if scored and best:
        best_trial = scored[0]
        keep = [item for item in (best_trial.get("factorIds") or [])] or [order[0]]
        for step, threshold in enumerate((0.25, 0.5, 0.75, 1.0)):
            plan.append(
                (keep[0], "sign_threshold",
                 {"entryThreshold": threshold, "exitThreshold": -threshold / 2,
                  "windowScale": 1 + (step % 3)})
            )
        for factor_id in order[: max(1, limit // 4)]:
            plan.append(
                (factor_id, "mean_reversion", {"entryThreshold": 0.75, "exitThreshold": -0.25})
            )
    for factor_id, kind, parameters in plan:
        if len(proposals) >= limit:
            break
        proposal_id = _proposal_ids(round_number, index)
        if any(item.get("proposalId") == proposal_id for item in prior_trials):
            index += 1
            continue
        proposals.append(
            {
                "proposalId": proposal_id,
                "kind": "parameter_set" if kind == "sign_threshold" else "rule",
                "factorIds": [factor_id if factor_id.startswith("vibezoo:") else factor_id],
                "parameters": {key: float(value) for key, value in parameters.items()},
                "rule": {"type": kind, "horizonBars": int(params.get("horizonBars") or 0) or None},
                "hypothesis": _hypothesis(kind, factor_id, parameters, round_number),
                "expectedFailureMode": (
                    "换手成本吃掉信号；或该因子在这组标的上只是市场 beta 的代理"
                ),
            }
        )
        index += 1
    for proposal in proposals:
        if proposal["rule"].get("horizonBars") is None:
            proposal["rule"].pop("horizonBars")
    stop = ""
    if not proposals:
        stop = "空间里已经没有未试过的提案"
    return proposals, stop, warnings


def agent_propose(catalog: Catalog, params: dict[str, Any]) -> dict[str, Any]:
    """One round of proposals, from the frozen space and the visible history."""
    round_number = int(params.get("round") or 1)
    prior = list(params.get("priorTrials") or [])
    proposals, stop, warnings = _round_proposals(
        catalog, params, round_number=round_number, prior_trials=prior,
        budget=params.get("budget") or {},
    )
    return {"proposals": proposals, "warnings": warnings, "stopReason": stop}


def agent_reflect(catalog: Catalog, params: dict[str, Any]) -> dict[str, Any]:
    """The previous round's outcome, fed back into the next round's candidates.

    The reflection is a sentence and a plan, not a performance claim: this plugin
    never reports a return, and the trials it reasons about are the engine's numbers.
    """
    round_number = int(params.get("round") or 1)
    trials = list(params.get("trials") or [])
    remaining = int(params.get("remainingRounds") or 0)
    scored = [item for item in trials if item.get("sharpe") is not None]
    if not scored:
        reflection = (
            f"第 {round_number} 轮没有可用的试验读数（{len(trials)} 个提案），"
            "下一轮保持同样的因子集合，只改阈值"
        )
    else:
        ordered = sorted(scored, key=lambda item: float(item["sharpe"]), reverse=True)
        worst = ordered[-1]
        reflection = (
            f"第 {round_number} 轮共 {len(scored)} 个读数，最好 {ordered[0]['proposalId']} "
            f"(Sharpe {float(ordered[0]['sharpe']):.2f})，最差 {worst['proposalId']} "
            f"({float(worst['sharpe']):.2f})；下一轮收敛到最好者附近并换一组阈值"
        )
    proposals, stop, warnings = _round_proposals(
        catalog,
        {**params, "round": round_number + 1, "priorTrials": trials},
        round_number=round_number + 1,
        prior_trials=trials,
        budget=params.get("budget") or {},
    )
    if remaining <= 0:
        stop = stop or "轮数用尽"
    return {
        "reflection": reflection,
        "proposals": proposals,
        "stopReason": stop,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# JSON-RPC surface
# --------------------------------------------------------------------------

def health(catalog: Catalog) -> dict[str, Any]:
    library = quantdesk_library()
    local = library.catalog()["factors"]
    provenance = library_provenance()
    return {
        "ok": True,
        "message": (
            f"zoo {len(catalog.factors)} 个（{'/'.join(ZOO_INTERVALS)}）+ "
            f"自研 {len(local)} 个（{'/'.join(library.SUPPORTED_TIMEFRAMES)}）"
        ),
        "providerVersion": catalog.provider_version,
        "factors": len(catalog.factors) + len(local),
        "zooFactors": len(catalog.factors),
        "localFactors": len(local),
        "generatedAt": catalog.generated_at,
        "vendorCommit": catalog.vendor_commit,
        "localLibrarySha256": str(provenance.get("sha256") or ""),
        "intervals": sorted(set(ZOO_INTERVALS) | set(library.SUPPORTED_TIMEFRAMES)),
        "network": False,
    }


def catalog_result(catalog: Catalog) -> dict[str, Any]:
    warnings = [
        f"白名单收录 {catalog.stats.get('upstreamTotal')} 个上游因子中的 "
        f"{catalog.stats.get('included')} 个；被排除的原因见 factor_allowlist.json 的 excluded 列表"
    ]
    excluded = catalog.stats.get("excludedByGate") or {}
    if excluded:
        warnings.append(
            "排除原因：" + "；".join(f"{name} {count} 个" for name, count in sorted(excluded.items()))
        )
    if catalog.policy.get("note"):
        warnings.append(str(catalog.policy["note"]))
    library = quantdesk_library()
    local = library.catalog()["factors"]
    zoo = [catalog.definition(item) for item in catalog.factors]
    warnings.append(
        f"另含 QuantDesk 自研因子 {len(local)} 个（{'/'.join(library.SUPPORTED_TIMEFRAMES)}），"
        "实现由 plugins/vibe-factors/plugin.py 逐字节复制而来"
    )
    zoo_ids = {entry["id"] for entry in zoo}
    overlap = sorted(zoo_ids & {entry["id"] for entry in local})
    if overlap:
        raise ProviderError(f"两族因子 ID 冲突：{', '.join(overlap)}")
    return {
        "providerVersion": catalog.provider_version,
        "factors": zoo + local,
        "warnings": warnings,
    }


def dispatch(catalog: Catalog, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "health":
        return health(catalog)
    if method == "factor.catalog":
        return catalog_result(catalog)
    if method == "factor.compute":
        return compute(catalog, params)
    if method == "agent.manifest":
        return agent_manifest(catalog, params)
    if method == "agent.propose":
        return agent_propose(catalog, params)
    if method == "agent.reflect":
        return agent_reflect(catalog, params)
    raise ProviderError(f"不支持的方法：{method}")


def main() -> int:
    try:
        catalog = Catalog()
    except (OSError, ValueError, ProviderError) as exc:
        log(f"插件无法启动：{exc}")
        return 2
    log(
        f"vibe-backtest-lab {PLUGIN_VERSION} 就绪：{len(catalog.factors)} 个因子，"
        f"上游提交 {catalog.vendor_commit[:8]}"
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            result = dispatch(catalog, str(request.get("method") or ""), request.get("params") or {})
            response: dict[str, Any] = {"jsonrpc": PROTOCOL, "id": request.get("id"), "result": result}
        except ProviderError as exc:
            response = {
                "jsonrpc": PROTOCOL,
                "id": request.get("id"),
                "error": {"code": -32000, "message": str(exc)},
            }
        except Exception as exc:  # never let a traceback reach stdout
            log(traceback.format_exc())
            response = {
                "jsonrpc": PROTOCOL,
                "id": request.get("id"),
                "error": {"code": -32603, "message": f"插件内部错误：{type(exc).__name__}: {exc}"},
            }
        sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
