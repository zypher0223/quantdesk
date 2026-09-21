"""SQLite persistence: candles cache, journal, reports.

One database file under QUANTDESK_HOME (default ~/.quantdesk/quantdesk.db).
WAL mode; all timestamps are UTC epoch milliseconds. Bar open time is the
primary time axis (a 15m bar stamped 12:00 covers 12:00-12:15).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
import threading
import time
from pathlib import Path

from .schema import SCHEMA, SCHEMA_VERSION, apply_migrations  # noqa: F401


# Bumped when the shape or meaning of a written bar changes, so a stored row can
# be attributed to the code that produced it.
COLLECTOR_VERSION = "collector/1"


def _bounds(rows: list[dict]) -> tuple[int | None, int | None]:
    if not rows:
        return None, None
    lo, hi = rows[0].get("lo"), rows[0].get("hi")
    return (int(lo) if lo is not None else None, int(hi) if hi is not None else None)


@dataclass
class UpsertReport:
    """What a write actually did, per row.

    `submitted` is not a useful number on its own: a repeated or overlapping
    backfill would claim to have fetched bars it already had. These counts are
    what the store did, and `written` is the number an operator should see as
    "bars added or corrected".
    """

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    # Rows refused because a derived/imported series may not overwrite venue data.
    lower_rank: int = 0

    @property
    def written(self) -> int:
        return self.inserted + self.updated

    @property
    def submitted(self) -> int:
        return self.inserted + self.updated + self.unchanged + self.lower_rank

    def as_dict(self) -> dict:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "lowerRank": self.lower_rank,
            "written": self.written,
            "submitted": self.submitted,
        }

    def __add__(self, other: "UpsertReport") -> "UpsertReport":
        return UpsertReport(
            inserted=self.inserted + other.inserted,
            updated=self.updated + other.updated,
            unchanged=self.unchanged + other.unchanged,
            lower_rank=self.lower_rank + other.lower_rank,
        )


class Database:
    """Thin sqlite wrapper; safe for multi-threaded FastAPI usage."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            # Shape and upgrades live in `datahub/schema.py`: the DDL is applied
            # on every open, and the ordered migrations bring an older file
            # forward once each.
            apply_migrations(self._conn)
    # -- generic ---------------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def acquire_ai_paper_lease(
        self, profile_id: str, owner: str, *, now_ms: int, ttl_ms: int
    ) -> bool:
        """Atomically acquire an expiring AI evaluation lease.

        The database is the coordinator because the API and background monitor
        may use different service objects, threads, or processes.
        """
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO ai_paper_leases (profile_id,owner,acquired_ts,expires_ts) VALUES (?,?,?,?) "
                "ON CONFLICT(profile_id) DO UPDATE SET owner=excluded.owner,"
                "acquired_ts=excluded.acquired_ts,expires_ts=excluded.expires_ts "
                "WHERE ai_paper_leases.expires_ts < excluded.acquired_ts",
                (profile_id, owner, int(now_ms), int(now_ms + ttl_ms)),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def release_ai_paper_lease(self, profile_id: str, owner: str) -> None:
        """Release only the caller's lease; never delete a newer owner's row."""
        self.execute(
            "DELETE FROM ai_paper_leases WHERE profile_id=? AND owner=?",
            (profile_id, owner),
        )

    def _compress_stored_factor_values(self, *, limit: int = 200) -> int:
        """Compress factor series written before the column existed.

        The work lives with the schema it upgrades; this keeps the store's own
        callers working.
        """
        from .schema import compress_legacy_factor_values

        with self._lock:
            return compress_legacy_factor_values(self._conn, limit=limit)

    def close(self) -> None:
        """Close the underlying connection for long-lived service owners."""
        with self._lock:
            self._conn.close()

    def record_alert_trigger(self, *, event: tuple, rule_id: str, triggered_ts: int) -> None:
        """Persist the trigger journal row and rule cursor atomically."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO alert_events "
                    "(id,rule_id,venue_symbol,condition_type,timeframe,metric,threshold,observed_ts,triggered_ts,title,message,notification_results) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    event,
                )
                self._conn.execute(
                    "UPDATE alert_rules SET last_triggered_ts=? WHERE id=?",
                    (triggered_ts, rule_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def create_alert_rule(self, *, parent: tuple, conditions: list[tuple]) -> None:
        """Insert a rule and all of its conditions as one registry change."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO alert_rules "
                    "(id,name,venue_symbol,condition_type,timeframe,threshold,cooldown_seconds,enabled,"
                    "severity,quiet_start,quiet_end,timezone,daily_limit,confirmation_count,hysteresis,created_ts,updated_ts) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    parent,
                )
                self._conn.executemany(
                    "INSERT INTO alert_rule_conditions "
                    "(id,rule_id,position,condition_type,timeframe,threshold,strategy_id,strategy_parameters,signal_direction) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    conditions,
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def replace_alert_rule(self, *, rule_id: str, parent: tuple, conditions: list[tuple]) -> None:
        """Update a rule and replace its condition graph atomically."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE alert_rules SET name=?,venue_symbol=?,condition_type=?,timeframe=?,threshold=?,"
                    "cooldown_seconds=?,enabled=?,severity=?,quiet_start=?,quiet_end=?,timezone=?,daily_limit=?,"
                    "confirmation_count=?,hysteresis=?,last_condition=0,last_observed_ts=NULL,last_observation_key=NULL,"
                    "consecutive_count=0,armed=1,updated_ts=? WHERE id=?",
                    (*parent, rule_id),
                )
                self._conn.execute("DELETE FROM alert_rule_conditions WHERE rule_id=?", (rule_id,))
                self._conn.executemany(
                    "INSERT INTO alert_rule_conditions "
                    "(id,rule_id,position,condition_type,timeframe,threshold,strategy_id,strategy_parameters,signal_direction) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    conditions,
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def kv_get(self, key: str) -> str | None:
        rows = self.query("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _kv_float_locked(self, key: str, default: float = 0.0) -> float:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return float(row["value"]) if row and row["value"] is not None else default

    def _kv_set_locked(self, key: str, value: float) -> None:
        self._conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, repr(round(value, 6))),
        )

    def open_paper_position(
        self,
        *,
        venue: str,
        symbol: str,
        side: str,
        qty: float,
        avg_price: float,
        leverage: float,
        liq_price: float | None,
        entry_fee: float,
        rationale: str,
        updated_ts: int,
        expected_cash: float,
        expected_open_ids: list[int],
        stop_loss: float | None = None,
        take_profit_1: float | None = None,
        take_profit_2: float | None = None,
    ) -> int | None:
        """Insert a position and debit its fee atomically.

        The optimistic state check prevents two API requests that valued the
        same account snapshot from both spending it.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                current_cash = self._kv_float_locked("paper_cash", expected_cash)
                current_ids = [
                    int(row["id"])
                    for row in self._conn.execute(
                        "SELECT id FROM positions WHERE closed_ts IS NULL ORDER BY id"
                    ).fetchall()
                ]
                if round(current_cash, 6) != round(expected_cash, 6) or current_ids != expected_open_ids:
                    self._conn.rollback()
                    return None
                cursor = self._conn.execute(
                    "INSERT INTO positions (venue, symbol, side, qty, avg_price, leverage, liq_price, entry_fee, funding_paid, updated_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                    (venue, symbol, side, qty, avg_price, leverage, liq_price, entry_fee, updated_ts),
                )
                position_id = int(cursor.lastrowid)
                if rationale:
                    self._conn.execute(
                        "INSERT INTO position_notes (position_id, notes, updated_ts) VALUES (?, ?, ?)",
                        (position_id, rationale, updated_ts),
                    )
                protective_orders = (
                    ("stop_loss", stop_loss, 1.0),
                    ("take_profit_1", take_profit_1, 0.5 if take_profit_2 is not None else 1.0),
                    ("take_profit_2", take_profit_2, 1.0),
                )
                self._conn.executemany(
                    "INSERT INTO paper_orders "
                    "(position_id, order_type, trigger_price, close_fraction, status, created_ts) "
                    "VALUES (?, ?, ?, ?, 'open', ?)",
                    [
                        (position_id, order_type, trigger_price, close_fraction, updated_ts)
                        for order_type, trigger_price, close_fraction in protective_orders
                        if trigger_price is not None
                    ],
                )
                self._kv_set_locked("paper_cash", current_cash - entry_fee)
                self._kv_set_locked(
                    "paper_fees",
                    self._kv_float_locked("paper_fees") + entry_fee,
                )
                self._conn.commit()
                return position_id
            except Exception:
                self._conn.rollback()
                raise

    def settle_paper_funding(self, position_id: int, cost: float) -> bool:
        """Book one already-authorised funding event as a single transaction."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT id FROM positions WHERE id = ? AND closed_ts IS NULL", (position_id,)
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    return False
                self._conn.execute(
                    "UPDATE positions SET funding_paid = funding_paid + ? WHERE id = ?",
                    (cost, position_id),
                )
                self._kv_set_locked("paper_cash", self._kv_float_locked("paper_cash") - cost)
                self._kv_set_locked("paper_funding", self._kv_float_locked("paper_funding") + cost)
                key = "paper_funding_paid" if cost > 0 else "paper_funding_received"
                self._kv_set_locked(key, self._kv_float_locked(key) + abs(cost))
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def settle_paper_funding_once(
        self,
        position_id: int,
        *,
        funding_ts: int,
        rate: float,
        mark_price: float,
        cost: float,
    ) -> bool:
        """Book a venue funding timestamp at most once for one position."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT id FROM positions WHERE id = ? AND closed_ts IS NULL", (position_id,)
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    return False
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO paper_funding_settlements "
                    "(position_id, funding_ts, rate, mark_price, cost) VALUES (?, ?, ?, ?, ?)",
                    (position_id, int(funding_ts), float(rate), float(mark_price), float(cost)),
                )
                if cursor.rowcount == 0:
                    self._conn.rollback()
                    return False
                self._conn.execute(
                    "UPDATE positions SET funding_paid = funding_paid + ? WHERE id = ?",
                    (cost, position_id),
                )
                self._kv_set_locked("paper_cash", self._kv_float_locked("paper_cash") - cost)
                self._kv_set_locked("paper_funding", self._kv_float_locked("paper_funding") + cost)
                key = "paper_funding_paid" if cost > 0 else "paper_funding_received"
                self._kv_set_locked(key, self._kv_float_locked(key) + abs(cost))
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def paper_orders(self, position_id: int, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM paper_orders WHERE position_id = ?"
        params: list = [position_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id"
        return self.query(sql, tuple(params))

    def reduce_paper_position(
        self,
        position_id: int,
        *,
        qty: float,
        exit_price: float,
        exit_fee: float,
        exit_reason: str,
        order_id: int | None = None,
        closed_ts: int | None = None,
    ) -> dict | None:
        """Realise all or part of a position and append one immutable fill record."""
        now = int(closed_ts or time.time() * 1000)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                raw = self._conn.execute(
                    "SELECT * FROM positions WHERE id = ? AND closed_ts IS NULL", (position_id,)
                ).fetchone()
                if raw is None:
                    self._conn.rollback()
                    return None
                position = dict(raw)
                if order_id is not None:
                    order = self._conn.execute(
                        "SELECT status FROM paper_orders WHERE id = ? AND position_id = ?",
                        (order_id, position_id),
                    ).fetchone()
                    if order is None or order["status"] != "open":
                        self._conn.rollback()
                        return None
                current_qty = float(position["qty"])
                close_qty = min(float(qty), current_qty)
                if close_qty <= 0:
                    self._conn.rollback()
                    return None
                fraction = close_qty / current_qty
                remaining_qty = current_qty - close_qty
                entry_fee = round(float(position.get("entry_fee") or 0) * fraction, 6)
                funding = round(float(position.get("funding_paid") or 0) * fraction, 6)
                direction = 1 if position["side"] == "long" else -1
                gross = round(direction * close_qty * (exit_price - position["avg_price"]), 6)
                total_fees = round(entry_fee + float(exit_fee), 6)
                net = round(gross - total_fees - funding, 6)
                note = self._conn.execute(
                    "SELECT notes FROM position_notes WHERE position_id = ?", (position_id,)
                ).fetchone()
                rationale = note["notes"] if note else ""
                opened_ts = int(position["updated_ts"])
                digest = journal_hash(
                    venue=position["venue"], symbol=position["symbol"], side=position["side"],
                    qty=close_qty, entry_price=position["avg_price"], exit_price=exit_price,
                    opened_ts=opened_ts, closed_ts=now, net_pnl=net,
                )
                if remaining_qty <= max(current_qty * 1e-10, 1e-12):
                    self._conn.execute(
                        "UPDATE positions SET closed_ts = ?, updated_ts = ?, qty = 0, entry_fee = 0, funding_paid = 0 WHERE id = ?",
                        (now, now, position_id),
                    )
                    self._conn.execute(
                        "UPDATE paper_orders SET status = 'canceled' WHERE position_id = ? AND status = 'open'",
                        (position_id,),
                    )
                else:
                    self._conn.execute(
                        "UPDATE positions SET qty = ?, entry_fee = ?, funding_paid = ? WHERE id = ?",
                        (
                            remaining_qty,
                            float(position.get("entry_fee") or 0) - entry_fee,
                            float(position.get("funding_paid") or 0) - funding,
                            position_id,
                        ),
                    )
                if order_id is not None:
                    self._conn.execute(
                        "UPDATE paper_orders SET status = 'filled', triggered_ts = ? WHERE id = ?",
                        (now, order_id),
                    )
                self._conn.execute(
                    "INSERT INTO journal (position_id, opened_ts, closed_ts, venue, symbol, side, qty, "
                    "entry_price, exit_price, leverage, liq_price, gross_pnl, funding_paid, fees, net_pnl, "
                    "exit_reason, rationale, tags, signal_id, report_id, source, entry_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        position_id, opened_ts, now, position["venue"], position["symbol"], position["side"],
                        close_qty, position["avg_price"], exit_price, position["leverage"], position["liq_price"],
                        gross, funding, total_fees, net, exit_reason, rationale, None, None, None, "paper", digest,
                    ),
                )
                self._kv_set_locked("paper_cash", self._kv_float_locked("paper_cash") + gross - float(exit_fee))
                self._kv_set_locked("paper_fees", self._kv_float_locked("paper_fees") + float(exit_fee))
                self._kv_set_locked("paper_realized", self._kv_float_locked("paper_realized") + net)
                self._conn.commit()
                return {
                    "gross_pnl": gross,
                    "funding_paid": funding,
                    "fees": total_fees,
                    "net_pnl": net,
                    "closed_ts": now,
                    "entry_hash": digest,
                    "closed_qty": close_qty,
                    "remaining_qty": max(0.0, remaining_qty),
                }
            except Exception:
                self._conn.rollback()
                raise

    # -- candles ---------------------------------------------------------
    # Where a bar's price came from, ranked. A higher-ranked source may correct a
    # lower-ranked one; two venue sources may correct each other. Anything not in
    # this table ranks 0, which is exactly the bug that made backfilled rows
    # un-updatable when their source string was not listed.
    SOURCE_RANKS = {
        "venue_ws": 4,
        "venue_rest": 3,
        "local_derived": 2,
        "imported": 1,
    }

    def upsert_candles(
        self,
        venue: str,
        symbol: str,
        interval: str,
        rows: list[dict],
        *,
        source: str = "venue_rest",
        collector: str = COLLECTOR_VERSION,
        received_ts: int | None = None,
        ingestion_mode: str = "live",
    ) -> "UpsertReport":
        """Store closed bars with the provenance of the path that produced them.

        `source` is where the price came from: `venue_ws` (a confirmed stream
        frame), `venue_rest` (a venue REST read), `local_derived` (reconstructed
        locally from another timeframe) or `imported` (a file the operator
        supplied). `ingestion_mode` is how it arrived: `live`, `backfill`,
        `repair`, `import` or `derived`. The two are kept apart on purpose.

        Returns an `UpsertReport` counting what actually happened per row, so a
        repeated or overlapping backfill cannot report work it did not do.
        """
        if not rows:
            return UpsertReport()
        stamp = int(received_ts if received_ts is not None else time.time() * 1000)
        payload = [
            (
                venue,
                symbol,
                interval,
                int(r["ts"]),
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                float(r.get("volume") or 0),
                r.get("trades"),
                str(r.get("source") or source),
                str(r.get("ingestion_mode") or ingestion_mode),
                int(r["exchange_ts"]) if r.get("exchange_ts") is not None else None,
                int(r.get("received_ts") or stamp),
                str(r.get("collector") or collector),
            )
            for r in rows
        ]
        report = self._classify_candle_writes(venue, symbol, interval, payload)
        with self._lock:
            # A row's values and provenance are one unit. Venue WS and REST may
            # reconcile one another (the closed REST bar can correct a streamed
            # frame), but derived/imported rows cannot overwrite venue data.
            existing_rank = (
                "CASE COALESCE(candles.source, 'unknown') "
                "WHEN 'venue_ws' THEN 4 WHEN 'venue_rest' THEN 3 "
                "WHEN 'local_derived' THEN 2 WHEN 'imported' THEN 1 ELSE 0 END"
            )
            incoming_rank = (
                "CASE COALESCE(excluded.source, 'unknown') "
                "WHEN 'venue_ws' THEN 4 WHEN 'venue_rest' THEN 3 "
                "WHEN 'local_derived' THEN 2 WHEN 'imported' THEN 1 ELSE 0 END"
            )
            both_venue = (
                "(excluded.source IN ('venue_ws', 'venue_rest') AND "
                "candles.source IN ('venue_ws', 'venue_rest'))"
            )
            incoming_wins = f"({incoming_rank} >= {existing_rank} OR {both_venue})"
            changed = (
                "(candles.open IS NOT excluded.open OR candles.high IS NOT excluded.high OR "
                "candles.low IS NOT excluded.low OR candles.close IS NOT excluded.close OR "
                "candles.volume IS NOT excluded.volume OR candles.trades IS NOT excluded.trades OR "
                "candles.source IS NOT excluded.source)"
            )
            self._conn.executemany(
                "INSERT INTO candles "
                "(venue, symbol, interval, open_ts, open, high, low, close, volume, trades, "
                " source, ingestion_mode, exchange_ts, received_ts, collector) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(venue, symbol, interval, open_ts) DO UPDATE SET "
                f"  open=CASE WHEN {incoming_wins} THEN excluded.open ELSE candles.open END, "
                f"  high=CASE WHEN {incoming_wins} THEN excluded.high ELSE candles.high END, "
                f"  low=CASE WHEN {incoming_wins} THEN excluded.low ELSE candles.low END, "
                f"  close=CASE WHEN {incoming_wins} THEN excluded.close ELSE candles.close END, "
                f"  volume=CASE WHEN {incoming_wins} THEN excluded.volume ELSE candles.volume END, "
                f"  trades=CASE WHEN {incoming_wins} THEN excluded.trades ELSE candles.trades END, "
                f"  source=CASE WHEN {incoming_wins} THEN excluded.source ELSE candles.source END, "
                f"  ingestion_mode=CASE WHEN {incoming_wins} AND {changed} "
                "                      THEN excluded.ingestion_mode ELSE candles.ingestion_mode END, "
                # A winning refresh that changed content or upgraded its source is
                # a new observation. An identical repeat keeps the first-seen stamp.
                f"  exchange_ts=CASE WHEN {incoming_wins} AND {changed} "
                "                   THEN COALESCE(excluded.exchange_ts, candles.exchange_ts) ELSE candles.exchange_ts END, "
                f"  received_ts=CASE WHEN {incoming_wins} AND {changed} "
                "                  THEN excluded.received_ts ELSE candles.received_ts END, "
                f"  collector=CASE WHEN {incoming_wins} AND {changed} "
                "                THEN excluded.collector ELSE candles.collector END",
                payload,
            )
            self._conn.commit()
        return report

    def _classify_candle_writes(self, venue: str, symbol: str, interval: str, payload: list[tuple]) -> "UpsertReport":
        """Decide, before writing, what each incoming row will actually change.

        SQLite's upsert does not say which rows it inserted and which it merely
        rewrote, and "rows submitted" is not an answer an operator can use: a
        re-run of the same pages would claim to have fetched a thousand bars it
        already had. Reading the affected timestamps first makes the count exact.
        """
        stamps = [row[3] for row in payload]
        existing: dict[int, tuple] = {}
        # Chunked IN lists: SQLite's parameter limit is 999 by default.
        for start in range(0, len(stamps), 400):
            chunk = stamps[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.query(
                "SELECT open_ts, open, high, low, close, volume, trades, source "
                f"FROM candles WHERE venue=? AND symbol=? AND interval=? AND open_ts IN ({placeholders})",
                (venue, symbol, interval, *chunk),
            )
            for row in rows:
                existing[int(row["open_ts"])] = (
                    row["open"], row["high"], row["low"], row["close"],
                    row["volume"], row["trades"], row["source"],
                )
        report = UpsertReport()
        for row in payload:
            ts = row[3]
            incoming_rank = self.SOURCE_RANKS.get(str(row[10] or ""), 0)
            current = existing.get(ts)
            if current is None:
                report.inserted += 1
                continue
            current_rank = self.SOURCE_RANKS.get(str(current[6] or ""), 0)
            both_venue = str(row[10]) in ("venue_ws", "venue_rest") and str(current[6]) in ("venue_ws", "venue_rest")
            if incoming_rank < current_rank and not both_venue:
                # A derived or imported row cannot overwrite venue data.
                report.lower_rank += 1
                continue
            same = (
                current[0] == row[4] and current[1] == row[5] and current[2] == row[6]
                and current[3] == row[7] and current[4] == row[8] and current[5] == row[9]
                and str(current[6] or "") == str(row[10] or "")
            )
            if same:
                report.unchanged += 1
            else:
                report.updated += 1
        return report

    def last_open_ts(self, venue: str, symbol: str, interval: str) -> int | None:
        rows = self.query(
            "SELECT MAX(open_ts) AS m FROM candles WHERE venue=? AND symbol=? AND interval=?",
            (venue, symbol, interval),
        )
        return rows[0]["m"] if rows and rows[0]["m"] is not None else None

    def first_open_ts(self, venue: str, symbol: str, interval: str) -> int | None:
        """The oldest stored bar: how far back this symbol's history reaches."""
        rows = self.query(
            "SELECT MIN(open_ts) AS m FROM candles WHERE venue=? AND symbol=? AND interval=?",
            (venue, symbol, interval),
        )
        return rows[0]["m"] if rows and rows[0]["m"] is not None else None

    def count_candles(self, venue: str, symbol: str, interval: str) -> int:
        rows = self.query(
            "SELECT COUNT(*) AS n FROM candles WHERE venue=? AND symbol=? AND interval=?",
            (venue, symbol, interval),
        )
        return int(rows[0]["n"])

    def prune_scheduler_runs(self, older_than_ts: int) -> int:
        """Drop scheduler run history past its retention window.

        The rotating collector writes a row every few seconds, so this table is
        the one that grows without bound in a long-running installation.
        """
        with self._lock:
            cursor = self._conn.execute("DELETE FROM scheduler_runs WHERE started_ts < ?", (int(older_than_ts),))
            self._conn.commit()
            return cursor.rowcount

    def load_candles(
        self,
        venue: str,
        symbol: str,
        interval: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        sql = ("SELECT open_ts AS ts, open, high, low, close, volume, trades, "
               "source, ingestion_mode, exchange_ts, received_ts, collector FROM candles "
               "WHERE venue=? AND symbol=? AND interval=?")
        params: list = [venue, symbol, interval]
        if start_ts is not None:
            sql += " AND open_ts >= ?"
            params.append(start_ts)
        if end_ts is not None:
            sql += " AND open_ts <= ?"
            params.append(end_ts)
        sql += " ORDER BY open_ts"
        if limit:
            sql = f"SELECT * FROM ({sql} DESC LIMIT ?) ORDER BY ts"
            params.append(limit)
        return self.query(sql, tuple(params))

    def upsert_funding(self, venue: str, symbol: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO funding (venue, symbol, ts, rate) VALUES (?, ?, ?, ?)",
                [(venue, symbol, int(r["ts"]), float(r["rate"])) for r in rows],
            )
            self._conn.commit()
        return len(rows)

    def upsert_oi(self, venue: str, symbol: str, rows: list[dict], interval: str = "1h") -> int:
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO open_interest (venue, symbol, interval, ts, oi) VALUES (?, ?, ?, ?, ?)",
                [(venue, symbol, interval, int(r["ts"]), float(r["oi"])) for r in rows],
            )
            self._conn.commit()
        return len(rows)

    # -- derivatives snapshots -------------------------------------------
    _SNAPSHOT_COLUMNS = (
        "last_price", "mark_price", "index_price", "funding_rate",
        "funding_interval_hour", "next_funding_time", "open_interest",
        "open_interest_value", "turnover_24h", "volume_24h", "price_24h_pct",
        "high_24h", "low_24h",
    )

    # -- TradingAgents cost ledger ---------------------------------------
    def record_agent_cost(self, entry: dict) -> None:
        """One row per research run: what it cost, what it read, what it produced."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tradingagents_costs "
                "(run_id, venue_symbol, trade_date, profile, provider, deep_model, quick_model, "
                " config_fingerprint, prompt_version, data_version, data_as_of, analysts, missing_analysts, "
                " reuse_key, reused_from, ok, rating, staleness, usage, usage_known, cost_usd, cost_detail, "
                " duration_s, retries, analyst_retries, failure, created_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry["run_id"],
                    entry["venue_symbol"],
                    entry["trade_date"],
                    entry["profile"],
                    entry["provider"],
                    entry.get("deep_model"),
                    entry.get("quick_model"),
                    entry["config_fingerprint"],
                    entry.get("prompt_version"),
                    entry.get("data_version"),
                    entry.get("data_as_of"),
                    json.dumps(entry.get("analysts") or [], ensure_ascii=False),
                    json.dumps(entry.get("missing_analysts") or [], ensure_ascii=False),
                    entry["reuse_key"],
                    entry.get("reused_from"),
                    1 if entry.get("ok") else 0,
                    entry.get("rating"),
                    json.dumps(entry.get("staleness") or {}, ensure_ascii=False),
                    json.dumps(entry.get("usage") or {}, ensure_ascii=False),
                    1 if entry.get("usage_known") else 0,
                    entry.get("cost_usd"),
                    json.dumps(entry.get("cost_detail") or {}, ensure_ascii=False),
                    entry.get("duration_s"),
                    int(entry.get("retries") or 0),
                    int(entry.get("analyst_retries") or 0),
                    json.dumps(entry.get("failure") or {}, ensure_ascii=False) if entry.get("failure") else None,
                    int(entry.get("created_ts") or time.time() * 1000),
                ),
            )
            self._conn.commit()

    def agent_cost_totals(self, *, start_ts: int, end_ts: int | None = None) -> dict:
        """Spend over a window, and whether any run in it was unpriced."""
        rows = self.query(
            "SELECT COUNT(*) AS runs, COALESCE(SUM(cost_usd), 0) AS usd, "
            "SUM(CASE WHEN cost_usd IS NULL AND usage_known = 1 THEN 1 ELSE 0 END) AS unpriced, "
            "SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed "
            "FROM tradingagents_costs WHERE created_ts >= ? AND created_ts < ?",
            (int(start_ts), int(end_ts if end_ts is not None else time.time() * 1000 + 1)),
        )
        row = rows[0] if rows else {}
        return {
            "runs": int(row.get("runs") or 0),
            "usd": float(row.get("usd") or 0.0),
            "unpricedRuns": int(row.get("unpriced") or 0),
            "failedRuns": int(row.get("failed") or 0),
        }

    def find_reusable_run(self, reuse_key: str, *, since_ts: int | None = None) -> dict | None:
        """The newest successful run with this exact reuse key, if any."""
        sql = "SELECT * FROM tradingagents_costs WHERE reuse_key=? AND ok=1"
        params: list = [reuse_key]
        if since_ts is not None:
            sql += " AND created_ts >= ?"
            params.append(int(since_ts))
        # created_ts alone is not a total order: two runs recorded in the same
        # millisecond would make "the newest answer" arbitrary, and the reuse rule
        # depends on that word meaning something.
        sql += " ORDER BY created_ts DESC, rowid DESC LIMIT 1"
        rows = self.query(sql, tuple(params))
        return rows[0] if rows else None

    def list_agent_costs(self, limit: int = 50) -> list[dict]:
        return self.query(
            "SELECT * FROM tradingagents_costs ORDER BY created_ts DESC LIMIT ?", (int(limit),)
        )

    # -- history backfill state and snapshots ----------------------------
    def upsert_backfill_state(self, entry: dict) -> None:
        """Record how far one data series' backfill walked, and how it ended.

        The key is (venue, symbol, interval, data_kind): a contract's candles,
        mark prices, funding, open interest and risk ladder are five independent
        walks over the same symbol, and one must not overwrite another's progress.
        """
        now = int(entry.get("updated_ts") or time.time() * 1000)
        with self._lock:
            self._conn.execute(
                "INSERT INTO backfill_state "
                "(venue, symbol, interval, data_kind, oldest_ts, newest_ts, complete, pages, "
                " rows_available, attempts, status, reason, last_error, last_error_kind, "
                " last_run_ts, updated_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(venue, symbol, interval, data_kind) DO UPDATE SET "
                " oldest_ts=excluded.oldest_ts, newest_ts=excluded.newest_ts, complete=excluded.complete, "
                " pages=excluded.pages, rows_available=excluded.rows_available, attempts=excluded.attempts, "
                " status=excluded.status, reason=excluded.reason, last_error=excluded.last_error, "
                " last_error_kind=excluded.last_error_kind, last_run_ts=excluded.last_run_ts, "
                " updated_ts=excluded.updated_ts",
                (
                    entry["venue"], entry["symbol"], entry.get("interval") or "",
                    entry.get("data_kind") or "trade_candle",
                    entry.get("oldest_ts"), entry.get("newest_ts"),
                    1 if entry.get("complete") else 0,
                    int(entry.get("pages") or 0), int(entry.get("rows_available") or 0),
                    int(entry.get("attempts") or 0),
                    entry.get("status") or "ok", entry.get("reason"),
                    entry.get("last_error"), entry.get("last_error_kind"),
                    entry.get("last_run_ts"), now,
                ),
            )
            self._conn.commit()

    def load_backfill_state(
        self, venue: str, symbol: str, interval: str = "", data_kind: str = "trade_candle"
    ) -> dict | None:
        rows = self.query(
            "SELECT * FROM backfill_state WHERE venue=? AND symbol=? AND interval=? AND data_kind=?",
            (venue, symbol, interval, data_kind),
        )
        return rows[0] if rows else None

    def list_backfill_state(
        self, *, venue: str | None = None, symbol: str | None = None, data_kind: str | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM backfill_state WHERE 1=1"
        params: list = []
        if venue:
            sql += " AND venue=?"
            params.append(venue)
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        if data_kind:
            sql += " AND data_kind=?"
            params.append(data_kind)
        sql += " ORDER BY symbol, data_kind, interval"
        return self.query(sql, tuple(params))

    # -- backfill task matrix ---------------------------------------------
    TASK_STATUSES = ("pending", "running", "paused", "done", "failed", "unsupported", "cancelled")

    def upsert_backfill_task(self, entry: dict, *, reset: bool = False) -> int:
        """Create or refresh one unit of work, keyed by the series it covers."""
        now = int(entry.get("updated_ts") or time.time() * 1000)
        with self._lock:
            self._conn.execute(
                "INSERT INTO backfill_tasks "
                "(venue, symbol, interval, data_kind, status, pages, pages_estimate, rows_available, "
                " attempts, failure_attempts, max_attempts, last_error, last_error_kind, reason, cancel_requested, "
                " created_ts, started_ts, finished_ts, updated_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(venue, symbol, interval, data_kind) DO UPDATE SET "
                " status=CASE WHEN ? THEN 'pending' ELSE backfill_tasks.status END, "
                " cancel_requested=CASE WHEN ? THEN 0 ELSE backfill_tasks.cancel_requested END, "
                " updated_ts=excluded.updated_ts",
                (
                    entry["venue"], entry["symbol"], entry.get("interval") or "",
                    entry["data_kind"], entry.get("status") or "pending",
                    int(entry.get("pages") or 0), int(entry.get("pages_estimate") or 0),
                    int(entry.get("rows_available") or 0), int(entry.get("attempts") or 0),
                    int(entry.get("failure_attempts") or 0), int(entry.get("max_attempts") or 3), entry.get("last_error"),
                    entry.get("last_error_kind"), entry.get("reason"),
                    1 if entry.get("cancel_requested") else 0,
                    int(entry.get("created_ts") or now), entry.get("started_ts"),
                    entry.get("finished_ts"), now,
                    1 if reset else 0, 1 if reset else 0,
                ),
            )
            self._conn.commit()
            rows = self._conn.execute(
                "SELECT id FROM backfill_tasks WHERE venue=? AND symbol=? AND interval=? AND data_kind=?",
                (entry["venue"], entry["symbol"], entry.get("interval") or "", entry["data_kind"]),
            ).fetchall()
        return int(rows[0][0]) if rows else 0

    def list_backfill_tasks(self, *, status: str | None = None, symbol: str | None = None,
                            limit: int = 500) -> list[dict]:
        sql = "SELECT * FROM backfill_tasks WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        sql += " ORDER BY id LIMIT ?"
        params.append(int(limit))
        return self.query(sql, tuple(params))

    def get_backfill_task(self, task_id: int) -> dict | None:
        rows = self.query("SELECT * FROM backfill_tasks WHERE id=?", (int(task_id),))
        return rows[0] if rows else None

    def claim_next_backfill_task(self) -> dict | None:
        """Atomically take the next pending task, so two workers cannot share one."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM backfill_tasks WHERE status='pending' AND cancel_requested=0 "
                "ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            task_id = int(row["id"])
            self._conn.execute(
                "UPDATE backfill_tasks SET status='running', started_ts=?, attempts=attempts+1, "
                " updated_ts=? WHERE id=?",
                (int(time.time() * 1000), int(time.time() * 1000), task_id),
            )
            self._conn.commit()
            claimed = self._conn.execute(
                "SELECT * FROM backfill_tasks WHERE id=?", (task_id,)
            ).fetchone()
        return dict(claimed) if claimed else None

    def update_backfill_task(self, task_id: int, **fields) -> None:
        allowed = {
            "status", "pages", "pages_estimate", "rows_available", "complete", "attempts", "failure_attempts",
            "max_attempts",
            "last_error", "last_error_kind", "reason", "cancel_requested", "started_ts", "finished_ts",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        now = int(time.time() * 1000)
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self._lock:
            self._conn.execute(
                f"UPDATE backfill_tasks SET {assignments}, updated_ts=? WHERE id=?",
                (*updates.values(), now, int(task_id)),
            )
            self._conn.commit()

    def task_summary(self) -> dict:
        rows = self.query(
            "SELECT status, COUNT(*) AS total FROM backfill_tasks GROUP BY status"
        )
        by_status = {row["status"]: int(row["total"]) for row in rows}
        total = sum(by_status.values())
        finished = sum(by_status.get(name, 0) for name in ("done", "unsupported"))
        return {
            "total": total,
            "byStatus": by_status,
            "finished": finished,
            "pending": by_status.get("pending", 0) + by_status.get("running", 0) + by_status.get("paused", 0),
        }

    # -- instrument metadata ---------------------------------------------
    def upsert_instrument_meta(self, entry: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO instrument_meta "
                "(venue, symbol, display_symbol, contract_type, status, launch_ts, tick_size, qty_step, "
                " min_notional, funding_interval_hours, raw_json, collected_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry["venue"], entry["symbol"], entry.get("display_symbol"),
                    entry.get("contract_type"), entry.get("status"), entry.get("launch_ts"),
                    entry.get("tick_size"), entry.get("qty_step"), entry.get("min_notional"),
                    entry.get("funding_interval_hours"),
                    json.dumps(entry.get("raw") or {}, ensure_ascii=False),
                    int(entry.get("collected_ts") or time.time() * 1000),
                ),
            )
            self._conn.commit()

    def load_instrument_meta(self, venue: str, symbol: str) -> dict | None:
        rows = self.query("SELECT * FROM instrument_meta WHERE venue=? AND symbol=?", (venue, symbol))
        return rows[0] if rows else None

    def list_instrument_meta(self, *, venue: str | None = None) -> list[dict]:
        sql = "SELECT * FROM instrument_meta WHERE 1=1"
        params: list = []
        if venue:
            sql += " AND venue=?"
            params.append(venue)
        sql += " ORDER BY symbol"
        return self.query(sql, tuple(params))

    def record_history_snapshot(self, entry: dict) -> None:
        """Store one pinned view of a symbol's stored history.

        Each data kind gets its own snapshot: candles, marks, funding and open
        interest are versioned independently, so a result can say exactly which
        series it read without pretending they share one version.
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO history_snapshots "
                "(venue, symbol, interval, data_kind, version, from_ts, to_ts, bars, bars_available, "
                " complete, missing_in_session, sources, created_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry["venue"], entry["symbol"], entry.get("interval") or "",
                    entry.get("data_kind") or "trade_candle", entry["version"],
                    int(entry["from_ts"]), int(entry["to_ts"]), int(entry.get("bars") or 0),
                    int(entry.get("barsAvailable") or entry.get("bars_available") or 0),
                    1 if entry.get("complete") else 0,
                    int(entry.get("missing_in_session") or 0),
                    json.dumps(entry.get("sources") or {}, ensure_ascii=False),
                    int(entry.get("created_ts") or time.time() * 1000),
                ),
            )
            self._conn.commit()

    def list_history_snapshots(
        self, *, symbol: str | None = None, interval: str | None = None,
        data_kind: str | None = None, limit: int = 50,
    ) -> list[dict]:
        sql = "SELECT * FROM history_snapshots WHERE 1=1"
        params: list = []
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        if interval:
            sql += " AND interval=?"
            params.append(interval)
        if data_kind:
            sql += " AND data_kind=?"
            params.append(data_kind)
        sql += " ORDER BY created_ts DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return self.query(sql, tuple(params))

    def find_history_snapshot(self, version: str) -> dict | None:
        rows = self.query("SELECT * FROM history_snapshots WHERE version=? LIMIT 1", (version,))
        return rows[0] if rows else None

    # -- external evidence store -----------------------------------------
    def upsert_external_evidence(self, entry: dict) -> None:
        """Store one external reading under its cache key.

        A row for the same key is replaced, so the store holds the newest reading
        of a given (provider, endpoint, symbol, as-of, parameters) combination
        rather than a growing pile of near-duplicates.
        """
        now = int(entry.get("updated_ts") or time.time() * 1000)
        with self._lock:
            self._conn.execute(
                "INSERT INTO external_evidence "
                "(cache_key, request_key, provider, endpoint, symbol, topic, as_of, published_at, observed_at, "
                " expires_at, source_url, payload_json, content_hash, point_in_time, status, warning, "
                " created_ts, updated_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                " request_key=excluded.request_key, provider=excluded.provider, endpoint=excluded.endpoint, "
                " symbol=excluded.symbol, topic=excluded.topic, as_of=excluded.as_of, "
                " published_at=excluded.published_at, observed_at=excluded.observed_at, "
                " expires_at=excluded.expires_at, source_url=excluded.source_url, "
                " payload_json=excluded.payload_json, content_hash=excluded.content_hash, "
                " point_in_time=excluded.point_in_time, status=excluded.status, "
                " warning=excluded.warning, updated_ts=excluded.updated_ts",
                (
                    entry["cache_key"],
                    entry.get("request_key") or entry["cache_key"],
                    entry["provider"],
                    entry["endpoint"],
                    entry["symbol"],
                    entry.get("topic") or "",
                    entry.get("as_of"),
                    entry.get("published_at"),
                    entry["observed_at"],
                    entry.get("expires_at"),
                    entry.get("source_url"),
                    entry.get("payload_json"),
                    entry.get("content_hash"),
                    1 if entry.get("point_in_time") else 0,
                    entry.get("status") or "ok",
                    entry.get("warning"),
                    int(entry.get("created_ts") or now),
                    now,
                ),
            )
            self._conn.commit()

    def find_external_evidence(self, cache_key: str) -> dict | None:
        rows = self.query("SELECT * FROM external_evidence WHERE cache_key=?", (cache_key,))
        return rows[0] if rows else None

    def list_external_evidence_by_request(self, request_key: str) -> list[dict]:
        """Every reading stored for one request, newest rows first.

        A request yields one row per reading, so the cache lookup cannot use the
        per-reading unique key: it asks for the request and gets the batch.
        """
        return self.query(
            "SELECT * FROM external_evidence WHERE request_key=? ORDER BY id ASC", (request_key,)
        )

    def list_external_evidence(
        self, *, symbol: str | None = None, topic: str | None = None, limit: int = 50
    ) -> list[dict]:
        sql = "SELECT * FROM external_evidence WHERE 1=1"
        params: list = []
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        if topic:
            sql += " AND topic=?"
            params.append(topic)
        sql += " ORDER BY updated_ts DESC LIMIT ?"
        params.append(int(limit))
        return self.query(sql, tuple(params))

    def external_evidence_stats(self) -> list[dict]:
        """Counts a status page can show: what is cached, and how it went."""
        return self.query(
            "SELECT provider, status, COUNT(*) AS total, MAX(updated_ts) AS newest "
            "FROM external_evidence GROUP BY provider, status"
        )

    # -- series counts and bounds ----------------------------------------
    def count_funding(self, venue: str, symbol: str) -> int:
        rows = self.query(
            "SELECT COUNT(*) AS n FROM funding WHERE venue=? AND symbol=?", (venue, symbol)
        )
        return int(rows[0]["n"]) if rows else 0

    def count_oi(self, venue: str, symbol: str, interval: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM open_interest WHERE venue=? AND symbol=?"
        params: tuple = (venue, symbol)
        if interval is not None:
            sql += " AND interval=?"
            params = (venue, symbol, interval)
        rows = self.query(sql, params)
        return int(rows[0]["n"]) if rows else 0

    def count_mark_candles(self, venue: str, symbol: str, interval: str) -> int:
        rows = self.query(
            "SELECT COUNT(*) AS n FROM mark_candles WHERE venue=? AND symbol=? AND interval=?",
            (venue, symbol, interval),
        )
        return int(rows[0]["n"]) if rows else 0

    def funding_bounds(self, venue: str, symbol: str) -> tuple[int | None, int | None]:
        rows = self.query(
            "SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM funding WHERE venue=? AND symbol=?",
            (venue, symbol),
        )
        return _bounds(rows)

    def oi_bounds(self, venue: str, symbol: str, interval: str | None = None) -> tuple[int | None, int | None]:
        sql = "SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM open_interest WHERE venue=? AND symbol=?"
        params: tuple = (venue, symbol)
        if interval is not None:
            sql += " AND interval=?"
            params = (venue, symbol, interval)
        rows = self.query(sql, params)
        return _bounds(rows)

    def mark_bounds(self, venue: str, symbol: str, interval: str) -> tuple[int | None, int | None]:
        rows = self.query(
            "SELECT MIN(open_ts) AS lo, MAX(open_ts) AS hi FROM mark_candles "
            "WHERE venue=? AND symbol=? AND interval=?",
            (venue, symbol, interval),
        )
        return _bounds(rows)

    def load_oi(
        self, venue: str, symbol: str, *, start_ts: int | None = None, end_ts: int | None = None,
        limit: int | None = None, interval: str | None = None,
    ) -> list[dict]:
        sql = "SELECT ts, oi, interval FROM open_interest WHERE venue=? AND symbol=?"
        params: list = [venue, symbol]
        if interval is not None:
            sql += " AND interval=?"
            params.append(interval)
        if start_ts is not None:
            sql += " AND ts >= ?"
            params.append(int(start_ts))
        if end_ts is not None:
            sql += " AND ts <= ?"
            params.append(int(end_ts))
        sql += " ORDER BY ts"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self.query(sql, tuple(params))

    def prune_external_evidence(self, *, max_age_days: int, now_ms: int | None = None) -> int:
        """Drop readings older than the retention window."""
        reference = now_ms if now_ms is not None else time.time() * 1000
        cutoff = int(reference - max_age_days * 86_400 * 1000)
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM external_evidence WHERE updated_ts < ?", (cutoff,)
            )
            self._conn.commit()
        return int(cursor.rowcount or 0)

    # -- external analytics runs -----------------------------------------
    def record_external_analytics(self, entry: dict) -> None:
        now = int(entry.get("updated_ts") or time.time() * 1000)
        with self._lock:
            self._conn.execute(
                "INSERT INTO external_analytics_runs "
                "(provider, kind, as_of, input_hash, market_snapshot_version, positions_hash, "
                " parameters_json, result_json, request_id, status, error, duration_ms, "
                " created_ts, updated_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry["provider"],
                    entry["kind"],
                    entry.get("as_of"),
                    entry["input_hash"],
                    entry.get("market_snapshot_version"),
                    entry.get("positions_hash"),
                    entry.get("parameters_json"),
                    entry.get("result_json"),
                    entry.get("request_id"),
                    entry.get("status") or "ok",
                    entry.get("error"),
                    entry.get("duration_ms"),
                    int(entry.get("created_ts") or now),
                    now,
                ),
            )
            self._conn.commit()

    def find_external_analytics(self, input_hash: str, *, kind: str | None = None) -> dict | None:
        """The newest *successful* run for this exact input.

        Failures are deliberately excluded: a failed call must not be replayed as
        though it were the answer, and must not overwrite the last good result.
        """
        sql = "SELECT * FROM external_analytics_runs WHERE input_hash=? AND status='ok'"
        params: list = [input_hash]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY updated_ts DESC, id DESC LIMIT 1"
        rows = self.query(sql, tuple(params))
        return rows[0] if rows else None

    def list_external_analytics(self, *, kind: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM external_analytics_runs WHERE 1=1"
        params: list = []
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY updated_ts DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return self.query(sql, tuple(params))

    def external_analytics_stats(self) -> list[dict]:
        return self.query(
            "SELECT provider, kind, status, COUNT(*) AS total, "
            " AVG(duration_ms) AS average_ms, MAX(updated_ts) AS newest "
            "FROM external_analytics_runs GROUP BY provider, kind, status"
        )

    def prune_external_analytics(self, *, max_age_days: int, now_ms: int | None = None) -> int:
        reference = now_ms if now_ms is not None else time.time() * 1000
        cutoff = int(reference - max_age_days * 86_400 * 1000)
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM external_analytics_runs WHERE updated_ts < ?", (cutoff,)
            )
            self._conn.commit()
        return int(cursor.rowcount or 0)

    # -- risk ladder and mark price --------------------------------------
    def upsert_risk_tiers(
        self,
        venue: str,
        symbol: str,
        tiers: list[dict],
        *,
        source: str = "bybit:risk-limit",
        synced_at: int | None = None,
    ) -> int:
        """Replace one contract's risk ladder with the venue's current one.

        The ladder is short and per-contract, so it is replaced wholesale rather
        than merged: a rung the venue removed must disappear locally too.
        """
        stamp = int(synced_at if synced_at is not None else time.time() * 1000)
        with self._lock:
            self._conn.execute("DELETE FROM risk_tiers WHERE venue=? AND symbol=?", (venue, symbol))
            self._conn.executemany(
                "INSERT OR REPLACE INTO risk_tiers "
                "(venue, symbol, tier_id, risk_limit_value, maintenance_margin_rate, mm_deduction, "
                " max_leverage, initial_margin_rate, lowest_risk, source, synced_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        venue,
                        symbol,
                        int(row["tier_id"]),
                        float(row["risk_limit_value"]),
                        float(row["maintenance_margin_rate"]),
                        float(row.get("mm_deduction") or 0.0),
                        float(row["max_leverage"]),
                        None if row.get("initial_margin_rate") is None else float(row["initial_margin_rate"]),
                        1 if row.get("lowest_risk") else 0,
                        source,
                        stamp,
                    )
                    for row in tiers
                ],
            )
            self._conn.commit()
        return len(tiers)

    def load_risk_tiers(self, venue: str, symbol: str) -> list[dict]:
        """One contract's ladder, smallest rung first."""
        return self.query(
            "SELECT tier_id, risk_limit_value, maintenance_margin_rate, mm_deduction, max_leverage, "
            "initial_margin_rate, lowest_risk, source, synced_at "
            "FROM risk_tiers WHERE venue=? AND symbol=? ORDER BY risk_limit_value ASC",
            (venue, symbol),
        )

    def risk_tier_status(self, venue: str) -> dict:
        """Which contracts have a ladder locally, and how old it is."""
        rows = self.query(
            "SELECT symbol, COUNT(*) AS tiers, MIN(synced_at) AS oldest, MAX(synced_at) AS newest "
            "FROM risk_tiers WHERE venue=? GROUP BY symbol ORDER BY symbol",
            (venue,),
        )
        return {
            "symbols": len(rows),
            "tiers": sum(int(row["tiers"]) for row in rows),
            "oldestSyncedAt": min((row["oldest"] for row in rows), default=None),
            "newestSyncedAt": max((row["newest"] for row in rows), default=None),
            "bySymbol": {row["symbol"]: int(row["tiers"]) for row in rows},
        }

    def upsert_mark_candles(
        self, venue: str, symbol: str, interval: str, rows: list[dict], *, source: str = "rest_mark"
    ) -> "UpsertReport":
        """Store mark-price bars; the venue's own mark, not the last trade.

        Reports per-row outcomes for the same reason the candle store does: a
        repeated walk must not claim to have fetched marks it already had.
        """
        if not rows:
            return UpsertReport()
        now = int(time.time() * 1000)
        payload = [
            (
                venue,
                symbol,
                interval,
                int(row["ts"]),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                int(row.get("received_ts") or now),
                source,
            )
            for row in rows
        ]
        stamps = [row[3] for row in payload]
        existing: dict[int, tuple] = {}
        for start in range(0, len(stamps), 400):
            chunk = stamps[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            found = self.query(
                "SELECT open_ts, open, high, low, close FROM mark_candles "
                f"WHERE venue=? AND symbol=? AND interval=? AND open_ts IN ({placeholders})",
                (venue, symbol, interval, *chunk),
            )
            for row in found:
                existing[int(row["open_ts"])] = (
                    row["open"], row["high"], row["low"], row["close"],
                )
        report = UpsertReport()
        for row in payload:
            current = existing.get(row[3])
            if current is None:
                report.inserted += 1
            elif current == (row[4], row[5], row[6], row[7]):
                report.unchanged += 1
            else:
                report.updated += 1
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO mark_candles "
                "(venue, symbol, interval, open_ts, open, high, low, close, received_ts, source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                payload,
            )
            self._conn.commit()
        return report

    def load_mark_candles(
        self,
        venue: str,
        symbol: str,
        interval: str,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        sql = (
            "SELECT open_ts AS ts, open, high, low, close FROM mark_candles "
            "WHERE venue=? AND symbol=? AND interval=?"
        )
        params: list = [venue, symbol, interval]
        if start_ts is not None:
            sql += " AND open_ts >= ?"
            params.append(int(start_ts))
        if end_ts is not None:
            sql += " AND open_ts <= ?"
            params.append(int(end_ts))
        if limit is not None:
            sql += " ORDER BY open_ts DESC LIMIT ?"
            params.append(int(limit))
            rows = self.query(sql, tuple(params))
            return list(reversed(rows))
        sql += " ORDER BY open_ts ASC"
        return self.query(sql, tuple(params))

    def count_mark_candles(self, venue: str, symbol: str, interval: str) -> int:
        rows = self.query(
            "SELECT COUNT(*) AS n FROM mark_candles WHERE venue=? AND symbol=? AND interval=?",
            (venue, symbol, interval),
        )
        return int(rows[0]["n"])

    def upsert_market_snapshot(self, venue: str, symbol: str, snapshot: dict) -> None:
        """Store the latest derivatives snapshot for one contract.

        An older exchange timestamp never overwrites a newer one: out-of-order
        frames must not move the displayed quote backwards.
        """
        received_ts = int(snapshot.get("received_ts") or time.time() * 1000)
        exchange_ts = snapshot.get("exchange_ts")
        with self._lock:
            row = self._conn.execute(
                "SELECT exchange_ts FROM market_snapshots WHERE venue=? AND symbol=?",
                (venue, symbol),
            ).fetchone()
            if row is not None and row["exchange_ts"] is not None:
                # An unstamped REST quote has no safe ordering relationship to a
                # persisted exchange-stamped WebSocket quote.
                if exchange_ts is None or int(exchange_ts) < int(row["exchange_ts"]):
                    return
            columns = ["venue", "symbol", *self._SNAPSHOT_COLUMNS, "exchange_ts", "received_ts", "source"]
            values = [
                venue,
                symbol,
                *[snapshot.get(column) for column in self._SNAPSHOT_COLUMNS],
                exchange_ts,
                received_ts,
                str(snapshot.get("source") or "websocket"),
            ]
            placeholders = ", ".join("?" for _ in columns)
            self._conn.execute(
                f"INSERT OR REPLACE INTO market_snapshots ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(values),
            )
            self._conn.commit()

    def load_market_snapshot(self, venue: str, symbol: str) -> dict | None:
        rows = self.query(
            "SELECT * FROM market_snapshots WHERE venue=? AND symbol=?", (venue, symbol)
        )
        return rows[0] if rows else None

    def load_market_snapshots(self, venue: str | None = None) -> list[dict]:
        if venue:
            return self.query("SELECT * FROM market_snapshots WHERE venue=? ORDER BY symbol", (venue,))
        return self.query("SELECT * FROM market_snapshots ORDER BY symbol")

    def load_funding(
        self,
        venue: str,
        symbol: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        sql = "SELECT ts, rate FROM funding WHERE venue=? AND symbol=?"
        params: list = [venue, symbol]
        if start_ts is not None:
            sql += " AND ts >= ?"
            params.append(int(start_ts))
        if end_ts is not None:
            sql += " AND ts <= ?"
            params.append(int(end_ts))
        sql += " ORDER BY ts"
        if limit:
            # Keep the newest `limit` rows but return them oldest-first.
            sql = f"SELECT * FROM ({sql} DESC LIMIT ?) ORDER BY ts"
            params.append(int(limit))
        return self.query(sql, tuple(params))

    def load_open_interest(
        self, venue: str, symbol: str, start_ts: int | None = None, limit: int | None = None
    ) -> list[dict]:
        sql = "SELECT ts, oi FROM open_interest WHERE venue=? AND symbol=?"
        params: list = [venue, symbol]
        if start_ts is not None:
            sql += " AND ts >= ?"
            params.append(int(start_ts))
        sql += " ORDER BY ts"
        if limit:
            sql = f"SELECT * FROM ({sql} DESC LIMIT ?) ORDER BY ts"
            params.append(limit)
        return self.query(sql, tuple(params))

    # -- position notes --------------------------------------------------
    def position_notes(self, position_id: int) -> str:
        rows = self.query("SELECT notes FROM position_notes WHERE position_id = ?", (position_id,))
        return rows[0]["notes"] if rows else ""

    def set_position_note(self, position_id: int, notes: str) -> None:
        """Rationale is editable; the journal derived from it is not."""
        self.execute(
            "INSERT INTO position_notes (position_id, notes, updated_ts) VALUES (?, ?, ?) "
            "ON CONFLICT(position_id) DO UPDATE SET notes = excluded.notes, updated_ts = excluded.updated_ts",
            (position_id, notes, int(time.time() * 1000)),
        )

    def close_position(
        self,
        position_id: int,
        exit_price: float,
        *,
        funding_paid: float = 0.0,
        fees: float = 0.0,
        closed_ts: int | None = None,
        exit_reason: str = "manual",
    ) -> dict | None:
        """Realise a position and append its journal entry in one transaction.

        The position row and the journal row move together, so a crash cannot
        leave a closed position without an audit entry or vice versa.
        """
        now = int(closed_ts or time.time() * 1000)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
                if row is None or row["closed_ts"] is not None:
                    self._conn.rollback()
                    return None
                position = dict(row)
                note = self._conn.execute(
                    "SELECT notes FROM position_notes WHERE position_id = ?", (position_id,)
                ).fetchone()
                rationale = note["notes"] if note else ""
                entry = position["avg_price"]
                direction = 1 if position["side"] == "long" else -1
                gross = round(direction * position["qty"] * (exit_price - entry), 6)
                exit_fee = round(fees, 6)
                entry_fee = round(float(position.get("entry_fee") or 0), 6)
                accrued_funding = round(float(position.get("funding_paid") or 0), 6)
                additional_funding = round(funding_paid, 6)
                total_funding = round(accrued_funding + additional_funding, 6)
                total_fees = round(entry_fee + exit_fee, 6)
                net = round(gross - total_fees - total_funding, 6)
                opened_ts = int(position["updated_ts"])
                digest = journal_hash(
                    venue=position["venue"], symbol=position["symbol"], side=position["side"],
                    qty=position["qty"], entry_price=entry, exit_price=exit_price,
                    opened_ts=opened_ts, closed_ts=now, net_pnl=net,
                )
                self._conn.execute(
                    "UPDATE positions SET closed_ts = ?, updated_ts = ? WHERE id = ?",
                    (now, now, position_id),
                )
                self._conn.execute(
                    "UPDATE paper_orders SET status = 'canceled' WHERE position_id = ? AND status = 'open'",
                    (position_id,),
                )
                self._conn.execute(
                "INSERT INTO journal (position_id, opened_ts, closed_ts, venue, symbol, side, qty, "
                "entry_price, exit_price, leverage, liq_price, gross_pnl, funding_paid, fees, net_pnl, "
                "exit_reason, rationale, tags, signal_id, report_id, source, entry_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    position_id,
                    opened_ts,
                    now,
                    position["venue"],
                    position["symbol"],
                    position["side"],
                    position["qty"],
                    entry,
                    exit_price,
                    position["leverage"],
                    position["liq_price"],
                    gross,
                    total_funding,
                    total_fees,
                    net,
                    exit_reason,
                    rationale,
                    None,
                    None,
                    None,
                    "paper",
                    digest,
                ),
            )
                self._kv_set_locked(
                    "paper_cash",
                    self._kv_float_locked("paper_cash") + gross - exit_fee - additional_funding,
                )
                self._kv_set_locked("paper_fees", self._kv_float_locked("paper_fees") + exit_fee)
                self._kv_set_locked("paper_realized", self._kv_float_locked("paper_realized") + net)
                if additional_funding:
                    self._kv_set_locked(
                        "paper_funding", self._kv_float_locked("paper_funding") + additional_funding
                    )
                    key = "paper_funding_paid" if additional_funding > 0 else "paper_funding_received"
                    self._kv_set_locked(key, self._kv_float_locked(key) + abs(additional_funding))
                self._conn.commit()
                return {
                    "gross_pnl": gross, "funding_paid": total_funding, "fees": total_fees,
                    "net_pnl": net, "closed_ts": now, "entry_hash": digest,
                }
            except Exception:
                self._conn.rollback()
                raise

    def journal_entries(self, limit: int = 200, symbol: str | None = None) -> list[dict]:
        sql = "SELECT * FROM journal"
        params: list = []
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(symbol)
        sql += " ORDER BY closed_ts DESC LIMIT ?"
        params.append(int(limit))
        return self.query(sql, tuple(params))

    def journal_integrity(self) -> dict:
        """Re-hash every entry so tampering is detectable after the fact."""
        rows = self.query("SELECT * FROM journal ORDER BY id")
        broken = []
        for row in rows:
            expected = journal_hash(
                venue=row["venue"],
                symbol=row["symbol"],
                side=row["side"],
                qty=row["qty"],
                entry_price=row["entry_price"],
                exit_price=row["exit_price"],
                opened_ts=row["opened_ts"],
                closed_ts=row["closed_ts"],
                net_pnl=row["net_pnl"],
            )
            if row.get("entry_hash") and row["entry_hash"] != expected:
                broken.append(row["id"])
        return {"entries": len(rows), "tampered": broken, "intact": not broken}


def journal_hash(
    *,
    venue: str,
    symbol: str,
    side: str,
    qty: float,
    entry_price: float,
    exit_price: float,
    opened_ts: int,
    closed_ts: int,
    net_pnl: float,
) -> str:
    """Content hash for a journal row; any later edit breaks the match."""
    import hashlib

    payload = "|".join(
        str(value)
        for value in (venue, symbol, side, qty, entry_price, exit_price, opened_ts, closed_ts, net_pnl)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
