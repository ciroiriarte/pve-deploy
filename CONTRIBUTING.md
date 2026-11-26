# Contributing Guidelines

Contributions to `pve-deploy` are welcome. Please ensure all modifications adhere to the following architectural standards and commit conventions.

## Commit Message Format

This repository strictly enforces **Conventional Commits**.

Allowed types:
- `feat:` A new feature or role capability.
- `fix:` A bug fix in playbooks, roles, or templates.
- `docs:` Documentation additions or updates.
- `ci:` Changes to CI workflows or linting rules.
- `refactor:` Code refactoring without behavioral changes.

### Required Commit Trailers

Every commit **MUST** include a Developer Certificate of Origin (DCO) sign-off trailer:

`Signed-off-by: Ciro Iriarte <ciro.iriarte+software@gmail.com>`

If work was performed with AI assistant pairing, add an `Assisted-By:` trailer:

`Assisted-By: <Tool Name>`

> [!CAUTION]
> **Do NOT use `Co-Authored-By` or `Generated-By` trailers.**

#### Example Commit Message:
```text
feat(pve_ceph): add dynamic OSD crush rule assignment

Implement declarative CRUSH rule selection for NVMe vs SATA OSD pools.

Signed-off-by: Ciro Iriarte <ciro.iriarte+software@gmail.com>
Assisted-By: Claude 3.5 Sonnet
```

## Versioning & Documentation Rules

1. **Semantic Versioning:** Releases follow `MAJOR.MINOR.PATCH` (SemVer).
2. **Documentation Parity:** Any code or role modification **must** be accompanied by updates to `README.md`, `docs/OPERATOR.md`, or `docs/LAYOUTS.md` within the same pull request.
3. **CI Validation:** All pull requests must pass `ansible-lint`, `yamllint`, and `ruff` checks before merging.
