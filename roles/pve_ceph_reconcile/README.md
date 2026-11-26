# pve_ceph_reconcile

Phase C/D. Idempotent reconciliation of a bootstrapped Ceph cluster into its intended shape: device class, class-scoped CRUSH rule, autotuning, production pools, and CephFS.

Unlike `pve_ceph_bootstrap`, this role is **safe to re-run**. The CLI tasks read current state first (`ceph osd crush rule ls`, `ceph fs ls`, `ceph config get`) and only write when the desired state is missing; the module tasks are idempotent by `state: present`. A converged cluster produces no changes.

## Module vs CLI surface

Two operations are `community.proxmox` modules, run from the controller against the PVE API (`delegate_to: localhost`): the **pool create** (`proxmox_ceph_pool`) and the **MDS create** (`proxmox_ceph_mds`, looping the node list). Everything else stays CLI because no module (or no module argument) covers it:

- **CRUSH rule create** (`ceph osd crush rule create-replicated`) and the **OSD device-class reclass** fallback — `device_class.yml`.
- **Autotuning** (`ceph config set/get`, `ceph balancer`) — `tuning.yml`.
- **Pool compression** (`ceph osd pool set … compression_*`) — `proxmox_ceph_pool` has no compression parameter, so this stays a read-before-write CLI task that runs **after** the module creates the pool.
- **CephFS** (`pveceph fs create`, the pool→rule bind, and the `pvesm set --content` task) — `cephfs.yml`.

### PVE API connection

Same contract as `pve_ceph_bootstrap`, wired at the play level (`playbooks/16-ceph.yml` `module_defaults` for `group/community.proxmox.proxmox`): `pve_api_host`, `pve_api_user`, `pve_api_token_id`, `pve_api_token_secret` (vault/`-e`), `pve_api_validate_certs`. The play asserts `pve_api_token_secret` is non-empty. Requires `community.proxmox >= 2.0.0` and `proxmoxer >= 2.3.0` on the controller.

## What It Does

1. **Device class (C1).** Reclasses each OSD that is not already on `pve_ceph_device_class` (`nvme`) — the drives auto-detect as `ssd` behind the MegaRAID — then creates the class-scoped replicated rule `repl-nvme-host` with a host failure domain.
2. **Autotuning (C2).** `osd_memory_target_autotune false`, `osd_memory_target 8 GiB` (both `osd` scope), balancer `upmap` + on, and `osd_pool_default_pg_autoscale_mode on` (`global` scope). Scopes follow the hand-run script exactly.
3. **Pools (C5/C6).** Creates the production RBD pools via `proxmox_ceph_pool` (`add_storages: true`): `nvme` (no compression) and `nvme-compressed` (zstd/aggressive), both size 3 / min_size 2, bound to the class rule. Compression is a CLI follow-up (no module parameter).
4. **CephFS (D1/D2).** MDS on the first three nodes via `proxmox_ceph_mds`, then CLI `pveceph fs create --name cfs01 --add-storage 1`, binds the two `cfs01_*` pools to the class rule, and sets the PVE storage content to `iso,vztmpl,snippets,backup`.

## Execution Model

Cluster-wide `ceph`/`pveceph`/`pvesm` CLI commands run `run_once`, delegated to the deterministic first node (`groups['pve'] | sort | first`). The `proxmox_ceph_pool` and `proxmox_ceph_mds` module tasks run from the controller against the PVE API (`delegate_to: localhost`); the MDS task loops the first three nodes with `node: {{ item }}`.

> **`application` (RBD tag) — confirm during live validation.** `proxmox_ceph_pool` has no `application` parameter (the old `pveceph pool create` passed `--application rbd`). Confirm the created pools carry the `rbd` application tag (`ceph osd pool application get <pool>`); if not, add `ceph osd pool application enable <pool> rbd` as a CLI follow-up.

## Key Variables

- `pve_ceph_device_class` (`nvme`), `pve_ceph_crush_rule` (`repl-nvme-host`), `pve_ceph_crush_failure_domain` (`host`).
- `pve_ceph_osd_memory_target_autotune` (`false`), `pve_ceph_osd_memory_target` (`8589934592`), `pve_ceph_balancer_mode` (`upmap`), `pve_ceph_pg_autoscale_default` (`on`).
- `pve_ceph_pools` — list of `{name, compression_algorithm, compression_mode}`; `pve_ceph_pool_size` (3), `pve_ceph_pool_min_size` (2), `pve_ceph_pool_pg_autoscale_mode` (`on`, per-pool autoscaler passed to the module).
- `pve_ceph_fs_name` (`cfs01`), `pve_ceph_fs_content` (`iso,vztmpl,snippets,backup`).
- PVE API: `pve_api_host`, `pve_api_user`, `pve_api_token_id`, `pve_api_token_secret`, `pve_api_validate_certs` (wired via play `module_defaults`).

## Report

Emits a change report through `tools/report.py` (`ceph-reconcile-change.json` → Markdown + HTML), with verification facts for health, CRUSH rule presence, pool presence, CephFS presence, and device-class membership.
