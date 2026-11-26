# pve_network

Renders `/etc/network/interfaces` from spreadsheet-generated inventory and applies changes safely using `ifreload`.

## Source of truth

The role consumes generated host variables from the live spreadsheet:

- Cabling tab `OS Device` -> Linux interface name (`ens13f0np0`, etc.).
- Cabling tab `Aggregation` -> desired bond name (`bond0`..`bond3`).
- Logical Networking Table -> host IPs, VLAN IDs, CIDRs, gateways, and MTUs.

Do not hard-code physical interface names in this role. Populate the spreadsheet first, run `tools/sheet_sync.py sync --env <site> --apply --yes`, and then run Phase 11.

## Architectural Decisions

- **SSH File Templating**: PVE API (`POST /nodes/{node}/network`) lacks support for mandatory parameters `bond-miimon` and `bond-lacp-rate`. Thus, full network configuration is managed via SSH file rendering.
- **Cabling-Driven Bonds**: Bond members come from Cabling `OS Device + Aggregation`, so per-host PCI/slot naming differences are represented in inventory rather than role defaults.
- **ConnectX-6 Bridge VLAN Limits**: ConnectX-6 NICs feature a 512-entry vport limit. Setting broad ranges (for example `2-4094`) can trigger driver/kernel instability. `vmbr1` uses an explicit narrow VLAN list.
- **Private Staging Path**: The candidate config renders to `pve_network_staging_path` (`/etc/network/interfaces.pve-deploy`), deliberately **not** `/etc/network/interfaces.new`. That path belongs to PVE: the web UI writes pending edits there and `pvenetcommit.service` commits it to `/etc/network/interfaces` on boot. Staging there made the UI show permanently pending changes and let a reboot apply and delete the file behind the role's back. The role now warns if PVE has its own `interfaces.new` pending.
- **Safety Rollback**: Default `pve_network_apply: false` generates and diffs configs without modifying active links.

## How the rollback actually works

Recovery runs **on the node**, not from the controller. Before the new config is
applied, the role arms a transient `systemd-run` timer
(`pve_network_rollback_unit`, default 120s) that restores the pre-apply backup and
runs `ifreload -a`. The timer is disarmed only once reachability is confirmed.

This matters because the controller cannot fix what it cannot reach. An earlier
version tried to restore over SSH from a `rescue` block, which could not work: a
severed management path makes the host *unreachable*, and Ansible does not run
`rescue` for unreachable hosts, it drops them from the play. Even had it run, the
restore needed the SSH path that had just been lost. The rollback therefore only
executed in the case where it was not needed.

Phase 11 also runs `serial: 1` with `any_errors_fatal: true`, so a config that is
syntactically valid but wrong stops at the first node instead of taking the fleet
down together.

If a node is still unreachable after the rollback window, recover via the BMC
serial console; the backup is at `/etc/network/interfaces.bak.<UTC stamp>`.

## Requirement Tags

- `phase02`
- `pve_network`
