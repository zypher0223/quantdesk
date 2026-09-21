"""断点续传探针：在真实回填进行中重启网关，判断它是接着游标走还是从头再走。

判据（都取自真实库，不改任何数据）：
  * 页数：重启前后 backfill_state.pages 是否继续累加，而不是回到 0/1；
  * 游标：oldest_ts 是否沿着重启前的值继续变老；从头再走会先跳回“最近一页”的
    时间附近（约 now - 1000 根），因此这个跳变就是“没有断点续传”的证据；
  * attempts：重启后 +1（新进程新的一次 run），但 rows_available 不回退。

    .venv/bin/python scripts/restart_resume_probe.py --wait 300
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DB = Path.home() / ".quantdesk" / "quantdesk.db"


def rows(db: Path) -> dict[int, dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tasks = {
            int(row["id"]): dict(row)
            for row in conn.execute("SELECT * FROM backfill_tasks")
        }
        states = {
            (row["symbol"], row["interval"] or "", row["data_kind"]): dict(row)
            for row in conn.execute("SELECT * FROM backfill_state")
        }
    finally:
        conn.close()
    return {"tasks": tasks, "states": states}


def sample(db: Path, task_id: int) -> dict:
    snap = rows(db)
    task = snap["tasks"][task_id]
    state = snap["states"].get((task["symbol"], task["interval"] or "", task["data_kind"])) or {}
    return {
        "status": task["status"],
        "pages": int(task["pages"] or 0),
        "statePages": int(state.get("pages") or 0),
        "rows": int(task["rows_available"] or 0),
        "oldestTs": state.get("oldest_ts"),
        "complete": bool(state.get("complete")),
        "attempts": int(state.get("attempts") or 0),
    }


def restart() -> None:
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/com.quantdesk.local"], check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--min-pages", type=int, default=2)
    parser.add_argument("--wait", type=int, default=300, help="最多等多久出现目标任务")
    parser.add_argument("--watch", type=int, default=25, help="重启后观察多少秒")
    args = parser.parse_args()
    db = Path(args.db)

    deadline = time.time() + args.wait
    target: int | None = None
    while time.time() < deadline:
        snap = rows(db)
        for task_id, task in snap["tasks"].items():
            if (task["status"] == "running" and task["interval"] == args.interval
                    and int(task["pages"] or 0) >= args.min_pages):
                target = task_id
                break
        if target is not None:
            break
        time.sleep(0.5)
    if target is None:
        print(f"{args.wait}s 内没有抓到 {args.interval} 的进行中任务")
        return 2

    before = sample(db, target)
    print(f"任务 #{target} 重启前：{json.dumps(before, ensure_ascii=False)}")
    restart()
    print("已重启网关，继续采样：")
    seen: list[dict] = []
    end = time.time() + args.watch
    while time.time() < end:
        time.sleep(1.5)
        current = sample(db, target)
        seen.append(current)
        print(f"  {time.strftime('%H:%M:%S')} {json.dumps(current, ensure_ascii=False)}")
        if current["status"] in ("done", "failed", "cancelled"):
            break

    if not seen:
        print("没有采到重启后的样本")
        return 1
    resumed_pages = any(item["pages"] >= before["pages"] and item["pages"] > 0 for item in seen)
    cursor_kept = all(
        item["oldestTs"] is not None and before["oldestTs"] is not None
        and item["oldestTs"] <= before["oldestTs"] + 1000 * 60 * 60
        for item in seen
    )
    no_rollback = all(item["rows"] >= before["rows"] for item in seen)
    print(f"页数继续累加：{resumed_pages}；游标未跳回最近一页：{cursor_kept}；"
          f"记录数未回退：{no_rollback}")
    return 0 if (resumed_pages and cursor_kept and no_rollback) else 1


if __name__ == "__main__":
    sys.exit(main())
