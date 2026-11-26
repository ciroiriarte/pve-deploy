# pve_validate

Validates the already-applied PVE network configuration after Phase 11 and firewall preparation after Phase 12.

## Default behavior

Phase 13 is read-only by default. It validates:

- play scope is limited to inventory group `pve`;
- no staged `/etc/network/interfaces.new` is pending, or it matches `/etc/network/interfaces`;
- LACP bonds exist, have MII up, at least two slaves, no down slaves, and a single aggregator ID;
- expected VLAN interfaces exist with the spreadsheet-generated IP addresses;
- configured gateways respond from their local source addresses;
- all PVE hosts can ICMP-ping each other per PVE segment;
- MTU paths work with IPv4 do-not-fragment payload probes;
- `pve-firewall status` command succeeds;
- BIOS, BMC/XCC, NIC, disk, and HBA firmware versions are consistent across all PVE nodes using the latest Phase 01 BMC hardware evidence;
- optional hardware target versions match when `pve_validate_hardware_version_targets` is provided;
## Output modes

The role defaults reproduce the original per-node behaviour. `13-validate-net.yml`
overrides three variables to aggregate instead, producing one environment-wide
report; it is the supported caller. The defaults remain for anyone including the
role directly:

| Variable | Default (v1) | v2 |
| --- | --- | --- |
| `pve_validate_fragment_dir` | `""` (no fragment) | the run's `fragments/` directory |
| `pve_validate_emit_node_report` | `true` | `pve_validate_legacy_node_reports`, default `false` |
| `pve_validate_fail_per_host` | `true` | `false` — the aggregate play fails once |

`pve_validate_fail_per_host: false` matters under `serial: 1`: a per-host assert
aborts the play on the first bad node, so later nodes are never probed and the
environment report would describe a partial cluster while looking complete.

- JSON, Markdown and HTML assessment reports are emitted under `reports/`. The HTML
  companion shares the Markdown's filename stem and is written beside it; it groups the
  checks by subsystem, defaults to collapsed, auto-opens any group holding a failure, and
  offers a failures-only filter. Skip it with `report.py render --no-html`.

## Optional flent characterisation

`pve_validate_flent_enabled=true` runs [flent](https://flent.org) to measure
**latency under load**, which iperf3 cannot show: iperf3 answers "how fast",
flent answers "what happens to latency while it is that fast" — the signal that
exposes bufferbloat and AQM problems on a converged fabric.

Deliberately **outside the pass/fail gate**. An RRUL run costs tens of seconds
per pair, so a full mesh is a campaign rather than a validation step, and the
result is a baseline to compare between runs rather than a verdict that blocks a
deployment. A failed or unrunnable measurement produces a **warning**.

**Dependencies are handled by the role.** iperf3 is installed permanently by
Phase 10 because it is small and broadly useful; flent pulls `netperf` and a
Python/matplotlib stack that has no business sitting on a hypervisor between
tests. The role installs what is missing, runs, and removes exactly what it
installed. Packages already present are left alone, and the removal happens in an
`always` block so it runs even when the measurements fail.

`netserver` is likewise started on the target for the duration of each
measurement and stopped afterwards, rather than left listening on a production
node.

| Variable | Default | Purpose |
| --- | --- | --- |
| `pve_validate_flent_packages` | `[flent, netperf]` | provisioned for the run |
| `pve_validate_flent_install_deps` | `true` | install what is missing |
| `pve_validate_flent_remove_deps_after` | `true` | leave the node as found |

| Variable | Default | Purpose |
| --- | --- | --- |
| `pve_validate_flent_enabled` | `false` | opt in |
| `pve_validate_flent_test` | `rrul` | flent test name |
| `pve_validate_flent_length` | `30` | seconds per measurement |
| `pve_validate_flent_full_mesh` | `false` | one direction per pair; enable when hunting asymmetry |
| `pve_validate_flent_networks` | underlay, ceph replica | segments worth measuring; cost scales with pair count |
| `pve_validate_flent_evidence_dir` | `<site>/environments/<site>/evidence/flent` | where `.flent.gz` files are collected |

Results appear in the environment report as a metrics table (throughput, latency,
loss) with the `.flent.gz` artifact path per row, worst first.

> **Untested against real flent.** The tooling is not installed on the reference fleet,
> so the graceful-degradation path is verified but the summary parsing in
> `flent.yml` has never seen real flent output and will likely need adjusting on
> first use.

## Optional iperf3 mesh

Set `pve_validate_iperf_enabled=true` to run TCP/UDP iperf3 tests. This starts temporary one-shot iperf3 server processes on target nodes through SSH and may require Phase 12 to be rerun with `pve_firewall_allow_validation=true` so TCP/UDP `5201` is allowed.

Example:

```bash
ansible-playbook -i $SITE/inventories/<site>/hosts.yml -u root playbooks/13-validate-net.yml \
  -e pve_validate_iperf_enabled=true
```

Leave it disabled for a pure read-only reachability/MTU validation pass.

## Hardware firmware validation

Phase 13 consumes the latest BMC hardware evidence JSON files under `environments/<env>/evidence/` and checks that the PVE fleet is homogeneous for:

- system BIOS version;
- BMC/XCC firmware version;
- Mellanox NIC firmware versions by adapter device name;
- storage controller/HBA firmware versions by model and slot;
- physical disk firmware revisions by model and slot.

The validation is consistency-first: every discovered component in the same category must report one version across all PVE nodes. To pin desired levels explicitly, set `pve_validate_hardware_version_targets`; the role writes the map to `reports/hardware-version-targets.json` and validates it with `tools/validate_fleet_versions.py`.

Example target map:

```yaml
pve_validate_hardware_version_targets:
  bios: KAE142G
  bmc: KAX368P 6.20 2025-12-19
  nic:
    default: 26.46.3048
  hba:
    SAS3808N @ Slot 16: 52.32.0-6386
    SAS3908 @ Slot 2: 52.32.0-5990
  disk:
    Micron_7450_MTFDKBA480TFR @ RAID_Slot16: E2MU300
```

Leave the map empty to require only fleet consistency without enforcing a vendor target.
