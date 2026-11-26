# Upstream tooling gaps

Things `pve-deploy` needs that `pc` (pve-cli), `pve-tools`, `community.proxmox` or the Proxmox VE API
cannot do yet. Each gap is worked around locally, filed upstream where we own the code, and retired
once a release lands.

**Rule:** every row carries either a real issue link or the literal text `not filed yet`. Never invent
an issue number or a version — a tracking table full of plausible dead links is worse than an empty
one.

## Minimum versions pinned

| Tool | Minimum | Latest released | Notes |
| :--- | :--- | :--- | :--- |
| `pc` (pve-cli) | v0.13.0 | v0.13.0 | Primary API verb layer. Pre-1.0, so CLI surface and JSON output may change between minors. |
| `pve-tools` | v1.11.0 | v1.11.0 | Bundle version; individual scripts carry their own `VERSION`. |
| `community.proxmox` | 2.0.0 | 2.0.0 | Requires `proxmoxer >= 2.3.0`. |

## Gaps

| ID | Gap | Phase | Upstream | Issue | Local workaround | Retire when |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| GAP-01 | No curated command group for SDN **fabric** objects (`/cluster/sdn/fabrics`, new in PVE 9.2 — OpenFabric/OSPF/BGP underlay). | `16-sdn` | [pve-cli](https://github.com/ciroiriarte/pve-cli) | not filed yet | `pc api POST /cluster/sdn/fabrics` with a templated JSON payload. | `pc sdn fabric` exists and is released. |
| GAP-02 | No `pveceph init` equivalent — no `proxmox_ceph_init` module. `community.proxmox` covers `proxmox_ceph_mon/mgr/mds/osd/pool`, but those all operate on an **already-initialised** Ceph cluster: they need `ceph.conf` (fsid + public/cluster networks) to exist first, and only `pveceph init` / `POST /nodes/{node}/ceph/init` creates it. (This is *Ceph* init specifically — **PVE cluster** create/join is covered by `proxmox_cluster` with `link0`/`link1`, so it is **not** part of this gap.) | `16-ceph` | [community.proxmox](https://github.com/ansible-collections/community.proxmox) | not filed yet | `pc api` (or `pveceph init` over SSH) against `/nodes/{node}/ceph/init`, with a read-back state comparison supplying idempotency. | A `proxmox_ceph_init` module ships. |
| GAP-09 | No module for `pveceph install` — installing the Ceph packages on a node is apt-level and must precede every `proxmox_ceph_*` module. | `16-ceph` | [community.proxmox](https://github.com/ansible-collections/community.proxmox) | not filed yet | Gated `pveceph install --version <release> --repository no-subscription` over SSH (`APT::Get::Assume-Yes` drop-in for the run). | An install/package module ships (unlikely — treat as near-permanent, package-manager territory). |
| GAP-12 | `proxmox_firewall` does not meet our functional requirements for rule management, so `pve_firewall` **keeps its recipe**. (1) Idempotency is **positional** — `compare_list_of_dicts(existing, new, uid="pos")` identifies rules by position, not by a stable key; our recipe keys on **comment**, which survives reorders and out-of-band additions. (2) **No check-mode** (`supports_check_mode=False`) — cannot dry-run against an enforcing firewall. (3) It merges but never deletes, so it is neither a clean full-declarative manager nor a robust additive one. All 21 live datacenter rules are pve-deploy-owned, so declarative management is *possible*, but the positional + no-check-mode design is a regression from the comment-keyed `pc api` approach. | `15-firewall` | [community.proxmox](https://github.com/ansible-collections/community.proxmox) | [#501](https://github.com/ansible-collections/community.proxmox/issues/501) (comment match), [#502](https://github.com/ansible-collections/community.proxmox/issues/502) (check_mode) | **Keep the recipe** — `pve_firewall`'s `pc api` comment-keyed additive rule management (idempotent by stable comment, dry-runnable via read-then-diff). Firewall options (enable/policy) stay on the same path. | The module gains (a) comment-based rule identity/idempotency and (b) check-mode. Both are worth proposing as upstream enhancement PRs (see below). |
| GAP-11 | `pve_cluster` cannot migrate to `proxmox_cluster` — **Day-0 auth ordering**. The module authenticates to the API (token or `api_password`), but at cluster-formation time (phase 14) no API token exists yet (token creation is a post-cluster step) and the root password is already rotated to a hash-only (phase 10), so there is no credential to present. Additionally, `proxmox_cluster`'s **join requires a password** (`payload["password"] = master_api_password`; no token option), whereas the role deliberately joins over **SSH-key trust** (`pvecm add --use_ssh 1`) for exactly that reason. Cluster create is also an irreversible one-shot that cannot be validated on a live cluster. | `14-cluster` | [community.proxmox](https://github.com/ansible-collections/community.proxmox) | [#503](https://github.com/ansible-collections/community.proxmox/issues/503) (join auth) | Cluster create/join stay CLI (`pvecm create --link0` / `pvecm add --use_ssh 1`), with the network-safety asserts, dry-run gate, and quorum waits the role already carries. | The module accepts token auth for join AND the pipeline provisions a Day-0 token before cluster formation (a re-architecture — treat as near-permanent). |
| GAP-10 | `proxmox_ceph_osd` accepts only the **kernel devpath** (`/dev/sdX`): its `check_dev()` matches `dev` against the PVE `disks/list` `devpath` and rejects `/dev/disk/by-id` links (verified in the 2.0.0 source). `/dev/sdX` is **not stable** across reboots/rescans, so feeding it to a destructive OSD create risks targeting the wrong disk. | `16-ceph` | [community.proxmox](https://github.com/ansible-collections/community.proxmox) | [#500](https://github.com/ansible-collections/community.proxmox/issues/500) | **OSD create stays CLI**: `pveceph osd create <by-id> --crush-device-class <class>` (the stable link the hand-run bootstrap used; `pveceph` resolves it safely). A guarded host-side shell selects devices by model+size+no-mountpoint+not-already-OSD and resolves the by-id link. mon/mgr/mds/pool DID migrate to modules — only OSD create is held back. | The module accepts a stable identifier (by-id/WWN) or resolves it internally before `check_dev`. |
| GAP-03 | No Windows template builder. Planned as a new self-contained bash script `pve-build-windows-template` — no Packer. Media and product keys stay operator-supplied: unlike the Linux cloud images, Windows ISOs may not be fetched, cached or redistributed, so the `pve-import-cloud-images` scan-the-mirror model cannot be reused. Rationale and interface sketch are in the upstream issue. | `17-images` | [pve-tools](https://github.com/ciroiriarte/pve-tools) | [pve-tools#1](https://github.com/ciroiriarte/pve-tools/issues/1) | None. Windows templates are built by hand until this exists. | `pve-build-windows-template` ships in `pve-tools`. |
| GAP-04 | The network API accepts only `bond_mode`, `bond_xmit_hash_policy`, `bond-primary` and `slaves`. It does **not** accept `bond-miimon` or `bond-lacp-rate`, both of which production configs require. | `11-network` | Proxmox VE | upstream limitation, not filed | `/etc/network/interfaces` is a templated file applied over SSH and reloaded with `ifreload -a`. | The endpoint accepts full LACP tuning. Unlikely — treat as permanent. |
| GAP-05 | No API for FRR at all. PVE generates `/etc/frr/frr.conf` from SDN objects; anything beyond that lives in `frr.conf.local`, which is node-local and must be distributed by us. | `16-sdn` | Proxmox VE | upstream limitation, not filed | Templated `frr.conf.local` over SSH, verified with `vtysh -c 'show running-config'` after apply — an exit code is not proof, since FRR 10.x can silently drop an unreadable local config. | An FRR configuration endpoint exists. Treat as permanent. |
| GAP-06 | No "apply all pending updates" endpoint; `POST /nodes/{node}/apt/update` only refreshes the package index. | `10-baseline` | Proxmox VE | upstream limitation, not filed | Gated `dist-upgrade` over SSH. | An upgrade endpoint exists. Treat as permanent. |
| GAP-07 | `proxmox-auto-install-assistant` **exits 0 on failure**, in at least two subcommands. `validate-answer` prints `Error: Found issues in the answer file.` to stderr and returns 0, so an exit-code check on it can never fail. `prepare-iso` does the same. With `xorriso` absent it prints `Error: Could not find the 'xorriso' binary. Please install it.` and returns 0, having written no ISO. Reproduced on assistant 9.2.7 / Debian 13. `xorriso` is a runtime dependency the package does not declare, so a clean install of the assistant cannot build an ISO and says nothing about it. | `02-install` | Proxmox VE | upstream limitation, not filed | `os_install` never trusts the exit code. `prepare_iso.yml` stats the output path and fails with the assistant's own stdout when the file is absent, then runs `inspect-iso` and asserts on the `Auto-install:` and `Fetch mode:` lines. | `prepare-iso` returns non-zero on failure **and** the package depends on `xorriso`. |
| GAP-08 | Lenovo XCC does not implement the DMTF `VirtualMedia.InsertMedia` action. All ten slots (`EXT1-4`, `Remote1-4`, `RDOC1-2`) report `Actions: []` on firmware KAX368P 6.20, so standards-only virtual-media code finds nothing to call. | `02-install` | Lenovo XCC | vendor limitation, not filed | `roles/os_install/tasks/media/lenovo_xcc_mount.yml` drives the OEM `LenovoRemoteMapService`: POST the image into `MountImages`, then call `.Mount`, which takes no parameters. Verification is on resulting state, because `.Mount` answers 204 even when it mounted nothing. | XCC exposes `InsertMedia`; a `media/virtual_media_mount.yml` backend then replaces it. |

## community.proxmox adoption & the CLI boundary

**Current state (2026-08-15): the collection is declared but unused.** `requirements.yml`
pins `community.proxmox >= 2.0.0` and the README lists it, but **no role invokes a single
module**. All state mutation is hand-rolled: `pc api` / `pveceph` / `pvecm` / `pvesh` over SSH,
with idempotency reconstructed in Jinja by reading current state and diffing. Live validation of
the Ceph roles surfaced three bugs that are direct symptoms of that hand-rolled layer (a
non-JSON `ceph osd crush class ls-osd`, an invalid `ceph config get global` entity, and a
non-idempotent `pvesm set`) — each would have been the module's problem, not ours.

**Decision: hybrid.** Use `community.proxmox` modules for the covered *state* surface; keep CLI
only for a small, well-defined Day-0 island. This is a tool refactor (the already-deployed
cluster is unaffected) and must be validated on a lab/scratch target — `proxmox_ceph_osd` create,
`proxmox_cluster` create, etc. are destructive and cannot be exercised on production.

### What genuinely must stay CLI (the real Day-0 island)

The boundary is narrower than "all of Day-0". Only these are truly uncovered:

- **`pveceph install`** — no module; apt-level (GAP-09).
- **`pveceph init` / `ceph.conf`** — no `proxmox_ceph_init`; the ceph modules need it first (GAP-02).
- **First API-token provisioning** — the modules authenticate to the API from the controller
  (`api_password` *or* token). A fresh node has the root@pam password, so this is soft, but if we
  standardise on tokens (best practice), creating the first one is a `pveum`/`pvesh` step.
- **OS-level prep outside PVE's API domain** — network (`/etc/network/interfaces`, GAP-04),
  bootloader, kernel, RAID/disk partitioning, `frr.conf.local` (GAP-05).
- **Low-level Ceph tuning with no module argument** — OSD device-class reclass, `ceph config`
  autotuning, and the benchmark (`rados bench`, fio).
- **OSD *create*** — `proxmox_ceph_osd` accepts only the unstable kernel `/dev/sdX` and rejects
  `/dev/disk/by-id` (GAP-10, upstream [#500](https://github.com/ansible-collections/community.proxmox/issues/500)).
  Destructive create by an unstable handle is unsafe, so it stays `pveceph osd create <by-id>`.
  The other ceph objects (mon/mgr/mds/pool) DID migrate.
- **PVE cluster create/join** — a *hard* blocker, not judgement (GAP-11). `proxmox_cluster` exists,
  but at cluster-formation time there is no API credential to authenticate with (no token yet, root
  password rotated to a hash), and its **join requires a password** while the role joins over
  SSH-key trust (`--use_ssh 1`). Plus it is the irreversible one-shot that cannot be validated on a
  live cluster. Stays `pvecm create/add`.

### What is module-capable (migration targets, post-`ceph.conf`)

Migrated: `proxmox_ceph_mon` · `proxmox_ceph_mgr` · `proxmox_ceph_mds` · `proxmox_ceph_pool`.
Available but no role uses it yet: `proxmox_storage` (no standalone storage role today).
Held back (module does not meet our functional requirements — **we keep the recipe**):
`proxmox_ceph_osd` create (by-id safety, GAP-10), `proxmox_cluster` (Day-0 auth ordering +
SSH-trust join, GAP-11), and `proxmox_firewall` / firewall options (positional idempotency +
no check-mode, GAP-12).

**Guiding principle:** migrate to a `community.proxmox` module only where it satisfies our
functional requirements (stable idempotency, safe validation, no capability regression);
otherwise keep the existing recipe and record the gap. Wave 1 (ceph mon/mgr/mds/pool) met the
bar; OSD-create, cluster, and firewall did not.

### Candidate upstream enhancement PRs

Worth proposing to `community.proxmox` so more of the surface can migrate later:

- **`proxmox_ceph_osd`: accept a stable device identifier** (`/dev/disk/by-id`, WWN) — filed as
  [#500](https://github.com/ansible-collections/community.proxmox/issues/500) (GAP-10).
- **`proxmox_firewall`: comment-based rule identity/idempotency** — match rules by a stable key
  (e.g. `comment`) in addition to `pos`, so automation that coexists with reordering does not
  churn or mis-update (GAP-12) — [#501](https://github.com/ansible-collections/community.proxmox/issues/501).
- **`proxmox_firewall`: `check_mode` support** — predict changes without mutating an enforcing
  firewall (GAP-12) — [#502](https://github.com/ansible-collections/community.proxmox/issues/502).
- **`proxmox_cluster`: token (or SSH-key) auth for join** — join without the master's root password
  (GAP-11) — [#503](https://github.com/ansible-collections/community.proxmox/issues/503).

### Migration constraints & sequencing

- **Execution-model shift is the real cost.** Roles run `root` over SSH driving CLI on the node;
  modules run from the **controller against the API (`:8006`) via `proxmoxer` + API tokens**. That
  needs `module_defaults` (`api_host`, token, `validate_certs`), a token lifecycle, cert/CA
  handling, and `delegate_to: localhost`. Do not mix "runs on node via SSH" with API modules.
- **Stage it, idempotency-gated.** Read-only parity first → low-risk Ceph (pool, mon/mgr/mds) →
  OSD → cluster/storage. Every migrated task must: change as expected on run 1, report no change
  on run 2, and match an independent `ceph -f json` / `pvesh` read-back.
- **Firewall last.** `proxmox_firewall` advertises **no check-mode**, and rule ordering/digest is
  delicate — migrate it only after the rest is proven, backed by explicit read-back validation.
- **Retire `pc` where modules cover it.** `pc` is a pre-1.0 in-house REST CLI (a maintenance and
  bus-factor liability); anything a module handles should stop going through `pc`, with `pvesh`
  reserved for the genuine gaps above.

## Filing checklist

When a gap is filed upstream, replace `not filed yet` with the issue URL in the same commit that
touches the workaround, so the workaround and its exit condition never drift apart.
