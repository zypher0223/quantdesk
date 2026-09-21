"""Factors and statistical validation, through the plugin boundary.

The engine supplies closed bars and its own finished backtest; an enabled
`factor_provider` / `backtest_validator` plugin returns factor values and
statistical diagnostics. Nothing here reaches the network, and nothing here
recomputes the P&L - the engine's numbers are the truth a validator describes.

Two rules shape this module:

* the catalogue is stored, so the factor library stays readable when the plugin is
  disabled or its process will not start;
* a missing plugin is `unavailable`, never an exception in the middle of a page -
  the same treatment external research gets.
"""

from __future__ import annotations

import json
import time
import zlib
from typing import Any, Callable

from .config.settings import quantdesk_home
from .studies import StudyError
from .datahub.db import Database
from .plugins import PluginError, PluginManager, PluginRegistry
from .plugins.protocol import (
    FactorComputeRequest,
    FactorDefinition,    ValidationAnalyzeRequest,
)

MAX_FACTORS_PER_RUN = 64

# How much factor work one synchronous request may ask for: bars x factors.
# Measured on this machine, 140k units (5,000 bars x 28 factors) takes about five
# seconds and produces a multi-megabyte answer, and 560k units takes twenty. Past
# that a request stops being an interaction, so it is refused with the two ways to
# narrow it instead of being allowed to become a browser timeout.
SYNC_FACTOR_UNIT_BUDGET = 150_000


def factor_units(bars: int, factors: int) -> int:
    return max(0, int(bars)) * max(0, int(factors))


def require_factor_budget(bars: int, factors: int, *, budget: int = SYNC_FACTOR_UNIT_BUDGET) -> int:
    units = factor_units(bars, factors)
    if units > budget:
        raise StudyError(
            "too_large",
            f"该因子请求预计 {units:,} 单位工作量（{bars:,} 根 × {factors} 个因子），"
            f"超过单次上限 {budget:,} 单位。请减少K线根数或分成几批因子计算；"
            "结果会连同覆盖率一起存入因子任务，可用 GET /api/factors/runs/{id} 读取。",
            status=409,
            detail={
                "title": "因子请求过大",
                "detail": f"预计 {units:,} 单位，单次上限 {budget:,} 单位",
                "action": "减少K线根数，或分批选择因子；已计算的批次可在因子任务里查看",
                "factorUnits": units,
                "factorBudget": budget,
            },
        )
    return units


class FactorUnavailable(RuntimeError):
    """No enabled factor provider can answer this request."""


def manager() -> PluginManager:
    return PluginManager(quantdesk_home())


def enabled_provider(manager_: PluginManager | None = None, capability: str = "factor_provider") -> str | None:
    """The id of the enabled plugin that serves a capability, if there is one."""
    from .research.external_bridge import enabled_plugin_id

    return enabled_plugin_id(manager_ or manager(), capability)


def _definition(row: Any) -> dict[str, Any]:
    return {
        "id": row["factor_id"], "name": row["name"], "family": row["family"], "mode": row["mode"],
        "sources": json.loads(row["sources_json"] or "[]"),
        "requiredFields": json.loads(row["required_json"] or "[]"),
        "warmupBars": int(row["warmup_bars"] or 0),
        "supportedTimeframes": json.loads(row["timeframes_json"] or "[]"),
        "implementationVersion": row["implementation"],
        "formulaHash": row["formula_hash"],
        "description": row["description"],
        "provider": row["provider"],
        "providerVersion": row["provider_version"],
    }


def stored_catalog(db: Database, provider: str | None = None) -> list[dict[str, Any]]:
    """What the catalogues we have already seen say, without asking a plugin."""
    if provider:
        rows = db.query("SELECT * FROM factor_definitions WHERE provider=? ORDER BY family, factor_id",
                        (provider,))
    else:
        rows = db.query("SELECT * FROM factor_definitions ORDER BY provider, family, factor_id")
    return [_definition(row) for row in rows]


def record_definitions(db: Database, provider: str, definitions: list[dict[str, Any]],
                       *, provider_version: str = "") -> int:
    now = int(time.time() * 1000)
    written = 0
    for item in definitions:
        db.execute(
            "INSERT INTO factor_definitions "
            "(provider, factor_id, name, family, mode, sources_json, required_json, warmup_bars, "
            " timeframes_json, implementation, formula_hash, description, provider_version, "
            " created_ts, updated_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(provider, factor_id) DO UPDATE SET "
            " name=excluded.name, family=excluded.family, mode=excluded.mode, "
            " sources_json=excluded.sources_json, required_json=excluded.required_json, "
            " warmup_bars=excluded.warmup_bars, timeframes_json=excluded.timeframes_json, "
            " implementation=excluded.implementation, formula_hash=excluded.formula_hash, "
            " description=excluded.description, provider_version=excluded.provider_version, "
            " updated_ts=excluded.updated_ts",
            (
                provider, item["id"], item.get("name") or "", item.get("family") or "",
                item.get("mode") or "time_series",
                json.dumps(item.get("sources") or [], ensure_ascii=False),
                json.dumps(item.get("requiredFields") or [], ensure_ascii=False),
                int(item.get("warmupBars") or 0),
                json.dumps(item.get("supportedTimeframes") or [], ensure_ascii=False),
                item.get("implementationVersion") or "", item.get("formulaHash") or "",
                item.get("description") or "", provider_version, now, now,
            ),
        )
        written += 1
    return written


def catalog(db: Database, manager_: PluginManager | None = None, *, refresh: bool = True) -> dict[str, Any]:
    """The factor library: asked of the plugin when one is enabled, else stored."""
    manager_ = manager_ or manager()
    provider = enabled_provider(manager_)
    result: dict[str, Any] = {"provider": provider, "providerVersion": "", "factors": [], "warnings": [],
                              "source": "stored", "available": False}
    if provider:
        try:
            outcome = PluginRegistry(manager_).factor_catalog(provider)
            definitions = [item.model_dump() for item in outcome.factors]
            if refresh:
                record_definitions(db, provider, definitions, provider_version=outcome.providerVersion)
            result.update({
                "providerVersion": outcome.providerVersion,
                "factors": [{**item, "provider": provider, "providerVersion": outcome.providerVersion}
                            for item in definitions],
                "warnings": list(outcome.warnings),
                # Only a plugin that answered may be reported as the source.
                "source": "plugin",
                "available": True,
            })
            return result
        except PluginError as exc:
            result["warnings"].append(f"因子插件未响应：{exc}")
    stored = stored_catalog(db)
    result["factors"] = stored
    result["provider"] = stored[0]["provider"] if stored else None
    result["providerVersion"] = stored[0]["providerVersion"] if stored else ""
    if not stored:
        result["warnings"].append("没有可用的因子提供者：既没有启用的插件，也没有已存目录")
    return result


def _series_for(db: Database, symbol: str, interval: str, bars: int) -> tuple[list[dict], dict, str]:
    """The closed bars a factor run reads, with the version they belong to."""
    from .config.instruments import require_instrument
    from .datahub.view import read_history

    spec = require_instrument(symbol)
    snapshot = read_history(
        db, symbol=spec.venue_symbol, interval=interval, bars=bars,
        display_symbol=spec.display_symbol, product_type=spec.product_type,
        with_funding=True, with_marks=False,
    )
    bars_payload = [
        {"time": int(row["ts"]), "open": float(row["open"]), "high": float(row["high"]),
         "low": float(row["low"]), "close": float(row["close"]),
         "volume": float(row.get("volume") or 0.0),
         "turnover": (float(row["close"]) * float(row.get("volume") or 0.0)) or None}
        for row in snapshot.bars
    ]
    return bars_payload, snapshot.provenance(), snapshot.version or ""


# The plugin protocol caps these lists at 20,000 entries each. Open interest is
# sampled hourly whatever the bar interval is, so a long daily window overflows it:
# 2,000 daily bars cover 48,000 hourly snapshots. Truncating to the most recent
# entries keeps what a factor can actually read (its warmup is measured in bars, and
# the longest in either library is 200) and turns a crash into a stated limitation.
MAX_CARRY_ROWS = 20_000


def _carry_inputs(db: Database, symbol: str, interval: str, bars: list[dict],
                  *, oi_interval: str = "1h") -> tuple[list[dict], list[dict], list[str]]:
    """Funding and open interest over the same window as the bars.

    Two of the factor families are about carry and positioning, and the engine owns
    that history: a provider never fetches, so it is handed exactly the settlements
    and snapshots that cover the bars it was given - nothing newer, nothing outside
    the window. When that window is longer than the protocol can carry, the tail is
    kept and the truncation is reported rather than silently sent.
    """
    from .config.instruments import require_instrument

    if not bars:
        return [], [], []
    spec = require_instrument(symbol)
    start, end = int(bars[0]["time"]), int(bars[-1]["time"])
    # `load_funding` is defined twice in the store (the later definition wins) and
    # the effective signature takes the window positionally; call it that way.
    funding = db.load_funding("bybit", spec.venue_symbol, start, end)
    interest = db.load_oi("bybit", spec.venue_symbol, start_ts=start, end_ts=end,
                          interval=oi_interval)
    notes: list[str] = []
    if len(interest) > MAX_CARRY_ROWS:
        notes.append(
            f"持仓量快照 {len(interest):,} 条超过协议上限 {MAX_CARRY_ROWS:,}，"
            f"只传最近 {MAX_CARRY_ROWS:,} 条；更早的K线上依赖持仓量的因子会返回 null"
        )
        interest = interest[-MAX_CARRY_ROWS:]
    if len(funding) > MAX_CARRY_ROWS:
        notes.append(
            f"资金费结算 {len(funding):,} 条超过协议上限 {MAX_CARRY_ROWS:,}，"
            f"只传最近 {MAX_CARRY_ROWS:,} 条"
        )
        funding = funding[-MAX_CARRY_ROWS:]
    return (
        [{"ts": int(row["ts"]), "rate": float(row["rate"] or 0.0)} for row in funding],
        [{"ts": int(row["ts"]), "oi": float(row["oi"] or 0.0)} for row in interest],
        notes,
    )


def compute(db: Database, *, symbol: str, interval: str = "1h", bars: int = 2_000,
            factor_ids: list[str] | None = None, parameters: dict | None = None,
            include_values: bool = True, progress: Callable[[float, str], None] | None = None,
            manager_: PluginManager | None = None) -> dict[str, Any]:
    """Compute factors for one contract over its stored history.

    The window is whatever the store holds - the run records the data version it
    read, so the same factors can be recomputed and compared later.
    """
    def report(fraction: float, label: str) -> None:
        if progress is not None:
            progress(fraction, label)

    manager_ = manager_ or manager()
    provider = enabled_provider(manager_)
    if not provider:
        return {"available": False, "provider": None, "series": [],
                "reason": "没有启用的因子插件（capability: factor_provider）"}
    known = {item["id"] for item in stored_catalog(db, provider)}
    requested = list(factor_ids or sorted(known))[:MAX_FACTORS_PER_RUN]
    if not requested:
        return {"available": False, "provider": provider, "series": [],
                "reason": "因子目录为空，请先刷新目录"}
    unknown = [item for item in requested if item not in known]
    bars_payload, provenance, version = _series_for(db, symbol, interval, bars)
    if not bars_payload:
        return {"available": False, "provider": provider, "series": [],
                "reason": f"{symbol} 在 {interval} 没有本地K线，请先回填历史"}
    started = int(time.time() * 1000)
    # A plugin's stdout is capped at 1 MB, and a factor series is one JSON object
    # per bar. Asking for every factor at once would simply be refused by the
    # runtime, so the engine splits the request into batches that fit and merges
    # the answers - the boundary is the runtime's, and respecting it is the
    # engine's job, not something to negotiate with a plugin.
    batches = _batches(len(bars_payload), requested)
    report(0.05, f"准备 {len(bars_payload):,} 根K线与 {len(requested)} 个因子")
    funding_rows, interest_rows, carry_notes = _carry_inputs(
        db, symbol, interval, bars_payload, oi_interval=_oi_interval(interval))
    series: list[dict] = []
    warnings: list[str] = list(carry_notes)
    errors: list[str] = []
    for index, batch in enumerate(batches, start=1):
        # A batch is the natural checkpoint: the queue's progress bar moves while
        # a wide request runs, and a cancellation lands here.
        report(0.05 + 0.9 * ((index - 1) / max(1, len(batches))),
               f"因子批次 {index}/{len(batches)}")
        request = FactorComputeRequest(
            symbol=symbol, timeframe=interval, snapshotHash=str(version or ""),
            factorIds=batch, candles=bars_payload,
            funding=funding_rows, openInterest=interest_rows, parameters=parameters or {},
        )
        try:
            outcome = PluginRegistry(manager_).compute_factors(provider, request)
        except PluginError as exc:
            errors.append(f"第 {index}/{len(batches)} 批失败：{exc}")
            continue
        series.extend(item.model_dump() for item in outcome.series)
        warnings.extend(outcome.warnings)
        echoed = str(outcome.snapshotHash or "")
        if echoed and version and echoed != version:
            warnings.append(f"插件回报的数据版本 {echoed} 与引擎读取的版本 {version} 不一致")
    if not series:
        reason = "；".join(errors) or "因子插件没有返回任何序列"
        _record_run(db, provider, symbol, interval, version, requested, parameters, status="error",
                    bars=len(bars_payload), series=[], warnings=warnings, error=reason, started=started,
                    sandbox=sandbox_label())
        return {"available": True, "provider": provider, "series": [], "reason": f"因子插件执行失败：{reason}",
                "snapshotHash": version}
    coverage = {
        item["factorId"]: sum(1 for value in item["values"] if value["value"] is not None)
        for item in series
    }
    if len(batches) > 1:
        warnings.append(f"分 {len(batches)} 批计算（插件单次输出上限 1MB）")
    if errors:
        warnings.extend(errors)
    warnings += [f"目录中未收录：{', '.join(unknown)}"] if unknown else []
    # The row records the version of the data the *engine* read: that is what a
    # later comparison against a backtest needs.
    report(0.97, "整理覆盖率与引用版本")
    run_id = _record_run(db, provider, symbol, interval, version, requested, parameters, status="ok",
                         bars=len(bars_payload), series=series, warnings=warnings, error=None,
                         started=started, coverage=coverage, sandbox=sandbox_label())
    return {
        "available": True, "provider": provider, "runId": run_id, "symbol": symbol,
        "interval": interval, "snapshotHash": version, "batches": len(batches),
        "bars": len(bars_payload),
        # The values are stored with the run either way; a caller that asked only
        # for the shape gets the coverage and the run id to read them later.
        "series": series if include_values else [],
        "valuesIncluded": bool(include_values),
        "coverage": coverage, "warnings": warnings, "provenance": provenance,
    }


def _oi_interval(interval: str) -> str:
    """Open interest is sampled hourly regardless of the bar interval asked about."""
    return "1h"


def _batches(bars: int, factor_ids: list[str], *, budget_bytes: int = 800_000,
             bytes_per_value: int = 45) -> list[list[str]]:
    """Split a factor request so no single plugin answer breaches its output cap.

    The estimate is deliberately generous: a compact `{"time":…,"value":…}` pair is
    well under 45 bytes, and a batch that is refused costs a whole round trip.
    """
    per_factor = max(1, bars) * bytes_per_value
    size = max(1, min(len(factor_ids), int(budget_bytes // max(1, per_factor))))
    return [factor_ids[index:index + size] for index in range(0, len(factor_ids), size)]


def _record_run(db: Database, provider: str, symbol: str, interval: str, version: str,
                factor_ids: list[str], parameters: dict | None, *, status: str, bars: int,
                series: list[dict], warnings: list[str], error: str | None, started: int,
                coverage: dict | None = None, sandbox: str = "") -> int:
    # A 28-factor run over 20k bars is ~25 MB of JSON. Stored compressed it is a
    # couple of megabytes, and every read that wants the values says so.
    blob = zlib.compress(json.dumps(series, ensure_ascii=False).encode("utf-8"), 6) if series else None
    db.execute(
        "INSERT INTO factor_runs "
        "(provider, symbol, interval, snapshot_hash, factor_ids, parameters_json, status, bars, "
        " series_count, coverage_json, values_json, values_blob, warnings_json, error, sandbox, "
        " duration_ms, created_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?)",
        (
            provider, symbol, interval, str(version or ""),
            json.dumps(factor_ids, ensure_ascii=False),
            json.dumps(parameters or {}, ensure_ascii=False), status, int(bars), len(series),
            json.dumps(coverage or {}, ensure_ascii=False),
            blob, json.dumps(warnings, ensure_ascii=False),
            error, sandbox, int(time.time() * 1000) - started, int(time.time() * 1000),
        ),
    )
    rows = db.query("SELECT id FROM factor_runs ORDER BY id DESC LIMIT 1")
    return int(rows[0]["id"]) if rows else 0


def runs(db: Database, *, symbol: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    sql = ("SELECT id, provider, symbol, interval, snapshot_hash, factor_ids, status, bars, "
           "series_count, coverage_json, warnings_json, error, duration_ms, created_ts "
           "FROM factor_runs")
    params: list[Any] = []
    if symbol:
        sql += " WHERE symbol=?"
        params.append(symbol)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    return [
        {
            "id": int(row["id"]), "provider": row["provider"], "symbol": row["symbol"],
            "interval": row["interval"], "snapshotHash": row["snapshot_hash"],
            "factorIds": json.loads(row["factor_ids"] or "[]"), "status": row["status"],
            "bars": int(row["bars"] or 0), "seriesCount": int(row["series_count"] or 0),
            "coverage": json.loads(row["coverage_json"] or "{}"),
            "warnings": json.loads(row["warnings_json"] or "[]"),
            "error": row["error"], "durationMs": row["duration_ms"], "createdTs": int(row["created_ts"]),
        }
        for row in db.query(sql, tuple(params))
    ]


def run_detail(db: Database, run_id: int, *, include_values: bool = False) -> dict[str, Any]:
    """One factor run. The stored series is only parsed when it was asked for.

    A wide run holds megabytes of values; a caller that wants to see the coverage
    of what it computed should not pay to deserialise and ship all of them, so the
    values are opt-in and the response says whether they are there.
    """
    if include_values:
        rows = db.query("SELECT * FROM factor_runs WHERE id=?", (int(run_id),))
    else:
        rows = db.query(
            "SELECT id, provider, symbol, interval, snapshot_hash, factor_ids, parameters_json, "
            "status, bars, series_count, coverage_json, warnings_json, error, duration_ms, created_ts, "
            "sandbox FROM factor_runs WHERE id=?",
            (int(run_id),),
        )
    if not rows:
        raise KeyError(f"没有找到因子任务 {run_id}")
    row = rows[0]
    return {
        "id": int(row["id"]), "provider": row["provider"], "symbol": row["symbol"],
        "interval": row["interval"], "snapshotHash": row["snapshot_hash"],
        "factorIds": json.loads(row["factor_ids"] or "[]"),
        "parameters": json.loads(row["parameters_json"] or "{}"),
        "status": row["status"], "bars": int(row["bars"] or 0),
        "seriesCount": int(row["series_count"] or 0),
        "valuesIncluded": bool(include_values),
        "series": _stored_series(row) if include_values else [],
        "coverage": json.loads(row["coverage_json"] or "{}"),
        "warnings": json.loads(row["warnings_json"] or "[]"),
        "error": row["error"], "durationMs": row["duration_ms"], "createdTs": int(row["created_ts"]),
        "sandbox": row["sandbox"] if "sandbox" in row.keys() else "",
    }


def sandbox_label() -> str:
    """How the plugin was isolated when it answered.

    A factor matrix produced by an unisolated process is still useful, but the
    run should say so: "which isolation was in force" is part of a result's
    provenance, and it is recorded here rather than inferred later.
    """
    try:
        from .plugins.sandbox import status_cached

        state = status_cached()
    except Exception:  # noqa: BLE001 - provenance must never fail a computation
        return "unknown"
    return f"{state.policy}:{state.backend or 'none'}:{'enforced' if state.enforced else 'unenforced'}"


def _stored_series(row: Any) -> list[dict]:
    """The stored series, whichever way that row happens to hold it.

    Rows written before the compressed column existed still carry text; both
    shapes read the same way, and a row that fails to decompress is reported as
    empty rather than pretending to be a shorter series.
    """
    blob = row["values_blob"] if "values_blob" in row.keys() else None
    if blob:
        try:
            return json.loads(zlib.decompress(blob).decode("utf-8"))
        except (zlib.error, UnicodeError, json.JSONDecodeError):
            return []
    text_value = row["values_json"] if "values_json" in row.keys() else None
    return json.loads(text_value) if text_value else []


# -- statistical validation of a finished run --------------------------------


def _trial_material(run: dict) -> dict[str, Any]:
    """The multiple-testing material a completed study already contains.

    A validation run searched parameters on every walk-forward window. The
    candidate-by-window out-of-sample matrix is exactly what a CSCV/PBO reading
    needs, and the selected Sharpes give the Deflated Sharpe its spread, so both
    travel to the validator instead of being estimated from a single series.
    """
    result = run.get("result") or {}
    matrix: list[list[float]] = []
    sharpes: list[float] = []
    windows = (result.get("walkForward") or {}).get("windows") or []
    for window in windows:
        candidates = window.get("candidates") or []
        if not candidates:
            continue
        column: list[float] = []
        for candidate in candidates:
            validation = candidate.get("validation") or {}
            value = validation.get("total_return_pct")
            column.append(float(value) if isinstance(value, (int, float)) else 0.0)
        matrix.append(column)
    # matrix is built window-by-window (candidates inside); transpose to
    # candidates x blocks, which is the shape the validator documents.
    transposed: list[list[float]] = []
    if matrix:
        width = min(len(column) for column in matrix)
        transposed = [[column[index] for column in matrix] for index in range(width)]
    search = result.get("parameterSearch") or {}
    for item in search.get("ranking") or []:
        value = item.get("sharpe")
        if isinstance(value, (int, float)):
            sharpes.append(float(value))
    if not sharpes:
        # An older stored run only counted its candidates; the ranking list is what
        # carries their numbers, and its absence is not an error.
        legacy = search.get("candidates")
        for candidate in legacy if isinstance(legacy, list) else []:
            validation = (candidate or {}).get("outOfSample") or {}
            value = validation.get("sharpe")
            if isinstance(value, (int, float)):
                sharpes.append(float(value))
    combinations = 0
    grid = search.get("grid") or {}
    if grid:
        combinations = 1
        for values in grid.values():
            combinations *= max(1, len(values))
    return {
        "trials": max(combinations, len(transposed), 1),
        "parameterCombinations": combinations or len(transposed),
        "candidateMatrix": transposed,
        "candidateSharpes": sharpes,
    }


def analyze_run(db: Database, run_id: int, *, seed: int = 42, tests: dict | None = None,
                manager_: PluginManager | None = None) -> dict[str, Any]:
    """Run the validator over a finished backtest and store its verdicts."""
    from .backtest_runs import RunQueue

    manager_ = manager_ or manager()
    provider = enabled_provider(manager_, "backtest_validator")
    run = RunQueue(db).get(int(run_id), with_result=True)
    if provider is None:
        return {"available": False, "provider": None, "verdicts": [],
                "reason": "没有启用的统计验证插件（capability: backtest_validator）"}
    result = run.get("result")
    if not result:
        return {"available": False, "provider": provider, "verdicts": [],
                "reason": f"任务 {run_id} 还没有结果，无法做统计验证"}
    curve = result.get("equity_curve") or result.get("equityCurve") or []
    if not curve and (result.get("selectedRun") or {}).get("equityCurve"):
        curve = result["selectedRun"]["equityCurve"]
    trades = result.get("trades") or (result.get("selectedRun") or {}).get("trades") or []
    benchmark = _benchmark_curve(db, result)
    trials = _trial_material(run)
    settings = dict(tests or {})
    settings["scope"] = "portfolio" if (result.get("portfolio") or result.get("members")) else "single"
    settings.setdefault("enabled", ["pathRisk", "bootstrap", "randomization", "multipleTesting"])
    settings.setdefault("multipleTesting", {})
    settings["multipleTesting"] = {**trials, **settings["multipleTesting"]}
    request = ValidationAnalyzeRequest(
        runId=str(run_id), seed=int(seed), interval=(run.get("interval") or "1h"),
        equityCurve=[
            {"time": int(point["time"]), "equity": float(point["equity"])}
            for point in curve if point.get("time") and point.get("equity") is not None
        ],
        trades=[_validation_trade(trade) for trade in trades],
        benchmark=benchmark,
        tests=settings,
    )
    try:
        outcome = PluginRegistry(manager_).analyze_validation(provider, request)
    except PluginError as exc:
        return {"available": True, "provider": provider, "verdicts": [],
                "reason": f"统计验证插件执行失败：{exc}"}
    payload = outcome.model_dump()
    verdicts = store_verdicts(db, int(run_id), provider, payload,
                              scope="portfolio" if run.get("kind") == "portfolio" else "single")
    return {"available": True, "provider": provider, "runId": int(run_id), "analysis": payload,
            "verdicts": verdicts, "summary": verdict_summary(verdicts)}


def _validation_trade(trade: dict) -> dict[str, Any]:
    """One engine trade in the validator's shape.

    The engine writes `net_pnl` / `return_pct` / `bars_held`; the protocol spells
    them `netPnl` / `returnPct` / `barsHeld`. Getting this wrong is silent - every
    trade would arrive worth zero, and a path-risk reading of a flat series looks
    plausible - so the mapping lives in one place and is tested.
    """
    direction = str(trade.get("direction") or "long")
    return {
        "entryTime": int(trade.get("entry_time") or trade.get("entryTime") or 0),
        "exitTime": int(trade.get("exit_time") or trade.get("exitTime") or 0),
        # The engine labels a long "多"; the protocol only knows long/short.
        "direction": "short" if direction.lower() in ("short", "空") else "long",
        "netPnl": float(trade.get("net_pnl") or trade.get("netPnl") or 0.0),
        "returnPct": float(trade.get("return_pct") or trade.get("returnPct") or 0.0),
        "barsHeld": int(trade.get("bars_held") or trade.get("barsHeld") or 0),
    }


def _portfolio_benchmark(db: Database, result: dict) -> dict[str, Any]:
    """Buy-and-hold over the same window for every member, at the book's weights.

    Without it a portfolio's signal-shift test would only permute the combined
    equity increments, which asks whether the *order* of the returns mattered -
    a much weaker question than "would this allocation have earned this much if it
    had been timed at random".
    """
    weights = result.get("weights") or {}
    members = result.get("members") or {}
    # A member envelope carries its own read window but not the interval: that
    # belongs to the portfolio, which read every leg over the same one.
    interval = str((result.get("readRange") or {}).get("interval")
                   or result.get("interval") or "1h")
    curves: dict[int, float] = {}
    for symbol in sorted(members):
        share = float(weights.get(symbol, 0.0)) / 100.0
        if share <= 0:
            continue
        single = _benchmark_curve(db, {**members[symbol], "symbol": symbol, "interval": interval,
                                       "readRange": (members[symbol] or {}).get("readRange")})
        points = single.get("equityCurve") or []
        if not points:
            continue
        base = float(points[0]["equity"]) or 1.0
        for point in points:
            curves[int(point["time"])] = curves.get(int(point["time"]), 0.0) + share * float(point["equity"]) / base
    if not curves:
        return {}
    return {
        "kind": "weighted_buy_and_hold", "source": "local_candles",
        "equityCurve": [{"time": stamp, "equity": round(value, 6)} for stamp, value in sorted(curves.items())],
    }


def _benchmark_curve(db: Database, result: dict) -> dict[str, Any]:
    """The market the strategy was trading in, for the signal-shift test.

    A finished run does not always carry a benchmark curve, but the engine has the
    bars it read. Buy-and-hold over exactly that window is the reference the shift
    test needs - without it the validator can only permute the strategy's own
    returns, which is a much weaker question.
    """
    benchmark = result.get("benchmark") or {}
    if benchmark.get("equityCurve"):
        return benchmark
    members = result.get("members") or {}
    if members:
        curve = (list(members.values())[0].get("benchmark") or {}).get("equityCurve") or []
        if curve:
            return {"equityCurve": curve}
        weighted = _portfolio_benchmark(db, result)
        if weighted:
            return weighted
    curve = result.get("benchmarkEquityCurve") or []
    if curve:
        return {"equityCurve": curve}
    symbol = result.get("symbol") or result.get("displaySymbol")
    interval = result.get("interval")
    read = result.get("readRange") or {}
    from_ts, to_ts = read.get("fromTs"), read.get("toTs")
    if not symbol or not interval or not from_ts or not to_ts:
        return {}
    try:
        from .config.instruments import require_instrument
        from .datahub.view import read_history

        spec = require_instrument(symbol)
        history = read_history(
            db, symbol=spec.venue_symbol, interval=interval, bars=int(read.get("bars") or 2_000),
            from_ts=int(from_ts), to_ts=int(to_ts), with_funding=False, with_marks=False,
        )
        bars = history.bars
        if len(bars) < 2 or not bars[0].get("close"):
            return {}
        base = float(bars[0]["close"])
        initial = float(read.get("initialEquity") or result.get("initial_capital") or 10_000.0)
        return {
            "kind": "buy_and_hold", "source": "local_candles", "symbol": spec.venue_symbol,
            "equityCurve": [
                {"time": int(row["ts"]), "equity": round(initial * float(row["close"]) / base, 6)}
                for row in bars if row.get("close")
            ],
        }
    except Exception:  # noqa: BLE001 - a missing benchmark degrades the test, not the run
        return {}


def store_verdicts(db: Database, run_id: int, provider: str, analysis: dict[str, Any], *,
                   scope: str = "single") -> list[dict[str, Any]]:
    """Turn one validation answer into rows a page can read without re-running it.

    `scope` describes the book the numbers belong to. It comes from the run, not
    from the validator's answer: the protocol's result has no such field, and a
    portfolio's verdicts must not be labelled as a single contract's.
    """
    now = int(time.time() * 1000)
    # One validator, one reading per run: a second analysis replaces the first
    # rather than leaving two answers to the same question side by side.
    db.execute("DELETE FROM backtest_validation_results WHERE run_id=? AND provider=?", (run_id, provider))
    rows: list[dict[str, Any]] = []
    bootstrap = analysis.get("bootstrap") or {}
    label = "组合合并账本" if scope == "portfolio" else "单标的"
    if bootstrap.get("resamples"):
        interval = bootstrap.get("sharpe") or {}
        low = interval.get("low")
        rows.append({
            "kind": "bootstrap", "verdict": "pass" if (low or 0) > 0 else "warn",
            "statistic": bootstrap.get("positiveSharpeProbability"),
            "pValue": None, "threshold": 0.95 if (low or 0) > 0 else None,
            "detail": (
                f"{label} · 移动分块 bootstrap（块长 {bootstrap.get('blockSize')}，"
                f"{bootstrap.get('resamples')} 次）："
                f"Sharpe 95% 区间 [{_fmt(low)}, {_fmt(interval.get('high'))}]，"
                f"正 Sharpe 概率 {_pct(bootstrap.get('positiveSharpeProbability'))}"
            ),
        })
    randomization = analysis.get("randomization") or {}
    if randomization.get("permutations"):
        p_value = randomization.get("pValue")
        rows.append({
            "kind": "randomization", "verdict": _p_verdict(p_value),
            "statistic": randomization.get("observedSharpe"), "pValue": p_value, "threshold": 0.05,
            "detail": (
                f"{label} · 信号随机化（{randomization.get('method')}，"
                f"{randomization.get('permutations')} 次）："
                f"p = {_fmt(p_value)}"
                + ("，信号不含信息的原假设未被否定" if (p_value or 1) > 0.05 else "，随机化下难以复现")
            ),
        })
    multiple = analysis.get("multipleTesting") or {}
    if not multiple.get("applied") and multiple.get("note"):
        # "No correction was applied, and here is why" is a finding in its own
        # right: without a row, a reader of the run cannot tell the difference
        # between "passed" and "never tested".
        rows.append({
            "kind": "multiple_testing_note", "verdict": "info",
            "statistic": None, "pValue": None, "threshold": None,
            "detail": str(multiple["note"]),
        })
    if multiple.get("applied"):
        rows.append({
            "kind": "deflated_sharpe", "verdict": "pass" if (multiple.get("deflatedSharpe") or 0) >= 0.95 else "warn",
            "statistic": multiple.get("deflatedSharpe"), "pValue": None, "threshold": 0.95,
            "detail": f"Deflated Sharpe（{multiple.get('trials')} 次尝试）：{_fmt(multiple.get('deflatedSharpe'))}；{multiple.get('note') or ''}",
        })
        pbo = multiple.get("probabilityOfBacktestOverfitting")
        if pbo is not None:
            rows.append({
                "kind": "pbo", "verdict": "pass" if pbo <= 0.5 else "fail",
                "statistic": pbo, "pValue": None, "threshold": 0.5,
                "detail": f"CSCV 回测过拟合概率 PBO = {_fmt(pbo)}（>0.5 说明选参数的过程本身在解释结果）",
            })
    path = analysis.get("pathRisk") or {}
    if path.get("simulations"):
        drawdown = path.get("drawdown") or {}
        rows.append({
            "kind": "path_risk", "verdict": "info",
            "statistic": drawdown.get("low"), "pValue": None, "threshold": None,
            "detail": (
                f"路径风险（{path.get('simulations')} 次交易重排）：回撤区间 "
                f"[{_fmt(drawdown.get('low'))}, {_fmt(drawdown.get('high'))}]；"
                "只说明顺序敏感度，不构成显著性检验"
            ),
        })
    isolation = sandbox_label()
    for row in rows:
        # The response carries the same field the stored row does, so a caller
        # reading the answer now and reading it back later sees one shape.
        row["sandbox"] = isolation
        db.execute(
            "INSERT INTO backtest_validation_results "
            "(run_id, kind, verdict, statistic, p_value, threshold, provider, detail, detail_json, "
            " sandbox, created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, row["kind"], row["verdict"], row.get("statistic"), row.get("pValue"),
             row.get("threshold"), provider, row["detail"], None, isolation, now),
        )
    return rows


def verdict_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Count the verdicts, and say in one line what they add up to."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    parts = []
    for verdict, label in (("pass", "通过"), ("warn", "关注"), ("fail", "不通过"), ("info", "提示")):
        if counts.get(verdict):
            parts.append(f"{counts[verdict]} 项{label}")
    if counts.get("fail"):
        headline = "统计验证未通过：" + "、".join(parts)
    elif counts.get("warn"):
        headline = "统计验证通过但有保留：" + "、".join(parts)
    else:
        headline = "统计验证通过：" + "、".join(parts)
    return {"counts": counts, "headline": headline, "total": len(rows)}


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        return f"{value:.4g}"
    return str(value)


def _pct(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def _p_verdict(p_value: Any) -> str:
    if p_value is None:
        return "info"
    return "pass" if float(p_value) <= 0.05 else ("warn" if float(p_value) <= 0.10 else "fail")


__all__ = [
    "FactorDefinition",
    "FactorUnavailable",
    "analyze_run",
    "catalog",
    "compute",
    "enabled_provider",
    "record_definitions",
    "run_detail",
    "runs",
    "stored_catalog",
    "store_verdicts",
]


# -- the queued form of a factor computation ---------------------------------
#
# A wide factor request is a job, not an interaction: sixteen thousand bars across
# the whole catalogue is minutes of CPU. It goes through the same run queue as a
# backtest, which means one submission, visible progress, a stored result and a
# record that can be read back - instead of a browser timeout.


def run_factors(db: Database, request: Any, *,
                progress: Callable[[float, str], None] | None = None) -> dict[str, Any]:
    """Run one factor computation for the queue.

    The result carries the coverage and the id of the stored factor run; the
    values themselves stay in `factor_runs`, because a run record is what a page
    reads and a matrix of numbers is not.
    """
    outcome = compute(
        db, symbol=request.symbol, interval=request.interval, bars=request.bars,
        factor_ids=request.factorIds, parameters=request.parameters,
        include_values=False, progress=progress,
    )
    if not outcome.get("available"):
        raise StudyError("not_ready", outcome.get("reason") or "因子服务不可用", status=409)
    return {
        **{key: value for key, value in outcome.items() if key not in ("series", "provenance")},
        "kind": "factors",
        "series": [],
        "factorRunId": outcome.get("runId"),
        "readBack": f"/api/factors/runs/{outcome.get('runId')}?values=true",
    }


