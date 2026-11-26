#!/usr/bin/env python3
"""Validate fleet-wide software/firmware version consistency from local evidence.

This tool is intentionally read-only. It consumes the BMC hardware evidence JSON
written by Phase 01 plus optional target versions, and emits report.py-compatible
validation checks for Phase 13.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

Check = dict[str, str]

NIC_FW_RE = re.compile(
    r"^(?P<prefix>.+?Slot(?P<slot>\d+)PhysicalPort(?P<port>\d+)LogicalPort(?P<logical>\d+))_FamilyFirmwareVersion$"
)
NIC_DEVICE_RE = re.compile(
    r"^(?P<prefix>.+?Slot(?P<slot>\d+)PhysicalPort(?P<port>\d+)LogicalPort(?P<logical>\d+))_DeviceName$"
)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def latest_evidence(evidence_dir: Path, host: str) -> Path | None:
    matches = sorted(evidence_dir.glob(f"{host}-hardware-*.json"))
    return matches[-1] if matches else None


def clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def add_component(components: dict[str, dict[str, set[str]]], component: str, host: str, value: Any) -> None:
    """Record one observed version for (component, host).

    Values accumulate into a set rather than overwriting. A component key such as
    nic:<device> deliberately drops slot/port so the same adapter model is compared
    across the fleet, which means one host contributes several readings for it --
    eight, for a pair of dual-port ConnectX-6 adapters. Assigning would keep only
    whichever port happened to be last in JSON order and silently hide a port left
    on older firmware, in the tool whose whole purpose is finding that.
    """
    value_s = clean(value)
    if value_s:
        components[component][host].add(value_s)


def controller_key(ctrl: Mapping[str, Any], index: int) -> str:
    model = clean(ctrl.get("Model")) or clean(ctrl.get("Name")) or f"controller-{index}"
    slot = clean((ctrl.get("Location") or {}).get("Info"))
    return f"hba:{model}{(' @ ' + slot) if slot else ''}"


def drive_key(drive: Mapping[str, Any], index: int) -> str:
    raw = drive.get("raw") if isinstance(drive.get("raw"), Mapping) else drive
    model = clean(raw.get("Model")) or clean(raw.get("Name")) or clean(drive.get("model")) or f"drive-{index}"
    subsystem = clean(drive.get("subsystem"))
    return f"disk:{model}{(' @ ' + subsystem) if subsystem else ''}"


def drive_firmware(drive: Mapping[str, Any]) -> str:
    candidates = [
        drive.get("FirmwareVersion"),
        drive.get("firmware_version"),
        drive.get("firmware"),
    ]
    raw = drive.get("raw")
    if isinstance(raw, Mapping):
        candidates.extend([raw.get("FirmwareVersion"), raw.get("firmware_version"), raw.get("Revision"), raw.get("RevisionNumber")])
    for candidate in candidates:
        value = clean(candidate)
        if value:
            return value
    return ""


def extract_components(host: str, evidence: Mapping[str, Any]) -> tuple[dict[str, dict[str, set[str]]], list[str]]:
    components: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    missing: list[str] = []

    add_component(components, "bios:system", host, (evidence.get("system") or {}).get("bios_version"))
    add_component(components, "bmc:xcc", host, (evidence.get("bmc") or {}).get("firmware_version"))

    bios_attrs = evidence.get("bios_attributes") or {}
    nic_devices: dict[str, str] = {}
    if isinstance(bios_attrs, Mapping):
        for key, value in bios_attrs.items():
            m_dev = NIC_DEVICE_RE.match(str(key))
            if m_dev:
                nic_devices[m_dev.group("prefix")] = clean(value)
        found_nic_fw = False
        for key, value in bios_attrs.items():
            m_fw = NIC_FW_RE.match(str(key))
            if not m_fw:
                continue
            found_nic_fw = True
            prefix = m_fw.group("prefix")
            device = nic_devices.get(prefix, "NIC")
            # Compare by device class, not by MAC/slot, so every equivalent NIC model
            # across the fleet must have the same firmware.
            add_component(components, f"nic:{device}", host, value)
        if not found_nic_fw:
            missing.append("nic firmware: no *_FamilyFirmwareVersion BIOS attributes found")
    else:
        missing.append("nic firmware: bios_attributes missing")

    storage = evidence.get("storage") or {}
    controllers = storage.get("controllers") or [] if isinstance(storage, Mapping) else []
    if controllers:
        for idx, ctrl in enumerate(controllers):
            if isinstance(ctrl, Mapping):
                add_component(components, controller_key(ctrl, idx), host, ctrl.get("FirmwareVersion") or ctrl.get("firmware_version"))
    else:
        missing.append("hba firmware: storage.controllers missing or empty")

    drives = storage.get("physical_drives") or [] if isinstance(storage, Mapping) else []
    found_drive_fw = False
    if drives:
        for idx, drive in enumerate(drives):
            if not isinstance(drive, Mapping):
                continue
            fw = drive_firmware(drive)
            if fw:
                found_drive_fw = True
                add_component(components, drive_key(drive, idx), host, fw)
        if not found_drive_fw:
            missing.append("disk firmware: physical drives present but no firmware/revision field exposed in evidence")
    else:
        missing.append("disk firmware: storage.physical_drives missing or empty")

    return components, missing


def merge_components(dst: dict[str, dict[str, set[str]]], src: dict[str, dict[str, set[str]]]) -> None:
    for component, values_by_host in src.items():
        for host, values in values_by_host.items():
            dst[component][host].update(values)


def target_for(component: str, targets: Mapping[str, Any]) -> str:
    if not targets:
        return ""
    if component in targets:
        return clean(targets[component])
    category, _, name = component.partition(":")
    category_targets = targets.get(category)
    if isinstance(category_targets, str):
        return category_targets
    if isinstance(category_targets, Mapping):
        for key in (name, "default", "*"):
            if key in category_targets:
                return clean(category_targets[key])
    aliases = {
        "bios:system": ["bios_version", "bios"],
        "bmc:xcc": ["bmc_firmware", "bmc"],
    }
    for alias in aliases.get(component, []):
        if alias in targets:
            return clean(targets[alias])
    return ""


def make_check(name: str, command: str, expected: str, actual: str, ok: bool) -> Check:
    return {
        "name": name,
        "command": command,
        "expected": expected,
        "actual": actual,
        "result": "pass" if ok else "fail",
    }


def validate(args: argparse.Namespace) -> dict[str, Any]:
    hosts = [h for h in args.hosts if h]
    evidence_dir = Path(args.evidence_dir)
    targets: Mapping[str, Any] = {}
    if args.targets_file:
        targets = load_json(Path(args.targets_file))

    all_components: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    checks: list[Check] = []
    artifacts: list[dict[str, str]] = []

    for host in hosts:
        path = latest_evidence(evidence_dir, host)
        if not path:
            checks.append(make_check(
                f"hardware_evidence_present on {host}",
                f"find {evidence_dir} -name '{host}-hardware-*.json' | sort | tail -1",
                "latest per-host BMC hardware evidence JSON exists",
                "missing",
                False,
            ))
            continue
        artifacts.append({"host": host, "path": str(path), "description": "Latest BMC hardware evidence JSON used for version validation"})
        evidence = load_json(path)
        status = clean((evidence.get("collection") or {}).get("status"))
        checks.append(make_check(
            f"hardware_evidence_status on {host}",
            f"jq .collection.status {path}",
            "success",
            status or "missing",
            status == "success",
        ))
        components, missing = extract_components(host, evidence)
        merge_components(all_components, components)
        for item in missing:
            checks.append(make_check(
                f"hardware_version_field_present {item} on {host}",
                f"jq hardware version fields {path}",
                "required firmware/version field is present in BMC evidence",
                item,
                False,
            ))

    for component in sorted(all_components):
        values_by_host = all_components[component]
        missing_hosts = [h for h in hosts if not values_by_host.get(h)]
        actual = "; ".join(
            f"{h}=" + ("/".join(sorted(values_by_host[h])) if values_by_host.get(h) else "missing")
            for h in hosts
        )

        # A host reporting more than one version for the same component means the
        # devices behind that key disagree with each other -- for example one NIC
        # port left on older firmware. That is a real inconsistency and must fail
        # on its own, not be averaged away by the fleet comparison below.
        split_hosts = sorted(h for h in hosts if len(values_by_host.get(h, ())) > 1)
        if split_hosts:
            checks.append(make_check(
                f"intra_host_version_consistency {component}",
                f"validate_fleet_versions.py --component {component}",
                "one version per host for this component",
                "; ".join(f"{h}=" + "/".join(sorted(values_by_host[h])) for h in split_hosts),
                False,
            ))

        fleet_values = {v for values in values_by_host.values() for v in values}
        checks.append(make_check(
            f"fleet_version_consistency {component}",
            f"validate_fleet_versions.py --component {component}",
            "same non-empty version on every PVE node",
            actual,
            not missing_hosts and not split_hosts and len(fleet_values) == 1,
        ))
        wanted = target_for(component, targets)
        if wanted:
            checks.append(make_check(
                f"fleet_version_target {component}",
                f"validate_fleet_versions.py --target {component}={wanted}",
                wanted,
                actual,
                not missing_hosts and fleet_values == {wanted},
            ))

    return {
        "checks": checks,
        "failed_checks": [c["name"] for c in checks if c["result"] != "pass"],
        "artifacts": artifacts,
        # Sets are not JSON-serialisable; emit deterministic sorted lists.
        "components": {
            component: {host: sorted(values) for host, values in sorted(by_host.items())}
            for component, by_host in sorted(all_components.items())
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--hosts", nargs="+", required=True)
    parser.add_argument("--targets-file", help="Optional JSON target-version map")
    args = parser.parse_args(argv)
    result = validate(args)
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 1 if result["failed_checks"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
