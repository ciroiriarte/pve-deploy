#!/usr/bin/env python3
"""Evaluate Phase 13 measurements against per-site expectations.

A measurement without an expectation cannot fail, which is how 24 Gbit/s and
2 Gbit/s ended up sitting in the same column both marked pass. This applies
floors and ceilings so a measured check earns a verdict instead of merely
reporting a number.

Expectations live in a per-site targets file, alongside the existing
hardware-version-targets.json, because they are site facts: link rate depends on
the cabling, and an acceptable latency depends on what the segment carries. Role
defaults would have to be either wrong or uselessly permissive.

Deliberately advisory by default: a measurement below target produces a WARNING,
not a failure, unless the target sets "enforce": true. A capacity floor that
fails a deployment gate on a transient 3% dip is worse than no floor at all.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "pve.phase13.network_targets.v1"

# metric key -> (comparison, human phrasing). "floor" means higher is better.
METRIC_DIRECTION = {
    "goodput_mbps": "floor",
    "loss_pct": "ceiling",
    "jitter_ms": "ceiling",
    "latency_ms": "ceiling",
    "retransmits": "ceiling",
    # Offered load is an input we chose and a derived sum is not a measurement,
    # so neither is ever evaluated.
    "offered_mbps": None,
    "bidir_total_mbps": None,
    # flent's per-direction throughput is not evaluated either, for a different
    # reason: RRUL's fixed 4 streams per direction hash onto a single LACP member
    # 12.5% of the time, so a floor here fires at random on a healthy fabric. A
    # 40 Gbit/s floor would have warned on a quarter of all pairs. Capacity is
    # gated on iperf3 goodput_mbps, which uses enough streams to be reproducible.
    "download_mbps": None,
    "upload_mbps": None,
}


def load_targets(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("schema_version") not in (SCHEMA_VERSION, None):
        raise ValueError(f"{path}: unexpected schema_version {data.get('schema_version')!r}")
    return data


def expectations_for(targets: dict[str, Any], network: str, kind: str) -> dict[str, Any]:
    """Merge defaults with per-network then per-kind overrides, most specific winning."""
    merged: dict[str, Any] = dict(targets.get("defaults") or {})
    net = (targets.get("networks") or {}).get(network) or {}
    merged.update({k: v for k, v in net.items() if k != "kinds"})
    merged.update(((net.get("kinds") or {}).get(kind)) or {})
    return merged


def evaluate(payload: dict[str, Any], targets: dict[str, Any]) -> dict[str, Any]:
    """Annotate measured pairs with target verdicts. Mutates and returns payload."""
    enforce_default = bool(targets.get("enforce", False))
    applied = 0
    breaches: list[dict[str, Any]] = []

    for check in payload.get("checks", {}).get("pairs") or []:
        metrics = check.get("metrics") or {}
        if not metrics:
            continue
        exp = expectations_for(targets, check.get("network", ""), check.get("kind", ""))
        if not exp:
            continue
        enforce = bool(exp.get("enforce", enforce_default))
        verdicts = []
        for key, value in metrics.items():
            direction = METRIC_DIRECTION.get(key)
            if direction is None:
                continue
            target = exp.get(f"{key}_min" if direction == "floor" else f"{key}_max")
            if target is None:
                continue
            applied += 1
            ok = (float(value) >= float(target)) if direction == "floor" else (float(value) <= float(target))
            verdicts.append({
                "metric": key,
                "value": value,
                "operator": ">=" if direction == "floor" else "<=",
                "target": target,
                "ok": ok,
            })
            if not ok:
                breaches.append({
                    "id": check["id"], "metric": key, "value": value,
                    "operator": ">=" if direction == "floor" else "<=",
                    "target": target, "enforced": enforce,
                })
        if not verdicts:
            continue
        check["target_verdicts"] = verdicts
        failed = [v for v in verdicts if not v["ok"]]
        if failed and check["result"] == "pass":
            # Advisory unless the site asks for enforcement.
            check["result"] = "fail" if enforce else "warn"
            detail = ", ".join(
                f"{v['metric']} {v['value']} {v['operator']} {v['target']} not met" for v in failed
            )
            check["actual"] = f"{check.get('actual', '')} | target: {detail}"

    payload.setdefault("targets", {})
    payload["targets"].update({
        "applied_comparisons": applied,
        "breaches": breaches,
        "enforced": enforce_default,
    })

    # Recompute the summary: results changed above, and a stale summary that still
    # says 372/372 while a check now warns is worse than no evaluation at all.
    all_checks = list(payload.get("checks", {}).get("fleet") or [])
    for host_checks in (payload.get("checks", {}).get("nodes") or {}).values():
        all_checks.extend(host_checks)
    all_checks.extend(payload.get("checks", {}).get("pairs") or [])
    s = payload.setdefault("summary", {})
    s["total"] = len(all_checks)
    s["passed"] = sum(1 for c in all_checks if c["result"] == "pass")
    s["warned"] = sum(1 for c in all_checks if c["result"] == "warn")
    s["failed"] = sum(1 for c in all_checks if c["result"] == "fail")
    s["failed_checks"] = [c["id"] for c in all_checks if c["result"] == "fail"]
    s["warned_checks"] = [c["id"] for c in all_checks if c["result"] == "warn"]
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", help="environment.json to annotate in place")
    parser.add_argument("--targets-file", help=f"per-site targets ({SCHEMA_VERSION})")
    args = parser.parse_args(argv)

    try:
        targets = load_targets(args.targets_file)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        sys.stderr.write(f"ERROR: {e}\n")
        return 2

    if not targets:
        print("no network targets configured; measurements left unevaluated")
        return 0

    path = Path(args.payload)
    payload = json.loads(path.read_text(encoding="utf-8"))
    evaluate(payload, targets)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    t = payload["targets"]
    print(f"applied {t['applied_comparisons']} target comparison(s); "
          f"{len(t['breaches'])} breach(es)"
          f"{' (enforced)' if t['enforced'] else ' (advisory)'}")
    return 1 if any(b["enforced"] for b in t["breaches"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
