# pve_repos

Configures APT repositories for Proxmox VE 9.2 on Debian 13 Trixie using the deb822 `.sources` format.

## Architectural Decisions
- **Debian Trixie Format**: Uses deb822 `.sources` files for Debian, Proxmox VE, Ceph, and OBS repositories. Legacy `.list` files are explicitly purged where the role owns the source.
- **Ceph Target**: Defaults to `ceph-tentacle` for PVE 9.2.
- **OBS URL Fidelity**: OBS-backed tools (`pve-cli`, `pve-tools`, `nic-xray`) use the documented OBS download URL layout and matching release key paths.
- **Proxmox Repository Fidelity**: With no subscription key, PVE is written to `/etc/apt/sources.list.d/proxmox.sources` with `URIs: http://download.proxmox.com/debian/pve` and `Components: pve-no-subscription`; Ceph no-subscription uses `http://download.proxmox.com/debian/<ceph-release>`. With a subscription key, the enterprise paths use `https://enterprise.proxmox.com/...`.
- **Package Installation Should Work**: The baseline does not impose a narrow package allow-list. Repository health is validated with `apt-get update`, then required operational packages are installed normally.
- **Full Upgrade in Phase 10**: After repository convergence, Phase 10 applies pending package upgrades by default with APT dist-upgrade semantics (`pve_repos_apply_full_upgrade: true`) so nodes do not remain behind after the baseline run.
- **IPv4 Acquire Preference**: The role may set an APT acquire preference for IPv4 when the environment's IPv6/CDN path is unreliable. This is a client-side repository reachability guard, not a firewall policy and not host-wide IPv6 disablement.
- **PVE API Integration**: Attempts configuration via `pc api` tool when `pve_repos_use_api` is true, falling back to declarative deb822 templates when custom third-party sources or control-plane bootstrapping require disk templates.

## Requirement Tags
- `phase01`
- `pve_repos`

## Variables
- `pve_subscription_key`: Subscription key string. Defaults to empty (selects `pve-no-subscription`).
- `pve_ceph_archive`: Ceph archive version (default `ceph-tentacle`).
- `pve_apt_suite`: Debian suite (default `trixie`).
- `pve_repos_apply_full_upgrade`: Apply all pending package upgrades after repository convergence using dist-upgrade semantics (default `true`). Set to `false` only for a deliberate repository-only dry convergence where package levels must not change.
