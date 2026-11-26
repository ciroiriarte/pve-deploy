# pve-deploy

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Linting](https://github.com/ciroiriarte/pve-deploy/actions/workflows/lint.yml/badge.svg)](https://github.com/ciroiriarte/pve-deploy/actions/workflows/lint.yml)

`pve-deploy` is a reusable, product-grade Ansible repository designed to drive Proxmox VE deployments from bare-metal out-of-band hardware provisioning to a fully validated, production-ready cluster.

It provides structured, multi-tier automation intended for deployment engineers operating in high-reliability enterprise environments.

## Target Stack

- **Host Operating System:** Debian 13 (Trixie)
- **Hypervisor:** Proxmox VE 9.2
- **Distributed Storage:** Ceph Tentacle
- **Backup Infrastructure:** Proxmox Backup Server 4.2

## Two-Tier Architecture

To bridge the gap between unconfigured bare-metal hardware and a running hypervisor cluster, `pve-deploy` partitions execution into two isolated connection tiers:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          Control Host (Ansible)                         │
└────────────────────┬───────────────────────────────┬────────────────────┘
                     │                               │
       Tier A (OOB)  │                               │ Tier B (In-Band)
      Redfish / ASU  │                               │ SSH + REST API (pc)
                     ▼                               ▼
       ┌───────────────────────────┐   ┌───────────────────────────┐
       │   Baseboard Management    │   │  Proxmox VE Cluster Nodes │
       │     Controllers (BMC)     │   │      (Debian 13 OS)       │
       └───────────────────────────┘   └───────────────────────────┘
```

> [!WARNING]
> **TIER A EXECUTES HARDWARE-LEVEL REBOOTS AND RECONFIGURATIONS**
> Roles executing in Tier A interact directly with physical hardware BMCs (Redfish/Lenovo ASU/OneCLI). Modifications to BIOS, RAID arrays, or firmware will trigger physical host reboots and disrupt active execution if run against live hardware. Always verify target BMC inventories before execution.

### Tier A — Out-of-Band (OOB)
- **Scope:** Pre-OS bare-metal provisioning.
- **Connection:** `hosts: bmc`, `connection: local`.
- **Protocols & Tools:** DMTF Redfish REST APIs, Lenovo ASU / OneCLI.
- **Roles:** `bmc_inventory`, `bmc_config`, `bios_config`, `raid_config`, `os_install`.
- **Idempotency Lifecycle:** Every Tier A role strictly enforces:
  `Read Current State` → `Compute Diff` → `Apply Diff Only` → `Single Reboot (if diff exists)` → `Verify State`.

### Tier B — In-Band
- **Scope:** Operating system configuration, hypervisor initialization, clustering, storage, and networking.
- **Connection:** `hosts: pve` via SSH, supplemented by `delegate_to: localhost` tasks executing the `pc` REST client against the Proxmox cluster API.
- **Scope Guard:** Applying PVE playbooks assert that every target host is a member of the `pve` inventory group before making changes, preventing accidental modification of BMC, PBS, database, switch, firewall, or other roles during PVE deployment.
- **Roles:** `pve_repos`, `pve_baseline`, `pve_bootloader`, `pve_network`, `pve_firewall`, `pve_cluster`, `pve_ceph`, `pve_sdn`, `pve_storage_licensed`, `pve_images`, `pve_validate`, `pve_report`.

## Tool Division of Labour

| Tool / Layer | Responsibility | Execution Model |
| :--- | :--- | :--- |
| **Ansible** | Orchestration, inventory, state diffing, execution ordering, gating, change reporting. | Host controller playbook execution. |
| **`pc` CLI** ([`pve-cli`](https://github.com/ciroiriarte/pve-cli)) | Primary API verb layer for Proxmox VE REST API. Replaces `pvesh` over SSH. Employs `-o json` for state reads and `--wait` for asynchronous tasks. | Executed locally on control host (`delegate_to: localhost`). |
| **`community.proxmox`** | Declared dependency (`>= 2.0.0`, `proxmoxer >= 2.3.0`) — **not yet invoked by any role**; state mutation is currently hand-rolled via the `pc` CLI with Jinja read-back idempotency. Planned migration target for the covered state surface (ceph mon/mgr/mds/osd/pool, cluster, storage, firewall) — see [`docs/UPSTREAM-GAPS.md` → community.proxmox adoption](docs/UPSTREAM-GAPS.md#communityproxmox-adoption--the-cli-boundary). | Ansible against the API (`delegate_to: localhost` + API token). |
| **`pve-tools`** ([`pve-tools`](https://github.com/ciroiriarte/pve-tools)) | Specialized node-local operational scripts (`pve-import-cloud-images`, `pve-sdn-healthcheck`, `pve-rolling-upgrade`, `pve-ceph-upgrade`, `pve-vmnic-fix`). | Executed on PVE hypervisor hosts via SSH. |
| **Templated Files** | Non-API file surfaces (`/etc/network/interfaces`, `frr.conf.local`, `/etc/sysctl.d/*`, Corosync ring additions, raw `ceph.conf`, `proxmox-boot-tool`). | Pipelined SSH file deployment. |

## Repository Structure

```
.
├── .github/
│   └── workflows/
│       └── lint.yml          # CI linting for Ansible, YAML, and Python
├── docs/
│   ├── LAYOUTS.md            # Hardware, network, and storage topology matrix
│   ├── OPERATOR.md           # Deployment runbook, prerequisites, and gating
│   └── UPSTREAM-GAPS.md      # Gap tracking for upstream tooling
├── inventory/                # Generated and committed inventory files
├── playbooks/                # Sequential deployment playbooks (01 through 18)
├── roles/                    # Tier A and Tier B Ansible roles
├── tools/
│   └── sheet_sync.py         # Google Sheets inventory synchronization tool
├── ansible.cfg               # Ansible configuration
├── CONTRIBUTING.md           # Contribution guidelines
├── LICENSE                   # MIT License
├── requirements.yml          # Galaxy requirements and tooling pins
└── README.md
```

## Phase Ordering & Gating

Execution is structured into strict sequential playbooks. Each phase contains explicit validation gates that must pass prior to initiating the subsequent phase:

Phase numbers are banded by tier. `00`–`09` is Tier A, out-of-band and pre-OS;
`10`–`19` is Tier B, in-band against a running operating system. The gap between
the bands is deliberate — it leaves room to insert pre-OS phases without
renumbering in-band ones, and makes the tier of any phase readable from its
number alone.

```
Tier A (out-of-band)          Tier B (in-band)
00-firmware ──► 01-hardware ──► 02-install ──► 10-baseline ──► 11-network
                                                                    │
                                                             13-validate-net
                                                                    │
19-functional ◄── 18-images ◄── 17-sdn ◄── 16-ceph ◄── 15-firewall ◄── 14-cluster
```

**The firewall runs after the cluster, not before it.** The whole PVE firewall
lives in `/etc/pve` — `firewall/cluster.fw` for datacenter rules,
`nodes/<node>/host.fw` for node rules — and `man pvecm` is unambiguous:

> All existing configuration in `/etc/pve` is overwritten when joining a cluster.

Rules applied before clustering therefore survive only on the founding node;
every node that joins loses them at the moment it joins. `15-firewall.yml`
asserts cluster membership and refuses to run without it, because the failure is
otherwise invisible — a node looks firewalled right up until it joins and
silently reverts to no rules at all.

`13-validate-net.yml` also reports firewall state, which will read as unset when
it runs at its position above. Run it again after `15-firewall.yml` for a
verdict that covers the operational security state as well as the topology.

1. **`00-firmware.yml`** *(not implemented)*: Converges BMC, BIOS, RAID, NIC and drive firmware to a pinned per-model baseline. Runs **before** `01-hardware` because a flash renames BIOS attribute keys and resets settings to defaults — see the file header.
2. **`01-hardware.yml`**: Captures BMC hardware inventory, then audits — or converges — BIOS attributes and RAID topology against the site baseline. Runs read-only by default; see [Phase 01 apply gate](#phase-01-apply-gate).
3. **`02-install.yml`** *(not implemented)*: Installs Proxmox VE via Redfish virtual media onto the boot mirror `01-hardware` converged, then hands the running node to Tier B.
4. **`10-baseline.yml`**: Configures Debian/Proxmox/Ceph/OBS APT repositories, installs baseline packages, applies sysctl parameters, and converges bootloader policy to the preferred systemd-boot/UEFI state.
5. **`11-network.yml`**: Deploys bond interfaces, Linux bridges, and host IP parameters via templated `/etc/network/interfaces`.
6. **`13-validate-net.yml`**: Evaluates active/staged interface drift, LACP bond health, VLAN addresses, gateway reachability, ICMP/MTU paths across PVE VLANs, firewall state, and optional iperf3 mesh (`pve_validate_iperf_enabled=true`). Firewall state reads as unset here; re-run after `15-firewall.yml`.
7. **`14-cluster.yml`** *(not implemented)*: Creates Corosync cluster, joins secondary nodes, and provisions API tokens.
8. **`15-firewall.yml`**: Configures host and cluster firewall rules, then optionally enables the datacenter firewall. Runs **after** `14-cluster` — see the ordering note above; the playbook refuses to run on a node that is not yet a cluster member.
9. **`16-ceph.yml`**: Initializes Ceph Tentacle monitors, managers, OSDs, and CRUSH map rules.
10. **`17-sdn.yml`**: Configures Software-Defined Networking (VLAN/EVPN zones and VNets) via `pc`.
11. **`18-images.yml`**: Provisions cloud images, LXC templates, and storage pools using `pve-tools`.
12. **`19-functional.yml`**: Executes synthetic guest provisioning, storage replication, migration tests, and emits change reports.

### Phase 01 apply gate

Phase 01 is the only phase that both audits and applies from one playbook. That
is deliberate: for BIOS and RAID the audit *is* the apply path with its last step
withheld — read current state, compute the diff, report — so a separate audit
playbook could only ever drift from what apply actually sets.

| Mode | Invocation | Effect |
| :--- | :--- | :--- |
| Audit (default) | `ansible-playbook playbooks/01-hardware.yml` | Reads every BMC, diffs BIOS and storage against the baseline, writes evidence and an assessment report. Zero writes. |
| Apply | `... -e hardware_apply=true -e hardware_apply_confirm=<site>` | The same read and diff, then commits the diff, reboots once, verifies. |

Apply needs two keys, not one. `hardware_apply` alone is too easy to inherit
from a wrapper script, a copied group_var or a stale CI variable;
`hardware_apply_confirm` has to be typed on the command line and has to match
`pve_site_name`, so a run pointed at the wrong inventory fails closed instead of
reconfiguring another site's fleet. Pair it with `--limit`.

Two properties hold regardless of mode:

- **`bmc_inventory` never writes.** It is read-only by construction rather than
  by flag — inventory capture has no apply mode. That is what makes a
  default-mode run safe to repeat during BMC recovery, and why Phase 13 can rely
  on the evidence it leaves behind.
- **`--tags readonly` cannot mutate.** Apply tasks carry `apply` and never
  `readonly`, so the tag is load-bearing rather than decorative.

> [!NOTE]
> The gate is implemented; the apply paths behind it are not. `bios_config` and
> `raid_config` currently **fail** in apply mode rather than warn, so a run that
> asks to converge hardware cannot be mistaken for one that did. See
> `roles/*/tasks/apply_*.yml` for what each still needs.

## Quick Start

### 1. Install Dependencies
```bash
ansible-galaxy collection install -r requirements.yml
pip install proxmoxer>=2.3.0
```

### 2. Generate Inventory
Sync inventory from the master environment definition spreadsheet (commits output to git for offline execution):
```bash
python3 tools/sheet_sync.py --sheet-id <GOOGLE_SHEET_ID> --output inventory/hosts.yml
git add inventory/hosts.yml && git commit -m "docs(inventory): sync environment state"
```

### 3. Run Pre-Flight Hardware Audit
```bash
ansible-playbook playbooks/01-hardware.yml
```

### 4. Execute Full Deployment Sequence
```bash
ansible-playbook playbooks/site.yml
```

## Sibling Projects

- **`pc` (PVE CLI):** [github.com/ciroiriarte/pve-cli](https://github.com/ciroiriarte/pve-cli) — Remote-first Go REST client for PVE/PDM.
- **`pve-tools`:** [github.com/ciroiriarte/pve-tools](https://github.com/ciroiriarte/pve-tools) — Node-local helper scripts for Proxmox VE.
