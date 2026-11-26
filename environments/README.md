# environments/

Per-site hardware evidence lands **outside this repository** when a site
inventory sets `pve_site_root`.

Tier A roles (`bmc_inventory`, `bios_config`, `raid_config`) write BMC, BIOS and
storage evidence here, and `pve_validate` reads it back to check firmware
consistency across the fleet:

```yaml
evidence_dir: "{{ pve_site_root }}/environments/{{ pve_site_name }}/evidence"
```

`pve_site_name` is a **path** selector and is deliberately not the same variable
as `pve_environment` / `environment_name`, which are report metadata naming the
deployment tier. Overloading one variable for both meant that setting the tier
inventory-wide silently redirected the evidence lookup at a directory that did
not exist, and the firmware validation then found nothing to compare.

See `inventories/README.md` for what a site inventory must define.

This directory stays empty in a clean checkout, and exists so a default
(site-less) run has somewhere to write.
