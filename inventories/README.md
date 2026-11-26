# inventories/

Site inventories live **outside this repository**.

`pve-deploy` is the tool: playbooks, roles and `tools/`. A deployment's
inventory (hostnames, addressing, credentials) and its records (reports,
hardware evidence) belong to that deployment, not to the tool. Keeping them
apart means this repo can be cloned for any site without carrying another
site's data, and a site's records stay with the rest of that project's
documentation.

This directory is a mount point. Point `-i` at an inventory wherever it lives:

```bash
ansible-playbook -i /path/to/site-repo/inventories/<site>/hosts.yml \
                 playbooks/10-baseline.yml
```

## What a site inventory must define

Roles default `pve_site_root` to this checkout, so **a site inventory that does
not set it will write that site's records back into this repository.** Set it,
along with the site identity and service addresses, in
`group_vars/all/site.yml`:

```yaml
# Resolved from the inventory's own location, so it holds regardless of where
# this tool checkout lives or where ansible-playbook was invoked from.
pve_site_root: "{{ (inventory_dir ~ '/../..') | realpath }}"

pve_site_name: "<site>"        # selects environments/<site>/evidence/
pve_environment: "production"  # report metadata: the deployment tier
environment_name: "production" # same, for the Tier A roles

ntp_servers: ["..."]
dns_servers: ["..."]
dns_search_domain: "..."
```

`pve_site_name` selects a path; `pve_environment` / `environment_name` are
report metadata naming the tier. They are deliberately separate variables — see
`roles/pve_validate/defaults/main.yml`.

## Generating an inventory

`tools/sheet_sync.py` renders one from a planning spreadsheet. It writes only
the files it owns (`hosts.yml`, `group_vars/<group>/generated.yml`, `host_vars/`),
so hand-maintained material — `group_vars/all/site.yml`, and above all the
vaulted `group_vars/bmc/vault.yml` — survives a re-sync:

```bash
python3 tools/sheet_sync.py sync --root /path/to/site-repo --env <site> --apply
```

`--env` is required: an implicit default makes it easy to render the wrong
site, the same reasoning that keeps `inventory` unset in `ansible.cfg`.
