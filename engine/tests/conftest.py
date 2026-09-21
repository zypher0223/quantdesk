"""Test-wide guards.

Two rules this file enforces, both learned the hard way in this project:

* a test must not open the operator's live database. A test once built a
  `Database` directly while a spawned study process inherited the ambient
  `QUANTDESK_HOME`, so it read and wrote `~/.quantdesk/quantdesk.db` and passed for
  the wrong reason - on production data. Opening it now fails immediately, with the
  path in the message;
* the session runs against a temporary home unless a test asks for its own, so the
  default is isolation rather than a promise to be careful.

The guard is on the *open*, not on the file's state: the running gateway writes to
its own database continuously, and a size/mtime check would fire on its work rather
than on a test's.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from quantdesk.datahub.db import Database


def _live_database() -> Path | None:
    home = os.environ.get("QUANTDESK_LIVE_HOME")
    path = Path(home) / "quantdesk.db" if home else Path.home() / ".quantdesk" / "quantdesk.db"
    return path if path.exists() else None


@pytest.fixture(scope="session", autouse=True)
def _isolated_home():
    """Point the whole session at a temporary home when the caller has not.

    A test that wants a specific home still sets it (most of them do), and the
    environment variable is restored afterwards so nothing leaks between runs.
    """
    original = os.environ.get("QUANTDESK_HOME")
    created: tempfile.TemporaryDirectory | None = None
    if not original:
        created = tempfile.TemporaryDirectory(prefix="quantdesk-tests-")
        os.environ["QUANTDESK_HOME"] = created.name
    try:
        yield os.environ.get("QUANTDESK_HOME")
    finally:
        if created is not None:
            os.environ.pop("QUANTDESK_HOME", None)
            created.cleanup()
        elif original is not None:
            os.environ["QUANTDESK_HOME"] = original


@pytest.fixture(scope="session", autouse=True)
def _no_test_opens_the_live_database():
    """Refuse a `Database` handle on the operator's live file, whoever asks."""
    live = _live_database()
    if live is None:
        yield
        return
    resolved_live = live.resolve()
    original = Database.__init__

    def guarded(self, path, *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            resolved = Path(path).expanduser().resolve()
        except (OSError, TypeError):
            resolved = None
        if resolved == resolved_live:
            raise AssertionError(
                f"测试试图打开真实数据库 {resolved_live}：测试必须隔离到临时目录，"
                "否则会在生产数据上跑出'通过'。用 tmp_path/QUANTDESK_HOME 指向临时库。"
            )
        return original(self, path, *args, **kwargs)

    Database.__init__ = guarded  # type: ignore[method-assign]
    try:
        yield
    finally:
        Database.__init__ = original  # type: ignore[method-assign]
