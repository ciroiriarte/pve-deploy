# bmc_inventory

`bmc_inventory` is the first Tier A audit role for `pve-deploy`. It talks to Lenovo XClarity Controller BMCs through read-only Redfish calls and writes hardware evidence for later deployment phases.

## Safety Contract

This role is read-only against hardware.

It does not perform Redfish `POST`, `PATCH`, or `PUT`. It does not change BIOS settings, RAID/storage configuration, firmware, boot order, virtual media, or power state. All Redfish collection tasks use information-gathering modules or HTTP `GET` only and set `changed_when: false`.

The role is safe under `--check`. Local evidence/report files are still written under check mode so an audit run produces usable artifacts.

## What It Gathers

Per BMC:

- System identity: serial number, manufacturer, model/SKU, machine type, UUID, BIOS version, processor model/count, and total memory.
- BMC identity: XCC/manager firmware version plus BMC manager NIC information.
- Host NIC inventory: permanent/current MACs plus best-effort adapter, slot, and port fields.
- Storage inventory: controllers, physical drives, volumes, and best-effort unconfigured/JBOD candidate detection.
- Full BIOS attribute payload for later drift checking.
- Power state and health/status rollups where exposed.

## What It Deliberately Does Not Touch

- No BIOS changes.
- No RAID or virtual disk changes.
- No disk initialization.
- No boot override changes.
- No virtual media attach/detach.
- No power action.
- No firmware update.

## Variables

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `environment_name` | `production` | Deployment tier stamped into artifact/report metadata. |
| `bmc_username` | `USERID` | BMC login user. |
| `bmc_password` | undefined | BMC password. Must be supplied through Ansible Vault, usually `group_vars/bmc/vault.yml`. |
| `bmc_redfish_validate_certs` | `false` | Certificate validation toggle. Defaults false because factory XCC certificates are self-signed. |
| `bmc_redfish_ca_path` | `""` | Optional CA bundle path when certificate validation is enabled. |
| `bmc_redfish_timeout` | `60` | Redfish request timeout in seconds. |
| `bmc_inventory_stamp` | UTC `YYYY-MM-DDTHHMM` | Shared artifact timestamp. |
| `bmc_inventory_evidence_dir` | `environments/{{ environment_name }}/evidence` | Per-host evidence output directory. |
| `bmc_inventory_summary_path` | `.../bmc-hardware-summary-{{ stamp }}.md` | Consolidated Markdown summary path. |
| `bmc_inventory_include_raw_redfish` | `true` | Include raw module/GET payloads in evidence. Keep true for first deployment audits. |

## Evidence Schema

Each host writes:

`environments/{{ environment_name }}/evidence/{{ parent_host }}-hardware-{{ stamp }}.json`

Top-level fields:

- `schema_version`: stable schema identifier.
- `generated_at`: UTC audit timestamp in `YYYY-MM-DDTHHMM`.
- `environment`: deployment environment.
- `bmc_host`: inventory hostname for the BMC.
- `parent_host`: server hostname correlated back to server `host_vars`.
- `bmc_address`: BMC IP or DNS target from `ansible_host`.
- `collection`: status, errors, read-only assertion, and Redfish connection metadata.
- `system`: normalized system identity, CPU, memory, power state, and health.
- `bmc`: normalized BMC/manager identity, MAC/IP information, and firmware inventory.
- `network_adapters`: normalized host NIC records with permanent MAC, adapter, slot, and port when available.
- `storage`: controllers, physical drives, volume records, and candidate unconfigured flags.
- `bios_attributes`: verbatim BIOS attribute payload.
- `raw_redfish`: raw facts and defensive GET payloads for fields not yet normalized.

## Worked Example

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-07-26T1415",
  "environment": "production",
  "bmc_host": "pve-01-bmc",
  "parent_host": "pve-01",
  "bmc_address": "192.0.2.40",
  "collection": {
    "status": "success",
    "errors": [],
    "readonly": true
  },
  "system": {
    "serial_number": "J100ABC1",
    "manufacturer": "Lenovo",
    "model": "ThinkSystem SR655 V3",
    "sku": "7D9G",
    "machine_type": "7D9G",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "bios_version": "IVE123A",
    "power_state": "On",
    "processor_model": "AMD EPYC",
    "processor_count": 2,
    "memory_total_mib": 1048576
  },
  "network_adapters": [
    {
      "name": "OCP Port 1",
      "slot": "OCP",
      "adapter": "NVIDIA ConnectX-6",
      "port_index": "1",
      "permanent_mac": "00:11:22:33:44:55"
    }
  ],
  "storage": {
    "physical_drives": [
      {
        "slot": "Bay 1",
        "model": "SSD",
        "serial": "DRIVE123",
        "capacity_bytes": 3840000000000,
        "media_type": "SSD",
        "protocol": "NVMe",
        "usage": "JBOD",
        "is_unconfigured": true
      }
    ],
    "volumes": []
  }
}
```

## Reporting

The role writes a consolidated Markdown summary and then renders an assessment report with:

```bash
tools/report.py render environments/<site>/evidence/bmc-hardware-report-<stamp>.json --out reports/
```

The report kind is `assessment`, not `change`.
