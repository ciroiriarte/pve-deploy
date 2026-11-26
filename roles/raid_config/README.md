# raid_config (Audit Mode)

The `raid_config` role discovers storage topology on Lenovo ThinkSystem hardware via Redfish (Lenovo XCC) and derives `ceph_osd_candidates` for Proxmox VE Ceph deployment.

## Absolute Constraint: Zero Writes

This role runs against live production nodes carrying real workloads. In audit mode, it:
- Performs **ZERO write operations** to RAID controllers or physical drives.
- Never creates, deletes, or modifies virtual disks or controller modes.
- Operates cleanly under `--check` and always reports `changed=0`.

Mutating tasks are gated behind `when: not (raid_config_audit_only | bool)` and live in `tasks/apply_raid.yml`, outside the `readonly`-tagged block so that `--tags readonly` cannot select them. The `| bool` filter matters: a string-valued `raid_config_audit_only` is truthy in Jinja whatever it contains, so the bare form silently skips apply even when the operator asked for it.

`raid_config_audit_only` is normally set by `playbooks/01-hardware.yml` from its `hardware_apply` gate rather than by hand.

The apply path is **not implemented yet**. It `fail`s rather than warning, so a run that asked to converge storage cannot be mistaken for one that did; `tasks/apply_raid.yml` records what implementing it requires — including refusing to run at all when `boot_device_confident` is false.

## Storage Architecture & Safety Rules

### Rule 1: Ceph Requires Direct HBA / JBOD Mode (No Hardware RAID)
Ceph BlueStore requires direct, unmediated access to physical block devices via an HBA or controller operating in IT/JBOD mode.
- A controller configured in RAID mode presenting single-drive RAID-0 virtual disks for OSDs is a **finding** (non-compliant configuration).
- The role explicitly detects and reports hardware RAID-0 wrappers as non-compliant findings.

### Rule 2: Conservative Boot Device Protection
The OS boot device must **never** be included in `ceph_osd_candidates`.
- The role inspects virtual disk names (e.g. `VD_BOOT`, `OS`) and dedicated physical slot mirrors.
- **Safety Rule**: If the boot device cannot be identified with high confidence, every potential candidate drive is marked as **`unverified`** and highlighted loudly in the report, preventing accidental destruction of a live node in subsequent deployment steps.

### Rule 3: OS Disk Redundancy Reporting
The role reports whether the OS installation resides on a mirrored volume (RAID1/RAID10) or a single drive.

A single root **device** is not by itself a redundancy gap. Where a hardware RAID
controller presents a mirrored pair as one virtual disk, the operating system
correctly sees one disk and the redundancy lives in the controller. That is the
intended layout wherever hardware RAID is available, and a ZFS mirror must **not**
be stacked on top of it: the redundancy already exists one layer down, and doing
both halves usable capacity while giving ZFS no independent devices to repair
from. Only report missing redundancy when the root device is a bare physical
drive with no RAID volume behind it.

A common case: two NVMe boot devices sit behind the controller in one RAID slot
and are presented to the OS as a single volume, so a single-ZFS-disk root on
such a node is correct rather than a deviation.

## Output Artifacts

1. JSON evidence document: `environments/{{ environment_name }}/evidence/{{ parent_host }}-storage-<timestamp>.json`
2. Human-readable Markdown report: `reports/{{ parent_host }}-raid-summary.md`
3. Assessment JSON payload: `reports/{{ parent_host }}-storage-assessment.json` rendered via `tools/report.py`.
```
