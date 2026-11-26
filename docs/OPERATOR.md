# Deployment Operator Runbook

This document details the operational procedures, prerequisites, gating requirements, and recovery paths for deploying Proxmox VE clusters using `pve-deploy`.

## Prerequisites & Environment Setup

Before executing deployment playbooks, the control host and hardware environment must meet the following baseline requirements:

### Control Host Tooling
- **Python:** `>= 3.11`
- **Ansible:** Core `>= 2.17.0` (`community.proxmox` 2.0.0 is tested against ansible-core 2.17–2.20)
- **Go Tooling:** `pc` CLI `>= v0.13.0` installed and available in `$PATH` (`github.com/ciroiriarte/pve-cli`)
- **Python Libraries:** `proxmoxer >= 2.3.0`, `requests`, `netaddr`, `google-api-python-client` (for `sheet_sync.py`)
- **Linting Tools:** `ansible-lint`, `yamllint`, `ruff`

### Credentials & Access
1. **Out-of-Band (BMC):** Administrative Redfish credentials (`username`/`password`) defined in Ansible Vault.
2. **In-Band (SSH):** SSH public keys pre-seeded on target OS or injected via Proxmox auto-installer answer file.
3. **API Access:** PVE API token (`PVEAPIToken=USER@REALM!TOKENID=UUID`) configured for `pc` CLI operation on `localhost`.
4. **Node root password:** a SHA-512 crypt **hash** in `pve_root_password_hash`, applied by `pve_baseline` in phase 10. See below.

### Generating the root password hash

Phase 02 writes an install-time password into the answer file in **cleartext**,
inside an ISO published over plain HTTP so the BMC can fetch it. Anyone who can
reach that image can read it, and the image names are predictable from the
inventory. Phase 10 is what retires it — a fleet is not finished until phase 10
has run with this set.

Generate the hash on your own workstation, so the cleartext never reaches the
repository, a playbook argument, or shell history:

```bash
# Debian/Ubuntu: apt install whois
mkpasswd -m sha512crypt

# Anywhere with OpenSSL (-6 selects SHA-512 crypt):
openssl passwd -6
```

Both prompt for the password and print a hash beginning `$6$`. Put that in the
site inventory, e.g. `inventories/<env>/group_vars/pve/credentials.yml`:

```yaml
pve_root_password_hash: "$6$<salt>$<hash>"
```

**It must be a hash, not a password.** `ansible.builtin.user` writes this value
into `/etc/shadow` verbatim, so a cleartext string is stored as though it were
already hashed: the password you typed will not work, and the account cannot
authenticate at all. `pve_baseline` asserts the value matches `^\$[0-9a-z]+\$`
and refuses to continue otherwise — but that only catches the obvious case.

Two consequences worth knowing before choosing the password:

- **`root@pam` authenticates against it**, so it is also the PVE web UI login
  for every node in the group.
- **A hash is not a secret you can publish.** sha512crypt is deliberately slow
  but offline-crackable, so a weak password behind it is recoverable by anyone
  holding the file. Keep it in `ansible-vault` where a vault password exists; a
  private repository is a weaker control, not an equivalent one.

Leaving `pve_root_password_hash` empty is allowed for sites managing root
credentials by another route. The rotation is then skipped and phase 10 prints a
warning naming the node — the alternative, passing silently, would leave a live
credential inside a published image with nothing to indicate it.

---

## Technical Constraint: Proxmox Unattended Auto-Installation

> [!IMPORTANT]
> **THE PROXMOX AUTO-INSTALL ANSWER FILE SUPPORTS EXACTLY ONE NETWORK INTERFACE**

The official Proxmox VE automated installer answer file schema supports the configuration of **only one network interface**. It does not support LACP bonding, VLAN tagging, Linux Bridges, custom MTU sizing, or multi-homed management interfaces during the initial installation phase.

### Operational Implication:
1. Bare-metal automated OS installation (`01-hardware` / `os_install`) provisions the host on a **single temporary bootstrap port** with a static IP or DHCP address.
2. Complex network topologies (e.g., LACP bond `bond0`, VLAN sub-interfaces `bond0.100`, Linux Bridge `vmbr0`, MTU 9000) are **not** created by the installer.
3. Complete host network architecture is applied in Tier B during **Phase 11 (`11-network.yml`)**, which rewrites `/etc/network/interfaces` and reloads `ifupdown2`.

---

## Inventory Management & Offline Reproducibility

The repository inventory is strictly managed via offline, version-controlled YAML files derived from a centralized Google Sheet definition.

```
┌───────────────────────┐
│     Google Sheet      │
│ (Master Definition)   │
└───────────┬───────────┘
            │
            │ python3 tools/sheet_sync.py
            ▼
┌───────────────────────┐
│  inventory/hosts.yml  │ ◄── Committed to Git repository
└───────────┬───────────┘
            │
            │ ansible-playbook -i inventory/hosts.yml ...
            ▼
┌───────────────────────┐
│   Playbook Execution  │ (Runs 100% offline, zero runtime API calls)
└───────────────────────┘
```

### Generating & Committing Inventory:
```bash
# Fetch and validate remote sheet definition
python3 tools/sheet_sync.py --sheet-id <SPREADSHEET_ID> --output-dir inventory/

# Inspect diffs against committed inventory
git diff inventory/

# Commit updated inventory to repository
git add inventory/
git commit -m "feat(inventory): update cluster node parameters for site-alpha"
```

Playbooks **never** query Google Sheets dynamically during runtime. This guarantees deterministic, reproducible deployments in air-gapped or restricted network segments.

---

## Execution Runbook & Phase Gates

Playbooks must be executed in order. Each phase enforces specific criteria (gates) before allowing subsequent execution.

### Running Phase 01

```bash
# Credentials come from group_vars/<env>/bmc/vault.yml (see vault.yml.example).
# Read-only by default: reads every BMC, diffs BIOS and storage, writes evidence.
ansible-playbook -i $SITE/inventories/<site>/hosts.yml playbooks/01-hardware.yml

# Single host, for a quick check:
ansible-playbook -i $SITE/inventories/<site>/hosts.yml playbooks/01-hardware.yml \
    --limit pve-01-bmc
```

To converge hardware instead of only reporting on it, clear the apply gate. Both
keys are required, `hardware_apply_confirm` must match `pve_site_name`, and the
run reboots every host that has a pending diff — so pair it with `--limit`:

```bash
ansible-playbook -i $SITE/inventories/<site>/hosts.yml playbooks/01-hardware.yml \
    -e hardware_apply=true \
    -e hardware_apply_confirm=<site> \
    --limit pve-01-bmc
```

Proving a run cannot write, for a change window where that has to be shown:

```bash
ansible-playbook -i $SITE/inventories/<site>/hosts.yml playbooks/01-hardware.yml \
    --tags readonly
```

**Expect roughly 10-15 minutes.** Phase 01 makes about 40 sequential HTTPS
round-trips per BMC — 14 `redfish_info` calls, each authenticating separately,
plus raw collection discovery and per-member expansion for every NIC and storage
device. Lenovo XCC is not fast. Hosts run in parallel, so wall-clock for a
six-node fleet is close to the single-host time. Do not shorten this by dropping
the per-member expansion: those calls are what produce the per-NIC permanent MAC
addresses that Phase 13 needs to prove the cabling matches the plan.

The BMCs answer Redfish over HTTPS but **do not respond to ICMP**, so a failed
`ping` says nothing about reachability. Test with
`curl -sk -o /dev/null -w '%{http_code}' https://<bmc>/redfish/v1/` instead.

Phase 01 writes nothing to the hardware. It issues no `redfish_command`, no
POST/PATCH/PUT, and no power action, and every task is `changed_when: false`, so
a successful run reports `changed=0`. A non-zero changed count means something
was added that should not have been.

### Running Phase 1

```bash
ansible-playbook -i $SITE/inventories/<site>/hosts.yml -u root playbooks/10-baseline.yml
```

Phase 1 is intentionally limited to hosts in the `pve` inventory group. The playbook asserts this in `pre_tasks` before applying changes, so non-PVE roles are not modified during PVE baseline deployment.

Primary validation after Phase 1:

```bash
ansible -i $SITE/inventories/<site>/hosts.yml pve -u root -m ping
ansible -i $SITE/inventories/<site>/hosts.yml pve -u root -m shell -a 'apt-get update >/tmp/apt-update.log 2>&1; echo APT_RC=$?'
ansible -i $SITE/inventories/<site>/hosts.yml pve -u root -m shell -a 'proxmox-boot-tool status'
```

Repository failures should be triaged as DNS/CDN/reachability problems before changing firewall policy. IPv4 APT acquire preference is only a client-side workaround for unreliable IPv6 repository paths; it does not disable host IPv6 and is not a substitute for firewall egress policy.

| Phase Playbook | Primary Objective | Gating Criteria / Proof of Pass |
| :--- | :--- | :--- |
| `01-hardware.yml` | Hardware & Redfish validation; BIOS/RAID convergence when the apply gate is cleared | BMC reachability, firmware version match, CPU/RAM/Disk health checks pass. In apply mode, BIOS and storage match the site baseline after a single reboot. |
| `10-baseline.yml` | OS baseline, repository configuration, packages, and bootloader convergence | Debian/Proxmox/Ceph/OBS APT repositories configured, `apt-get update` succeeds, baseline packages installed, preferred systemd-boot/UEFI state confirmed. |
| `11-network.yml` | In-band network interface setup | `/etc/network/interfaces` applied, `ifupdown2` active, default routes reachable. |
| `15-firewall.yml` | Proxmox VE firewall initialization (**after** `14-cluster`) | Host and cluster firewall rules active; PVE management, SSH, API, corosync, migration, Ceph, VXLAN/BGP/BFD, and console service paths allowed as inventory defines. |
| `13-validate-net.yml` | End-to-end connectivity check | Jumbo frame (MTU 9000) ping pass across all VLANs, cluster communication path verified. |
| `14-cluster.yml` | Corosync cluster creation | `pvecm status` confirms full quorum across all cluster nodes. |
| `15-ceph.yml` | Ceph Tentacle cluster deployment | `ceph health` returns `HEALTH_OK` or `HEALTH_WARN` (expected prior to OSD balance). |
| `16-sdn.yml` | SDN zones and VNets setup | SDN state reloaded, VNets created and present in `pc sdn vnet list`. |
| `17-images.yml` | Storage pools & cloud-init images | Storage backends active, OS templates imported via `pve-import-cloud-images`. |
| `18-functional.yml` | Validation & evidence bundle creation | Synthetic VM creation, live migration test, snapshot, and report generation pass. |

> [!NOTE]
> **Firewall Gating Order:** `15-firewall.yml` runs **after** `14-cluster.yml`, not before network validation.
>
> The PVE firewall lives entirely in `/etc/pve` (`firewall/cluster.fw`, `nodes/<node>/host.fw`), and `man pvecm` states that *all existing configuration in `/etc/pve` is overwritten when joining a cluster*. Rules written before clustering therefore survive only on the founding node — every joining node loses them silently, at the moment it joins. The playbook asserts cluster membership and refuses to run without it.
>
> The consequence for validation: when `13-validate-net.yml` runs at its normal position it reports firewall state as unset, which is expected. Re-run it after `15-firewall.yml` for a verdict covering the operational security state as well as the topology.

---

## Evidence Bundles & Change Reports

Upon completion of any applying playbook run, Ansible generates an execution evidence bundle and a markdown change report adhering to the following naming convention:

`change-report-YYYY-MM-DDTHHMM-<slug>.md`

### Change Report Structure:
1. **Execution Metadata:** Date, operator, git commit hash, inventory target.
2. **Outcome Summary:** Total task counts (changed, ok, failed, skipped).
3. **Verification Evidence:** Raw CLI output from validation commands (`pvecm status`, `ceph status`, `pve-sdn-healthcheck`).
4. **Reproducible State Definition:** Diff snippets of applied configuration files (`/etc/network/interfaces`, `/etc/pve/corosync.conf`, `/etc/pve/sdn/zones.cfg`).

Example report path: `artifacts/change-report-2026-07-26T1430-cluster-init.md`.

---

## Phase Failure Recovery & Rollback Procedures

### 1. Tier A (OOB / Redfish) Failure Recovery
- **Symptom:** Redfish job timeouts or stalled BIOS configuration.
- **Action:**
  1. Clear stuck Redfish job queues via `pc` or direct Redfish DELETE requests:
     ```bash
     curl -X DELETE -k -u "admin:pass" https://<bmc-ip>/redfish/v1/TaskService/Tasks/<TaskID>
     ```
  2. Perform soft BMC reset:
     ```bash
     ipmitool -H <bmc-ip> -U admin -P pass mc reset cold
     ```
  3. Re-run `01-hardware.yml` to re-synchronize inventory state. Safe to repeat:
     without `hardware_apply` the playbook only reads, and `bmc_inventory` has no
     apply mode at all. Do **not** add the apply flags to a recovery run — a BMC
     that has just been cold-reset is the worst moment to commit a BIOS diff.

### 2. Phase 11 (Network) Recovery
- **Symptom:** Host loses SSH connectivity following `/etc/network/interfaces` update.
- **Action:**
  1. Connect to host via BMC virtual console (KVM).
  2. Restore fallback interfaces file generated during task execution:
     ```bash
     cp /etc/network/interfaces.bak /etc/network/interfaces
     ifreload -a
     ```
  3. Correct network variables in `inventory/group_vars/pve.yml` and re-execute `11-network.yml`.

### 3. Phase 14 (Cluster Initialization) Rollback
- **Symptom:** Corosync fails to achieve quorum or node join operations time out.
- **Action:**
  1. On joining nodes, stop cluster services:
     ```bash
     systemctl stop pve-cluster corosync
     pmxcfs -l
     rm -rf /etc/corosync/* /etc/pve/corosync.conf
     killall pmxcfs
     systemctl start pve-cluster
     ```
  2. On primary node, remove failed node reference:
     ```bash
     pvecm delnode <failed-node-name>
     ```
  3. Re-run `14-cluster.yml`.
