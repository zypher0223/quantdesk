"""The controlled factor library: what a campaign is allowed to propose from.

A gate scan writes one report per (group, interval, horizon). This module is the
small amount of state on top of those reports: where they live, how they merge into
"the library for this group", and which tier a factor is in.

The two tiers exist because the seven gates answer two different questions:

* `validated` - the factor cleared all seven, including the predictive gate. Its
  association with forward returns is not explained by noise at the stated horizon.
* `candidate` - the factor cleared the data-hygiene, persistence, cost and
  redundancy gates, but not the predictive one. These are the factors a campaign is
  *for*: they are computable, tradeable and not copies of each other, and whether
  they carry information is exactly what the campaign's out-of-sample segment is
  going to decide. Running them is not free of multiple-testing risk, which is why
  the campaign records how many candidates it tried (see `trials` in the DSR).

A factor in neither tier is not proposed by anything: it is either uncomputable on
our data, degenerate, too expensive to trade, or a near-copy of something already in
the library.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# The gates a candidate must clear. `predictive` is deliberately absent: it is the
# gate the campaign's own validation segment exists to test.
HYGIENE_GATES = ("coverage", "finiteness", "dispersion", "persistence", "cost", "redundancy")
ALL_GATES = ("coverage", "finiteness", "dispersion", "predictive", "persistence", "cost", "redundancy")


def library_root(home: Path) -> Path:
    return Path(home) / "factor-library"


def report_name(group: str, interval: str, horizon: int) -> str:
    return f"{group}-{interval}-h{horizon}.json"


def save_report(report: dict[str, Any], home: Path) -> Path:
    root = library_root(home)
    root.mkdir(parents=True, exist_ok=True)
    path = root / report_name(report["group"], report["interval"], int(report["horizonBars"]))
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def tier_of(evidence: dict[str, Any]) -> str | None:
    """Which tier a factor's gate evidence puts it in, if any."""
    passed = {gate["name"]: bool(gate["passed"]) for gate in evidence["gates"]}
    if all(passed.get(name, False) for name in ALL_GATES):
        return "validated"
    if all(passed.get(name, False) for name in HYGIENE_GATES):
        return "candidate"
    return None


def entries_of(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The library a campaign may draw from, best evidence first."""
    graded: list[dict[str, Any]] = []
    for evidence in report.get("evidence", []):
        tier = tier_of(evidence)
        if tier is None:
            continue
        predictive = next(
            (gate for gate in evidence["gates"] if gate["name"] == "predictive"), {"metric": {}}
        )
        cost = next((gate for gate in evidence["gates"] if gate["name"] == "cost"), {"metric": {}})
        graded.append(
            {
                "factorId": evidence["factorId"],
                "family": evidence["family"],
                "tier": tier,
                "ic": predictive["metric"].get("ic"),
                "icPValue": predictive["metric"].get("pValue"),
                "signConsistency": predictive["metric"].get("signConsistency"),
                "netBps": cost["metric"].get("netBps"),
                "gates": [
                    {"name": gate["name"], "passed": gate["passed"], "detail": gate["detail"]}
                    for gate in evidence["gates"]
                ],
            }
        )
    graded.sort(key=lambda item: (item["tier"] != "validated", -(abs(item["ic"] or 0.0))))
    return graded


def load_reports(home: Path) -> list[dict[str, Any]]:
    root = library_root(home)
    if not root.is_dir():
        return []
    reports = []
    for path in sorted(root.glob("*.json")):
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return reports


def load_library_report(home: Path) -> dict[str, Any] | None:
    """The merged view: the newest report per (group, interval, horizon)."""
    newest: dict[tuple, dict[str, Any]] = {}
    for report in load_reports(home):
        key = (report.get("group"), report.get("interval"), int(report.get("horizonBars") or 0))
        current = newest.get(key)
        if current is None or str(report.get("generatedAt") or "") >= str(current.get("generatedAt") or ""):
            newest[key] = report
    if not newest:
        return None
    scans = [newest[key] for key in sorted(newest, key=lambda item: tuple(str(part) for part in item))]
    return {
        "scans": scans,
        "generatedAt": max(str(report.get("generatedAt") or "") for report in scans),
    }


def library_for(
    group: str, interval: str, *, horizon: int | None = None, home: Path
) -> list[dict[str, Any]]:
    """What a campaign on this group and interval may propose, best evidence first."""
    merged = load_library_report(home)
    if merged is None:
        return []
    best: list[dict[str, Any]] = []
    for report in merged["scans"]:
        if report.get("group") != group or report.get("interval") != interval:
            continue
        if horizon is not None and int(report.get("horizonBars") or 0) != int(horizon):
            continue
        if horizon is None and best:
            # Without an explicit horizon take the scan with the most validated
            # factors, then the largest sample: the strongest evidence available.
            current = entries_of(report)
            if len(current) <= len(best):
                continue
        best = entries_of(report)
    return best


def horizon_sensitivity(home: Path) -> dict[str, dict[str, Any]]:
    """Which factors were graded at more than one horizon, and how they moved.

    A factor that is significant at one holding period and reversed at another is not
    the same object as one that holds at both: the first may be a horizon the search
    happened to like. The scans are recorded per horizon, so this is a read over them
    rather than a claim, and it is attached to the library a campaign draws from.
    """
    merged = load_library_report(home)
    if merged is None:
        return {}
    per_factor: dict[str, dict[str, Any]] = {}
    for scan in merged["scans"]:
        horizon = int(scan.get("horizonBars") or 0)
        for evidence in scan.get("evidence", []):
            entry = per_factor.setdefault(
                evidence["factorId"], {"factorId": evidence["factorId"], "horizons": {}}
            )
            predictive = next(
                (gate for gate in evidence["gates"] if gate["name"] == "predictive"), {"metric": {}}
            )
            entry["horizons"][str(horizon)] = {
                "group": scan.get("group"),
                "interval": scan.get("interval"),
                "tier": tier_of(evidence),
                "ic": predictive["metric"].get("ic"),
                "pValue": predictive["metric"].get("pValue"),
                "netBps": next(
                    (gate["metric"].get("netBps") for gate in evidence["gates"]
                     if gate["name"] == "cost"),
                    None,
                ),
            }
    for entry in per_factor.values():
        signs = [
            (item["ic"] > 0) - (item["ic"] < 0)
            for item in entry["horizons"].values()
            if item["ic"] is not None
        ]
        tiers = {item["tier"] for item in entry["horizons"].values()}
        entry["signFlips"] = len(signs) > 1 and len(set(signs)) > 1
        entry["tierChanges"] = len(tiers) > 1
        entry["horizonSensitive"] = bool(entry["signFlips"] or entry["tierChanges"])
    return {key: value for key, value in per_factor.items() if len(value["horizons"]) > 1}


def summarise(report: dict[str, Any]) -> str:
    lines = []
    for scan in report["scans"]:
        entries = entries_of(scan)
        validated = [item for item in entries if item["tier"] == "validated"]
        candidates = [item for item in entries if item["tier"] == "candidate"]
        lines.append(
            f"{scan['group']}/{scan['interval']} 视野 {scan['horizonBars']} bar："
            f"{len(scan.get('evidence', []))} 个因子中已验证 {len(validated)} 个、候选 {len(candidates)} 个"
            f"（{'/'.join(scan.get('symbols', []))[:60]}）"
        )
        for item in entries[:10]:
            lines.append(
                f"  [{item['tier']:9s}] {item['factorId']:24s} "
                f"IC {item['ic'] if item['ic'] is None else round(item['ic'], 4)} "
                f"净 {item['netBps'] if item['netBps'] is None else round(item['netBps'], 2)} bps"
            )
    return "\n".join(lines)
