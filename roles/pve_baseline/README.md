# pve_baseline

Configures system baseline parameters for Proxmox VE 9.2 on Debian Trixie.

## Architectural Decisions
- **NTP Verification**: Asserts synchronization against the site-configured `ntp_servers` via `chronyc sources`, failing if no source is active.
- **SSH Hardening**: Validates configuration syntax (`sshd -t`) before signaling both `ssh.service` and socket-activated `ssh.socket` units.
- **Kernel Command Line**: Manages `/etc/kernel/cmdline` directly and runs `proxmox-boot-tool refresh`, preserving recovery parameters (`console=ttyS0,115200n8`).
- **Sysctl**: Writes exclusively to `/etc/sysctl.d/99-pve-baseline.conf` as PVE 9 deprecates `/etc/sysctl.conf`.

## Requirement Tags
- `phase01`
- `pve_baseline`
