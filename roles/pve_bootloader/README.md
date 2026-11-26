# pve_bootloader

Asserts and converges Proxmox VE hosts to the preferred bootloader policy: `systemd-boot` on UEFI hosts managed by `proxmox-boot-tool`.

## Architectural Decisions

- **Preferred State**: UEFI nodes should use `systemd-boot`/`proxmox-boot-tool` unless a documented host-specific exception requires GRUB.
- **Migration Enabled by Default**: `pve_bootloader_migrate: true` allows the role to correct real GRUB drift automatically.
- **ESP Auto-Detection**: `pve_esp_partition: "auto"` discovers the EFI System Partition before any migration, avoiding unsafe assumptions about disk names.
- **PVE 9 Status Compatibility**: A node is treated as compliant when `proxmox-boot-tool status` reports `systemd-boot`, `configured with: uefi`, or `System currently booted with uefi`.
- **Stale UUID Maintenance**: Already-compliant UEFI nodes with missing/stale `proxmox-boot-uuids` entries are cleaned with `proxmox-boot-tool clean` and refreshed with `proxmox-boot-tool refresh`.
- **Pre-reboot Verification**: Migration paths verify `proxmox-boot-tool status`, EFI boot state, and serial-console loader entries before reporting completion.

## Variables

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `pve_bootloader_migrate` | `true` | Permit automatic convergence from real GRUB drift to the preferred bootloader. |
| `pve_esp_partition` | `"auto"` | Auto-detect the EFI System Partition. Set explicitly only for exceptional hardware layouts. |

## Requirement Tags

- `phase01`
- `pve_bootloader`
