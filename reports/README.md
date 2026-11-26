# reports/

Generated deployment records land **outside this repository** when a site
inventory sets `pve_site_root`.

Every role derives its output location from that variable:

```yaml
reports_dir: "{{ pve_site_root }}/reports"
```

`pve_site_root` defaults to this checkout so a standalone clone works with no
extra configuration. A real deployment should point it at that site's own
repository, so the records live with the rest of that project's documentation
rather than accumulating in the tool repo. See `inventories/README.md`.

Note that `chdir` for the report renderer stays at `pve_deploy_root` — that is
where `tools/report.py` lives. Only the `--out` destination follows the site.

This directory therefore stays empty in a clean checkout, and exists so a
default (site-less) run has somewhere to write.
