# pve_ceph_bootstrap

Phase B. One-shot bootstrap of a Ceph Tentacle cluster on top of an existing Proxmox VE cluster: package install, `pveceph init`, monitors, managers, and OSDs.

This role is **not idempotent** and is **human-gated**. It refuses to run unless `pve_ceph_bootstrap_confirm: true` is passed explicitly. Run it once, against a freshly clustered, dataless fleet. Reconciliation and day-2 shape live in `pve_ceph_reconcile`.

## Module vs CLI surface

Monitors and managers are created through the `community.proxmox` modules (`proxmox_ceph_mon`, `proxmox_ceph_mgr`), idempotent (`state: present`), run from the controller against the PVE API (`delegate_to: localhost`, looping the node list). **OSD create stays CLI** — `pveceph osd create <by-id> --crush-device-class <class>` — because `proxmox_ceph_osd` only accepts the unstable kernel `/dev/sdX` and rejects the stable `/dev/disk/by-id` link, which is unsafe for a destructive create (GAP-10 / upstream [#500](https://github.com/ansible-collections/community.proxmox/issues/500)). The package install (`pveceph install`), cluster init (`pveceph init`), the guarded OSD **device discovery** shell, the mon-quorum wait and the verification read also stay CLI.

The OSD create stamps the target CRUSH device class (`--crush-device-class {{ pve_ceph_device_class }}`), so OSDs land on the right class directly; `pve_ceph_reconcile`'s reclass is only a fallback.

### PVE API connection

The modules need the shared connection contract, wired at the play level (`playbooks/16-ceph.yml` `module_defaults` for `group/community.proxmox.proxmox`): `pve_api_host`, `pve_api_user` (`root@pam`), `pve_api_token_id` (`ansible`), `pve_api_token_secret` (vault/`-e` secret), `pve_api_validate_certs` (`true`, pinned to the PVE cluster CA via `pve_api_ca_path`). The play asserts `pve_api_token_secret` is non-empty before running. Requires `community.proxmox >= 2.0.0` and `proxmoxer >= 2.3.0` on the controller (see `requirements.yml`).

### OSD `dev`: by-id (resolved — stays CLI)

The discovery shell reports each eligible disk by its stable `/dev/disk/by-id/` link, and OSD create uses that link (`pveceph osd create <by-id>`). The `proxmox_ceph_osd` module could not be used for this: its `check_dev` validates `dev` against the PVE `disks/list` `devpath` (the kernel `/dev/sdX`) and rejects by-id (verified on the live cluster). `/dev/sdX` is not stable across reboots/rescans, so it is unsafe for a destructive create. Filed upstream as [#500](https://github.com/ansible-collections/community.proxmox/issues/500); until the module accepts a stable identifier, OSD create is intentionally CLI.

## Safety Model

- **Human gate.** `pve_ceph_bootstrap_confirm` defaults to `false`; the role asserts it is true before doing anything, the same shape as `pve_firewall`'s datacenter enable.
- **Guarded OSD selection.** A disk becomes an OSD only when its model contains `pve_ceph_osd_disk_model_match` **and** its size exceeds `pve_ceph_osd_min_size_bytes` **and** it has no mountpoint **and** it is not already an OSD. It is then addressed by its `/dev/disk/by-id/` link. The boot disk is a different model and is mounted, so it is excluded by construction.
- **Ordering.** `init` runs on the first node only, then the role waits for `/etc/pve/ceph.conf` to replicate to every node before creating mons; mons are created sequentially and the role gates on full quorum before continuing.

## Execution Model

All orchestration runs `run_once` from the deterministic first node (`groups['pve'] | sort | first`). Cluster-wide `ceph`/`pveceph` commands are delegated to that node; `pveceph install`, the OSD **device discovery** shell, and `pveceph osd create <by-id>` are delegated to each specific node. The mon/mgr **create** module tasks run from the controller against the PVE API (`delegate_to: localhost`), looping the node list with `node: {{ item }}`.

## Preserved Gotchas

- **apt confirm prompt.** `pveceph install` shells to apt, which prompts even under `DEBIAN_FRONTEND=noninteractive`. The role drops `APT::Get::Assume-Yes "true";` into `/etc/apt/apt.conf.d/` for the install and removes it afterwards (`always`, so a failed install cannot leave it behind).
- **ceph.conf propagation.** `/etc/pve` is the replicated pmxcfs; the role waits for `ceph.conf` on every node before creating mons.
- **Device class.** The NVMe drives present as `/dev/sdX` behind the MegaRAID, so Ceph would auto-detect class `ssd`. The OSD module sets `crush_device_class: nvme` at create time, so the OSDs land on the right class directly; `pve_ceph_reconcile`'s reclass remains as a fallback for OSDs made before this was wired in.

## Required Inventory / Site Vars

- `pve_ceph_public_network` and `pve_ceph_cluster_network` (CIDRs) — empty by default; supply from site vars. Example values: public `198.51.100.64/28`, cluster `198.51.100.80/28`.
- The `pve` inventory group with at least three nodes.

## Key Variables

- `pve_ceph_bootstrap_confirm` (default `false`) — the human gate.
- `pve_ceph_release` (default `tentacle`), `pve_ceph_repository` (default `no-subscription`).
- `pve_ceph_mon_nodes` (default first three of `groups['pve'] | sort`) — mon/mgr/mds nodes.
- `pve_ceph_device_class` (`nvme`) — CRUSH class stamped on each OSD at create time.
- `pve_ceph_osd_disk_model_match` (`MTFDKCC`), `pve_ceph_osd_min_size_bytes` (`6000000000000`), `pve_ceph_osd_byid_glob` (`/dev/disk/by-id/scsi-2*`).
- PVE API: `pve_api_host`, `pve_api_user`, `pve_api_token_id`, `pve_api_token_secret`, `pve_api_validate_certs` (wired via play `module_defaults`).

## Report

Emits a change report through `tools/report.py` (`ceph-bootstrap-change.json` → Markdown + HTML in `reports_dir`), with verification facts for HEALTH, monitor quorum size, managers, and OSD up/in counts.
