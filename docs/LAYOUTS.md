# Architectural Layouts & Topology Matrix

This document defines supported architecture layouts for `pve-deploy`, detailing how each mode is activated via inventory variables, bootloader policies, and hardware-specific constraints.

## Topology Selection Matrix

Layouts are selected by configuring variables within `inventory/group_vars/all.yml` or specific host group definitions.

| Layout Dimension | Supported Options | Primary Inventory Variables |
| :--- | :--- | :--- |
| **Storage Architecture** | Ceph Tentacle (Hyper-converged) vs. External (SAN/iSCSI/NFS/PBS) | `ceph_enabled: true \| false`<br>`storage_backend: "ceph" \| "external"` |
| **SDN Fabric** | Linux VLAN-backed vs. BGP EVPN / VXLAN Fabric | `sdn_mode: "vlan" \| "evpn"`<br>`sdn_bgp_as: 65000` |
| **Node Roles** | Hyper-converged (Compute+Storage) vs. Dedicated Storage | `node_role: "hyperconverged" \| "compute" \| "storage"` |
| **Host Root Storage** | ZFS Mirror vs. Hardware RAID vs. Single Disk (ext4/xfs) | `root_storage_type: "zfs_mirror" \| "hw_raid" \| "single_disk"` |

### Choosing the root storage type

Follow the boot media, not a blanket preference:

- **Hardware RAID controller present** → `hw_raid`. The controller owns the
  mirror and presents one virtual disk; PVE installs a **single ZFS disk** on
  top. Do **not** stack a ZFS mirror over a hardware-mirrored volume — the
  redundancy already exists one layer down, and doing both halves usable
  capacity while giving ZFS no independent devices to repair from.
- **No hardware RAID (plain HBA or onboard NVMe)** → `zfs_mirror` across two
  devices, so `proxmox-boot-tool` keeps both ESPs in sync.

A node reporting a single root device is therefore **not automatically a
redundancy gap** — check whether that device is a hardware RAID virtual disk
first. A common case: two NVMe boot devices sit behind the controller in one
RAID slot and are presented to the OS as a single volume, so `hw_raid` with a
single ZFS disk is the correct and intended layout, not a missing mirror.

---

## Bootloader Selection Policy

> [!IMPORTANT]
> **SYSTEMD-BOOT / UEFI IS THE DEFAULT BOOTLOADER POLICY FOR PROXMOX VE 9.2**

`pve-deploy` converges PVE nodes toward `systemd-boot` on UEFI systems, managed by `proxmox-boot-tool`. This is the preferred state for supported UEFI Proxmox VE 9 deployments unless a documented host-specific exception requires GRUB.

### Operational Rules

1. **UEFI + Proxmox boot tooling available** $\rightarrow$ **`systemd-boot` / `proxmox-boot-tool`**.
2. **Actual GRUB drift on a node that should be UEFI/systemd-boot** $\rightarrow$ automatic migration is enabled by default (`pve_bootloader_migrate: true`).
3. **EFI System Partition selection** defaults to `pve_esp_partition: "auto"`; the role discovers the ESP before any migration.
4. **Already-compliant UEFI nodes** may still be maintained: stale `proxmox-boot-uuids` entries are cleaned and refreshed without formatting an ESP or rebooting.
5. **GRUB exceptions** must be explicit and documented per host. Do not infer a GRUB exception merely because the node boots from hardware RAID; a hardware RAID virtual disk can still host a ZFS root and UEFI/systemd-boot layout.

### Compliance Detection

PVE 9 may report the compliant state as `uefi` rather than the literal string `systemd-boot`. The role treats any of these `proxmox-boot-tool status` outputs as compliant preferred state:

- `systemd-boot`
- `configured with: uefi`
- `System currently booted with uefi`

### Hardware RAID Note

Hardware RAID affects the root storage topology, not the bootloader decision by itself. Where mirrored boot devices are presented by the controller as one virtual disk, PVE uses a single ZFS root on that volume. That layout remains compatible with the preferred UEFI/systemd-boot policy.

---

## Hardware Constraint: Mellanox ConnectX-6 NICs & Bridge VLAN Filtering

When deploying network configurations on hosts equipped with Mellanox ConnectX-6 (or ConnectX-5) Network Interface Cards, strict hardware table limitations apply regarding VLAN filtering on Linux Bridges (`vmbr`).

### ConnectX-6 Hardware Limitation:
- ConnectX-6 embedded switches (eSwitch) enforce a hard limit of **512 virtual port (vport) VLAN filter entries** per physical interface.
- Configuring a Linux Bridge with standard broad VLAN ranges (e.g., `bridge-vlan-aware yes` with `bridge-vids 2-4094`) exceeds internal hardware table capacities, causing driver warnings, dropped packets, or kernel panics under heavy network event churn.

### Mandatory Configuration Rules:
1. **Narrow VLAN ID Ranges:** `bridge-vids` definitions **must** remain strictly limited to explicitly required VLAN IDs (e.g., `10 20 30-40`).
2. **Prohibited Ranges:** Never set `bridge-vids 2-4094` or provide unconstrained ranges on ConnectX-6 interfaces.

#### Valid Inventory Example (`inventory/host_vars/pve-01.yml`):
```yaml
network_interfaces:
  - name: bond0
    type: bond
    slaves: [enp133s0f0, enp133s0f1]
    bond_mode: 802.3ad
    bond_miimon: 100
    bond_lacp_rate: fast

  - name: vmbr0
    type: bridge
    ports: [bond0]
    vlan_aware: true
    # NARROW RANGE ENFORCED: Must not exceed 512 total entries
    vlan_ids: "10 20 100-105"
```

---

## Sample Inventory Layout Combinations

### Scenario A: Hyper-Converged Ceph + BGP EVPN
```yaml
# inventory/group_vars/all.yml
ceph_enabled: true
storage_backend: "ceph"
sdn_mode: "evpn"
sdn_bgp_as: 65000
root_storage_type: "zfs_mirror"
```

### Scenario B: Dedicated Compute + External iSCSI/NFS + VLAN SDN
```yaml
# inventory/group_vars/all.yml
ceph_enabled: false
storage_backend: "external"
sdn_mode: "vlan"
root_storage_type: "hw_raid" # Hardware RAID virtual disk; bootloader remains policy-driven, normally systemd-boot on UEFI
```
