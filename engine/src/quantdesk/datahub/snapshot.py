"""One pinned, attributable view of the stored market data.

Every consumer that reads history - the backtest, the alert engine, the strategy
validators, TradingAgents research and the panel routes - has to answer the same
three questions before it can be trusted: which bars did it read, where did each
one come from, and is a bar that is missing a defect or just the market being
closed. This module answers them in one place.

A `DataSnapshot` is a frozen view: the bars for a range, their provenance, the
gaps in the range with a reason for each, and a version hash over the whole thing.
Two readers holding the same version are provably looking at the same data, and a
result that recorded a version can be re-checked later instead of believed.

Gap classification uses the trading calendar, not a guess:

* `missing_in_session` - the market was open and the bar is not here. This is a
  defect and is repairable from the venue.
* `off_hours`, `weekend`, `holiday` - the underlying market was shut. Repairing
  these would invent bars, so they are reported and never backfilled.
* `service_down` - the window falls inside a period when the in-process service
  was not running and no other writer covered it, which is a local outage rather
  than a venue one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterator

from . import calendar as cal
from .venue import INTERVAL_MS

SNAPSHOT_VERSION = "snapshot/1"
# A repair list longer than this is a range problem, not a list; the report keeps
# the head and says how many more there are.
MAX_REPAIR_LIST = 200

# Sources ordered by trust: a venue frame beats a venue REST read, which beats a
# locally derived bar, which beats an imported file.
SOURCE_TRUST = {
    "venue_ws": 4,
    "venue_rest": 3,
    "local_derived": 2,
    "imported": 1,
    "unknown": 0,
}


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class BarCoverage:
    """What the range should have held, and what it holds."""

    interval: str
    step_ms: int
    from_ts: int | None
    to_ts: int | None
    expected: int
    present: int
    missing_in_session: int
    off_hours: int
    weekend: int
    holiday: int
    service_down: int
    session_gated: bool
    sources: dict[str, int] = field(default_factory=dict)
    collectors: dict[str, int] = field(default_factory=dict)
    first_bar_ts: int | None = None
    last_bar_ts: int | None = None
    newest_received_ts: int | None = None
    oldest_received_ts: int | None = None

    @property
    def complete(self) -> bool:
        return self.missing_in_session == 0 and self.service_down == 0

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["complete"] = self.complete
        return payload


@dataclass
class Gap:
    """One stretch of bars the range expected but does not hold."""

    from_ts: int
    to_ts: int
    bars: int
    kind: str
    reason: str
    repairable: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SnapshotProvenance:
    """The identity of one snapshot: what it read and what it came from."""

    version: str
    venue: str
    symbols: list[str]
    interval: str | None
    from_ts: int | None
    to_ts: int | None
    bars: int
    data_hash: str
    sources: dict[str, int]
    collectors: dict[str, int]
    generated_at: int
    newest_received_ts: int | None
    oldest_exchange_ts: int | None
    newest_exchange_ts: int | None
    coverage: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def history_version(
    venue: str,
    symbol: str,
    interval: str,
    from_ts: int | None,
    to_ts: int | None,
    bars: list[dict],
    *,
    funding: list[dict] | None = None,
    marks: list[dict] | None = None,
) -> str:
    """The canonical id of a set of stored rows.

    Both the coverage report and the history a consumer pins go through this, so
    two readers that covered the same range can compare one string instead of
    comparing bars field by field. Content decides the id; when the read happened
    does not. Funding rows carry `rate` rather than prices, so they are folded in
    with their own digest.
    """
    import hashlib as _hashlib

    payload = ":".join(
        [
            "history/1",
            f"{venue}.{symbol}.{interval}",
            str(int(from_ts if from_ts is not None else 0)),
            str(int(to_ts if to_ts is not None else 0)),
            _bar_hash(bars),
            _rate_hash(funding) if funding else "nofunding",
            _bar_hash(marks) if marks else "nomarks",
        ]
    )
    return _hashlib.sha256(payload.encode()).hexdigest()[:16]


def pin_snapshot(snapshot: "DataSnapshot") -> str:
    """The canonical id of a snapshot, from the rows it actually holds.

    A snapshot that skipped funding and one that read it are not the same view, so
    each reports the id of what it has rather than of what the store contains.
    """
    return history_version(
        snapshot.venue,
        snapshot.symbols[0] if len(snapshot.symbols) == 1 else "*",
        snapshot.interval or "",
        snapshot.from_ts,
        snapshot.to_ts,
        [row for symbol in snapshot.symbols for row in snapshot.candles(symbol)],
        funding=[row for symbol in snapshot.symbols for row in snapshot.funding_rows(symbol)] or None,
        marks=[row for symbol in snapshot.symbols for row in snapshot.mark_rows(symbol)] or None,
    )


def _rate_hash(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(f"{int(row['ts'])}|{float(row.get('rate') or 0):.12g}\n".encode())
    return digest.hexdigest()[:8]


def _bar_hash(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            f"{int(row['ts'])}|{float(row['open']):.10g}|{float(row['high']):.10g}|"
            f"{float(row['low']):.10g}|{float(row['close']):.10g}|{float(row.get('volume') or 0):.10g}\n".encode()
        )
    return digest.hexdigest()[:16]


def classify_gap(ts: int) -> tuple[str, str, bool]:
    """Why a bar may be absent, and whether it is worth repairing.

    Returns `(kind, reason, repairable)`. Only an open-market gap is repairable:
    everything else is the market being shut.
    """
    kind, reason = cal.classify_timestamp(ts)
    if kind == "open":
        return "missing_in_session", "交易时段内缺失", True
    if kind == "holiday":
        return "holiday", reason, False
    if kind == "weekend":
        return "weekend", reason, False
    return "off_hours", reason, False


def expected_stamps(from_ts: int, to_ts: int, step_ms: int, anchor: int | None = None) -> list[int]:
    """Every bar open time the range should contain.

    The grid is anchored to a stored bar when one exists rather than to the epoch:
    exchanges stamp their bars on a boundary this code does not get to assume, and
    guessing it wrong would report every bar as missing. Without a stored bar the
    window start is the anchor, which only leaves the head of an empty range
    approximate - and that is reported as such.
    """
    if step_ms <= 0 or to_ts < from_ts:
        return []
    base = int(anchor) if anchor is not None else ((from_ts // step_ms) * step_ms)
    start = base
    while start - step_ms >= from_ts:
        start -= step_ms
    out: list[int] = []
    stamp = start
    while stamp <= to_ts:
        if stamp >= from_ts:
            out.append(stamp)
        stamp += step_ms
    return out


def _coverage(
    rows: list[dict],
    *,
    interval: str,
    from_ts: int,
    to_ts: int,
    session_gated: bool,
    product_type: str | None,
    service_windows: list[tuple[int, int]] | None = None,
) -> tuple[BarCoverage, list[Gap]]:
    step = INTERVAL_MS.get(interval)
    if step is None:
        raise ValueError(f"unsupported interval {interval!r}")
    present = {int(row["ts"]) for row in rows}
    expected = expected_stamps(from_ts, to_ts, step, anchor=min(present) if present else None)
    sources: dict[str, int] = {}
    collectors: dict[str, int] = {}
    received = [int(row["received_ts"]) for row in rows if row.get("received_ts")]
    exchange = [int(row["exchange_ts"]) for row in rows if row.get("exchange_ts")]
    for row in rows:
        sources[str(row.get("source") or "unknown")] = sources.get(str(row.get("source") or "unknown"), 0) + 1
        collectors[str(row.get("collector") or "unknown")] = collectors.get(str(row.get("collector") or "unknown"), 0) + 1

    coverage = BarCoverage(
        interval=interval,
        step_ms=step,
        from_ts=from_ts,
        to_ts=to_ts,
        expected=len(expected),
        present=sum(1 for stamp in expected if stamp in present),
        missing_in_session=0,
        off_hours=0,
        weekend=0,
        holiday=0,
        service_down=0,
        session_gated=session_gated,
        sources=sources,
        collectors=collectors,
        first_bar_ts=min(present) if present else None,
        last_bar_ts=max(present) if present else None,
        newest_received_ts=max(received) if received else None,
        oldest_received_ts=min(received) if received else None,
    )
    _ = exchange

    # Walk the missing stamps and group consecutive ones of the same kind.
    gaps: list[Gap] = []
    for stamp in expected:
        if stamp in present:
            continue
        kind, reason, repairable = classify_gap(stamp)
        if not session_gated:
            # Crypto trades continuously, including weekends and US equity
            # holidays. Equity-calendar closures cannot hide missing crypto bars.
            kind, reason, repairable = "missing_in_session", "交易时段内缺失", True
        if kind == "missing_in_session" and _inside_outage(stamp, step, service_windows):
            kind, reason, repairable = "service_down", "本地服务未运行期间的空洞", True
        _tally(coverage, kind)
        if gaps and gaps[-1].kind == kind and gaps[-1].to_ts + step == stamp:
            gaps[-1].to_ts = stamp
            gaps[-1].bars += 1
        else:
            gaps.append(Gap(from_ts=stamp, to_ts=stamp, bars=1, kind=kind, reason=reason, repairable=repairable))
    return coverage, gaps


def _tally(coverage: BarCoverage, kind: str) -> None:
    if kind == "missing_in_session":
        coverage.missing_in_session += 1
    elif kind == "holiday":
        coverage.holiday += 1
    elif kind == "weekend":
        coverage.weekend += 1
    elif kind == "service_down":
        coverage.service_down += 1
    else:
        coverage.off_hours += 1


def _inside_outage(stamp: int, step: int, windows: list[tuple[int, int]] | None) -> bool:
    """Is this missing bar fully inside a window when no writer was running?"""
    if not windows:
        return False
    return any(start <= stamp and stamp + step <= end for start, end in windows)


@dataclass
class DataSnapshot:
    """A pinned view of stored history: bars, provenance, coverage, gaps."""

    version: str = SNAPSHOT_VERSION
    venue: str = "bybit"
    symbols: list[str] = field(default_factory=list)
    interval: str | None = None
    from_ts: int | None = None
    to_ts: int | None = None
    bars: dict[str, list[dict]] = field(default_factory=dict)
    # Distinct rows in the store per symbol, counted at pin time. `bars` is the
    # pinned window; this is everything the series holds.
    bars_available: dict[str, int] = field(default_factory=dict)
    funding: dict[str, list[dict]] = field(default_factory=dict)
    marks: dict[str, list[dict]] = field(default_factory=dict)
    coverage: dict[str, BarCoverage] = field(default_factory=dict)
    gaps: dict[str, list[Gap]] = field(default_factory=dict)
    provenance: SnapshotProvenance | None = None
    notes: list[str] = field(default_factory=list)

    # -- reads -----------------------------------------------------------
    def candles(self, symbol: str) -> list[dict]:
        return self.bars.get(symbol, [])

    def funding_rows(self, symbol: str) -> list[dict]:
        return self.funding.get(symbol, [])

    def mark_rows(self, symbol: str) -> list[dict]:
        return self.marks.get(symbol, [])

    def repair_list(self, symbol: str) -> dict[str, Any]:
        """The bars a repair should fetch, and why the rest are not repairable."""
        gaps = self.gaps.get(symbol, [])
        repairable = [gap for gap in gaps if gap.repairable]
        total = sum(gap.bars for gap in repairable)
        head = repairable[:MAX_REPAIR_LIST]
        return {
            "symbol": symbol,
            "gaps": [gap.as_dict() for gap in head],
            "bars": total,
            "truncated": len(repairable) > len(head),
            "unrepairable": [gap.as_dict() for gap in gaps if not gap.repairable][:MAX_REPAIR_LIST],
        }

    def verify(self, current_bars: dict[str, list[dict]] | None = None) -> dict[str, Any]:
        """Does the data still match the version this snapshot recorded?

        Two ways it can stop matching: the pinned bars changed, or bars were added
        after the snapshot was taken. Both make a result unreproducible, and the
        second is the quieter one.
        """
        problems: list[str] = []
        rows = current_bars if current_bars is not None else {symbol: self.candles(symbol) for symbol in self.symbols}
        current = _bar_hash([row for symbol in self.symbols for row in rows.get(symbol, [])])
        if self.provenance and current != self.provenance.data_hash:
            problems.append(f"数据指纹不一致：记录 {self.provenance.data_hash}，当前 {current}")
        if self.provenance and self.provenance.to_ts is not None:
            newest = max((int(row["ts"]) for symbol in self.symbols for row in rows.get(symbol, [])), default=None)
            if newest is not None and newest > self.provenance.to_ts:
                problems.append(f"快照之后又写入了新K线（最新 {newest} 超出区间上界 {self.provenance.to_ts}）")
        return {"reproducible": not problems, "problems": problems, "currentHash": current}

    def verify_against(self, db) -> dict[str, Any]:
        """Re-read the pinned range and compare it with what was pinned."""
        rows = {
            symbol: db.load_candles(self.venue, symbol, self.interval, start_ts=self.from_ts, end_ts=self.to_ts)
            for symbol in self.symbols
        }
        result = self.verify(rows)
        # Newer bars beyond the pinned range count as drift too.
        if self.provenance and self.provenance.to_ts is not None:
            for symbol in self.symbols:
                newest = db.last_open_ts(self.venue, symbol, self.interval or "")
                if newest is not None and newest > self.provenance.to_ts:
                    result["problems"].append(f"{symbol} 在快照区间之后已有新K线（{newest}）")
                    result["reproducible"] = False
                    break
        return result

    # -- replay ----------------------------------------------------------
    def replay(self, *, include_funding: bool = True, include_marks: bool = False) -> Iterator[dict]:
        """Walk the stored history the way it arrived: chronological, tagged.

        This is a read of what is stored, not a re-fetch: a replay of a range with
        a gap raises that gap in the stream instead of hiding it, which is what
        makes it usable for reproducing a past decision.
        """
        timeline: list[tuple[int, str, str, dict]] = []
        for symbol in self.symbols:
            for row in self.candles(symbol):
                timeline.append((int(row["ts"]), symbol, "candle", row))
            for gap in self.gaps.get(symbol, []):
                timeline.append((gap.from_ts, symbol, "gap", gap.as_dict()))
            if include_funding:
                for row in self.funding_rows(symbol):
                    timeline.append((int(row["ts"]), symbol, "funding", row))
            if include_marks:
                for row in self.mark_rows(symbol):
                    timeline.append((int(row["ts"]), symbol, "mark", row))
        timeline.sort(key=lambda item: (item[0], item[1], item[2]))
        for stamp, symbol, kind, payload in timeline:
            yield {
                "ts": stamp,
                "iso": dt.datetime.fromtimestamp(stamp / 1000, cal.UTC).isoformat(),
                "symbol": symbol,
                "kind": kind,
                "payload": payload,
            }


def load_snapshot(
    db,
    *,
    symbols: list[str],
    interval: str,
    from_ts: int,
    to_ts: int,
    venue: str = "bybit",
    product_types: dict[str, str] | None = None,
    with_funding: bool = True,
    with_marks: bool = True,
    bar_limit: int | None = None,
    service_windows: list[tuple[int, int]] | None = None,
) -> DataSnapshot:
    """Read one range for a set of symbols and pin it.

    `product_types` decides whether a missing bar is judged against the equity
    calendar; a crypto contract is expected to print every hour.
    """
    if not symbols:
        raise ValueError("快照至少需要一个合约")
    step = INTERVAL_MS.get(interval)
    if step is None:
        raise ValueError(f"unsupported interval {interval!r}")
    snapshot = DataSnapshot(venue=venue, symbols=list(symbols), interval=interval, from_ts=from_ts, to_ts=to_ts)
    all_rows: list[dict] = []
    for symbol in symbols:
        rows = db.load_candles(venue, symbol, interval, start_ts=from_ts, end_ts=to_ts, limit=bar_limit)
        snapshot.bars[symbol] = rows
        snapshot.bars_available[symbol] = count_available(db, venue, symbol, interval)
        all_rows.extend(rows)
        product_type = (product_types or {}).get(symbol)
        session_gated = bool(product_type and product_type != "crypto")
        coverage, gaps = _coverage(
            rows,
            interval=interval,
            from_ts=from_ts,
            to_ts=to_ts,
            session_gated=session_gated,
            product_type=product_type,
            service_windows=service_windows,
        )
        snapshot.coverage[symbol] = coverage
        snapshot.gaps[symbol] = gaps
        if with_funding:
            snapshot.funding[symbol] = db.load_funding(venue, symbol, start_ts=from_ts, end_ts=to_ts)
        if with_marks:
            try:
                snapshot.marks[symbol] = db.load_mark_candles(venue, symbol, interval, start_ts=from_ts, end_ts=to_ts)
            except Exception:  # noqa: BLE001 - a missing mark table is not fatal here
                snapshot.marks[symbol] = []

    sources: dict[str, int] = {}
    collectors: dict[str, int] = {}
    for coverage in snapshot.coverage.values():
        for name, count in coverage.sources.items():
            sources[name] = sources.get(name, 0) + count
        for name, count in coverage.collectors.items():
            collectors[name] = collectors.get(name, 0) + count
    exchange_stamps = [int(row["exchange_ts"]) for row in all_rows if row.get("exchange_ts")]
    received_stamps = [int(row["received_ts"]) for row in all_rows if row.get("received_ts")]
    warnings: list[str] = []
    missing = sum(coverage.missing_in_session for coverage in snapshot.coverage.values())
    outages = sum(coverage.service_down for coverage in snapshot.coverage.values())
    if missing:
        warnings.append(f"交易时段内缺失 {missing} 根K线，可用 fetch candles 定向补洞")
    if outages:
        warnings.append(f"本地服务未运行期间留下 {outages} 根空洞")
    if not received_stamps:
        warnings.append("存量为历史数据：没有任何一根K线记录了接收时间，无法判断当时是否已收盘")
    empty = [symbol for symbol in snapshot.symbols if not snapshot.candles(symbol)]
    if empty and empty != list(snapshot.symbols):
        warnings.append(f"{len(empty)} 个合约在该区间没有任何K线：{', '.join(empty[:5])}")
    if len(empty) == len(snapshot.symbols):
        warnings.append("该区间没有任何K线，覆盖率无法评估；请先补数据或放宽区间")
        for coverage in snapshot.coverage.values():
            coverage.expected = 0
    if any(name in sources for name in ("local_derived", "imported")):
        warnings.append("快照包含本地推导或导入的K线，来源见 coverage.sources")

    snapshot.version = pin_snapshot(snapshot)
    snapshot.provenance = SnapshotProvenance(
        version=snapshot.version,
        venue=venue,
        symbols=list(symbols),
        interval=interval,
        from_ts=from_ts,
        to_ts=to_ts,
        bars=len(all_rows),
        data_hash=_bar_hash(all_rows),
        sources=sources,
        collectors=collectors,
        generated_at=_now_ms(),
        newest_received_ts=max(received_stamps) if received_stamps else None,
        oldest_exchange_ts=min(exchange_stamps) if exchange_stamps else None,
        newest_exchange_ts=max(exchange_stamps) if exchange_stamps else None,
        coverage={symbol: coverage.as_dict() for symbol, coverage in snapshot.coverage.items()},
        warnings=warnings,
    )
    snapshot.notes = warnings
    return snapshot


def repair_gaps(
    db,
    snapshot: DataSnapshot,
    client,
    *,
    max_ranges: int = 20,
    exchange_ts_from: Callable[[int, int], int] | None = None,
) -> dict[str, Any]:
    """Fetch only the bars a snapshot reports as repairable, and nothing else.

    Off-hours and holiday absences are not on this list by construction: repairing
    them would write bars the venue never printed. Each gap stretch is requested
    with its own window, so a single hole in a month of data costs one call rather
    than a full re-download.
    """
    if snapshot.interval is None:
        raise ValueError("快照没有周期，无法补洞")
    step = INTERVAL_MS[snapshot.interval]
    report: dict[str, Any] = {"symbols": 0, "ranges": 0, "bars": 0, "written": 0, "errors": [], "detail": []}
    for symbol in snapshot.symbols:
        gaps = [gap for gap in snapshot.gaps.get(symbol, []) if gap.repairable]
        if not gaps:
            continue
        report["symbols"] += 1
        for gap in gaps[:max_ranges]:
            # One bar of margin on each side: the venue answers a range, and the
            # boundary bars tell us whether the hole really ends there.
            start = gap.from_ts - step
            end = gap.to_ts + 2 * step
            try:
                rows = client.kline("linear", symbol, snapshot.interval, start, end)
            except Exception as exc:  # noqa: BLE001 - one range must not stop the rest
                report["errors"].append(f"{symbol} {gap.from_ts}-{gap.to_ts}: {type(exc).__name__}: {exc}")
                continue
            wanted = [
                row
                for row in rows
                if gap.from_ts <= int(row["ts"]) <= gap.to_ts and int(row["ts"]) + step <= _now_ms()
            ]
            if not wanted:
                report["detail"].append(
                    {"symbol": symbol, "from": gap.from_ts, "to": gap.to_ts, "written": 0,
                     "note": "交易所在该区间没有返回已收盘K线"}
                )
                continue
            payload = []
            for row in wanted:
                item = dict(row)
                item["source"] = "venue_rest"
                if exchange_ts_from is not None:
                    item["exchange_ts"] = exchange_ts_from(int(row["ts"]), step)
                payload.append(item)
            written = db.upsert_candles(
                snapshot.venue, symbol, snapshot.interval, payload,
                source="venue_rest", ingestion_mode="repair",
            ).written
            report["ranges"] += 1
            report["bars"] += len(wanted)
            report["written"] += written
            report["detail"].append(
                {"symbol": symbol, "from": gap.from_ts, "to": gap.to_ts, "bars": len(wanted), "written": written}
            )
        if len(gaps) > max_ranges:
            report["errors"].append(f"{symbol} 还有 {len(gaps) - max_ranges} 段缺口未处理（本轮上限 {max_ranges}）")
    return report


def count_available(db, venue: str, symbol: str, interval: str) -> int:
    """Distinct stored rows for a series: the number a reader can verify.

    Counted with `COUNT(*)` over the primary key, never accumulated from fetch
    totals, so a repeated or overlapping backfill reports the same number.
    """
    if not interval:
        return 0
    try:
        return int(db.count_candles(venue, symbol, interval))
    except Exception:  # noqa: BLE001 - a count must not break a report
        return 0


def snapshot_summary(snapshot: DataSnapshot) -> dict[str, Any]:
    """The JSON shape the API and the CLI both report."""
    provenance = snapshot.provenance
    return {
        "provenance": provenance.as_dict() if provenance else None,
        "symbols": {
            symbol: {
                # `bars` is what this pinned window holds; `barsAvailable` is the
                # distinct row count for the series in the store, read from the
                # database so overlapping or repeated backfills cannot inflate it.
                "bars": len(snapshot.candles(symbol)),
                "barsAvailable": snapshot.bars_available.get(symbol, 0),
                "coverage": snapshot.coverage[symbol].as_dict(),
                "gaps": [gap.as_dict() for gap in snapshot.gaps.get(symbol, [])][:MAX_REPAIR_LIST],
                "repair": snapshot.repair_list(symbol),
            }
            for symbol in snapshot.symbols
        },
        "notes": snapshot.notes,
    }


def snapshot_version(snapshot: DataSnapshot) -> str:
    """A short, stable id for the pinned data, for storing next to a result.

    This is the `snapshot/1` id the provenance records: the same string a
    consumer's `HistorySlice.version` carries, so a coverage report and a result
    can be compared directly.
    """
    if not snapshot.provenance:
        return ""
    return snapshot.provenance.version


def as_json(snapshot: DataSnapshot) -> str:
    return json.dumps(snapshot_summary(snapshot), ensure_ascii=False)
