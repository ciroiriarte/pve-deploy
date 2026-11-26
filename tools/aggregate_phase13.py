#!/usr/bin/env python3
"""Aggregate Phase 13 per-host fragments into one environment-wide report payload.

Phase 13 used to render one report per node, which meant the fleet-wide firmware
checks were recomputed and stored N times (verified byte-identical across all four
node reports on the reference fleet), and a reader had to open N files to judge one
cluster. This builds a single v2 payload instead, with every check classified by
scope and given a stable id so two runs can be diffed by identity rather than by
line.

Scopes:
  fleet  one result for the whole cluster (firmware consistency)
  node   a property of one host (bonds, VLAN addresses, gateway, firewall)
  pair   a directional probe, source host -> target host, per network segment

Input fragments are the raw check lists each host produced; the fleet fragment is
the output of validate_fleet_versions.py, run once.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "pve.phase13.network_validation.v2"

# "<item> on <host>" is how the role names every node-local check.
NODE_SUFFIX_RE = re.compile(r"^(?P<item>.+?)\s+on\s+(?P<host>\S+)$")
# Longest-first so a longer prefix is not shadowed by a shorter one: iperf3_ must
# be tried before iperf_, or an iperf3 check falls through to node scope and its
# source/target are lost.
PAIR_PREFIXES = ("iperf3_", "icmp_", "mtu_", "iperf_", "flent_")

# iperf3 embeds the protocol between the prefix and the network
# (iperf3_tcp_<network>_<src>_to_<dst>). Split it out rather than letting "tcp"
# become part of the network name.
PAIR_PROTOCOLS = ("tcp", "udp")
FLEET_PREFIXES = ("fleet_version_", "intra_host_version_consistency")


def slug(value: str) -> str:
    """Reduce a free-form label to a stable id fragment."""
    return re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")


def normalise_result(result: str | None) -> str:
    value = (result or "").strip().lower()
    if value in ("pass", "passed", "ok", "success"):
        return "pass"
    if value in ("warn", "warning"):
        return "warn"
    return "fail"


def split_pair(name: str, hosts: list[str]) -> tuple[str, str, str] | None:
    """Split 'icmp_<network>_<src>_to_<dst>' into (network, src, dst).

    Host names contain dashes and networks contain underscores, so the boundary is
    found by matching against the known host list rather than by counting
    separators, which would misparse 'pve-01'.
    """
    for prefix in PAIR_PREFIXES:
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix):]
        if "_to_" not in rest:
            return None
        left, _, dst = rest.rpartition("_to_")
        for host in sorted(hosts, key=len, reverse=True):
            if left.endswith("_" + host):
                return left[: -(len(host) + 1)], host, dst
        # Unknown source host: keep the check rather than dropping it.
        return left, "", dst
    return None


def classify(check: dict[str, Any], hosts: list[str]) -> dict[str, Any]:
    """Attach scope, stable id and scope-specific fields to one raw check."""
    name = str(check.get("name", "")).strip()
    result = normalise_result(check.get("result"))
    base = {
        "name": name,
        "command": check.get("command"),
        "expected": check.get("expected"),
        "actual": check.get("actual"),
        "result": result,
    }
    # Carried at every scope, not just for pairs. Node-scope checks measure too --
    # bond byte distribution, NIC steering rate -- and dropping their metrics here
    # left the numbers stranded in the human-readable `actual` string, invisible to
    # the renderer's metric columns, to phase13_diff's gates and to the target
    # evaluator. Optional throughout: most checks assert rather than measure.
    if check.get("metrics"):
        base["metrics"] = check["metrics"]

    if name.startswith(FLEET_PREFIXES):
        component = name.split(" ", 1)[1] if " " in name else name
        return {**base, "scope": "fleet", "id": f"fleet.{slug(component)}", "component": component}

    pair = split_pair(name, hosts)
    if pair:
        network, src, dst = pair
        kind = next(p[:-1] for p in PAIR_PREFIXES if name.startswith(p))
        protocol = ""
        for candidate in PAIR_PROTOCOLS:
            if network.startswith(candidate + "_"):
                protocol = candidate
                network = network[len(candidate) + 1:]
                break
        entry = {
            **base,
            "scope": "pair",
            "id": f"pair.{kind}{'.' + protocol if protocol else ''}"
                  f".{slug(network)}.{slug(src)}.to.{slug(dst)}",
            "kind": kind,
            "network": network,
            "source": src,
            "target": dst,
        }
        if protocol:
            entry["protocol"] = protocol
        # Characterisation tools (iperf3, flent) may also produce output files
        # alongside the numbers. Kept optional: reachability probes have neither.
        if check.get("artifacts"):
            entry["artifacts"] = check["artifacts"]
        return entry

    m = NODE_SUFFIX_RE.match(name)
    if m:
        item, host = m.group("item"), m.group("host")
        return {**base, "scope": "node", "id": f"node.{slug(host)}.{slug(item)}", "host": host, "item": item}

    return {**base, "scope": "node", "id": f"node.{slug(name)}", "host": "", "item": name}


def build_variance(node_checks: list[dict[str, Any]], hosts: list[str]) -> list[dict[str, Any]]:
    """Collapse per-node checks into one row per property across the fleet.

    Thirty nodes each rendering "Firewall runtime state 1/1" is thirty tables that
    say nothing. One row per property, with the deviating hosts named, puts the
    anomaly where the eye lands.
    """
    by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for check in node_checks:
        by_item[check.get("item") or check["name"]].append(check)

    rows: list[dict[str, Any]] = []
    for item, entries in sorted(by_item.items()):
        passing = [e for e in entries if e["result"] == "pass"]
        deviations = [
            {"host": e.get("host", ""), "result": e["result"], "actual": e.get("actual")}
            for e in entries
            if e["result"] != "pass"
        ]
        # Report the dominant expectation so a reader sees the fleet norm, not just a ratio.
        norms = Counter(str(e.get("expected") or "") for e in entries)
        rows.append({
            "item": item,
            "norm": norms.most_common(1)[0][0] if norms else "",
            "observed": len(entries),
            "passed": len(passing),
            "hosts_total": len(hosts),
            "deviations": sorted(deviations, key=lambda d: d["host"]),
            "result": "pass" if not deviations else "fail",
        })
    return rows


def build(fragments: list[dict[str, Any]], fleet_payload: dict[str, Any] | None,
          run: dict[str, Any], hosts: list[str]) -> dict[str, Any]:
    raw: list[dict[str, Any]] = []
    reported_hosts: set[str] = set()
    for frag in fragments:
        raw.extend(frag.get("checks") or [])
        if frag.get("host"):
            reported_hosts.add(frag["host"])
    if fleet_payload:
        raw.extend(fleet_payload.get("checks") or [])

    classified = [classify(c, hosts) for c in raw]

    # Every scope is deduplicated by id, not just fleet. Ids are the addressing
    # scheme the diff tool compares on, so a payload whose totals exceed its unique
    # ids reports coverage it cannot actually address: the extra copies are
    # invisible to any run-over-run comparison. A disagreement between copies is
    # itself a finding, so the worst result wins rather than whichever fragment
    # happened to be read last.
    severity = {"pass": 0, "warn": 1, "fail": 2}
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: dict[str, int] = {}
    for check in classified:
        cid = check["id"]
        kept = by_id.get(cid)
        if kept is None:
            by_id[cid] = check
            continue
        duplicate_ids[cid] = duplicate_ids.get(cid, 1) + 1
        if severity[check["result"]] > severity[kept["result"]]:
            by_id[cid] = check

    deduped = list(by_id.values())
    fleet = [c for c in deduped if c["scope"] == "fleet"]
    pairs = [c for c in deduped if c["scope"] == "pair"]
    node_all = [c for c in deduped if c["scope"] == "node"]

    nodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for check in node_all:
        nodes[check.get("host") or "unassigned"].append(check)

    networks: list[str] = sorted({p["network"] for p in pairs if p.get("network")})

    def tally(items: list[dict[str, Any]]) -> dict[str, int]:
        counts = Counter(i["result"] for i in items)
        return {
            "total": len(items),
            "passed": counts.get("pass", 0),
            "warned": counts.get("warn", 0),
            "failed": counts.get("fail", 0),
        }

    # Tally the retained set, not `classified`: the latter still holds every
    # duplicated fleet copy, which would inflate the totals the report leads with.
    retained = fleet + node_all + pairs
    failed_ids = [c["id"] for c in retained if c["result"] == "fail"]
    warned_ids = [c["id"] for c in retained if c["result"] == "warn"]

    return {
        "schema_version": SCHEMA_VERSION,
        "run": run,
        "cluster": {"hosts": hosts, "networks": networks},
        "summary": {
            **tally(retained),
            "by_scope": {"fleet": tally(fleet), "node": tally(node_all), "pair": tally(pairs)},
            "failed_checks": failed_ids,
            "warned_checks": warned_ids,
            "duplicate_ids": duplicate_ids,
        },
        "checks": {
            "fleet": sorted(fleet, key=lambda c: c["id"]),
            "nodes": {h: sorted(nodes[h], key=lambda c: c["id"]) for h in sorted(nodes)},
            "pairs": sorted(pairs, key=lambda c: c["id"]),
        },
        # Derived from the fragments actually received, NOT from checks.nodes.
        # The fleet validator emits "hardware_evidence_status on <host>" for every
        # host in the cluster, and those classify as node scope -- so a host that
        # never ran still appears in checks.nodes and a partial run looked
        # complete, which is the exact failure this field exists to prevent.
        "reported_hosts": sorted(reported_hosts),
        "variance": build_variance(node_all, hosts),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fragment-dir", required=True, help="Directory of per-host fragment JSON files")
    parser.add_argument("--fleet-file", help="validate_fleet_versions.py output, computed once")
    parser.add_argument("--hosts", nargs="+", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--environment", default="production")
    parser.add_argument("--site", default="default")
    parser.add_argument("--tool", default="pve-deploy 13-validate-net.yml")
    parser.add_argument("--started-at", default="")
    parser.add_argument("--out", required=True, help="Path to write the environment payload")
    args = parser.parse_args(argv)

    frag_dir = Path(args.fragment_dir)
    fragments: list[dict[str, Any]] = []
    for path in sorted(frag_dir.glob("*.json")):
        try:
            fragments.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            sys.stderr.write(f"ERROR: unreadable fragment {path}: {e}\n")
            return 2
    if not fragments:
        sys.stderr.write(f"ERROR: no per-host fragments found in {frag_dir}\n")
        return 2

    fleet_payload = None
    if args.fleet_file and Path(args.fleet_file).exists():
        try:
            fleet_payload = json.loads(Path(args.fleet_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            sys.stderr.write(f"ERROR: unreadable fleet payload {args.fleet_file}: {e}\n")
            return 2

    hosts = [h for h in args.hosts if h]
    payload = build(
        fragments,
        fleet_payload,
        {
            "id": args.run_id,
            "environment": args.environment,
            "site": args.site,
            "tool": args.tool,
            "started_at": args.started_at,
            "hosts_reporting": len(fragments),
        },
        hosts,
    )

    missing = [h for h in hosts if h not in set(payload.get("reported_hosts") or [])]
    if missing:
        # Surfaced rather than silently rendering a partial cluster as healthy.
        payload["summary"]["missing_hosts"] = missing

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    s = payload["summary"]
    print(f"aggregated {s['total']} checks from {len(fragments)} host(s): "
          f"{s['passed']} pass, {s['warned']} warn, {s['failed']} fail -> {out}")
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
