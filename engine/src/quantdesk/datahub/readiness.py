"""Is the local data good enough to run a formal backtest?

A backtest that silently runs on missing marks, absent funding or a partially
backfilled history produces a number nobody can defend. This gate answers seven
questions before a run is allowed:

1. do the candles cover the requested range,
2. do mark prices cover it (they price liquidation and funding),
3. does funding cover the settlements that should have been charged,
4. is there enough open interest for the factor inputs that need it,
5. does a data snapshot version exist for the series being read,
6. has the backfill reached the contract's listing date,
7. is any of the data degraded - an approximation or a proxy rather than the
   contract's own venue history.

The gate blocks a formal run when a *key* input is missing. An operator may
override with `allow_degraded`, and then every missing item and its consequence is
carried into the result rather than left in a log.
"""

from __future__ import annotations

import math
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from .venue import INTERVAL_MS

# Which absences are fatal by default. Marks and candles are load-bearing: without
# them there is no honest liquidation or valuation. Funding and open interest are
# reported and can be waived.
BLOCKING_KEYS = ("candles", "marks", "snapshot")

# Severity vocabulary for the report.
BLOCKING = "blocking"
DEGRADED = "degraded"
OK = "ok"


@dataclass
class Check:
    key: str
    label: str
    status: str
    detail: str
    blocking: bool = False
    values: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "blocking": self.blocking,
            "values": self.values,
        }


@dataclass
class DataReadiness:
    symbol: str
    interval: str
    from_ts: int
    to_ts: int
    checks: list[Check] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)
    degraded: bool = False
    proxy_data: bool = False
    missing: list[str] = field(default_factory=list)
    impacts: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> list[Check]:
        return [check for check in self.checks if check.blocking and check.status != OK]

    @property
    def ok(self) -> bool:
        return not self.blocking

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "fromTs": self.from_ts,
            "toTs": self.to_ts,
            "ok": self.ok,
            "degraded": self.degraded,
            "proxyData": self.proxy_data,
            "blocking": [check.as_dict() for check in self.blocking],
            "checks": [check.as_dict() for check in self.checks],
            "versions": self.versions,
            "missing": self.missing,
            "impacts": self.impacts,
        }


def read_range_of(interval: str, from_ts: int, to_ts: int, bars: int) -> dict:
    """One shape for "what this study read", used by every entry point."""
    return {
        "interval": interval,
        "fromTs": int(from_ts),
        "toTs": int(to_ts),
        "bars": int(bars),
        "expectedBars": _expected_bars(interval, from_ts, to_ts),
    }


def _expected_bars(interval: str, from_ts: int, to_ts: int) -> int:
    step = INTERVAL_MS.get(interval)
    if not step or to_ts <= from_ts:
        return 0
    return int((to_ts - from_ts) // step) + 1


def assess(
    db,
    *,
    venue: str,
    symbol: str,
    interval: str,
    from_ts: int,
    to_ts: int,
    needs_open_interest: bool = False,
    oi_interval: str = "1h",
    product_type: str | None = None,
    include_funding: bool = True,
    use_mark_price: bool = True,
    include_liquidation: bool = True,
    use_risk_tiers: bool = True,
) -> DataReadiness:
    """Check the store against what this run is about to read."""
    readiness = DataReadiness(symbol=symbol, interval=interval, from_ts=int(from_ts), to_ts=int(to_ts))
    step = INTERVAL_MS.get(interval)
    if step is None:
        readiness.checks.append(Check("interval", "周期", BLOCKING, f"不支持的周期：{interval}", True))
        return readiness

    _check_candles(db, readiness, venue, symbol, interval, from_ts, to_ts, step, product_type)
    _check_marks(db, readiness, venue, symbol, interval, from_ts, to_ts, step,
                 required=use_mark_price or include_liquidation)
    _check_funding(db, readiness, venue, symbol, from_ts, to_ts, required=include_funding)
    _check_open_interest(db, readiness, venue, symbol, needs_open_interest, oi_interval,
                         from_ts, to_ts)
    _check_risk_limit(db, readiness, venue, symbol, required=use_risk_tiers)
    _check_snapshot(db, readiness, venue, symbol, interval, oi_interval)
    _check_listing(db, readiness, venue, symbol, from_ts, interval)
    _check_provenance(db, readiness, venue, symbol, interval, from_ts, to_ts)
    readiness.degraded = any(check.status == DEGRADED for check in readiness.checks)
    return readiness


def _check_candles(db, readiness: DataReadiness, venue: str, symbol: str, interval: str,
                   from_ts: int, to_ts: int, step: int, product_type: str | None = None) -> None:
    rows = db.load_candles(venue, symbol, interval, start_ts=from_ts, end_ts=to_ts)
    expected = _expected_bars(interval, from_ts, to_ts)
    present = len(rows)
    status, detail = OK, f"{present}/{expected} 根"
    if present == 0:
        status, detail = BLOCKING, "该区间没有任何K线"
        readiness.missing.append("candles")
        readiness.impacts.append("没有成交K线，回测无法撮合")
    elif expected and present < expected:
        # These are exchange contracts, including tokenised-stock perpetuals.
        # Positions can be valued or liquidated outside the underlying cash session,
        # so a missing venue bar is still a missing bar.
        status, detail = BLOCKING, f"缺 {expected - present} 根K线"
        readiness.missing.append("candles")
        readiness.impacts.append("K线不完整，回测区间内的撮合与收益会被低估")
    readiness.checks.append(Check(
        "candles", "成交K线", status, detail, status == BLOCKING,
        {"present": present, "expected": expected, "barsAvailable": db.count_candles(venue, symbol, interval)},
    ))


def _check_marks(db, readiness: DataReadiness, venue: str, symbol: str, interval: str,
                 from_ts: int, to_ts: int, step: int, *, required: bool) -> None:
    rows = db.load_mark_candles(venue, symbol, interval, start_ts=from_ts, end_ts=to_ts)
    expected = _expected_bars(interval, from_ts, to_ts)
    present = len(rows)
    if present == 0:
        status = BLOCKING if required else OK
        detail = "该区间没有标记价格" if required else "本次配置不读取标记价格"
        if required:
            readiness.missing.append("marks")
            readiness.impacts.append("没有标记价格，强平与估值只能用收盘价近似")
    elif expected and present < expected:
        status = BLOCKING if required else DEGRADED
        detail = f"{present}/{expected} 根标记价格"
        readiness.missing.append("marks")
        readiness.impacts.append("标记价格不完整，强平判定可能落在错误的K线上")
    else:
        status, detail = OK, f"{present}/{expected} 根"
    readiness.checks.append(Check(
        "marks", "标记价格", status, detail, status == BLOCKING,
        {"present": present, "expected": expected,
         "barsAvailable": db.count_mark_candles(venue, symbol, interval)},
    ))


def _check_funding(db, readiness: DataReadiness, venue: str, symbol: str, from_ts: int,
                   to_ts: int, *, required: bool) -> None:
    if not required:
        readiness.checks.append(Check("funding", "资金费率", OK, "本次配置不计资金费", False))
        return
    rows = db.load_funding(venue, symbol, start_ts=from_ts, end_ts=to_ts)
    state = db.load_backfill_state(venue, symbol, "", "funding") or {}
    if state.get("status") == "unsupported":
        readiness.checks.append(Check(
            "funding", "资金费率", DEGRADED,
            state.get("reason") or "交易所不提供该合约的资金费率历史",
            False, {"settlements": 0, "unsupported": True},
        ))
        readiness.missing.append("funding")
        readiness.impacts.append("该合约没有资金费率历史，持仓成本未计入资金费")
        return
    if not rows:
        readiness.checks.append(Check(
            "funding", "资金费率", DEGRADED, "该区间没有资金费结算记录", False,
            {"settlements": 0},
        ))
        readiness.missing.append("funding")
        readiness.impacts.append("回测未计入资金费，永续合约的持仓成本会被低估")
        return
    meta = db.load_instrument_meta(venue, symbol) or {}
    hours = float(meta.get("funding_interval_hours") or 8)
    step = max(1, int(hours * 3_600_000))
    stamps = sorted(int(row["ts"]) for row in rows)
    gaps = [right - left for left, right in zip(stamps, stamps[1:])]
    start_ok = stamps[0] <= from_ts + step
    end_ok = stamps[-1] >= to_ts - step
    max_gap = max(gaps, default=0)
    expected = max(1, int((to_ts - from_ts) // step))
    coverage = start_ok and end_ok and len(stamps) >= max(1, expected - 1) and max_gap <= int(step * 1.5)
    status = OK if coverage else DEGRADED
    detail = f"{len(rows)}/{expected} 次结算" + ("" if coverage else "，区间覆盖不完整")
    if not coverage:
        readiness.missing.append("funding")
        readiness.impacts.append("资金费结算存在起点、终点或中间缺口，持仓成本不完整")
    readiness.checks.append(Check(
        "funding", "资金费率", status, detail, False,
        {"settlements": len(rows), "expectedSettlements": expected,
         "intervalHours": hours, "coverageStart": start_ok, "coverageEnd": end_ok,
         "maxGapMs": max_gap},
    ))


def _check_open_interest(db, readiness: DataReadiness, venue: str, symbol: str, needed: bool,
                         oi_interval: str, from_ts: int, to_ts: int) -> None:
    rows = db.load_oi(venue, symbol, start_ts=from_ts, end_ts=to_ts, interval=oi_interval)
    count = len(rows)
    if not needed:
        readiness.checks.append(Check("open_interest", "持仓量", OK, f"{count} 点（本次不需要）", False,
                                      {"points": count}))
        return
    step = {"5min": 300_000, "15min": 900_000, "30min": 1_800_000,
            "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}.get(oi_interval, 3_600_000)
    expected = max(1, int((to_ts - from_ts) // step) + 1)
    stamps = sorted(int(row["ts"]) for row in rows)
    max_gap = max((right - left for left, right in zip(stamps, stamps[1:])), default=0)
    covered = bool(stamps) and stamps[0] <= from_ts + step and stamps[-1] >= to_ts - step
    enough = count >= max(10, math.floor(expected * 0.95)) and max_gap <= step * 2
    status = OK if covered and enough else DEGRADED
    detail = f"{count}/{expected} 点"
    if status != OK:
        readiness.missing.append("open_interest")
        readiness.impacts.append("回测区间内持仓量覆盖不足，依赖它的因子无法可靠计算")
    readiness.checks.append(Check("open_interest", "持仓量", status, detail, False,
                                  {"points": count, "expectedPoints": expected,
                                   "interval": oi_interval, "maxGapMs": max_gap,
                                   "rangeCovered": covered}))


def _check_risk_limit(db, readiness: DataReadiness, venue: str, symbol: str, *, required: bool) -> None:
    rows = db.load_risk_tiers(venue, symbol) or []
    if not required:
        readiness.checks.append(Check("risk_limit", "风险档位", OK, "本次配置不使用风险档位", False))
        return
    if not rows:
        readiness.checks.append(Check("risk_limit", "风险档位", DEGRADED,
                                      "没有交易所风险档位，将使用回测默认保证金率", False))
        readiness.missing.append("risk_limit")
        readiness.impacts.append("缺少风险档位，杠杆上限和强平价格可能不符合交易所规则")
        return
    newest = max(int(row.get("synced_at") or 0) for row in rows)
    age_ms = max(0, int(time.time() * 1000) - newest)
    status = OK if newest and age_ms <= 7 * 86_400_000 else DEGRADED
    if status != OK:
        readiness.missing.append("risk_limit")
        readiness.impacts.append("风险档位超过 7 天未更新，交易所保证金规则可能已经变化")
    readiness.checks.append(Check("risk_limit", "风险档位", status,
                                  f"{len(rows)} 档，采集于 {newest}" if newest else f"{len(rows)} 档，采集时间未知",
                                  False, {"tiers": len(rows), "collectedAt": newest, "ageMs": age_ms}))


def _check_snapshot(db, readiness: DataReadiness, venue: str, symbol: str, interval: str,
                    oi_interval: str) -> None:
    rows = db.list_history_snapshots(symbol=symbol, interval=interval, data_kind="trade_candle", limit=1)
    if not rows:
        readiness.checks.append(Check("snapshot", "数据快照", BLOCKING, "该合约尚无数据快照版本", True))
        readiness.missing.append("snapshot")
        readiness.impacts.append("没有快照版本，回测结果无法引用它读取的数据")
        return
    record = rows[0]
    # `snapshot*` is the version the store pinned; the version of the window the
    # run actually reads is computed separately and reported alongside it, so a
    # reader can tell the two apart instead of seeing one silently overwrite the other.
    readiness.versions["snapshotCandles"] = record["version"]
    readiness.checks.append(Check(
        "snapshot", "数据快照", OK, f"{record['version']}（{record['bars']} 根）", False,
        {"version": record["version"], "bars": record["bars"]},
    ))
    # Every series the run reads should be citable. A family that holds rows but
    # has no snapshot version cannot be named in the result, which is a provenance
    # gap rather than a blocking one: the numbers can still be computed.
    holders = {
        "mark_candle": ("snapshotMarks", "标记价格", lambda: db.count_mark_candles(venue, symbol, interval)),
        "funding": ("snapshotFunding", "资金费率", lambda: db.count_funding(venue, symbol)),
        "open_interest": ("snapshotOpenInterest", "持仓量", lambda: db.count_oi(venue, symbol, oi_interval)),
        "risk_limit": ("snapshotRiskLimit", "风险档位", lambda: len(db.load_risk_tiers(venue, symbol) or [])),
    }
    uncitable: list[str] = []
    for kind, (key, label, counter) in holders.items():
        family_interval = oi_interval if kind == "open_interest" else None
        found = db.list_history_snapshots(symbol=symbol, interval=family_interval,
                                          data_kind=kind, limit=1)
        if found:
            readiness.versions[key] = found[0]["version"]
        elif counter():
            uncitable.append(label)
    if uncitable:
        readiness.checks.append(Check(
            "snapshot_kinds", "分族快照", DEGRADED,
            "以下数据有内容但没有快照版本，结果无法引用：" + "、".join(uncitable), False,
        ))
        readiness.missing.append("snapshot_kinds")
        readiness.impacts.append("部分数据族缺少快照版本，回测结果无法引用它们的版本号")

    # Versions of the exact ranges read by this run, kept separate from stored
    # snapshots so a repaired series cannot masquerade as the older pinned input.
    readers = {
        "readMarks": db.load_mark_candles(venue, symbol, interval,
                                           start_ts=readiness.from_ts, end_ts=readiness.to_ts),
        "readFunding": db.load_funding(venue, symbol,
                                        start_ts=readiness.from_ts, end_ts=readiness.to_ts),
        "readOpenInterest": db.load_oi(venue, symbol, interval=oi_interval,
                                        start_ts=readiness.from_ts, end_ts=readiness.to_ts),
        "readRiskLimit": db.load_risk_tiers(venue, symbol) or [],
    }
    for key, values in readers.items():
        if values:
            material = json.dumps(values, sort_keys=True, ensure_ascii=False, default=str)
            readiness.versions[key] = "read/1:" + hashlib.sha256(material.encode()).hexdigest()[:16]


def _check_listing(db, readiness: DataReadiness, venue: str, symbol: str, from_ts: int,
                   interval: str) -> None:
    meta = db.load_instrument_meta(venue, symbol)
    launch = int(meta["launch_ts"]) if meta and meta.get("launch_ts") else None
    first = db.first_open_ts(venue, symbol, interval)
    state = db.load_backfill_state(venue, symbol, interval, "trade_candle") or {}
    walked_to_the_end = bool(state.get("complete"))
    if launch is None and not walked_to_the_end:
        readiness.checks.append(Check(
            "listing", "上市日期", DEGRADED, "本地没有该合约的元数据，无法确认历史是否到顶", False,
        ))
        return
    if launch is not None:
        readiness.versions["listing"] = str(launch)
    # "Have we walked back as far as the venue will go" is the question that
    # matters, and the walk answers it: an empty window ends the walk. The
    # contract's launch time is not a threshold the venue's own history always
    # reaches - a perp often has no bars for its first days - so comparing against
    # it alone would mark a complete history as incomplete for ever.
    if walked_to_the_end:
        detail = "已回溯到交易所可提供的最早K线"
        if launch and first and int(first) > launch:
            gap_days = round((int(first) - launch) / 86_400_000, 1)
            detail += f"（交易所数据起点比上市时间晚 {gap_days} 天）"
        readiness.checks.append(Check("listing", "上市日期", OK, detail, False,
                                      {"launchTs": launch, "firstBarTs": first, "walkComplete": True}))
        return
    if first is None or (launch and first > launch + INTERVAL_MS.get(interval, 0) * 2):
        readiness.checks.append(Check(
            "listing", "上市日期", DEGRADED,
            f"历史尚未回溯到上市日期（上市 {launch}，最早 {first}）", False,
            {"launchTs": launch, "firstBarTs": first, "walkComplete": False},
        ))
        readiness.missing.append("listing")
        readiness.impacts.append("历史尚未回溯到上市日期，更早的区间没有数据")
    else:
        readiness.checks.append(Check("listing", "上市日期", OK, "已回溯到上市日期", False,
                                      {"launchTs": launch, "firstBarTs": first}))


def _check_provenance(db, readiness: DataReadiness, venue: str, symbol: str, interval: str,
                      from_ts: int, to_ts: int) -> None:
    """Name any bar that is not the contract's own venue history."""
    rows = db.load_candles(venue, symbol, interval, start_ts=from_ts, end_ts=to_ts)
    sources: dict[str, int] = {}
    for row in rows:
        sources[str(row.get("source") or "unknown")] = sources.get(str(row.get("source") or "unknown"), 0) + 1
    proxies = {name: count for name, count in sources.items() if name in ("local_derived", "imported")}
    if proxies:
        readiness.proxy_data = True
        readiness.checks.append(Check(
            "provenance", "数据来源", DEGRADED,
            "区间内含代理/导入数据：" + "、".join(f"{name} {count} 根" for name, count in proxies.items()),
            False, {"sources": sources},
        ))
        readiness.impacts.append("结果基于代理数据，不能称为该合约的合约回测")
    else:
        readiness.checks.append(Check("provenance", "数据来源", OK,
                                      "、".join(f"{name} {count}" for name, count in sources.items()) or "无数据",
                                      False, {"sources": sources}))


# -- the one formal research entry point ---------------------------------
#
# Every formal study - a single backtest, a parameter search, a walk-forward run
# or a portfolio of contracts - reads its data through `load_research_data`. That
# is what makes the three results comparable: they read the same local store, over
# the same kind of window, and answer to the same gate. A formal request never
# reaches the venue for a price: Bybit is where the data came from, not something
# a study fetches on demand.

class DataNotReady(RuntimeError):
    """A formal study was refused because the local data cannot support it."""

    def __init__(self, readiness: DataReadiness, action: str = ""):
        self.readiness = readiness
        self.action = action
        reasons = [
            f"{check.label}：{check.detail}"
            for check in (readiness.blocking or [c for c in readiness.checks if c.status != OK])
        ]
        super().__init__("数据未就绪" + ("（" + "；".join(reasons) + "）" if reasons else ""))

    def as_detail(self) -> dict:
        """The body a 409 carries: what is wrong, and what to do about it."""
        blocking = self.readiness.blocking or [
            check for check in self.readiness.checks if check.status == DEGRADED
        ]
        return {
            "title": "数据未就绪，已阻止正式研究",
            "detail": "；".join(check.detail for check in blocking) or "数据未就绪",
            "action": self.action or "补齐历史数据（fetch history / 历史数据页的矩阵回填）后重试，或显式允许降级模式",
            "readiness": self.readiness.as_dict(),
        }


def window_for(db, *, venue: str, symbol: str, interval: str, bars: int,
               require_marks: bool = False) -> tuple[int, int]:
    """The window a study reads: `bars` closed bars ending at the newest closed one.

    A study that needs mark prices cannot end on a bar the venue has not published
    a mark for yet. Mark klines trail the trade klines by a bar or two around a
    close, so without this the same request alternates between "ready" and
    "2,999/3,000 mark prices" depending on the minute it is asked - a refusal that
    says nothing about the data and everything about the clock. The window ends at
    the newest bar where every required input exists, and the result reports the
    range it actually read.
    """
    from .venue import last_closed_open_ts

    step = INTERVAL_MS[interval]
    stored = db.last_open_ts(venue, symbol, interval)
    to_ts = last_closed_open_ts(stored, step)
    if require_marks:
        _, newest_mark = db.mark_bounds(venue, symbol, interval)
        if newest_mark:
            to_ts = min(int(to_ts), int(newest_mark))
    return int(to_ts) - (bars - 1) * step, int(to_ts)


def common_window(db, *, venue: str, symbols: list[str], interval: str, bars: int,
                  require_marks: bool = False) -> tuple[int, int, dict]:
    """The window every member of a portfolio can be read over.

    A portfolio's legs have to cover the same span or the combined book compares
    different periods against each other. The window therefore ends at the
    *oldest* newest-bar among the members, and each member's own range is
    reported so a short history is visible rather than averaged away.

    When the study needs mark prices, the window also ends where the *marks* end:
    mark klines trail the trade klines by a bar or two around a close, and a
    portfolio that ends on a bar without a mark is refused for a reason that says
    nothing about the data. This is the same rule a single study follows, applied
    across the legs.
    """
    step = INTERVAL_MS[interval]
    ranges: dict[str, dict] = {}
    ends: list[int] = []
    from .venue import last_closed_open_ts

    for symbol in symbols:
        stored_first = db.first_open_ts(venue, symbol, interval)
        stored_last = db.last_open_ts(venue, symbol, interval)
        closed_last = last_closed_open_ts(stored_last, step) if stored_last is not None else None
        _, newest_mark = db.mark_bounds(venue, symbol, interval) if require_marks else (None, None)
        usable_end = closed_last
        if require_marks and newest_mark:
            usable_end = min(int(closed_last), int(newest_mark)) if closed_last is not None else None
        ranges[symbol] = {
            "firstBarTs": stored_first,
            "lastBarTs": stored_last,
            "lastClosedTs": closed_last,
            "lastUsableTs": usable_end,
            "lastMarkTs": newest_mark,
            "barsAvailable": db.count_candles(venue, symbol, interval),
        }
        if usable_end is not None:
            ends.append(int(usable_end))
    to_ts = min(ends) if ends else 0
    from_ts = int(to_ts) - (bars - 1) * step
    return from_ts, to_ts, ranges


@dataclass
class ResearchData:
    """One pinned read plus the gate's verdict on it, in one object."""

    symbol: str
    display_symbol: str
    interval: str
    from_ts: int
    to_ts: int
    candles: list[dict]
    funding: list[dict]
    marks: list[dict]
    risk_profile: Any | None
    history: Any
    readiness: DataReadiness
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.readiness.ok

    @property
    def degraded(self) -> bool:
        return self.readiness.degraded or not self.readiness.ok

    @property
    def versions(self) -> dict:
        return {
            **self.readiness.versions,
            # The version of the window this study actually read, which is the one
            # a re-run has to match to reproduce the numbers.
            "readCandles": getattr(self.history, "version", None),
        }

    def read_range(self) -> dict:
        return read_range_of(self.interval, self.from_ts, self.to_ts, len(self.candles))

    def envelope(self, *, cost_model: dict | None = None, strategy_version: dict | None = None) -> dict:
        """The provenance block every formal result carries."""
        return {
            "readRange": self.read_range(),
            "versions": self.versions,
            "readiness": self.readiness.as_dict(),
            "dataReady": bool(self.readiness.ok and not self.readiness.degraded),
            "degraded": bool(self.degraded),
            "missingData": list(self.readiness.missing),
            "dataImpacts": list(self.readiness.impacts),
            "costModel": cost_model or {},
            "strategyVersion": strategy_version or {},
        }

    def apply_to(self, result: Any) -> Any:
        """Fold the gate's findings into a BacktestResult before it is serialised."""
        result.data_quality["readiness"] = self.readiness.as_dict()
        result.data_quality["degraded"] = bool(self.degraded)
        for impact in self.readiness.impacts:
            if impact not in result.warnings:
                result.warnings.append(impact)
        return result


def require_ready(data: ResearchData, *, allow_degraded: bool, action: str = "") -> None:
    """Refuse a formal study unless the data is complete or degradation is accepted."""
    if allow_degraded:
        return
    if data.readiness.ok and not data.readiness.degraded:
        return
    raise DataNotReady(data.readiness, action)


def load_research_data(
    db,
    spec,
    *,
    interval: str,
    bars: int,
    venue: str = "bybit",
    include_funding: bool = True,
    use_mark_price: bool = True,
    include_liquidation: bool = True,
    use_risk_tiers: bool = True,
    needs_open_interest: bool = False,
    from_ts: int | None = None,
    to_ts: int | None = None,
    risk_book: Any | None = None,
) -> ResearchData:
    """Read one contract's history locally and assess it against the same window.

    The gate is handed exactly the range that was read, so "approved" cannot mean
    a different span from the one the study used.
    """
    from .view import read_history

    if from_ts is None or to_ts is None:
        from_ts, to_ts = window_for(
            db, venue=venue, symbol=spec.venue_symbol, interval=interval, bars=bars,
            require_marks=bool(use_mark_price or include_liquidation),
        )
    history = read_history(
        db,
        symbol=spec.venue_symbol,
        interval=interval,
        bars=bars,
        venue=venue,
        product_type=spec.product_type,
        display_symbol=spec.display_symbol,
        from_ts=int(from_ts),
        to_ts=int(to_ts),
        with_funding=include_funding,
        with_marks=use_mark_price or include_liquidation,
    )
    readiness = assess(
        db,
        venue=venue,
        symbol=spec.venue_symbol,
        interval=interval,
        from_ts=int(from_ts),
        to_ts=int(to_ts),
        needs_open_interest=needs_open_interest,
        product_type=spec.product_type,
        include_funding=include_funding,
        use_mark_price=use_mark_price,
        include_liquidation=include_liquidation,
        use_risk_tiers=use_risk_tiers,
    )
    profile = None
    if use_risk_tiers:
        from ..risk import RiskBook

        profile = (risk_book or RiskBook(db)).cached(spec.venue_symbol)
        if profile is not None and not profile.tiers:
            # No ladder locally: the constant fallback is used, and that fact is
            # already reported by the gate's risk check.
            profile = None
    return ResearchData(
        symbol=spec.venue_symbol,
        display_symbol=spec.display_symbol,
        interval=interval,
        from_ts=int(from_ts),
        to_ts=int(to_ts),
        candles=history.bars,
        funding=list(history.funding or []),
        marks=list(history.marks or []),
        risk_profile=profile,
        history=history,
        readiness=readiness,
    )


def portfolio_readiness(members: dict[str, ResearchData], ranges: dict[str, dict],
                        from_ts: int, to_ts: int) -> dict:
    """Compare members' coverage and say whether the legs describe one period."""
    short = [
        symbol for symbol, info in ranges.items()
        if not info.get("firstBarTs") or int(info["firstBarTs"]) > from_ts
    ]
    missing_bars = {
        symbol: {
            "present": len(data.candles),
            "expected": _expected_bars(data.interval, from_ts, to_ts),
        }
        for symbol, data in members.items()
        if len(data.candles) < _expected_bars(data.interval, from_ts, to_ts)
    }
    comparable = not short and not missing_bars
    notes: list[str] = []
    if short:
        notes.append(
            "以下成员的历史起点晚于组合区间，腿与腿不在同一段时间上：" + "、".join(sorted(short))
        )
    if missing_bars:
        notes.append(
            "以下成员在该区间内K线不足："
            + "、".join(f"{symbol}（{info['present']}/{info['expected']}）"
                        for symbol, info in sorted(missing_bars.items()))
        )
    return {
        "comparable": comparable,
        "fromTs": int(from_ts),
        "toTs": int(to_ts),
        "memberRanges": ranges,
        "shortHistory": sorted(short),
        "missingBars": missing_bars,
        "notes": notes,
    }


# -- what a series can actually be backtested over ------------------------
#
# "How many bars do we have" is not the question a reader has. The questions are:
# from when to when is the data *continuous enough to trust*, and has the walk
# reached the contract's listing date at all. Those are two different facts and
# the panel reports them separately.

# How many bars back the contiguity scan is willing to look. A gap-free run is
# found by walking back from the newest bar; the cap keeps a status request cheap
# on a series with hundreds of thousands of bars.
CONTIGUITY_SCAN_LIMIT = 20_000

STATUS_OK = "ok"
STATUS_GAPPED = "gapped"                  # a hole inside the stored range
STATUS_NOT_REACHED_LISTING = "not_reached_listing"
STATUS_UNSUPPORTED = "unsupported"        # the venue does not publish this family
STATUS_NO_DATA = "no_data"

# These families are not bar series, so "该区间无缺口" is not a question about them.
EVENT_SERIES_REASONS = {
    "funding": "资金费率按结算事件到达（每 8 小时一次），不按固定周期核对缺口",
    "open_interest": "持仓量按采样点到达（1 小时一点），不按固定周期核对缺口",
    "risk_limit": "风险档位是当前快照而不是时间序列，可用性由采集时间决定",
}


def series_state_interval(data_kind: str, interval: str) -> str:
    """Which backfill state row describes this series.

    Open interest is walked per sampling interval, so its state row is keyed by
    that interval even when the caller asks about the family as a whole. Reading
    the wrong key would report "还没有回溯到上线" for a walk that finished.
    """
    if data_kind == "open_interest" and not interval:
        from .history import DEFAULT_OI_INTERVAL

        return DEFAULT_OI_INTERVAL
    return interval


def stored_count(db, venue: str, symbol: str, data_kind: str, interval: str = "") -> int | None:
    """How many unique records one series has in the store right now.

    The board reports this rather than the counter a walk finished with: the
    live stream keeps appending bars after a task is done, and the risk book can
    sync a ladder without any backfill task. Two numbers that answer the same
    question would eventually disagree, so there is only one of them.
    """
    if data_kind == "trade_candle" and interval:
        return int(db.count_candles(venue, symbol, interval))
    if data_kind == "mark_candle" and interval:
        return int(db.count_mark_candles(venue, symbol, interval))
    if data_kind == "funding":
        return int(db.count_funding(venue, symbol))
    if data_kind == "open_interest":
        return int(db.count_oi(venue, symbol))
    if data_kind == "risk_limit":
        return len(db.load_risk_tiers(venue, symbol) or [])
    return None


def contiguous_tail(db, venue: str, symbol: str, interval: str, *,
                    limit: int = CONTIGUITY_SCAN_LIMIT) -> dict:
    """The newest unbroken run of bars: the range a formal study can use.

    Walks back from the newest bar and stops at the first missing stamp, so the
    answer is "you may backtest inside this window", not "you have N bars
    somewhere". The scan is bounded, and says so when the bound was reached.
    """
    step = INTERVAL_MS.get(interval)
    if step is None:
        return {"interval": interval, "bars": 0, "fromTs": None, "toTs": None,
                "scanLimitReached": False, "gapsBefore": 0}
    rows = db.query(
        "SELECT open_ts FROM candles WHERE venue=? AND symbol=? AND interval=? "
        "ORDER BY open_ts DESC LIMIT ?",
        (venue, symbol, interval, int(limit)),
    )
    if not rows:
        return {"interval": interval, "bars": 0, "fromTs": None, "toTs": None,
                "scanLimitReached": False, "gapsBefore": 0}
    newest = int(rows[0]["open_ts"])
    count = 1
    expected = newest
    for row in rows[1:]:
        stamp = int(row["open_ts"])
        if stamp != expected - step:
            break
        expected = stamp
        count += 1
    scan_limit_reached = count == len(rows) == int(limit)
    return {
        "interval": interval,
        "bars": count,
        "fromTs": int(expected),
        "toTs": newest,
        "scanLimitReached": scan_limit_reached,
        "gapsBefore": 0 if scan_limit_reached else count,
    }


def series_range(db, venue: str, symbol: str, data_kind: str, *, interval: str = "",
                 launch_ts: int | None = None, scan_limit: int = CONTIGUITY_SCAN_LIMIT) -> dict:
    """One row of the panel: what exists, what is usable, and what is missing."""
    state = db.load_backfill_state(
        venue, symbol, series_state_interval(data_kind, interval), data_kind
    ) or {}
    if state.get("status") == STATUS_UNSUPPORTED:
        return {
            "symbol": symbol, "dataKind": data_kind, "interval": interval,
            "status": STATUS_UNSUPPORTED,
            "reason": state.get("reason") or "交易所不提供该数据族",
            "barsAvailable": 0, "usableBars": 0,
            "historyFromTs": None, "historyToTs": None,
            "usableFromTs": None, "usableToTs": None,
            "gapFree": False, "hasGaps": False, "reachedListing": False,
            "scanLimitReached": False,
        }

    if data_kind == "trade_candle":
        bars = db.count_candles(venue, symbol, interval)
        first, last = db.first_open_ts(venue, symbol, interval), db.last_open_ts(venue, symbol, interval)
        step_ms = INTERVAL_MS.get(interval) or 0
        # Counting rows against the span proves the *whole* stored series is
        # unbroken without walking it; the bar-by-bar scan is only needed when
        # that arithmetic says something is missing somewhere.
        whole_series_contiguous = bool(
            first is not None and last is not None and step_ms and bars
            and int(bars) == (int(last) - int(first)) // step_ms + 1
        )
        if whole_series_contiguous:
            usable_bars, usable_from, usable_to = int(bars), first, last
            scan_limit_reached, reason = False, ""
        else:
            tail = contiguous_tail(db, venue, symbol, interval, limit=scan_limit)
            usable_bars, usable_from, usable_to = tail["bars"], tail["fromTs"], tail["toTs"]
            scan_limit_reached, reason = tail["scanLimitReached"], ""
    elif data_kind == "mark_candle":
        bars = db.count_mark_candles(venue, symbol, interval)
        first, last = db.mark_bounds(venue, symbol, interval)
        usable_bars, usable_from, usable_to, scan_limit_reached = bars, first, last, False
        reason = ""
    elif data_kind == "funding":
        bars = db.count_funding(venue, symbol)
        first, last = db.funding_bounds(venue, symbol)
        usable_bars, usable_from, usable_to, scan_limit_reached = bars, first, last, False
        reason = ""
    elif data_kind == "open_interest":
        bars = db.count_oi(venue, symbol)
        first, last = db.oi_bounds(venue, symbol)
        usable_bars, usable_from, usable_to, scan_limit_reached = bars, first, last, False
        reason = ""
    elif data_kind == "risk_limit":
        tiers = db.load_risk_tiers(venue, symbol) or []
        bars = len(tiers)
        collected = max((int(row.get("synced_at") or 0) for row in tiers), default=0)
        first = last = collected or None
        usable_bars, usable_from, usable_to, scan_limit_reached = bars, first, last, False
        reason = ""
    else:
        return {"symbol": symbol, "dataKind": data_kind, "interval": interval,
                "status": STATUS_NO_DATA, "reason": f"未知的数据族：{data_kind}",
                "barsAvailable": 0, "usableBars": 0, "historyFromTs": None, "historyToTs": None,
                "usableFromTs": None, "usableToTs": None,
                "gapFree": False, "hasGaps": False, "reachedListing": False,
                "scanLimitReached": False}

    if bars == 0:
        return {
            "symbol": symbol, "dataKind": data_kind, "interval": interval,
            "status": STATUS_NO_DATA, "reason": reason or "本地没有该数据族",
            "barsAvailable": 0, "usableBars": 0,
            "historyFromTs": None, "historyToTs": None,
            "usableFromTs": None, "usableToTs": None,
            "gapFree": False, "hasGaps": False, "reachedListing": False,
            "scanLimitReached": False,
        }

    # Three separate facts, deliberately not collapsed into one number:
    #   gapFree        - the *usable window* is unbroken, so a study may run in it;
    #   hasGaps        - the stored history has holes before that window;
    #   reachedListing - the walk has reached the contract's launch date.
    # Funding, open interest and the risk ladder have no fixed bar spacing, so
    # "the window is unbroken" is not a question about them. Saying "no gaps"
    # would be as wrong as saying "gapped": the answer is that it does not apply.
    step = INTERVAL_MS.get(interval) or 0
    spaced_series = bool(step) and data_kind in ("trade_candle", "mark_candle")
    contiguous = (
        bool(usable_from is not None and usable_to is not None and step
             and int(usable_bars) == (int(usable_to) - int(usable_from)) // step + 1)
        if spaced_series else None
    )
    # A bounded scan stops early on a long series. That is not a gap, and saying
    # so would mark every deep history as broken; the bound is reported instead.
    has_gaps = False if scan_limit_reached else (
        int(usable_bars) < int(bars) or contiguous is False
    )
    walked_to_the_end = bool(state.get("complete"))
    if walked_to_the_end:
        reached_listing: bool | None = True
    elif launch_ts and first is not None and step:
        reached_listing = int(first) <= int(launch_ts) + 2 * step
    else:
        reached_listing = None

    if has_gaps:
        status = STATUS_GAPPED
    elif reached_listing is False:
        status = STATUS_NOT_REACHED_LISTING
    else:
        status = STATUS_OK
    if scan_limit_reached and not reason:
        reason = (f"仅扫描最近 {int(scan_limit)} 根以确认连续性，未发现缺口；"
                  "更早的区间未逐根核对")
    elif not spaced_series and not reason:
        reason = EVENT_SERIES_REASONS.get(data_kind, "该数据族不按固定周期到达，不做逐根连续性核对")

    return {
        "symbol": symbol,
        "dataKind": data_kind,
        "interval": interval,
        "status": status,
        "reason": reason,
        "barsAvailable": int(bars),
        "usableBars": int(usable_bars),
        "historyFromTs": int(first) if first else None,
        "historyToTs": int(last) if last else None,
        "usableFromTs": int(usable_from) if usable_from else None,
        "usableToTs": int(usable_to) if usable_to else None,
        "gapFree": contiguous,
        "hasGaps": bool(has_gaps),
        "reachedListing": reached_listing,
        "scanLimitReached": bool(scan_limit_reached),
    }


def backtestable_ranges(db, *, venue: str = "bybit", symbols: list[str] | None = None,
                        intervals: tuple[str, ...] = ("15m", "1h", "4h", "1d"),
                        kinds: tuple[str, ...] = ("trade_candle", "mark_candle", "funding",
                                                  "open_interest", "risk_limit"),
                        scan_limit: int = CONTIGUITY_SCAN_LIMIT) -> dict:
    """The panel's data: per contract and family, what can be studied and what is missing."""
    wanted = list(symbols) if symbols else None
    if wanted is None:
        from ..config.instruments import VENUE_SYMBOLS

        wanted = list(VENUE_SYMBOLS)
    rows: list[dict] = []
    for symbol in wanted:
        meta = db.load_instrument_meta(venue, symbol) or {}
        launch_ts = int(meta["launch_ts"]) if meta.get("launch_ts") else None
        for kind in kinds:
            for interval in (intervals if kind in ("trade_candle", "mark_candle") else ("",)):
                rows.append(series_range(
                    db, venue, symbol, kind, interval=interval, launch_ts=launch_ts,
                    scan_limit=scan_limit,
                ))
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    return {
        "rows": rows,
        "byStatus": by_status,
        "summary": {
            "series": len(rows),
            "gapFree": sum(1 for row in rows if row["gapFree"] and row["barsAvailable"]),
            "reachedListing": sum(1 for row in rows if row["reachedListing"]),
            "unsupported": sum(1 for row in rows if row["status"] == STATUS_UNSUPPORTED),
            "empty": sum(1 for row in rows if row["status"] == STATUS_NO_DATA),
        },
    }
