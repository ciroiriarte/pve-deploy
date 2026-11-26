#!/usr/bin/env bash
#
# Phase 01 launcher that resolves --limit against the inventory BEFORE calling
# ansible-playbook.
#
# Why this exists: `ansible-playbook --limit <name>` where <name> matches no
# host prints "skipping: no hosts matched" and exits 0. On an apply run that is
# the worst possible outcome — the operator believes one node was converged
# while nothing ran. No guard inside the playbook can catch it, because the
# guard play is filtered by the same --limit.
#
# The trap this actually prevents: Tier A targets BMCs (pve-01-bmc),
# every other phase targets nodes (pve-01). Using the familiar Tier B
# name here silently matches nothing.
#
# Usage:
#   tools/hardware-run.sh -i <inventory> [--limit <pattern>] [-- <ansible args>]
#   tools/hardware-run.sh -i inv.yml --limit pve-01-bmc -- -e hardware_apply=true ...

set -euo pipefail

INVENTORY=""
LIMIT=""
PLAYBOOK="$(dirname "$0")/../playbooks/01-hardware.yml"
EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -i|--inventory) INVENTORY="$2"; shift 2 ;;
    --limit|-l)     LIMIT="$2";     shift 2 ;;
    --)             shift; EXTRA=("$@"); break ;;
    *)              EXTRA+=("$1");  shift ;;
  esac
done

if [[ -z "$INVENTORY" ]]; then
  echo "ERROR: -i <inventory> is required." >&2
  exit 2
fi

# Hosts in the bmc group, independent of any --limit.
mapfile -t BMC_HOSTS < <(
  ansible-inventory -i "$INVENTORY" --list 2>/dev/null \
    | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin).get("bmc",{}).get("hosts",[])))'
)

if [[ ${#BMC_HOSTS[@]} -eq 0 ]]; then
  echo "ERROR: inventory $INVENTORY defines no hosts in the 'bmc' group." >&2
  exit 2
fi

# Resolve what --limit actually selects. --list-hosts exits 0 with an empty set,
# so the count is what matters, not the exit status.
if [[ -n "$LIMIT" ]]; then
  MATCHED=$(
    ansible-playbook -i "$INVENTORY" "$PLAYBOOK" --limit "$LIMIT" --list-hosts 2>/dev/null \
      | grep -cE "^\s+\S+-bmc\s*$" || true
  )

  if [[ "$MATCHED" -eq 0 ]]; then
    echo "ERROR: --limit '$LIMIT' matches no host in the bmc group." >&2
    echo "" >&2
    echo "Tier A targets BMCs, which carry a -bmc suffix. Tier B phases target" >&2
    echo "the node itself. If you meant a node name, add -bmc:" >&2
    echo "" >&2
    for h in "${BMC_HOSTS[@]}"; do echo "    $h" >&2; done
    echo "" >&2
    echo "Refusing to run: ansible-playbook would have exited 0 having done nothing." >&2
    exit 1
  fi

  echo "Targeting $MATCHED of ${#BMC_HOSTS[@]} BMC host(s) via --limit '$LIMIT'."
else
  echo "WARNING: no --limit given; all ${#BMC_HOSTS[@]} BMC hosts are in scope." >&2
  echo "         The bmc group is broader than the PVE nodes — it also holds the" >&2
  echo "         PBS and database systems. Apply mode will refuse this without" >&2
  echo "         -e hardware_apply_allow_fleet=true." >&2
fi

set -x
exec ansible-playbook -i "$INVENTORY" "$PLAYBOOK" \
  ${LIMIT:+--limit "$LIMIT"} \
  "${EXTRA[@]}"
