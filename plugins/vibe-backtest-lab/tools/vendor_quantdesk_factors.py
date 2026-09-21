#!/usr/bin/env python3
"""Carry QuantDesk's own factor library into this plugin, byte for byte.

`plugins/vibe-factors/` stays the single source of the 28 self-authored factors
(and of the statistical validator, which it keeps). This plugin is the *only*
enabled `factor_provider`, so it has to answer for those factors too — otherwise
every existing `1h` factor run, all of which use `vibe.*` ids, would lose its
provider the moment the zoo plugin takes over.

Rather than porting the code by hand (and having two copies drift), the whole file
is copied verbatim and stored next to a sidecar recording where it came from and
what it hashed to. `engine/tests/test_vibe_backtest_lab.py` re-computes that hash,
so an edit on either side that is not mirrored fails the suite instead of silently
changing what a factor means.

Copied file: `quantdesk_factors.py` — the same module, importable, whose `main()`
is guarded, so importing it never starts a JSON-RPC loop.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = PLUGIN_ROOT.parent / "vibe-factors" / "plugin.py"
TARGET = PLUGIN_ROOT / "quantdesk_factors.py"
SIDECAR = PLUGIN_ROOT / "quantdesk_factors.provenance.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="vibe-factors 的 plugin.py")
    args = parser.parse_args()
    source = Path(args.source).expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"找不到源文件：{source}")

    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    shutil.copyfile(source, TARGET)
    SIDECAR.write_text(
        json.dumps(
            {
                "origin": str(source),
                "sha256": digest,
                "bytes": len(payload),
                "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "note": (
                    "本文件是 plugins/vibe-factors/plugin.py 的逐字节副本，由 "
                    "tools/vendor_quantdesk_factors.py 生成，请勿直接编辑。"
                    "改因子实现请改源文件后重新生成本副本；测试会比对 sha256，"
                    "两边不一致会直接失败。"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"copied {source.name} -> {TARGET.name}  sha256={digest[:16]}  ({len(payload)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
