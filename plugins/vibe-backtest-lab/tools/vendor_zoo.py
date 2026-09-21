#!/usr/bin/env python3
"""Build-time vendoring of the Vibe-Trading alpha zoo into this plugin.

Run with the plugin's own runtime interpreter (`~/.quantdesk/plugin-runtimes/
vibe-backtest-lab/bin/python`), which is the only place the pinned pandas/numpy
exist. It does three things, and executes nothing from upstream except the factor
functions themselves:

1. copies the factor package verbatim into `vendor/vibe-trading/`, plus the small
   adapter shim from `tools/shims/`;
2. reads every `__alpha_meta__` by AST, so the build needs neither pydantic nor
   Vibe's settings tree;
3. *decides the allowlist empirically.* Every candidate is computed on synthetic
   panels, and what it does decides its fate:

   * a factor whose value for symbol A changes when an unrelated symbol changes is
     reading across the cross-section and is excluded — a v3 `factor.compute`
     request carries one symbol's bars, and a one-column `rank()` is a constant.
     This test is implementation-independent on purpose: an earlier static scan for
     `rank`/`scale`/`zscore` missed a whole zoo that defines its own
     `_cross_sectional_zscore` and would have shipped twelve columns of nulls;
   * a factor that returns nothing on 2,700 daily bars is excluded rather than
     shipped as a column of nulls;
   * a factor that is not identical across two runs on the same panel is excluded,
     because a study that cannot be reproduced is not evidence.

The output `factor_allowlist.json` is a build artifact: re-running this on the same
upstream commit reproduces the same factor set, the same gate decisions and the same
hashes (only the `generatedAt` timestamp differs).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
import warnings
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
VENDOR_ROOT = PLUGIN_ROOT / "vendor" / "vibe-trading"
VENDOR_SRC = VENDOR_ROOT / "src"
SHIM_TREE = PLUGIN_ROOT / "tools" / "shims"
ALLOWLIST_PATH = PLUGIN_ROOT / "factor_allowlist.json"

# Repository-root files that travel with the source copy (provenance, not code).
COPY_ROOT_FILES = ("LICENSE", "NOTICE")
# `bench_runner`, `compare_runner`, `cli_handlers`, `factor_analysis_core` and
# `registry.py` are left behind: they are the CLI/benchmark surface. `_backend.py`
# is required, because `base.py` imports the bottleneck flag from it.
COPY_FILES = (
    "src/__init__.py",
    "src/factors/__init__.py",
    "src/factors/_backend.py",
    "src/factors/base.py",
    "src/factors/zoo/__init__.py",
)
COPY_TREES = (
    "src/factors/zoo/academic",
    "src/factors/zoo/alpha101",
    "src/factors/zoo/fundamental",
    "src/factors/zoo/gtja191",
    "src/factors/zoo/qlib158",
)

# The panel QuantDesk actually hands a factor provider: closed Bybit candles plus the
# notional derived from them. `vwap` is deliberately *not* here: the engine has no
# true VWAP (turnover/volume would degenerate to close), so a factor needing one is
# excluded rather than fed a fake.
PANEL_FIELDS = ("open", "high", "low", "close", "volume", "amount")
FUNDAMENTAL_PREFIX = "fund:"
INTERVAL = "1d"

# The synthetic panel the empirical gates run on. Fixed seed: the allowlist has to be
# reproducible, and a factor that passes only on one random draw is not a factor.
PROBE_BARS = 2700
PROBE_SEED = 20150101
EXCLUSION_ORDER = (
    "requires_fundamentals",
    "requires_true_vwap",
    "requires_sector",
    "requires_panel_field",
    "cross_sectional_needs_panel",
    "no_usable_values",
    "non_deterministic",
    "degenerate_constant",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def git(upstream: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(upstream), *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise SystemExit(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout.strip()


# --------------------------------------------------------------------------
# 1. vendor the bytes
# --------------------------------------------------------------------------

def copy_subset(upstream: Path) -> tuple[int, list[str]]:
    target = VENDOR_ROOT
    if target.exists():
        shutil.rmtree(target)
    (target / "src" / "factors" / "zoo").mkdir(parents=True)
    copied = 0
    for relative in COPY_ROOT_FILES:
        source = upstream / relative
        if not source.is_file():
            raise SystemExit(f"上游缺少文件：{relative}")
        shutil.copy2(source, target / relative)
        copied += 1
    for relative in COPY_FILES:
        source = upstream / "agent" / relative
        if not source.is_file():
            raise SystemExit(f"上游缺少文件：agent/{relative}")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied += 1
    for relative in COPY_TREES:
        source = upstream / "agent" / relative
        if not source.is_dir():
            raise SystemExit(f"上游缺少目录：agent/{relative}")
        for path in sorted(source.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            destination = target / relative / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            copied += 1
    shims = sorted(path for path in SHIM_TREE.rglob("*.py") if path.is_file())
    if not shims:
        raise SystemExit(f"缺少适配垫片：{SHIM_TREE}")
    for path in shims:
        destination = target / path.relative_to(SHIM_TREE)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied += 1
    return copied, [str(path.relative_to(SHIM_TREE)) for path in shims]


# --------------------------------------------------------------------------
# 2. read the metadata without importing upstream's registry
# --------------------------------------------------------------------------

def read_meta(path: Path) -> dict[str, Any] | None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if getattr(node.targets[0], "id", "") != "__alpha_meta__":
            continue
        try:
            return ast.literal_eval(node.value)
        except ValueError:
            return None
    return None


def panel_keys_of(tree: ast.AST) -> set[str]:
    keys = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "panel"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
    return keys


def scan_zoo() -> list[dict[str, Any]]:
    """Every alpha in the vendored copy, with the metadata upstream declares."""
    found = []
    zoo_root = VENDOR_SRC / "factors" / "zoo"
    for path in sorted(zoo_root.glob("*/*.py")):
        if path.name == "__init__.py":
            continue
        meta = read_meta(path)
        if not meta or not meta.get("id"):
            continue
        zoo_id = path.parent.name
        source = path.read_text(encoding="utf-8")
        found.append(
            {
                "id": str(meta["id"]),
                "zoo": zoo_id,
                "modulePath": f"src.factors.zoo.{zoo_id}.{path.stem}",
                "meta": meta,
                "panelKeys": sorted(panel_keys_of(ast.parse(source))),
                "moduleSha256": sha256_bytes(source.encode("utf-8")),
            }
        )
    return found


# --------------------------------------------------------------------------
# 3. let the factor itself decide
# --------------------------------------------------------------------------

def probe_panels():
    """One long panel, and a second symbol that is either B or C."""
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(PROBE_SEED)
    index = pd.date_range("2014-01-01", periods=PROBE_BARS, freq="D")

    def series():
        return pd.Series(100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.02, PROBE_BARS))), index=index)

    def panel(columns: dict[str, Any]):
        return {
            "close": pd.DataFrame(columns, dtype="float64"),
            "open": pd.DataFrame({n: s.shift(1).fillna(s.iloc[0]) for n, s in columns.items()}),
            "high": pd.DataFrame({n: s * 1.004 for n, s in columns.items()}),
            "low": pd.DataFrame({n: s * 0.996 for n, s in columns.items()}),
            "volume": pd.DataFrame(
                {n: pd.Series(rng.uniform(1e5, 1e6, PROBE_BARS), index=index) for n in columns}
            ),
            "amount": pd.DataFrame(
                {n: pd.Series(rng.uniform(1e7, 1e8, PROBE_BARS), index=index) for n in columns}
            ),
            # Present so that a vwap-dependent alpha can be *classified* completely
            # rather than crashing into a KeyError. It is never served to a real
            # request: every alpha that reads it is excluded by the vwap gate, since
            # QuantDesk has no true VWAP to give it.
            "vwap": pd.DataFrame({n: s * 1.001 for n, s in columns.items()}),
        }

    anchor = series()
    return panel({"A": anchor, "B": series()}), panel({"A": anchor, "C": series()})


def probe(item: dict[str, Any], first, second) -> dict[str, Any]:
    """Run one alpha and report what it actually does on a one-symbol panel."""
    import numpy as np

    module = importlib.import_module(item["modulePath"])
    # Everything the module reads, not just the servable fields: a factor that wants
    # a field QuantDesk will never provide still has to be probed to the end, so the
    # allowlist records what it really is instead of "it crashed".
    needed = {
        name
        for name in set(item["panelKeys"]) | set(item["meta"].get("columns_required") or [])
        if name in PANEL_FIELDS or name == "vwap"
    }
    needed.add("close")
    subset = lambda panel: {name: panel[name] for name in needed}  # noqa: E731

    solo = module.compute(subset(first)).iloc[:, 0]
    twin = module.compute(subset(second)).iloc[:, 0]
    if len(solo) != PROBE_BARS or len(twin) != PROBE_BARS:
        raise ValueError("因子返回的行数与面板不一致")

    valid = solo.notna().to_numpy().nonzero()[0]
    left = solo.to_numpy(dtype="float64")
    right = twin.to_numpy(dtype="float64")
    # Column A is the same series in both panels, and only the other symbol differs.
    # If A's values move, the factor read the cross-section.
    cross_sectional = not np.array_equal(left, right, equal_nan=True)

    repeat = module.compute(subset(first)).iloc[:, 0].to_numpy(dtype="float64")
    tail = solo.iloc[PROBE_BARS // 2 :].dropna()
    return {
        "warmupBars": int(valid[0]) + 1 if len(valid) else None,
        "crossSectional": bool(cross_sectional),
        "deterministic": bool(np.array_equal(left, repeat, equal_nan=True)),
        "distinctValues": int(tail.nunique()) if len(tail) else 0,
    }


def failed_gates(
    item: dict[str, Any], measured: dict[str, Any] | None, error: str
) -> list[tuple[str, str]]:
    """Every gate a candidate fails, with the evidence that failed it."""
    meta = item["meta"]
    read = set(meta.get("columns_required") or []) | set(item["panelKeys"])
    gates: list[tuple[str, str]] = []
    fundamentals = sorted(name for name in read if name.startswith(FUNDAMENTAL_PREFIX))
    if fundamentals:
        gates.append(("requires_fundamentals", f"需要基本面字段 {', '.join(fundamentals)}"))
    if "vwap" in read:
        gates.append(
            ("requires_true_vwap", "需要真实 VWAP；QuantDesk 只有收盘价×成交量，derive 会退化成 close")
        )
    if meta.get("requires_sector"):
        gates.append(("requires_sector", "需要行业分类，QuantDesk 不提供"))
    extra = sorted(
        name
        for name in read - set(PANEL_FIELDS)
        if not name.startswith(FUNDAMENTAL_PREFIX) and name != "vwap"
    )
    if extra:
        gates.append(("requires_panel_field", f"读取面板上没有的键 {', '.join(extra)}"))
    if error:
        gates.append(("no_usable_values", f"实测失败：{error}"))
        return gates
    if measured is None:
        return gates
    if measured["crossSectional"]:
        gates.append(
            (
                "cross_sectional_needs_panel",
                "换掉另一只标的后本标的取值发生变化：因子读取横截面，"
                "而 v3 请求只带一个标的的K线，单列 rank/zscore 是常数",
            )
        )
    if not measured["deterministic"]:
        gates.append(("non_deterministic", "同一面板两次计算结果不一致"))
    if measured["warmupBars"] is None:
        gates.append(("no_usable_values", f"{PROBE_BARS} 根日线之内没有任何非空取值"))
    elif measured["distinctValues"] <= 1:
        gates.append(
            ("degenerate_constant", f"预热之后只有 {measured['distinctValues']} 个不同取值，没有区分度")
        )
    order = {name: index for index, name in enumerate(EXCLUSION_ORDER)}
    return sorted(gates, key=lambda entry: order[entry[0]])


def build_allowlist(upstream: Path, alphas: list[dict[str, Any]], panels) -> dict[str, Any]:
    first, second = panels
    factors, excluded = [], []
    for item in alphas:
        error, measured = "", None
        try:
            measured = probe(item, first, second)
        except Exception as exc:  # a factor that crashes is excluded, not shipped
            error = f"{type(exc).__name__}: {exc}"[:200]
        gates = failed_gates(item, measured, error)
        if gates:
            excluded.append(
                {
                    "id": item["id"],
                    "zoo": item["zoo"],
                    "reason": gates[0][0],
                    "detail": gates[0][1],
                    "gates": [{"gate": name, "detail": detail} for name, detail in gates],
                }
            )
            continue
        meta = item["meta"]
        required = sorted(
            name for name in set(meta.get("columns_required") or []) if name in PANEL_FIELDS
        )
        declared = int(meta.get("min_warmup_bars") or 0)
        factors.append(
            {
                "id": item["id"],
                "zoo": item["zoo"],
                "name": (meta.get("nickname") or item["id"])[:120],
                "theme": list(meta.get("theme") or []),
                "modulePath": item["modulePath"],
                "requiredFields": required or ["close"],
                # Measured, not declared: qlib158 declares n where the window needs
                # n+1, and a run sized by the declared number would open with a null.
                "declaredWarmupBars": declared,
                "warmupBars": max(declared, int(measured["warmupBars"])),
                "measuredWarmupBars": int(measured["warmupBars"]),
                "decayHorizon": meta.get("decay_horizon"),
                "frequency": list(meta.get("frequency") or []),
                "universe": list(meta.get("universe") or []),
                "formulaLatex": (meta.get("formula_latex") or "")[:400],
                "moduleSha256": item["moduleSha256"],
            }
        )

    primary = Counter(entry["reason"] for entry in excluded)
    gates = Counter(gate["gate"] for entry in excluded for gate in entry["gates"])
    return {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "repository": "https://github.com/HKUDS/Vibe-Trading",
            "commit": git(upstream, "rev-parse", "HEAD"),
            "commitDate": git(upstream, "log", "-1", "--format=%cI"),
            "describe": git(upstream, "describe", "--tags", "--always"),
            "license": "MIT",
        },
        "policy": {
            "panelFields": list(PANEL_FIELDS),
            "interval": INTERVAL,
            "mode": "time_series",
            "probeBars": PROBE_BARS,
            "probeSeed": PROBE_SEED,
            "note": (
                "只收录单标的、日频、纯价量可算的因子。是否横截面由实测判定：固定 A 的序列，"
                "把另一只标的从 B 换成 C 再算一次，A 的取值若变化就说明因子读了横截面。"
            ),
        },
        "stats": {
            "upstreamTotal": len(alphas),
            "included": len(factors),
            "excluded": len(excluded),
            "excludedByPrimaryReason": dict(sorted(primary.items())),
            "excludedByGate": dict(sorted(gates.items())),
            "byZoo": dict(sorted(Counter(item["zoo"] for item in factors).items())),
            "maxWarmupBars": max((item["warmupBars"] for item in factors), default=0),
            "declaredWarmupTooShort": sum(
                1 for item in factors if item["measuredWarmupBars"] > item["declaredWarmupBars"]
            ),
        },
        "factors": factors,
        "excluded": excluded,
    }


def write_source_record(upstream: Path, copied: int, manifest_path: Path, shims: list[str]) -> None:
    lock = upstream / "requirements-lock.txt"
    record = {
        "repository": "https://github.com/HKUDS/Vibe-Trading",
        "package": "vibe-trading-ai",
        "tag": git(upstream, "describe", "--tags", "--abbrev=0"),
        "commit": git(upstream, "rev-parse", "HEAD"),
        "commitDate": git(upstream, "log", "-1", "--format=%cI"),
        "license": "MIT",
        "vendoredPaths": list(COPY_ROOT_FILES) + list(COPY_FILES) + list(COPY_TREES),
        "vendoredFileCount": copied,
        "shimFiles": list(shims),
        "upstreamRequirementsLockSha256": sha256_file(lock) if lock.is_file() else "",
        "upstreamManifestSha256": sha256_file(manifest_path),
        "note": (
            "MIT 许可下的上游源码副本，逐字复制未做修改；每个 zoo 目录自带 LICENSE.md/NOTICE。"
            "固定在此提交，升级必须重新运行 tools/vendor_zoo.py 并复查 factor_allowlist.json 的差异。"
            "shimFiles 是 QuantDesk 写的适配垫片（非上游代码），用来替掉对 Vibe 设置树的依赖，"
            "并固定走 numpy 回退路径以保证跨主机数值一致。"
        ),
    }
    (VENDOR_ROOT / "SOURCE.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def resolve_upstream(raw: str) -> Path:
    upstream = Path(raw).expanduser().resolve()
    if not (upstream / "agent" / "src" / "factors" / "zoo").is_dir():
        raise SystemExit(f"不像 Vibe-Trading 检出目录：{upstream}")
    if git(upstream, "status", "--porcelain"):
        raise SystemExit(f"上游检出有未提交改动，拒绝 vendor：{upstream}")
    return upstream


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upstream",
        default="~/.quantdesk/vendor/vibe-trading",
        help="Vibe-Trading 检出目录（默认 ~/.quantdesk/vendor/vibe-trading）",
    )
    args = parser.parse_args()
    upstream = resolve_upstream(args.upstream)

    import pandas  # noqa: F401  (named here so a wrong interpreter fails clearly)

    copied, shims = copy_subset(upstream)
    if str(VENDOR_ROOT) not in sys.path:
        sys.path.insert(0, str(VENDOR_ROOT))
    warnings.filterwarnings("ignore")

    alphas = scan_zoo()
    if not alphas:
        raise SystemExit("vendored 副本里没有找到任何 __alpha_meta__")
    manifest = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": git(upstream, "rev-parse", "HEAD"),
        "zoos": [
            {
                "zoo_id": zoo_id,
                "alphas": [
                    {"id": item["id"], "module_path": item["modulePath"], "meta": item["meta"]}
                    for item in alphas
                    if item["zoo"] == zoo_id
                ],
            }
            for zoo_id in sorted({item["zoo"] for item in alphas})
        ],
        "health": {"loaded": len(alphas), "failed": 0},
    }
    manifest_path = VENDOR_ROOT / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )

    allowlist = build_allowlist(upstream, alphas, probe_panels())
    allowlist["source"]["manifestSha256"] = sha256_file(manifest_path)
    ALLOWLIST_PATH.write_text(
        json.dumps(allowlist, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    write_source_record(upstream, copied, manifest_path, shims)

    print(json.dumps(allowlist["stats"], ensure_ascii=False, indent=1))
    print(f"vendored {copied} 个文件 -> {VENDOR_ROOT}")
    print(f"白名单 {len(allowlist['factors'])} 个 -> {ALLOWLIST_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
