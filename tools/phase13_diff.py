#!/usr/bin/env python3
"""Compare two Phase 13 environment payloads by check id.

Diffing rendered Markdown answers the wrong question: it reports that a latency
figure moved by 0.03ms and buries the one probe that went from pass to fail.
Check ids are stable across runs, so comparison happens on identity and the
output is the set an operator acts on -- what broke, what recovered, what
appeared, what silently stopped being checked.

That last category matters most. A check that vanishes between runs looks like
success everywhere else: the totals go down, nothing is red, and coverage was
quietly lost.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "pve.phase13.network_validation.v2"

# Measurements drift between runs, so a raw numeric comparison reports noise and
# buries real regression. A change counts only when it clears BOTH an absolute
# and a relative gate, and the direction that matters differs per metric:
# throughput regresses downward, loss and latency regress upward.
#   key: (direction, absolute_gate, relative_gate_fraction)
# Deliberately NOT gated: flent's download_mbps and upload_mbps. RRUL runs a fixed
# 4 streams per direction, and on a 2-member LACP bond the chance all 4 hash to
# one link is 2*(1/2)^4 = 12.5% per direction. Ephemeral ports re-roll that hash
# every run, so those figures legitimately halve and double between runs with
# nothing having changed -- gating them produced 35 "regressions" in one clean
# run. Capacity regression is detected on iperf3 goodput_mbps instead, which is
# reproducible now that it uses enough streams to occupy every bond member.
# flent's contribution is latency_ms, which is stable to a few hundredths of a ms.
METRIC_GATES: dict[str, tuple[str, float, float]] = {
    "goodput_mbps": ("down", 1000.0, 0.10),
    "loss_pct": ("up", 0.1, 0.25),
    "jitter_ms": ("up", 1.0, 0.25),
    "latency_ms": ("up", 2.0, 0.25),
    "retransmits": ("up", 100.0, 0.50),
}


def _metric_value(raw: Any) -> float | None:
    if isinstance(raw, dict):
        raw = raw.get("value")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def compare_metrics(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Report only metric moves that clear both gates in the regressing direction."""
    out: list[dict[str, Any]] = []
    b_m, a_m = before.get("metrics") or {}, after.get("metrics") or {}
    for key, gate in METRIC_GATES.items():
        direction, abs_gate, rel_gate = gate
        b, a = _metric_value(b_m.get(key)), _metric_value(a_m.get(key))
        if b is None or a is None:
            continue
        delta = a - b
        worse = delta < 0 if direction == "down" else delta > 0
        if not worse:
            continue
        if abs(delta) < abs_gate:
            continue
        if b and (abs(delta) / abs(b)) < rel_gate:
            continue
        out.append({
            "metric": key, "before": b, "after": a,
            "delta": round(delta, 3),
            "pct": round((delta / b) * 100, 1) if b else None,
        })
    return out


def load(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{path}: expected {SCHEMA_VERSION}, got {data.get('schema_version')!r}")
    return data


def flatten(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    checks = payload.get("checks") or {}
    items: list[dict[str, Any]] = list(checks.get("fleet") or [])
    for host_checks in (checks.get("nodes") or {}).values():
        items.extend(host_checks)
    items.extend(checks.get("pairs") or [])
    return {c["id"]: c for c in items if c.get("id")}


def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    a, b = flatten(old), flatten(new)
    a_ids, b_ids = set(a), set(b)

    def entry(cid: str, src: dict[str, dict[str, Any]]) -> dict[str, Any]:
        c = src[cid]
        return {"id": cid, "result": c.get("result"), "actual": c.get("actual")}

    changed = []
    actual_only = []
    measurement_regressions: list[dict[str, Any]] = []
    for cid in sorted(a_ids & b_ids):
        before, after = a[cid], b[cid]
        moves = compare_metrics(before, after)
        if moves:
            measurement_regressions.append({"id": cid, "moves": moves})
        if before.get("result") != after.get("result"):
            changed.append({
                "id": cid,
                "from": before.get("result"),
                "to": after.get("result"),
                "actual": after.get("actual"),
            })
        elif str(before.get("actual") or "") != str(after.get("actual") or ""):
            actual_only.append({"id": cid, "result": after.get("result"),
                                "before": before.get("actual"), "after": after.get("actual")})

    regressions = [c for c in changed if c["to"] != "pass"and c["from"] == "pass"]
    recoveries = [c for c in changed if c["to"] == "pass" and c["from"] != "pass"]
    other = [c for c in changed if c not in regressions and c not in recoveries]

    return {
        "old_run": (old.get("run") or {}).get("id"),
        "new_run": (new.get("run") or {}).get("id"),
        "regressions": regressions,
        "recoveries": recoveries,
        "result_changed_other": other,
        "new_checks": [entry(cid, b) for cid in sorted(b_ids - a_ids)],
        "missing_checks": [entry(cid, a) for cid in sorted(a_ids - b_ids)],
        "measurement_regressions": measurement_regressions,
        "actual_changed": actual_only,
        "totals": {"old": len(a_ids), "new": len(b_ids)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old")
    parser.add_argument("new")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--show-actual-changes", action="store_true",
                        help="Also list checks whose result held but whose output text moved")
    args = parser.parse_args(argv)

    try:
        result = diff(load(args.old), load(args.new))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        sys.stderr.write(f"ERROR: {e}\n")
        return 2

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Phase 13 diff: {result['old_run']} -> {result['new_run']}")
        print(f"  checks: {result['totals']['old']} -> {result['totals']['new']}")
        for label, key in (
            ("REGRESSIONS (pass -> not pass)", "regressions"),
            ("RECOVERIES (not pass -> pass)", "recoveries"),
            ("OTHER RESULT CHANGES", "result_changed_other"),
            ("NEW CHECKS", "new_checks"),
            ("MISSING CHECKS (coverage lost)", "missing_checks"),
        ):
            rows = result[key]
            print(f"\n{label}: {len(rows)}")
            for row in rows:
                if "from" in row:
                    print(f"  {row['id']}  {row['from']} -> {row['to']}")
                    if row.get("actual"):
                        print(f"      {row['actual']}")
                else:
                    print(f"  {row['id']}  ({row.get('result')})")
        rows = result["measurement_regressions"]
        print(f"\nMEASUREMENT REGRESSIONS (past both gates): {len(rows)}")
        for row in rows:
            print(f"  {row['id']}")
            for m in row["moves"]:
                pct = f" ({m['pct']:+.1f}%)" if m["pct"] is not None else ""
                print(f"      {m['metric']}: {m['before']} -> {m['after']}{pct}")

        if args.show_actual_changes:
            rows = result["actual_changed"]
            print(f"\nOUTPUT CHANGED, RESULT HELD: {len(rows)}")
            for row in rows:
                print(f"  {row['id']}\n      before: {row['before']}\n      after:  {row['after']}")

    # Regressions and lost coverage both mean "do not ship this run".
    return 1 if (result["regressions"] or result["missing_checks"]
                 or result["measurement_regressions"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
