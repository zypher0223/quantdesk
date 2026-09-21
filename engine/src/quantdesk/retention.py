"""Keeping the run record bounded, without losing what somebody still needs.

Two tables grow without limit if nothing prunes them: `backtest_runs` keeps every
study's full result, and `factor_runs` keeps every factor matrix. Neither is
history that must be kept forever - a run is a working record.

The policy is deliberately conservative and explicit:

* a run that is queued or running is never touched, whatever the age;
* the newest `keep_runs` finished runs of each kind stay, and anything older than
  `keep_days` goes;
* a delete takes the run's artifacts and validation verdicts with it, because a
  verdict without its run is not a record of anything;
* `plan()` reports exactly what would go and how many bytes it frees, so the
  decision can be inspected before it is taken;
* the engine runs the policy once at startup and reports what it did.
"""

from __future__ import annotations

import time
from typing import Any

from .config.settings import load_app_config
from .datahub.db import Database

# What a fresh install keeps unless config.toml says otherwise. Factor runs are
# capped harder because one of them is a matrix - megabytes of values - while a
# study record is a few kilobytes of result and artifacts.
DEFAULT_KEEP_RUNS = 500
DEFAULT_KEEP_FACTOR_RUNS = 100
DEFAULT_KEEP_DAYS = 30
FINAL_STATUSES = ("done", "failed", "cancelled")


def policy_from_config(home=None) -> dict[str, int]:
    """The retention policy, from `[retention]` in config.toml."""
    try:
        config = load_app_config(home)
    except Exception:  # noqa: BLE001 - a broken config must not stop the engine
        return {"keepRuns": DEFAULT_KEEP_RUNS, "keepDays": DEFAULT_KEEP_DAYS}
    section = getattr(config, "retention", None) or {}
    if not isinstance(section, dict):
        return {"keepRuns": DEFAULT_KEEP_RUNS, "keepFactorRuns": DEFAULT_KEEP_FACTOR_RUNS,
                "keepDays": DEFAULT_KEEP_DAYS}
    return {
        "keepRuns": int(section.get("runs", DEFAULT_KEEP_RUNS) or 0),
        "keepFactorRuns": int(section.get("factorRuns", DEFAULT_KEEP_FACTOR_RUNS) or 0),
        "keepDays": int(section.get("days", DEFAULT_KEEP_DAYS) or 0),
    }


# Each family of runs names its own clock and its own final states: a study row
# records when it finished, a factor run records when it was made.
_TABLES = {
    "backtest_runs": {
        "clock": "COALESCE(finished_ts, queued_ts, updated_ts)",
        "final": ("done", "failed", "cancelled"),
    },
    "factor_runs": {
        "clock": "COALESCE(created_ts + COALESCE(duration_ms, 0), created_ts)",
        "final": ("ok", "error", "unavailable"),
    },
}


def _doomed(db: Database, table: str, *, keep_runs: int, keep_days: int, now_ms: int) -> list[int]:
    """Finished runs beyond the newest `keep_runs` or older than `keep_days`."""
    spec = _TABLES[table]
    placeholders = ",".join("?" for _ in spec["final"])
    rows = db.query(
        f"SELECT id, {spec['clock']} AS stamp FROM {table} WHERE status IN ({placeholders}) "
        "ORDER BY stamp DESC, id DESC",
        tuple(spec["final"]),
    )
    doomed: list[int] = []
    cutoff = now_ms - keep_days * 86_400_000 if keep_days > 0 else 0
    for index, row in enumerate(rows):
        stamp = int(row["stamp"] or 0)
        too_many = keep_runs > 0 and index >= keep_runs
        too_old = bool(cutoff) and stamp and stamp < cutoff
        if too_many or too_old:
            doomed.append(int(row["id"]))
    return doomed


def plan(db: Database, *, keep_runs: int = DEFAULT_KEEP_RUNS,
         keep_factor_runs: int = DEFAULT_KEEP_FACTOR_RUNS,
         keep_days: int = DEFAULT_KEEP_DAYS, now_ms: int | None = None) -> dict[str, Any]:
    """What the policy would delete, and how much room that frees."""
    now_ms = int(now_ms or time.time() * 1000)
    runs = _doomed(db, "backtest_runs", keep_runs=keep_runs, keep_days=keep_days, now_ms=now_ms)
    factor_runs = _doomed(db, "factor_runs", keep_runs=keep_factor_runs, keep_days=keep_days,
                          now_ms=now_ms)
    return {
        "policy": {"keepRuns": keep_runs, "keepFactorRuns": keep_factor_runs, "keepDays": keep_days},
        "runs": runs,
        "factorRuns": factor_runs,
        "bytes": _bytes_for(db, runs, factor_runs),
    }


def prune(db: Database, *, keep_runs: int = DEFAULT_KEEP_RUNS,
          keep_factor_runs: int = DEFAULT_KEEP_FACTOR_RUNS,
          keep_days: int = DEFAULT_KEEP_DAYS, now_ms: int | None = None,
          dry_run: bool = False) -> dict[str, Any]:
    """Apply the policy and report what went."""
    report = plan(db, keep_runs=keep_runs, keep_factor_runs=keep_factor_runs,
                  keep_days=keep_days, now_ms=now_ms)
    if dry_run or (not report["runs"] and not report["factorRuns"]):
        return {**report, "deletedRuns": 0, "deletedFactorRuns": 0, "dryRun": bool(dry_run)}
    for run_id in report["runs"]:
        db.execute("DELETE FROM backtest_artifacts WHERE run_id=?", (run_id,))
        db.execute("DELETE FROM backtest_validation_results WHERE run_id=?", (run_id,))
        db.execute("DELETE FROM backtest_runs WHERE id=?", (run_id,))
    for run_id in report["factorRuns"]:
        db.execute("DELETE FROM factor_runs WHERE id=?", (run_id,))
    return {
        **report,
        "deletedRuns": len(report["runs"]),
        "deletedFactorRuns": len(report["factorRuns"]),
        "dryRun": False,
    }


def _bytes_for(db: Database, runs: list[int], factor_runs: list[int]) -> int:
    total = 0
    if runs:
        placeholders = ",".join("?" for _ in runs)
        rows = db.query(
            f"SELECT COALESCE(SUM(LENGTH(result_json)), 0) AS bytes FROM backtest_runs "
            f"WHERE id IN ({placeholders})",
            tuple(runs),
        )
        total += int(rows[0]["bytes"] or 0) if rows else 0
        rows = db.query(
            f"SELECT COALESCE(SUM(LENGTH(payload)), 0) AS bytes FROM backtest_artifacts "
            f"WHERE run_id IN ({placeholders})",
            tuple(runs),
        )
        total += int(rows[0]["bytes"] or 0) if rows else 0
    if factor_runs:
        placeholders = ",".join("?" for _ in factor_runs)
        rows = db.query(
            f"SELECT COALESCE(SUM(COALESCE(LENGTH(values_blob), 0) + COALESCE(LENGTH(values_json), 0)), 0) "
            f"AS bytes FROM factor_runs WHERE id IN ({placeholders})",
            tuple(factor_runs),
        )
        total += int(rows[0]["bytes"] or 0) if rows else 0
    return total


def run_startup_prune(db: Database, home=None) -> dict[str, Any]:
    """Apply the configured policy once, at startup, and report it."""
    policy = policy_from_config(home)
    return prune(db, keep_runs=policy["keepRuns"], keep_factor_runs=policy["keepFactorRuns"],
                 keep_days=policy["keepDays"], dry_run=False)
