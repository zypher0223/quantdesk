"""Run one formal study in its own process.

A parameter search is minutes of pure-Python number crunching. Inside the
gateway process that work holds the interpreter and the API stops answering -
measured: a 4.5M-unit study blocked `/health` for over twenty seconds. So a
queued study is executed here, in a child process, and the parent stays free to
serve the page that is watching it.

This module writes **nothing** to the database. The parent owns the run row and
records the progress reported on stdout and the result written to the file named
by `--result-file`; one writer, one lease, no races.

Protocol:

* stdin  - one JSON object: {"kind": ..., "request": {...}}
* stdout - one JSON object per line: {"progress": 0.42, "label": "..."}
* exit 0 - the result JSON is in the result file
* exit 3 - a study refused:         result file = {"error": {"kind", "message", "detail"}}
* exit 4 - an unexpected failure:   result file = {"error": {"kind": "internal", "message"}}

`--parent-pid` names the engine that started this process. A study must not
outlive its parent: if the engine is killed outright (where no shutdown hook can
run) the child would otherwise keep computing a result nobody is allowed to
publish. The watchdog makes that impossible.

    python -m quantdesk.studies_cli --result-file /tmp/run-1.json --parent-pid 1234
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


EXIT_OK = 0
EXIT_STUDY_REFUSED = 3
EXIT_INTERNAL_ERROR = 4
EXIT_PARENT_GONE = 5


def _watch_parent(pid: int, *, interval: float = 1.5) -> None:
    """Exit as soon as the engine that started this study is gone.

    A child is reparented when its parent dies, so the check is both "that pid is
    gone" and "my parent is no longer that pid". The process leaves immediately:
    its result could not be published anyway (the run's lease belongs to the
    engine that was killed).
    """
    import os
    import threading
    import time

    def watch() -> None:
        while True:
            time.sleep(interval)
            alive = True
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True
            if not alive or os.getppid() != pid:
                sys.stdout.write(json.dumps({"progress": 1.0, "label": "父进程已退出，停止计算"}) + "\n")
                sys.stdout.flush()
                os._exit(EXIT_PARENT_GONE)

    threading.Thread(target=watch, name="quantdesk-parent-watch", daemon=True).start()


def _emit_progress(fraction: float, label: str) -> None:
    sys.stdout.write(json.dumps({"progress": fraction, "label": label}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one QuantDesk study in its own process")
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--parent-pid", type=int, default=0,
                        help="引擎进程号；它消失后本进程立即退出")
    args = parser.parse_args(argv)
    result_path = Path(args.result_file)
    if args.parent_pid > 0:
        _watch_parent(args.parent_pid)

    raw = sys.stdin.readline()
    try:
        payload = json.loads(raw or "{}")
        kind = str(payload["kind"])
        body = payload["request"]
    except Exception as exc:  # noqa: BLE001 - the parent sent something unusable
        _write(result_path, {"error": {"kind": "invalid", "message": f"请求无法解析：{exc}"}})
        return EXIT_STUDY_REFUSED

    from .studies import StudyError, open_db, parse_request, run_study

    try:
        request = parse_request(kind, body)
        result = run_study(open_db(), kind, request, progress=_emit_progress)
    except StudyError as exc:
        _write(result_path, {"error": {"kind": exc.kind, "message": exc.message, "detail": exc.detail}})
        return EXIT_STUDY_REFUSED
    except Exception as exc:  # noqa: BLE001 - reported as an internal failure
        _write(result_path, {"error": {"kind": "internal", "message": f"{type(exc).__name__}: {exc}"}})
        return EXIT_INTERNAL_ERROR

    _write(result_path, {"result": result})
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised through the queue
    sys.exit(main())
