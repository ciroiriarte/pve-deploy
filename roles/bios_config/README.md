# bios_config (Audit Mode)

The `bios_config` role audits live BIOS settings against a site-defined production baseline on Lenovo ThinkSystem servers via Redfish (Lenovo XCC).

## Absolute Constraint: Zero Writes

This role runs against live production nodes carrying real workloads. In audit mode, it:
- Performs **ZERO write operations** to BMCs or BIOS settings.
- Never reboots or stages changes.
- Operates cleanly under `--check` and always reports `changed=0`.

Mutating tasks are gated behind `when: not (bios_config_audit_only | bool)` and live in `tasks/apply_bios.yml`, outside the `readonly`-tagged block so that `--tags readonly` cannot select them. The `| bool` filter matters: a string-valued `bios_config_audit_only` is truthy in Jinja whatever it contains, so the bare form silently skips apply even when the operator asked for it.

`bios_config_audit_only` is normally set by `playbooks/01-hardware.yml` from its `hardware_apply` gate rather than by hand.

The apply path is **not implemented yet**. It `fail`s rather than warning, so a run that asked to converge BIOS cannot be mistaken for one that did; `tasks/apply_bios.yml` records what implementing it requires.

## Baseline Attributes (`defaults/main.yml`)

The role evaluates current BIOS settings against `bios_baseline`:

| Attribute Key | Baseline Value | Severity | Rationale |
|---------------|----------------|----------|-----------|
| `SecureBootConfiguration.SecureBootSetting` | `Disabled` | **CRITICAL** | Bootloader policy precondition (systemd-boot derivation) |
| `OperatingModes.ChooseOperatingMode` | `Maximum Performance` | Warning | Host performance baseline |
| `Processors.SEV-SNPSupport` | `Enabled` | Warning | AMD SEV-SNP confidential compute |
| `Processors.SVMMode` | `Enabled` | Warning | Hardware virtualization support |
| `Processors.SMTMode` | `Enabled` | Warning | Simultaneous multithreading |
| `Processors.IOMMU` | `Enabled` | Warning | PCI passthrough / SR-IOV requirement |
| `Processors.SEVControl` | `Enabled` | Warning | AMD SEV security control |
| `DevicesandIOPorts.SRIOV` | `Enabled` | Warning | Single Root I/O Virtualization |
| `Memory.NUMANodesperSocket` | `NPS1` | Warning | NUMA topology optimization |
| `Memory.Interleave` | `Enabled` | Warning | Memory channel interleaving |

## Structural Rationale & Critical Attributes

### 1. `SecureBootSetting: Disabled` (Critical Severity)
In Proxmox VE, bootloader selection is derived automatically during installation based on system state:
- **UEFI + ZFS/Btrfs Root + Secure Boot Disabled** $\rightarrow$ **`systemd-boot`**
- **UEFI + ZFS/Btrfs Root + Secure Boot Enabled** $\rightarrow$ **`GRUB`**

If Secure Boot is enabled on any node, Proxmox silently falls back to GRUB even with ZFS root. This produces divergent bootloaders and mismatched kernel-pinning procedures (`proxmox-boot-tool` vs `update-grub`) across nodes in a cluster. Drift on this key is classified as **CRITICAL**.

### 2. Per-Port SR-IOV Settings (`SRIOV: Enabled`)
Global SR-IOV enablement must be complemented by per-NIC configuration on Mellanox ConnectX-6 adapters, where each port exposes a `VirtualizationMode` attribute (desired: `SR-IOV`). Because attribute keys vary by card slot and firmware version, this is evaluated via a **best-effort pattern match** over BIOS attribute keys and marked as `needs verification against a live XCC`.

## Classification & Pending Values

Each baseline key is reported as:
- **`match`**: Applied BIOS setting equals baseline.
- **`drift`**: Applied setting differs from baseline (captures current vs desired).
- **`absent`**: Key is not present on this specific hardware model (expected across server types, not treated as a failure).

Pending/staged BIOS values (settings modified in XCC that take effect upon reboot) are tracked separately so that unapplied values are never reported as active state.

## Companion Evidence Integration

The role checks for pre-existing evidence written by `bmc_inventory` under `environments/{{ environment_name }}/evidence/{{ parent_host }}-hardware-*.json`. If present, it consumes that document; otherwise, it falls back to querying Redfish directly (`community.general.redfish_info` and XCC endpoints).

## Output Artifacts

1. JSON evidence document: `environments/{{ environment_name }}/evidence/{{ parent_host }}-bios-<timestamp>.json`
2. Human-readable Markdown summary: `reports/{{ parent_host }}-bios-drift.md`
3. Assessment JSON payload: `reports/{{ parent_host }}-bios-assessment.json` rendered via `tools/report.py`.
