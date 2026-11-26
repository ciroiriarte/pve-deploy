# pve_firewall

This role converges Proxmox VE firewall aliases, IPSets, datacenter rules, and node rules before optionally enabling the datacenter firewall.

`pve_firewall_enable` defaults to `false`. A normal run writes and verifies the intended rules without flipping the datacenter firewall to active. Enabling must be an explicit operator decision.

## Safety Model

Enabling the Proxmox VE firewall with datacenter input policy `DROP` before the complete ruleset exists can lock out SSH, the web UI/API, consoles, and cluster traffic.

The worst omission is corosync. If UDP `5405-5412` is not allowed on the corosync segment, nodes can lose quorum. In an HA cluster that can trigger fencing and reboot running machines. Recovery then requires console access.

The role order is:

1. Build aliases/IPSets from `network_catalog`.
2. Write datacenter rules.
3. Write node rules.
4. Verify rules are present through the PVE API.
5. Enable the datacenter firewall only when `pve_firewall_enable: true`.
6. Verify SSH, PVE API, and corosync quorum.
7. Roll back by disabling the datacenter firewall if verification fails.

## Firewall Implementation

`pve_firewall_implementation` accepts:

- `iptables`: default. This is the legacy implementation and the predictable choice for upgrades and mixed operational assumptions.
- `nftables`: PVE 9 also offers the nftables-based `proxmox-firewall`. Use only after validating behavior on the target cluster.

The task that passes the implementation selector to `/cluster/firewall/options` is marked as needing live-cluster verification because the final PVE 9 API field shape must be confirmed.

## Required Inventory

The role derives segment CIDRs from `network_catalog` in generated inventory. Required keys by default:

- `pve_management`
- `pve_corosync`
- `pve_live_migration`
- `pve_underlay`
- `pve_ceph_access`
- `pve_ceph_replica`

Override `pve_firewall_segments` if another environment uses different catalog keys.

## Optional Rules

`pve_firewall_sdn_evpn_enabled: true` adds EVPN allowances for VXLAN UDP `4789`, BGP TCP `179`, and BFD UDP `3784`, `3785`, and multihop `4784`.

`pve_firewall_allow_validation: true` adds temporary Phase 13 iperf3 rules on TCP/UDP `5201`. Turn this off after validation is accepted.

DNS, NTP, and PBS outbound variables are exposed in defaults. Empty lists are not guessed.

## Console Recovery

If the firewall locks out the cluster:

1. Open the node BMC in the management controller network.
2. Start IPMI Serial-over-LAN from the operator workstation, for example:
   `ipmitool -I lanplus -H <bmc-address> -U <user> sol activate`
3. Log in on the host console.
4. Stop the host firewall:
   `pve-firewall stop`
5. If the datacenter firewall was enabled, disable it from a node after regaining access:
   `pvesh set /cluster/firewall/options --enable 0`
6. Check quorum before restarting workloads:
   `pvecm status`

Do not retry enable until the missing rule is identified and converged.
