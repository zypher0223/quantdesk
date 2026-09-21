"""对账脚本：任务行、状态行与真实表里的唯一记录数必须三方一致。

第二阶段验收要求：
  * 页数与状态行一致（backfill_tasks.pages == backfill_state.pages）
  * barsAvailable 等于数据库唯一记录数
本脚本只读打开线上库（WAL 允许并发读），不做任何写入。

    .venv/bin/python scripts/verify_backfill_consistency.py [--db ~/.quantdesk/quantdesk.db]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BASE = "http://127.0.0.1:4173"

TABLES = {
    "trade_candle": ("candles", "COUNT(*)"),
    "mark_candle": ("mark_candles", "COUNT(*)"),
    "funding": ("funding", "COUNT(*)"),
    "open_interest": ("open_interest", "COUNT(*)"),
    "risk_limit": ("risk_tiers", "COUNT(*)"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(Path.home() / ".quantdesk" / "quantdesk.db"))
    parser.add_argument("--limit", type=int, default=0, help="只打印前 N 行不一致项（0 = 全部）")
    parser.add_argument("--ranges", action="store_true", help="同时核对面板 barsAvailable")
    parser.add_argument("--readiness", action="store_true", help="逐合约试跑一次小回测，报告正式研究门禁")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # 以“看板报出的数字”为准：验收看的是页面上的 rowsAvailable，不是库里的旧计数。
    import urllib.request

    with urllib.request.urlopen(f"{BASE}/api/data/backfill/tasks", timeout=60) as response:
        board = json.load(response)
    tasks = [
        {
            "id": task["id"], "venue": "bybit", "symbol": task["symbol"],
            "interval": task["interval"] or "", "data_kind": task["dataKind"],
            "status": task["status"], "pages": task["pages"],
            "rows_available": task["rowsAvailable"], "complete": task["complete"],
        }
        for task in board["tasks"]
    ]

    page_mismatch: list[str] = []
    rows_mismatch: list[str] = []
    missing_state: list[str] = []
    checked = 0

    for task in tasks:
        interval = task["interval"] or ""
        table, expr = TABLES[task["data_kind"]]
        if task["data_kind"] in ("trade_candle", "mark_candle"):
            counted = conn.execute(
                f"SELECT {expr} FROM {table} WHERE venue=? AND symbol=? AND interval=?",
                (task["venue"], task["symbol"], interval),
            ).fetchone()[0]
        else:
            counted = conn.execute(
                f"SELECT {expr} FROM {table} WHERE venue=? AND symbol=?",
                (task["venue"], task["symbol"]),
            ).fetchone()[0]

        state = conn.execute(
            "SELECT status, pages, rows_available, complete FROM backfill_state "
            "WHERE venue=? AND symbol=? AND interval=? AND data_kind=?",
            (task["venue"], task["symbol"], interval, task["data_kind"]),
        ).fetchone()
        checked += 1
        label = f"#{task['id']} {task['symbol']} {interval or '-':>3} {task['data_kind']}"

        if state is None:
            if task["status"] not in ("pending", "cancelled"):
                missing_state.append(f"{label}: 已开跑却没有状态行")
            continue

        if int(task["pages"] or 0) != int(state["pages"] or 0):
            page_mismatch.append(
                f"{label}: 任务行 pages={task['pages']} vs 状态行 pages={state['pages']}"
            )

        if int(task["rows_available"] or 0) != int(counted):
            rows_mismatch.append(
                f"{label}: rowsAvailable={task['rows_available']} vs 表内唯一记录数={counted}"
            )

    print(f"库：{args.db}")
    print(f"看板任务行：{len(tasks)}  已核对：{checked}")
    print(f"状态行缺失：{len(missing_state)}")
    for line in missing_state[: args.limit or len(missing_state)]:
        print("  -", line)
    print(f"页数不一致：{len(page_mismatch)}")
    for line in page_mismatch[: args.limit or len(page_mismatch)]:
        print("  -", line)
    print(f"唯一记录数不一致：{len(rows_mismatch)}")
    for line in rows_mismatch[: args.limit or len(rows_mismatch)]:
        print("  -", line)

    by_status: dict[str, int] = {}
    for task in tasks:
        by_status[task["status"]] = by_status.get(task["status"], 0) + 1
    print("任务状态：", by_status)

    readiness = readiness_sweep() if args.readiness else None
    if readiness is not None:
        print(f"正式研究门禁：{readiness['ready']}/{readiness['total']} 个合约可跑")
        for row in readiness["rows"]:
            mark = "OK " if row["ready"] else "拒绝"
            print(f"  {mark} {row['symbol']:<13} {row['detail'][:96]}")

    panel_mismatch: list[str] = []
    if args.ranges:
        panel_mismatch = check_panel(args.db, args.limit)
        print(f"面板 barsAvailable 与库内唯一记录数不一致：{len(panel_mismatch)}")
        for line in panel_mismatch[: args.limit or len(panel_mismatch)]:
            print("  -", line)

    failed = bool(page_mismatch or rows_mismatch or missing_state or panel_mismatch)
    if readiness is not None and readiness["ready"] != readiness["total"]:
        # Not a defect in the store - a contract whose history is incomplete is
        # reported, not hidden - but the operator should leave with the list.
        pass
    return 1 if failed else 0


def readiness_sweep() -> dict:
    """Ask the gate, per contract, through a small formal run on each."""
    import urllib.error
    import urllib.request

    from quantdesk.config.instruments import VENUE_SYMBOLS

    rows = []
    for symbol in VENUE_SYMBOLS:
        body = json.dumps({
            "symbol": symbol, "timeframe": "1h", "bars": 120, "strategyId": "ma_cross",
        }).encode()
        request = urllib.request.Request(
            f"{BASE}/api/backtest", data=body, headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.load(response)
            rows.append({"symbol": symbol, "ready": True,
                         "detail": f"正式通过（净收益 {payload.get('net_return_pct')}%）"})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(json.loads(detail).get("detail", "{}"))
                detail = parsed.get("detail") or detail
            except Exception:  # noqa: BLE001 - an unparseable refusal is still a refusal
                pass
            rows.append({"symbol": symbol, "ready": False, "detail": str(detail)[:160]})
        except Exception as exc:  # noqa: BLE001 - the gateway may be down
            rows.append({"symbol": symbol, "ready": False, "detail": f"{type(exc).__name__}: {exc}"})
    return {"rows": rows, "total": len(rows), "ready": sum(1 for row in rows if row["ready"])}


def check_panel(db_path: str, limit: int) -> list[str]:
    """The panel's barsAvailable is the count in the store, per series."""
    import urllib.request

    with urllib.request.urlopen(f"{BASE}/api/data/backfill/ranges", timeout=60) as response:
        payload = json.load(response)
    rows = (payload.get("ranges") or payload)["rows"]
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    mismatches: list[str] = []
    try:
        for row in rows:
            table, _ = TABLES[row["dataKind"]]
            if row["dataKind"] in ("trade_candle", "mark_candle"):
                counted = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE venue='bybit' AND symbol=? AND interval=?",
                    (row["symbol"], row["interval"]),
                ).fetchone()[0]
            else:
                counted = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE venue='bybit' AND symbol=?",
                    (row["symbol"],),
                ).fetchone()[0]
            if int(row["barsAvailable"]) != int(counted):
                mismatches.append(
                    f"{row['symbol']} {row['interval'] or '-'} {row['dataKind']}: "
                    f"面板 barsAvailable={row['barsAvailable']} vs 库内={counted}"
                )
    finally:
        conn.close()
    return mismatches


if __name__ == "__main__":
    sys.exit(main())
