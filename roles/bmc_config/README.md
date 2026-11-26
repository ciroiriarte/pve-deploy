# bmc_config

Audits — and eventually converges — the BMC's own **service configuration**:
time synchronisation, name resolution, management protocol posture, and account
policy. This is distinct from `bmc_inventory`, which captures hardware facts, and
from `bios_config`, which configures the host firmware.

Invoked by `playbooks/01-hardware.yml`, which sets `bmc_config_audit_only` from
its `hardware_apply` gate.

## Absolute Constraint: Zero Writes

In audit mode this role:
- Performs **ZERO write operations** to the BMC.
- Never restarts the BMC or stages changes.
- Operates cleanly under `--check` and always reports `changed=0`.

Mutating tasks are gated behind `when: not (bmc_config_audit_only | bool)` and
live in `tasks/apply_bmc.yml`, outside the `readonly`-tagged block so
`--tags readonly` cannot select them. The `| bool` filter matters: a
string-valued `bmc_config_audit_only` is truthy in Jinja whatever it contains, so
the bare form silently skips apply even when the operator asked for it.

The apply path is **not implemented yet**. It `fail`s rather than warning, so a
run that asked to converge the BMC cannot be mistaken for one that did.

## What it checks

Baseline is expressed as **intent** in `bmc_baseline`, never as Redfish paths.
The mapping to Redfish lives in `tasks/audit_bmc.yml`, so a second vendor
replaces the lookups without an operator's baseline changing.

| Intent key | Redfish source |
| --- | --- |
| `ntp.enabled`, `ntp.servers` | `Managers/1/NetworkProtocol` → `NTP` |
| `dns.servers` | `Managers/1/EthernetInterfaces/*` → `StaticNameServers` |
| `protocols.{ssh,https,ipmi_over_lan,virtual_media,snmp}` | `Managers/1/NetworkProtocol` → `ProtocolEnabled` |
| `account_policy.{min_password_length,lockout_threshold}` | `AccountService` |
| `accounts.enabled` | `AccountService/Accounts` |

Every source is **standard DMTF Redfish**, not Lenovo OEM, so this gather is the
part most likely to work unchanged against iDRAC and iLO. Remote syslog, SMTP
alert destinations, SNMP trap targets and vendor security policy are OEM and are
deliberately **not** collected here — they belong behind the vendor driver seam.

A baseline key left undefined or empty is **skipped**, not passed. A partial
baseline narrows what is checked rather than silently approving what it never
looked at.

## Three statuses, not two

`match`, `drift`, and **`absent`** — the BMC does not expose the property at all.

Keeping `absent` distinct is the whole point. Conflating it with `match` is what
let the BIOS baseline report a fleet-wide `outcome: success` while resolving zero
of its ten keys. Here:

- `absent` counts toward a `partial` outcome, never `success`.
- If **nothing matched and something was unreadable**, the audit **fails
  outright** rather than reporting a vacuous pass.

That guard is deliberately not keyed on "zero drift": an earlier version was, and
a single key rendering as an empty list instead of absent was enough to defeat
it. A BMC that is genuinely 100% non-compliant (`match=0, absent=0, drift=N`)
does not trip it — that is real drift and the report should say so.

Absent-vs-empty is also distinguished for `dns.servers`: no interface exposing
`StaticNameServers` is *absent*, while the property present and empty is *drift*
(genuinely no resolvers configured).

## Accounts are reported, never changed

This role does not create, delete, enable or re-password BMC users. It reports
which accounts are enabled and flags any outside `bmc_expected_accounts`.
Credential lifecycle needs its own role and its own confirmation: locking
yourself out of a BMC is unrecoverable without physical access.

Evidence retains only `Id`, `UserName`, `RoleId`, `Enabled` and `Locked` —
enough to audit posture without writing account detail into a git repository.

## Notable defaults

`protocols.ipmi_over_lan: true`, classified **critical** rather than warning —
because it is mandatory, not because it is risky.

It carries Serial-over-LAN, which is how a host with no working network is
reached: during a bare-metal install, after a bad network change, or when a bond
fails to negotiate. Several phases here depend on that path. Finding it disabled
is therefore a critical finding, not a cosmetic one.

The security argument for turning it off — IPMI predates Redfish and
authenticates weakly — is real, but bound that exposure at the firewall rather
than by removing the recovery capability from the hardware.

## Testing

Classification is verifiable without hardware by seeding the collected facts and
including `tasks_from: audit_bmc.yml`. Both the normal path (match/drift/absent
counts, boolean `false`-vs-`true` drift, rogue account detection) and the
vacuous-pass guard are exercised that way.
