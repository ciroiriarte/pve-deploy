# firmware_config

Audits — and converges — server firmware to either a **pinned baseline** or the
**newest version in the catalog**. Covers BMC, UEFI, bootstrap tooling (LXPM /
LXUM), adapters, drives and PSUs.

Invoked by `playbooks/00-firmware.yml`, which sets `firmware_config_audit_only`
from its `hardware_apply_scope.firmware` gate.

## Two target modes

| Mode | Target comes from | Use when |
| --- | --- | --- |
| `baseline` (default) | `firmware_baseline`, a pinned map of component → version | You want a repeatable, qualified version set. Two runs a month apart converge the fleet to the same place. |
| `latest` | The newest entry per component in `firmware_catalog_file` | You want the newest firmware the deployment holds. |

**"Latest available" means newest in the catalog this deployment holds**, not
newest Lenovo has ever published. Resolving the latter would make the outcome
depend on the day the playbook ran, and could not be reviewed before execution.
Refreshing the catalog is a separate, deliberate step; then a run is a function
of the catalog, and the catalog is a reviewable artifact.

## The role never guesses which version is newer

Lenovo version strings share no format: `68P-6.20`, `42G-5.81`, `26.46.3048`,
`52.32.0-5990`, `E3MQ005`, `14.52`. No comparison function orders all of those
correctly, and the failure is silent and wrong in the dangerous direction —
lexically `68P-6.9` sorts **above** `68P-6.20`, so a naive "is newer" test would
happily downgrade a BMC.

So ordering comes from exactly one place: the catalog lists each component's
versions **newest first**, and `latest` takes the first entry. The diff itself
only ever asks *"does current differ from target"*, which is string equality and
always safe.

## Update order

```
bmc → uefi → bootstrap → adapter → drive → psu
```

The BMC is flashed first because it is the agent performing every other update;
flashing it last would mean applying the rest through firmware about to be
replaced. Drives and PSUs come last, being least likely to strand the host if
the sequence stops partway.

## Transport

`multipart_push` (default) POSTs the payload to the BMC's
`MultipartHttpPushUri`, so **the BMC is the server and needs no outbound
reachability**. That matters here: the OOBM-BMC segment is default-denied
outbound. The `simple_update` alternative — where the BMC fetches a URL — would
require opening BMC egress to a payload host first, which is a firewall change
and a decision, not an implementation detail.

`curl` is used for the push because `ansible.builtin.uri` cannot send
`multipart/form-data` with a file part, and base64-inlining a ~100MB payload
through Jinja is not viable.

## What is excluded, and why

| Pattern | Reason |
| --- | --- |
| `*-Pending` | Staging slots, not installed firmware. Version is null until something is staged, which would read as infinite drift. |
| `BMC-Backup` | The fallback image. Flashing both banks at once removes the recovery path. |
| `Slot_N.1` | Read-only device views reporting `Updateable: false`. The real target is `Slot_N.Bundle` — targeting the former silently fails. |
| `Bundle.*` | An aggregate marker, not a flashable component. |

A component the classifier does not recognise is reported and left out of the
plan, never grouped into a class it might not belong to — ordering depends on
the class, and flashing a BMC as though it were a drive is not a small mistake.

## Safety properties

- **Audit is the default.** `firmware_config_audit_only` must be explicitly
  cleared, via the phase's `hardware_apply_scope.firmware`.
- **All payloads are verified present before the first push.** Discovering a
  missing payload halfway leaves the host part-flashed across a BMC/UEFI
  boundary, which is the worst place to stop.
- **The machine type is checked against the catalog.** Flashing an SR650 UEFI
  onto an SR655 is not a recoverable mistake.
- **Update tasks are polled, not assumed complete.** The POST returns when the
  BMC accepts the payload, long before the component is written.
- **Versions are re-read after flashing.** A task reporting `Completed` means
  the payload was accepted, not that the component reports the wanted version.
- **A plan where nothing resolved to a target fails** rather than reporting a
  compliant fleet — that means the baseline or catalog does not describe this
  hardware.

## Status

The audit path is verified live against a production SR655 V3 fleet in both modes.
**No firmware has been flashed through this role yet.** Issue #4 records that
this phase should land after `02-install`, so a failed flash has an automated
reinstall as recovery.
