# pve_sdn

Declares the Proxmox VE **SDN EVPN** config-of-record — the EVPN **controller** and
**zones** (VRFs), and optionally **VNets/subnets** — idempotently via `pvesh`, then
applies it. SDN config lives in pmxcfs (cluster-wide), so the role runs **once** from
any cluster node (`run_once`).

## What it does

`get`-or-`create` per object (controllers → zones → VNets → subnets), then
`pvesh set /cluster/sdn` **only when something was created**, so re-runs are a true
no-op (no needless `ifreload`).

It **ensures presence and adds new objects**; it does **not** reconcile field drift on
an existing controller/zone (e.g. changing a live zone's `vrf-vxlan`) — that is a
deliberate, supervised operation, not something a converge run should do silently.

## The apply / ifreload safety

`pvesh set /cluster/sdn` triggers an `ifreload`. On the reference fleet that is **safe**
because the SDN bond MAC is pinned (`pve_network`, `pve_network_pin_sdn_bond_mac`): no promisc, no reboot-after-apply. On any cluster **without**
that pin, set `pve_sdn_apply: false` and apply under supervision (the `ifreload` will
otherwise drop the ConnectX-6 VXLAN underlay).

## Variables

| var | default | meaning |
|-----|---------|---------|
| `pve_sdn_apply` | `true` | run `pvesh set /cluster/sdn` after changes |
| `pve_sdn_controllers` | `[]` | `[{id, type, asn, peers}]` (peers = comma-sep VTEP IPs) |
| `pve_sdn_zones` | `[]` | `[{id, type, controller, vrf_vxlan, ipam}]` |
| `pve_sdn_vnets` | `[]` | `[{id, zone, tag, subnets: [{cidr, gateway}]}]` |

## Example (site group_vars)

```yaml
pve_sdn_controllers:
  - id: evpnctl
    type: evpn
    asn: 4200000006
    peers: "{{ groups['pve'] | map('extract', hostvars) | map(attribute='networks.pve_underlay.ip') | sort | join(',') }}"
pve_sdn_zones:
  - { id: TENANT1, type: evpn, controller: evpnctl, vrf_vxlan: 2,     ipam: pve }
  - { id: PROMED, type: evpn, controller: evpnctl, vrf_vxlan: 90030, ipam: pve }
pve_sdn_vnets: []   # populate when tenant subnets migrate onto the overlay
```

Run: `ansible-playbook playbooks/17-sdn.yml` (tag `pve_sdn`).
