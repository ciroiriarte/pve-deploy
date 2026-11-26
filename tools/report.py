#!/usr/bin/env python3
"""pve-deploy report generator - Single renderer for Ansible deployment reports.

PURPOSE:
    Renders structured JSON evidence collected by pve-deploy Ansible roles into
    standardized, production-grade Markdown reports. Ensures uniform report formatting
    across all automated infrastructure changes and checks.

    A self-contained HTML companion is written beside the Markdown with the same
    filename stem (--no-html to skip). Markdown stays the contractual artifact for
    the change-reporting policy; the HTML exists because a 61-check Phase 13 report
    is not readable as a flat table. It groups checks by subsystem, collapses clean
    groups, auto-opens any group holding a failure, and offers a failures-only
    filter. No CDN, fonts or build step: it has to open from a share with no network.

POLICY ENFORCEMENT:
    Every configuration change on any element requires a Markdown report. No change is
    considered complete without one. Reports must contain exact commands, before/after states,
    reproducibility steps, and verification results to allow manual audit or reproduction.

SAFETY:
    All captured outputs are safely fenced using backtick blocks dynamically sized to prevent
    Markdown escaping. Secrets (passwords, tokens, keys, authorization headers) are automatically
    redacted from output blocks using pattern matching. Overwriting existing report files
    requires explicit authorization via the --force flag.

USAGE EXAMPLES:
    Render a report from JSON file to output directory:
        report.py render change_data.json --out reports/

    Render a report reading JSON from stdin and writing to stdout:
        cat evidence.json | report.py render - --stdout

    Validate input JSON structure and rules without generating a report:
        report.py validate evidence.json

    Get the generated filename for Ansible playbooks:
        report.py name evidence.json

    Print JSON schema for input validation:
        report.py schema

ENVIRONMENT OVERRIDES:
    PVE_REPORT_DIR      Default output directory if --out is omitted (default: reports/)
    NO_COLOR            Disables ANSI color codes in console output when set
"""

from __future__ import annotations

import argparse
import difflib
import functools
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

VERSION = "1.0.0"


@functools.lru_cache(maxsize=1)
def tool_git_version() -> str:
    """Return the pve-deploy revision that produced this report, for provenance.

    Resolved from this file's own repository (tools/report.py -> repo root) so
    every generated report records exactly which tool version wrote it. Falls
    back to the module VERSION when git is unavailable (e.g. an exported tree).
    """
    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        rev = subprocess.run(
            ["git", "-C", repo_dir, "describe", "--always", "--dirty", "--tags"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        rev = ""
    return f"pve-deploy {rev}" if rev else f"pve-deploy v{VERSION} (git unavailable)"

# Secret redaction pattern rules
SECRET_PATTERNS = [
    (re.compile(r'(?i)(Authorization:\s*)([^\r\n]+)'), r'\1***REDACTED***'),
    (
        re.compile(
            r'(?i)\b(password|passphrase|secret|token|api_?key|access_?key|private_?key|passwd|pwd)'
            r'(\s*[:=]\s*)(["\']?)([^"\'\s\r\n,;]+)(["\']?)'
        ),
        r'\1\2\3***REDACTED***\5',
    ),
    (re.compile(r'(?i)\b(key)(\s*=\s*)(["\']?)([^"\'\s\r\n,;]+)(["\']?)'), r'\1\2\3***REDACTED***\5'),
    (re.compile(r'(?i)(--(?:password|token|secret|api-key|private-key|access-key))(?:=|\s+)(["\']?)([^"\'\s\r\n]+)(["\']?)'), r'\1=\2***REDACTED***\4'),
    (re.compile(r'-----BEGIN [A-Z0-9 ]+PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]+PRIVATE KEY-----'), r'***REDACTED PRIVATE KEY***'),
]


@dataclass
class Target:
    host: str
    address: str | None = None
    role: str | None = None


@dataclass
class Change:
    host: str | None = None
    description: str | None = None
    before: str | None = None
    after: str | None = None
    commands: list[str] = field(default_factory=list)


@dataclass
class VerificationCheck:
    name: str
    command: str | None = None
    expected: str | None = None
    actual: str | None = None
    result: str = "pass"


@dataclass
class Artifact:
    path: str
    sha256: str | None = None
    description: str | None = None


@dataclass
class Reference:
    title: str
    url_or_path: str


@dataclass
class ReportData:
    kind: str
    slug: str
    title: str
    outcome: str
    element: str | None = None
    environment: str | None = None
    operator: str | None = None
    tool: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    summary: str | None = None
    targets: list[Target] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    verification: list[VerificationCheck] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)


def sanitize_slug(slug_raw: str) -> str:
    """Sanitize raw slug string into lowercase kebab-case."""
    if not isinstance(slug_raw, str):
        return ""
    s = slug_raw.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-')


def redact_text(text: str | None) -> tuple[str | None, bool]:
    """Redact known secret patterns in text string. Returns (redacted_text, was_redacted)."""
    if not text:
        return text, False
    was_redacted = False
    current_text = text
    for pattern, replacement in SECRET_PATTERNS:
        new_text, count = pattern.subn(replacement, current_text)
        if count > 0:
            was_redacted = True
            current_text = new_text
    return current_text, was_redacted


def fence_code(content: str, lang: str = "") -> str:
    """Safely fence code blocks by using a backtick sequence longer than any inside content."""
    max_ticks = 0
    for match in re.finditer(r'`+', content):
        max_ticks = max(max_ticks, len(match.group(0)))
    fence_len = max(3, max_ticks + 1)
    fence = "`" * fence_len
    return f"{fence}{lang}\n{content}\n{fence}"


def parse_timestamp(ts_raw: str, field_name: str) -> tuple[datetime | None, str | None]:
    """Parse ISO timestamp string, ensuring 24-hour time component is present."""
    val = ts_raw.strip()
    if re.match(r'^\d{4}-\d{2}-\d{2}$', val):
        return None, f"Timestamp '{val}' in '{field_name}' is date-only; 24-hour time (HH:MM) is required."
    try:
        dt = datetime.fromisoformat(val.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt, None
    except ValueError as e:
        return None, f"Invalid ISO 8601 timestamp in '{field_name}': '{val}' ({e})."


def format_duration(started_dt: datetime, finished_dt: datetime) -> str:
    """Calculate and format human-readable duration string."""
    diff_sec = int((finished_dt - started_dt).total_seconds())
    if diff_sec < 0:
        return f"{diff_sec}s"
    hours = diff_sec // 3600
    minutes = (diff_sec % 3600) // 60
    seconds = diff_sec % 60
    if hours > 0:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes > 0:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def validate_and_parse_input(data: Any) -> tuple[ReportData | None, str | None, list[str]]:
    """Validate input JSON dictionary against schema and business rules.

    Returns (ReportData, filename, list_of_errors).
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return None, None, ["Input JSON root must be an object."]

    # Required fields
    kind_raw = data.get("kind")
    if not kind_raw or not isinstance(kind_raw, str) or not kind_raw.strip():
        errors.append("Mandatory field 'kind' is missing or empty.")
        kind = ""
    else:
        kind = kind_raw.strip().lower()

    slug_raw = data.get("slug")
    if not slug_raw or not isinstance(slug_raw, str):
        errors.append("Mandatory field 'slug' is missing or not a string.")
        slug = ""
    else:
        slug = sanitize_slug(slug_raw)
        if not slug:
            errors.append(f"Slug '{slug_raw}' sanitized to empty string, which is invalid.")

    title = data.get("title")
    if not title or not isinstance(title, str) or not title.strip():
        errors.append("Mandatory field 'title' is missing or empty.")
    else:
        title = title.strip()

    outcome_raw = data.get("outcome")
    valid_outcomes = {"success", "partial", "failed"}
    if not outcome_raw or not isinstance(outcome_raw, str) or outcome_raw.strip().lower() not in valid_outcomes:
        errors.append(f"Mandatory field 'outcome' must be one of {sorted(valid_outcomes)}.")
        outcome = ""
    else:
        outcome = outcome_raw.strip().lower()

    # Timestamps & filename determination
    started_at = data.get("started_at")
    finished_at = data.get("finished_at")
    started_dt: datetime | None = None
    finished_dt: datetime | None = None

    if started_at and isinstance(started_at, str) and started_at.strip():
        started_dt, err = parse_timestamp(started_at, "started_at")
        if err:
            errors.append(err)

    if finished_at and isinstance(finished_at, str) and finished_at.strip():
        finished_dt, err = parse_timestamp(finished_at, "finished_at")
        if err:
            errors.append(err)

    if errors:
        return None, None, errors

    # Determine filename timestamp
    ref_dt = started_dt or finished_dt or datetime.now().astimezone()
    ts_str = ref_dt.strftime("%Y-%m-%dT%H%M")

    if kind == "change":
        filename = f"change-report-{ts_str}-{slug}.md"
    else:
        filename = f"{kind}-{slug}-{ts_str}.md"

    # Optional fields parsing
    element = data.get("element")
    element = str(element).strip() if element else None

    environment = data.get("environment")
    environment = str(environment).strip() if environment else None

    operator = data.get("operator")
    operator = str(operator).strip() if operator else None

    tool = data.get("tool")
    tool = str(tool).strip() if tool else None

    summary = data.get("summary")
    summary = str(summary).strip() if summary else None

    # Parse targets
    targets: list[Target] = []
    if "targets" in data and isinstance(data["targets"], list):
        for item in data["targets"]:
            if isinstance(item, dict) and "host" in item and item["host"]:
                targets.append(Target(
                    host=str(item["host"]).strip(),
                    address=str(item["address"]).strip() if item.get("address") else None,
                    role=str(item["role"]).strip() if item.get("role") else None,
                ))

    # Parse changes
    changes: list[Change] = []
    if "changes" in data and isinstance(data["changes"], list):
        for item in data["changes"]:
            if isinstance(item, dict):
                cmds = item.get("commands", [])
                cmd_list = [str(c) for c in cmds] if isinstance(cmds, list) else []
                changes.append(Change(
                    host=str(item["host"]).strip() if item.get("host") else None,
                    description=str(item["description"]).strip() if item.get("description") else None,
                    before=str(item["before"]) if item.get("before") is not None else None,
                    after=str(item["after"]) if item.get("after") is not None else None,
                    commands=cmd_list,
                ))

    # Parse verification
    verification: list[VerificationCheck] = []
    if "verification" in data and isinstance(data["verification"], list):
        for item in data["verification"]:
            if isinstance(item, dict) and "name" in item and item["name"]:
                res = str(item.get("result", "pass")).strip().lower()
                if res not in {"pass", "fail", "warn"}:
                    res = "pass"
                verification.append(VerificationCheck(
                    name=str(item["name"]).strip(),
                    command=str(item["command"]).strip() if item.get("command") else None,
                    expected=str(item["expected"]).strip() if item.get("expected") else None,
                    actual=str(item["actual"]) if item.get("actual") is not None else None,
                    result=res,
                ))

    # Parse artifacts
    artifacts: list[Artifact] = []
    if "artifacts" in data and isinstance(data["artifacts"], list):
        for item in data["artifacts"]:
            if isinstance(item, dict) and "path" in item and item["path"]:
                artifacts.append(Artifact(
                    path=str(item["path"]).strip(),
                    sha256=str(item["sha256"]).strip() if item.get("sha256") else None,
                    description=str(item["description"]).strip() if item.get("description") else None,
                ))

    # Parse notes
    notes: list[str] = []
    if "notes" in data and isinstance(data["notes"], list):
        notes = [str(n).strip() for n in data["notes"] if str(n).strip()]

    # Parse references
    references: list[Reference] = []
    if "references" in data and isinstance(data["references"], list):
        for item in data["references"]:
            if isinstance(item, dict) and "title" in item and "url_or_path" in item:
                references.append(Reference(
                    title=str(item["title"]).strip(),
                    url_or_path=str(item["url_or_path"]).strip(),
                ))

    report_obj = ReportData(
        kind=kind,
        slug=slug,
        title=title,
        outcome=outcome,
        element=element,
        environment=environment,
        operator=operator,
        tool=tool,
        started_at=started_at,
        finished_at=finished_at,
        summary=summary,
        targets=targets,
        changes=changes,
        verification=verification,
        artifacts=artifacts,
        notes=notes,
        references=references,
    )

    return report_obj, filename, []


def render_markdown(report: ReportData) -> str:
    """Render ReportData model into production-grade Markdown document."""
    redaction_occurred = False

    def sanitize(text: str | None) -> str | None:
        nonlocal redaction_occurred
        res, red = redact_text(text)
        if red:
            redaction_occurred = True
        return res

    lines: list[str] = []

    # Title
    clean_title = sanitize(report.title) or report.title
    lines.append(f"# {clean_title}")
    lines.append("")

    # Placeholder for Redaction Notice (inserted at index if needed)
    notice_index = len(lines)

    # Metadata Table
    metadata_rows: list[tuple[str, str]] = []
    metadata_rows.append(("Kind", report.kind.title()))
    if report.element:
        metadata_rows.append(("Element", report.element.upper()))
    if report.environment:
        metadata_rows.append(("Environment", report.environment))
    if report.operator:
        metadata_rows.append(("Operator", report.operator))
    if report.tool:
        metadata_rows.append(("Tool", report.tool))
    if report.started_at:
        metadata_rows.append(("Started", report.started_at))
    if report.finished_at:
        metadata_rows.append(("Finished", report.finished_at))

    # Calculate duration if both start/finish present and parseable
    if report.started_at and report.finished_at:
        s_dt, _ = parse_timestamp(report.started_at, "started_at")
        f_dt, _ = parse_timestamp(report.finished_at, "finished_at")
        if s_dt and f_dt:
            metadata_rows.append(("Duration", format_duration(s_dt, f_dt)))

    metadata_rows.append(("Outcome", f"`{report.outcome.upper()}`"))
    gen_time = datetime.now().astimezone().isoformat(timespec="seconds")
    metadata_rows.append(("Generated At", gen_time))
    metadata_rows.append(("Tool Version", tool_git_version()))

    lines.append("| Field | Value |")
    lines.append("| :--- | :--- |")
    for k, v in metadata_rows:
        lines.append(f"| **{k}** | {v} |")
    lines.append("")

    # Outcome Section
    lines.append("## Outcome")
    lines.append("")
    verdict_str = report.outcome.upper()
    lines.append(f"**Verdict**: `{verdict_str}`")

    if report.verification:
        pass_cnt = sum(1 for v in report.verification if v.result == "pass")
        fail_cnt = sum(1 for v in report.verification if v.result == "fail")
        warn_cnt = sum(1 for v in report.verification if v.result == "warn")
        total_cnt = len(report.verification)
        lines.append("")
        lines.append(
            f"**Verification Checks**: {pass_cnt} passed, {fail_cnt} failed, {warn_cnt} warnings (Total: {total_cnt})"
        )
    lines.append("")

    # Summary Section
    if report.summary:
        clean_sum = sanitize(report.summary)
        if clean_sum:
            lines.append("## Summary")
            lines.append("")
            lines.append(clean_sum)
            lines.append("")

    # Targets Section
    if report.targets:
        lines.append("## Targets")
        lines.append("")
        lines.append("| Host | Address | Role |")
        lines.append("| :--- | :--- | :--- |")
        for t in report.targets:
            h = sanitize(t.host) or "-"
            a = sanitize(t.address) or "-"
            r = sanitize(t.role) or "-"
            lines.append(f"| {h} | {a} | {r} |")
        lines.append("")

    # Changes Applied Section
    if report.changes:
        lines.append("## Changes Applied")
        lines.append("")
        for idx, ch in enumerate(report.changes, 1):
            desc = sanitize(ch.description) or f"Change #{idx}"
            host_info = f" ({sanitize(ch.host)})" if ch.host else ""
            lines.append(f"### Change {idx}: {desc}{host_info}")
            lines.append("")

            if ch.commands:
                lines.append("#### Executed Commands")
                cmds_clean = [sanitize(c) or c for c in ch.commands]
                cmds_block = "\n".join(cmds_clean)
                lines.append(fence_code(cmds_block, "bash"))
                lines.append("")

            clean_before = sanitize(ch.before)
            clean_after = sanitize(ch.after)

            # Unified Diff if both before & after are multi-line
            if clean_before is not None and clean_after is not None:
                is_multiline = ("\n" in clean_before) or ("\n" in clean_after)
                if is_multiline:
                    b_lines = [line + "\n" for line in clean_before.splitlines()]
                    a_lines = [line + "\n" for line in clean_after.splitlines()]
                    diff = list(difflib.unified_diff(b_lines, a_lines, fromfile="before", tofile="after"))
                    if diff:
                        lines.append("#### Unified Diff")
                        diff_text = "".join(diff).rstrip("\n")
                        lines.append(fence_code(diff_text, "diff"))
                        lines.append("")

            if clean_before is not None:
                lines.append("#### Before State")
                lines.append(fence_code(clean_before, "text"))
                lines.append("")

            if clean_after is not None:
                lines.append("#### After State")
                lines.append(fence_code(clean_after, "text"))
                lines.append("")

    # Verification Section
    if report.verification:
        lines.append("## Verification")
        lines.append("")
        lines.append("| Check Name | Result | Expected | Actual |")
        lines.append("| :--- | :--- | :--- | :--- |")

        non_passing: list[VerificationCheck] = []
        for v in report.verification:
            c_name = sanitize(v.name) or "-"
            c_res = f"`{v.result.upper()}`"
            c_exp = sanitize(v.expected) or "-"
            c_act = sanitize(v.actual) or "-"
            # Shorten actual preview for table if multi-line
            c_act_inline = c_act.replace("\n", " ")
            if len(c_act_inline) > 60:
                c_act_inline = c_act_inline[:57] + "..."
            lines.append(f"| {c_name} | {c_res} | {c_exp} | {c_act_inline} |")

            if v.result != "pass":
                non_passing.append(v)
        lines.append("")

        if non_passing:
            lines.append("### Non-Passing Check Details")
            lines.append("")
            for np in non_passing:
                np_name = sanitize(np.name)
                lines.append(f"#### Check: {np_name} (`{np.result.upper()}`)")
                if np.command:
                    lines.append(f"- **Command**: `{sanitize(np.command)}`")
                if np.expected:
                    lines.append(f"- **Expected**: {sanitize(np.expected)}")
                lines.append("")
                if np.actual is not None:
                    lines.append("**Actual Output**:")
                    lines.append(fence_code(sanitize(np.actual) or "", "text"))
                    lines.append("")

    # Reproduce This Change Section
    all_cmds: list[tuple[str | None, list[str]]] = []
    if report.changes:
        for ch in report.changes:
            if ch.commands:
                cmds_clean = [sanitize(c) or c for c in ch.commands]
                all_cmds.append((ch.host, cmds_clean))

    if all_cmds:
        lines.append("## Reproduce This Change")
        lines.append("")
        lines.append("The following commands reproduce this change independently by hand:")
        lines.append("")
        reproduce_blocks: list[str] = []
        for host, cmds in all_cmds:
            h_clean = sanitize(host) if host else "unassigned"
            reproduce_blocks.append(f"# Host: {h_clean}")
            reproduce_blocks.extend(cmds)
            reproduce_blocks.append("")
        reproduce_text = "\n".join(reproduce_blocks).rstrip()
        lines.append(fence_code(reproduce_text, "bash"))
        lines.append("")

    # Artifacts Section
    if report.artifacts:
        lines.append("## Artifacts")
        lines.append("")
        lines.append("| Path | SHA256 | Description |")
        lines.append("| :--- | :--- | :--- |")
        for art in report.artifacts:
            p = sanitize(art.path) or "-"
            s = f"`{sanitize(art.sha256)}`" if art.sha256 else "-"
            d = sanitize(art.description) or "-"
            lines.append(f"| {p} | {s} | {d} |")
        lines.append("")

    # Notes Section
    if report.notes:
        lines.append("## Notes")
        lines.append("")
        for note in report.notes:
            clean_n = sanitize(note)
            if clean_n:
                lines.append(f"- {clean_n}")
        lines.append("")

    # References Section
    if report.references:
        lines.append("## References")
        lines.append("")
        for ref in report.references:
            t = sanitize(ref.title)
            u = sanitize(ref.url_or_path)
            lines.append(f"- [{t}]({u})")
        lines.append("")

    # Footer
    lines.append("---")
    lines.append(f"*Report generated by pve-deploy report tool v{VERSION}*")
    lines.append("")

    # Insert Redaction Notice at top if any secrets were redacted
    if redaction_occurred:
        lines.insert(
            notice_index,
            "> **Notice**: Automatic secret redaction was applied to captured text in this report.\n",
        )

    return "\n".join(lines)


# Check-name prefix -> human section heading. A Phase 13 report carries 61 checks;
# flat, they are unreadable. Grouped, a reader sees which subsystem failed before
# reading a single row. First match wins; anything unmatched lands in "Other".
CHECK_GROUPS: list[tuple[str, str]] = [
    ("active_staged_interfaces", "Network configuration drift"),
    ("lacp_bond_", "LACP bonds"),
    ("interface_", "VLAN interfaces and addresses"),
    ("gateway_", "Gateway reachability"),
    ("icmp_", "Peer reachability (ICMP)"),
    ("mtu_", "MTU path probes"),
    ("iperf", "Throughput mesh (iperf3)"),
    ("pve_firewall", "Firewall runtime state"),
    ("hardware_evidence", "Hardware evidence"),
    ("hardware_version_field", "Hardware evidence"),
    ("intra_host_version_consistency", "Firmware consistency"),
    ("fleet_version_", "Firmware consistency"),
]

# Status roles are the reserved status palette, never the categorical slots, and
# every badge pairs the colour with an icon AND a text label -- on a light surface
# warning/serious sit below 3:1 by design, so colour never carries meaning alone.
STATUS_ROLES: dict[str, tuple[str, str]] = {
    "pass": ("good", "Pass"),
    "warn": ("warning", "Warning"),
    "fail": ("critical", "Fail"),
}

HTML_STYLE = """
:root {
  color-scheme: light;
  --surface: #fcfcfb;
  --plane: #f9f9f7;
  --ink: #0b0b0b;
  --ink-2: #52514e;
  --muted: #898781;
  --rule: #e1e0d9;
  --ring: rgba(11,11,11,0.10);
  --good: #0ca30c;
  --good-text: #006300;
  --warning: #fab219;
  /* The amber fill is a background hue; as INK on a light surface it needs
     darkening to stay legible, which is why text has its own step. */
  --warning-text: #8a5a00;
  --critical: #d03b3b;
  --critical-text: #b32020;
  --good-wash: rgba(12,163,12,0.10);
  --warning-wash: rgba(250,178,25,0.16);
  --critical-wash: rgba(208,59,59,0.10);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface: #1a1a19;
    --plane: #0d0d0d;
    --ink: #ffffff;
    --ink-2: #c3c2b7;
    --muted: #898781;
    --rule: #2c2c2a;
    --ring: rgba(255,255,255,0.10);
    --good-text: #0ca30c;
    --warning-text: #fab219;
    --critical-text: #ef7c7c;
    --good-wash: rgba(12,163,12,0.16);
    --warning-wash: rgba(250,178,25,0.16);
    --critical-wash: rgba(208,59,59,0.20);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface: #1a1a19;
  --plane: #0d0d0d;
  --ink: #ffffff;
  --ink-2: #c3c2b7;
  --muted: #898781;
  --rule: #2c2c2a;
  --ring: rgba(255,255,255,0.10);
  --good-text: #0ca30c;
  --warning-text: #fab219;
  --critical-text: #ef7c7c;
  --good-wash: rgba(12,163,12,0.16);
  --warning-wash: rgba(250,178,25,0.16);
  --critical-wash: rgba(208,59,59,0.20);
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--plane);
  color: var(--ink);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
  padding: 32px 20px 72px;
}
.wrap { max-width: 1120px; margin: 0 auto; }
a { color: inherit; }

.masthead { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start; justify-content: space-between; }
.masthead > div { flex: 1 1 420px; min-width: 0; }
.masthead .theme-toggle { flex: 0 0 auto; }
h1 { font-size: 22px; line-height: 1.25; margin: 0 0 6px; font-weight: 650; letter-spacing: -0.01em; }
.subtitle { color: var(--ink-2); margin: 0; font-size: 14px; }
.theme-toggle {
  border: 1px solid var(--ring); background: var(--surface); color: var(--ink-2);
  border-radius: 999px; padding: 7px 14px; font: inherit; font-size: 13px; cursor: pointer;
}
.theme-toggle:hover { color: var(--ink); }

.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; }

/* Hero: the one number the report leads with. */
.hero { display: flex; flex-wrap: wrap; align-items: center; gap: 28px; padding: 22px 24px; margin: 22px 0 18px; }
.hero-figure { font-size: 52px; line-height: 1; font-weight: 680; letter-spacing: -0.02em; }
.hero-label { color: var(--ink-2); font-size: 13px; margin-top: 6px; }
.hero-meter { flex: 1 1 260px; min-width: 220px; }

/* display:block matters -- these are spans inside the summary row, and height is
   ignored on an inline box, which silently renders the meter invisible. */
.meter { display: block; height: 8px; border-radius: 999px; background: var(--rule); overflow: hidden; }
.meter > span { display: block; height: 100%; border-radius: 999px; background: var(--good); }
.meter.has-fail > span { background: var(--critical); }

.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 22px; }
.kpi { padding: 14px 16px; }
.kpi .v { font-size: 28px; font-weight: 660; line-height: 1.1; }
.kpi .k { color: var(--ink-2); font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; margin-top: 4px; }
.kpi.is-good .v { color: var(--good-text); }
.kpi.is-critical .v { color: var(--critical-text); }
.kpi.is-warning .v { color: var(--warning-text); }

.meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 2px 18px; padding: 16px 20px; margin-bottom: 22px; }
.meta div { padding: 5px 0; border-bottom: 1px solid var(--rule); }
.meta dt { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }
.meta dd { margin: 2px 0 0; font-size: 14px; word-break: break-word; }

h2 { font-size: 15px; margin: 26px 0 10px; font-weight: 640; }
.controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 0 0 14px; }
.controls input[type=search] {
  flex: 1 1 240px; min-width: 180px; padding: 8px 12px; font: inherit; font-size: 14px;
  border: 1px solid var(--ring); border-radius: 8px; background: var(--surface); color: var(--ink);
}
.controls label { display: inline-flex; gap: 7px; align-items: center; color: var(--ink-2); font-size: 14px; cursor: pointer; }
.count-note { color: var(--muted); font-size: 13px; }

details.group { margin-bottom: 10px; overflow: hidden; }
details.group > summary {
  display: flex; flex-wrap: wrap; align-items: center; gap: 12px;
  padding: 12px 16px; cursor: pointer; list-style: none; font-weight: 600;
}
details.group > summary::-webkit-details-marker { display: none; }
details.group > summary::before { content: "\\25B8"; color: var(--muted); transition: transform .12s ease; }
details.group[open] > summary::before { transform: rotate(90deg); }
.group-name { flex: 1 1 auto; }
.group-meter { flex: 0 0 110px; }
.group-tally { color: var(--ink-2); font-size: 13px; font-variant-numeric: tabular-nums; }

.table-scroll { overflow-x: auto; border-top: 1px solid var(--rule); }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th, td { text-align: left; padding: 9px 16px; border-bottom: 1px solid var(--rule); vertical-align: top; }
th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; }
tbody tr:last-child td { border-bottom: none; }
td.name { font-weight: 550; min-width: 210px; }
td code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
td.val { color: var(--ink-2); font-variant-numeric: tabular-nums; max-width: 460px; overflow-wrap: anywhere; }

/* Icon + label, so status never rests on colour alone. */
.badge { display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px; border-radius: 999px; font-size: 12px; font-weight: 620; white-space: nowrap; }
.badge .ico { font-size: 11px; line-height: 1; }
.badge.good { background: var(--good-wash); color: var(--good-text); }
.badge.warning { background: var(--warning-wash); color: var(--ink); }
.badge.critical { background: var(--critical-wash); color: var(--critical-text); }

ul.plain { margin: 0; padding-left: 18px; color: var(--ink-2); }
ul.plain li { margin: 4px 0; overflow-wrap: anywhere; }
.empty { color: var(--muted); font-size: 14px; padding: 14px 16px; }
footer { color: var(--muted); font-size: 12px; margin-top: 34px; text-align: center; }
tr[hidden], details[hidden] { display: none !important; }

@media print {
  body { background: #fff; padding: 0; }
  .controls, .theme-toggle { display: none; }
  details.group { break-inside: avoid; }
}
"""

HTML_SCRIPT = """
(function () {
  var root = document.documentElement;
  var btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', function () {
      var dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
      var current = root.getAttribute('data-theme') || (dark ? 'dark' : 'light');
      root.setAttribute('data-theme', current === 'dark' ? 'light' : 'dark');
    });
  }
  var search = document.getElementById('filter-text');
  var failOnly = document.getElementById('filter-fail');
  var note = document.getElementById('filter-count');
  function apply() {
    var q = (search && search.value || '').trim().toLowerCase();
    var only = failOnly && failOnly.checked;
    var shown = 0, total = 0;
    function rowHit(row) {
      return !q || row.textContent.toLowerCase().indexOf(q) !== -1;
    }
    // Rows that live outside a collapsible -- the failures table, fleet variance --
    // match on their own text, so those tables filter too.
    document.querySelectorAll('tr[data-result]').forEach(function (row) {
      if (row.closest('details.group')) { return; }
      var isFail = row.getAttribute('data-result') !== 'pass';
      row.hidden = !(rowHit(row) && (!only || isFail));
    });
    // Counting happens per group, not per row: a failing check appears both in the
    // failures table and in its own section, and counting rows would inflate the
    // denominator past the number of checks that actually ran.
    document.querySelectorAll('details.group').forEach(function (group) {
      var name = group.querySelector('.group-name');
      // A search matching the SECTION reveals all of it. The network name lives in
      // the heading, not in the rows, so matching rows alone made "corosync" find
      // its ICMP and MTU matrices but hide its iperf3 table -- 24 of 48 checks,
      // silently. A group hit has to win over the per-row test.
      var gtext = ((group.getAttribute('data-search') || '') + ' ' +
                   (name ? name.textContent : '')).toLowerCase();
      var groupHit = !q || gtext.indexOf(q) !== -1;
      var rows = group.querySelectorAll('tr[data-result]');
      var visible = 0;
      if (rows.length) {
        rows.forEach(function (row) {
          var isFail = row.getAttribute('data-result') !== 'pass';
          var show = (groupHit || rowHit(row)) && (!only || isFail);
          row.hidden = !show;
          if (show) { visible++; }
        });
        total += rows.length;
      } else {
        // A matrix section has no per-check row to hide, so it filters as a unit
        // on its heading text and its own failure tally.
        var count = parseInt(group.getAttribute('data-count') || '0', 10);
        var fails = parseInt(group.getAttribute('data-fail') || '0', 10);
        visible = (groupHit && (!only || fails > 0)) ? (only ? fails : count) : 0;
        total += count;
      }
      shown += visible;
      group.hidden = visible === 0;
      if (visible > 0 && (q || only)) { group.open = true; }
    });
    if (note) {
      note.textContent = (q || only) ? ('Showing ' + shown + ' of ' + total + ' checks') : (total + ' checks');
    }
  }
  if (search) { search.addEventListener('input', apply); }
  if (failOnly) { failOnly.addEventListener('change', apply); }
  apply();
})();
"""


def html_escape(value: Any) -> str:
    """Escape a value for safe HTML text/attribute interpolation."""
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def check_group_label(name: str) -> str:
    """Map a check name to its section heading."""
    lowered = (name or "").lower()
    for prefix, label in CHECK_GROUPS:
        if lowered.startswith(prefix):
            return label
    return "Other checks"


def normalise_result(result: str | None) -> str:
    """Collapse a result string onto one of the three status roles."""
    value = (result or "").strip().lower()
    if value in ("pass", "passed", "ok", "success"):
        return "pass"
    if value in ("warn", "warning"):
        return "warn"
    return "fail"


def status_badge(result: str | None) -> str:
    """Render a status badge carrying an icon AND a label, never colour alone."""
    key = normalise_result(result)
    role, label = STATUS_ROLES[key]
    icon = {"pass": "&#10003;", "warn": "&#9888;", "fail": "&#10007;"}[key]
    return f'<span class="badge {role}"><span class="ico" aria-hidden="true">{icon}</span>{label}</span>'


def render_html(report: ReportData) -> str:
    """Render the report as a self-contained HTML page.

    Deliberately dependency-free: no CDN, no fonts, no build step. The file has to
    survive being copied around, attached to a ticket, or opened from a share with
    no network, which rules out anything external.
    """
    checks = report.verification or []
    total = len(checks)
    passed = sum(1 for c in checks if normalise_result(c.result) == "pass")
    warned = sum(1 for c in checks if normalise_result(c.result) == "warn")
    failed = total - passed - warned
    pct = round((passed / total) * 100) if total else 0
    overall_fail = failed > 0 or (report.outcome or "").lower() not in ("success", "pass", "ok")

    # Preserve first-seen group order so the page reads in execution order.
    grouped: dict[str, list[VerificationCheck]] = {}
    for check in checks:
        grouped.setdefault(check_group_label(check.name), []).append(check)

    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    meta_rows: list[tuple[str, str]] = []
    for label, value in (
        ("Element", report.element),
        ("Environment", report.environment),
        ("Outcome", report.outcome),
        ("Tool", report.tool),
        ("Tool Version", tool_git_version()),
        ("Operator", report.operator),
        ("Started", report.started_at),
        ("Finished", report.finished_at),
    ):
        if value:
            meta_rows.append((label, str(value)))
    for target in report.targets:
        detail = target.address or ""
        if target.role:
            detail = f"{detail} ({target.role})" if detail else target.role
        meta_rows.append(("Target", f"{target.host} {detail}".strip()))

    out: list[str] = []
    a = out.append
    a("<!doctype html>")
    a('<html lang="en">')
    a("<head>")
    a('<meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width, initial-scale=1">')
    a(f"<title>{html_escape(report.title)}</title>")
    a(f"<style>{HTML_STYLE}</style>")
    a("</head>")
    a("<body>")
    a('<div class="wrap">')

    a('<header class="masthead">')
    a("<div>")
    a(f"<h1>{html_escape(report.title)}</h1>")
    if report.summary:
        a(f'<p class="subtitle">{html_escape(report.summary)}</p>')
    a("</div>")
    a('<button class="theme-toggle" id="theme-toggle" type="button">Toggle theme</button>')
    a("</header>")

    # Hero figure + overall meter.
    a('<section class="card hero">')
    a("<div>")
    a(f'<div class="hero-figure">{pct}%</div>')
    a(f'<div class="hero-label">{passed} of {total} checks passed</div>')
    a("</div>")
    a('<div class="hero-meter">')
    a(f'<div class="meter{" has-fail" if overall_fail else ""}"><span style="width:{pct}%"></span></div>')
    a(f'<div class="hero-label">{status_badge("fail" if overall_fail else "pass")} '
      f"&nbsp;{html_escape((report.outcome or '').upper())}</div>")
    a("</div>")
    a("</section>")

    a('<section class="kpis">')
    for value, label, cls in (
        (passed, "Passed", "is-good" if passed else ""),
        (failed, "Failed", "is-critical" if failed else ""),
        (warned, "Warnings", ""),
        (total, "Total checks", ""),
    ):
        a(f'<div class="card kpi {cls}"><div class="v">{value}</div><div class="k">{label}</div></div>')
    a("</section>")

    if meta_rows:
        a('<dl class="card meta">')
        for label, value in meta_rows:
            a(f"<div><dt>{html_escape(label)}</dt><dd>{html_escape(value)}</dd></div>")
        a("</dl>")

    if checks:
        a("<h2>Verification checks</h2>")
        a('<div class="controls">')
        a('<input type="search" id="filter-text" placeholder="Filter checks by name, expected or actual value">')
        a('<label><input type="checkbox" id="filter-fail"> Failures only</label>')
        a('<span class="count-note" id="filter-count"></span>')
        a("</div>")

        for group_name, group_checks in grouped.items():
            g_total = len(group_checks)
            g_pass = sum(1 for c in group_checks if normalise_result(c.result) == "pass")
            g_fail = g_total - g_pass
            g_pct = round((g_pass / g_total) * 100) if g_total else 0
            # Groups holding a failure start open; clean ones stay collapsed so the
            # reader's attention lands on what is broken.
            open_attr = " open" if g_fail else ""
            a(f'<details class="card group"{open_attr}>')
            a("<summary>")
            a(f'<span class="group-name">{html_escape(group_name)}</span>')
            a(f'<span class="group-meter"><span class="meter{" has-fail" if g_fail else ""}">'
              f'<span style="width:{g_pct}%"></span></span></span>')
            a(f'<span class="group-tally">{g_pass}/{g_total}</span>')
            a(status_badge("fail" if g_fail else "pass"))
            a("</summary>")
            a('<div class="table-scroll"><table>')
            a("<thead><tr><th>Status</th><th>Check</th><th>Expected</th><th>Actual</th></tr></thead><tbody>")
            for check in group_checks:
                res = normalise_result(check.result)
                a(f'<tr data-result="{res}">')
                a(f"<td>{status_badge(check.result)}</td>")
                name_cell = html_escape(check.name)
                if check.command:
                    name_cell += f'<br><code class="mono">{html_escape(check.command)}</code>'
                a(f'<td class="name">{name_cell}</td>')
                a(f'<td class="val">{html_escape(check.expected)}</td>')
                a(f'<td class="val">{html_escape(check.actual)}</td>')
                a("</tr>")
            a("</tbody></table></div>")
            a("</details>")

    if report.changes:
        a("<h2>Changes applied</h2>")
        for change in report.changes:
            a('<div class="card" style="padding:14px 18px;margin-bottom:10px">')
            head = change.description or "Change"
            if change.host:
                head = f"{head} &mdash; {html_escape(change.host)}"
            a(f"<strong>{html_escape(change.description or 'Change')}</strong>")
            if change.host:
                a(f' <span class="count-note">on {html_escape(change.host)}</span>')
            if change.before or change.after:
                a(f'<div class="count-note" style="margin-top:6px">'
                  f"before: {html_escape(change.before)} &rarr; after: {html_escape(change.after)}</div>")
            if change.commands:
                a('<ul class="plain" style="margin-top:8px">')
                for cmd in change.commands:
                    a(f'<li><code class="mono">{html_escape(cmd)}</code></li>')
                a("</ul>")
            a("</div>")

    if report.artifacts:
        a("<h2>Artifacts</h2>")
        a('<div class="card"><div class="table-scroll"><table>')
        a("<thead><tr><th>Path</th><th>Description</th></tr></thead><tbody>")
        for artifact in report.artifacts:
            a(f'<tr><td class="name"><code class="mono">{html_escape(artifact.path)}</code></td>'
              f'<td class="val">{html_escape(artifact.description)}</td></tr>')
        a("</tbody></table></div></div>")

    if report.notes:
        a("<h2>Notes</h2>")
        a('<div class="card" style="padding:14px 18px"><ul class="plain">')
        for note in report.notes:
            a(f"<li>{html_escape(note)}</li>")
        a("</ul></div>")

    a(f'<footer>Generated {html_escape(generated)} by pve-deploy report tool v{VERSION}. '
      "Markdown companion alongside this file.</footer>")
    a("</div>")
    a(f"<script>{HTML_SCRIPT}</script>")
    a("</body></html>")
    return "\n".join(out) + "\n"


ENVIRONMENT_SCHEMA = "pve.phase13.network_validation.v2"

# Above this host count an N x N matrix stops being readable and starts being
# wallpaper: 30 hosts is 870 cells per network, 300 is 89,700. The exception table
# carries the authoritative list at any size, so the matrix is the extra, not the
# source of truth.
MATRIX_HOST_LIMIT = 24

# Pair sections read in the order an operator diagnoses in -- can it reach, does
# the MTU hold, how fast is it, what does latency do under load -- rather than
# alphabetically, which put flent before icmp and buried the reachability result
# an operator looks at first.
PAIR_KIND_ORDER = ["icmp", "mtu", "iperf3", "flent"]

# Reachability and MTU assert; iperf3 and flent measure. Keeping them in separate
# sections stops a throughput figure from being read as a verdict.
PAIR_KIND_IS_MEASUREMENT = {"iperf3", "flent"}

# Section headings say what was tested, not which inventory key it came from. The
# v1 per-node report did this via check_group_label and the v2 environment report
# regressed to raw keys: "pve_ceph_replica - flent" states the schema, whereas
# "Ceph replication - latency under load" states the question being answered. The
# machine key is still rendered beside the heading, because an operator
# correlating against inventory or grepping the payload needs the exact string.
NETWORK_LABELS = {
    "pve_management": "Management",
    "pve_corosync": "Corosync cluster ring",
    "pve_live_migration": "Live migration",
    "pve_underlay": "Underlay (VXLAN transport)",
    "pve_ceph_access": "Ceph client access",
    "pve_ceph_replica": "Ceph replication",
}
PAIR_KIND_LABELS = {
    "icmp": "peer reachability (ICMP)",
    "mtu": "MTU path probe",
    "iperf3": "throughput (iperf3)",
    "flent": "latency under load (flent RRUL)",
}


def network_label(network: str) -> str:
    return NETWORK_LABELS.get(network) or (network or "").replace("_", " ").strip()


def pair_group_label(network: str, kind: str) -> str:
    """Human heading for a (network, probe kind) section."""
    return f"{network_label(network)} &mdash; {PAIR_KIND_LABELS.get(kind) or kind}"


# The MTU a probe targets is a site fact -- it follows whatever the NOS and the
# platform support -- so a reader cannot assume 9000 or 1500. It is recorded in
# each check's `expected`, but a matrix cell shows only a tick, so without this
# the target was visible nowhere on the page.
TARGET_MTU_RE = re.compile(r"\bmtu\s+(\d+)", re.IGNORECASE)


def group_target_note(group: list[dict[str, Any]]) -> str:
    """Short qualifier for a section heading when every probe shares one target.

    Silent unless the whole group agrees: a heading that states a single figure
    for a group holding several would be worse than saying nothing.
    """
    values: set[str] = set()
    for probe in group:
        found = TARGET_MTU_RE.search(str(probe.get("expected") or ""))
        if not found:
            return ""
        values.add(found.group(1))
    return f"MTU {values.pop()}" if len(values) == 1 else ""


def _pair_kind_rank(kind: str) -> int:
    return PAIR_KIND_ORDER.index(kind) if kind in PAIR_KIND_ORDER else len(PAIR_KIND_ORDER)

ENVIRONMENT_EXTRA_STYLE = """
/* The machine key stays visible beside the human heading: the heading is what
   an operator reads, the key is what they grep and correlate with inventory. */
.group-key { color: var(--muted); font-size: 11px; font-weight: 400; }
/* The probe's target value, stated in the heading rather than only on hover:
   for MTU it is site-specific and cannot be assumed from the segment name. */
.group-target {
  font-size: 11px; font-weight: 600; color: var(--ink-2);
  border: 1px solid var(--rule); border-radius: 4px; padding: 1px 6px; margin-left: 4px;
}
.controls.is-sticky {
  position: sticky; top: 0; z-index: 5; background: var(--plane);
  padding: 10px 0; margin: 0 0 14px; border-bottom: 1px solid var(--rule);
}
@media print { .controls.is-sticky { position: static; } }
.matrix-wrap { overflow: auto; max-height: 70vh; }
table.matrix { border-collapse: separate; border-spacing: 2px; font-size: 11px; }
table.matrix th { padding: 3px 6px; font-size: 10px; white-space: nowrap; text-transform: none; letter-spacing: 0; }
table.matrix th.row-head { text-align: right; position: sticky; left: 0; background: var(--surface); z-index: 1; }
table.matrix td { padding: 0; border: none; }
.cell {
  width: 20px; height: 20px; border-radius: 4px; display: flex; align-items: center;
  justify-content: center; font-size: 10px; font-weight: 700; color: #fff;
}
.cell.pass { background: var(--good); }
.cell.fail { background: var(--critical); }
.cell.warn { background: var(--warning); color: #0b0b0b; }
.cell.self { background: var(--rule); color: var(--muted); }
.cell.none { background: transparent; border: 1px dashed var(--rule); }
/* An asymmetric path is its own finding: ringed and re-glyphed so it reads as
   different even when this direction passed. */
.cell.asym { box-shadow: 0 0 0 2px var(--warning); }
.legend { display: flex; gap: 14px; flex-wrap: wrap; margin: 8px 0 0; color: var(--ink-2); font-size: 12px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 12px; height: 12px; border-radius: 3px; display: inline-block; font-style: normal; }
.dev { color: var(--critical-text); font-weight: 600; }
/* Configured and derived values are context, not findings. */
.muted-metric { color: var(--muted); }
.norm-cell { color: var(--ink-2); max-width: 340px; overflow-wrap: anywhere; }
"""


def _sev(result: str) -> int:
    return {"pass": 0, "warn": 1, "fail": 2}[normalise_result(result)]


def _pct(passed: int, total: int) -> int:
    return round((passed / total) * 100) if total else 0


def _worst(items: list[dict[str, Any]]) -> str:
    """Worst result in a group.

    A group holding only warnings must not badge as Fail: conflating the two
    makes a degraded-but-working path look like an outage and erodes trust in
    the colour, which is the one signal a reader scans for.
    """
    worst = "pass"
    for item in items:
        res = normalise_result(item.get("result"))
        if _sev(res) > _sev(worst):
            worst = res
    return worst


def _meter(passed: int, total: int) -> str:
    pct = _pct(passed, total)
    cls = " has-fail" if passed < total else ""
    return f'<span class="meter{cls}"><span style="width:{pct}%"></span></span>'


def render_environment_markdown(payload: dict[str, Any]) -> str:
    """Render the v2 environment payload as Markdown (the contractual artifact)."""
    run = payload.get("run") or {}
    cluster = payload.get("cluster") or {}
    summary = payload.get("summary") or {}
    checks = payload.get("checks") or {}
    hosts = cluster.get("hosts") or []

    out: list[str] = []
    a = out.append
    verdict = "FAILED" if summary.get("failed") else "SUCCESS"
    a(f"# PVE Phase 13 network validation — {run.get('environment', 'environment')} ({verdict})")
    a("")
    a(f"- **Run:** `{run.get('id', '')}`")
    a(f"- **Site:** {run.get('site', '')}")
    a(f"- **Tool:** {run.get('tool', '')}")
    a(f"- **Hosts:** {len(hosts)} ({', '.join(hosts)})")
    a(f"- **Networks:** {', '.join(cluster.get('networks') or [])}")
    a(f"- **Result:** {summary.get('passed', 0)}/{summary.get('total', 0)} checks passed, "
      f"{summary.get('failed', 0)} failed, {summary.get('warned', 0)} warnings")
    if summary.get("missing_hosts"):
        a(f"- **Missing hosts (no fragment reported):** {', '.join(summary['missing_hosts'])}")
    a("")

    by_scope = summary.get("by_scope") or {}
    a("## Scope summary")
    a("")
    a("| Scope | Total | Passed | Warned | Failed |")
    a("| --- | ---: | ---: | ---: | ---: |")
    for scope in ("fleet", "node", "pair"):
        s = by_scope.get(scope) or {}
        a(f"| {scope} | {s.get('total', 0)} | {s.get('passed', 0)} | {s.get('warned', 0)} | {s.get('failed', 0)} |")
    a("")

    failures = [c for c in checks.get("fleet", []) if c["result"] != "pass"]
    failures += [c for host in checks.get("nodes", {}).values() for c in host if c["result"] != "pass"]
    failures += [c for c in checks.get("pairs", []) if c["result"] != "pass"]
    a("## Failures and warnings")
    a("")
    if failures:
        a("| Result | Check | Expected | Actual |")
        a("| --- | --- | --- | --- |")
        for c in sorted(failures, key=lambda c: (-_sev(c["result"]), c["id"])):
            a(f"| `{c['result']}` | `{c['id']}` | {c.get('expected') or ''} | {c.get('actual') or ''} |")
    else:
        a("None. Every check passed.")
    a("")

    a("## Fleet variance (node-local properties)")
    a("")
    a("| Property | Fleet norm | Compliance | Deviations |")
    a("| --- | --- | ---: | --- |")
    for row in payload.get("variance") or []:
        devs = ", ".join(f"{d['host']} ({d['result']})" for d in row.get("deviations") or []) or "—"
        a(f"| {row['item']} | {row.get('norm') or ''} | {row['passed']}/{row['observed']} | {devs} |")
    a("")

    if checks.get("fleet"):
        a("## Fleet-wide checks")
        a("")
        a("| Result | Component | Expected | Actual |")
        a("| --- | --- | --- | --- |")
        for c in checks["fleet"]:
            a(f"| `{c['result']}` | {c.get('component') or c['name']} | {c.get('expected') or ''} | {c.get('actual') or ''} |")
        a("")

    pairs = checks.get("pairs") or []
    if pairs:
        a("## Pair probes")
        a("")
        a("| Network | Kind | Total | Failed |")
        a("| --- | --- | ---: | ---: |")
        seen: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for p in pairs:
            seen.setdefault((p.get("network", ""), p.get("kind", "")), []).append(p)
        for (net, kind), group in sorted(seen.items()):
            bad = sum(1 for g in group if g["result"] != "pass")
            a(f"| {net} | {kind} | {len(group)} | {bad} |")
        a("")

        # Measured probes carry numbers that are the point of running them, so
        # they get their own table rather than being reduced to a pass count.
        for (net, kind), group in sorted(seen.items()):
            columns = _metric_columns(group)
            if not columns:
                continue
            a(f"### {net} / {kind} measurements")
            a("")
            a("| Result | Source | Target | " + " | ".join(metric_label(c) for c in columns) + " |")
            a("| --- | --- | --- | " + " | ".join("---:" for _ in columns) + " |")
            for probe in sorted(group, key=lambda p: (-_sev(p["result"]), p.get("source", ""))):
                metrics = probe.get("metrics") or {}
                cells = " | ".join(
                    _format_metric(metric_value(metrics[c])) if metrics.get(c) is not None else "—"
                    for c in columns
                )
                a(f"| `{probe['result']}` | {probe.get('source')} | {probe.get('target')} | {cells} |")
            a("")

    a("---")
    a(f"*Generated by pve-deploy report tool v{VERSION} from schema `{payload.get('schema_version')}`. "
      "HTML companion alongside this file.*")
    return "\n".join(out) + "\n"


def _render_matrix(hosts: list[str], pairs: list[dict[str, Any]], network: str, kind: str) -> str:
    """Render one directional source x target grid for a network/probe kind."""
    index: dict[tuple[str, str], dict[str, Any]] = {
        (p.get("source", ""), p.get("target", "")): p for p in pairs
    }

    def asymmetric(src: str, dst: str) -> bool:
        """True when a path behaves differently in each direction.

        This is the failure that a row-versus-column comparison hides and that
        actually bit pve-01: management up, underlay down, one way only.
        Marked explicitly so it is a property of the cell rather than something
        the reader has to infer.
        """
        fwd, rev = index.get((src, dst)), index.get((dst, src))
        if fwd is None or rev is None:
            return False
        return normalise_result(fwd["result"]) != normalise_result(rev["result"])
    short = {h: h.split("-")[-1] or h for h in hosts}
    out: list[str] = []
    a = out.append
    a('<div class="matrix-wrap"><table class="matrix">')
    a("<thead><tr><th class=\"row-head\">from \\ to</th>"
      + "".join(f"<th>{html_escape(short[h])}</th>" for h in hosts) + "</tr></thead><tbody>")
    for src in hosts:
        a(f'<tr><th class="row-head">{html_escape(short[src])}</th>')
        for dst in hosts:
            if src == dst:
                a('<td><div class="cell self" title="same host">·</div></td>')
                continue
            probe = index.get((src, dst))
            if probe is None:
                a(f'<td><div class="cell none" title="{html_escape(src)} to {html_escape(dst)}: not probed"></div></td>')
                continue
            res = normalise_result(probe["result"])
            # A glyph as well as a hue: a matrix cell has no room for a label, so
            # the exception table below is the table view the colour leans on.
            glyph = {"pass": "✓", "warn": "!", "fail": "✗"}[res]
            # EXPECTED before actual. A matrix cell is a tick with no units, so
            # the tooltip is the only place a reader learns what was asked for --
            # and for MTU that target is site-specific, set by whatever the NOS
            # and platform support, not a constant anyone can assume. The probe
            # command is included for the same reason: it carries the payload
            # size actually put on the wire.
            title = f"{src} to {dst} [{network}/{kind}]: {res}"
            if probe.get("expected"):
                title += f" | expected: {probe['expected']}"
            if probe.get("command"):
                title += f" | probe: {probe['command']}"
            title += f" | actual: {probe.get('actual') or ''}"
            extra = ""
            if asymmetric(src, dst):
                rev = index.get((dst, src)) or {}
                # Ringed and glyphed, not just tinted: an asymmetric path is a
                # distinct finding even when this direction passes.
                extra = " asym"
                glyph = "⇄"
                title += f" | REVERSE DIFFERS: {dst} to {src} = {normalise_result(rev.get('result'))}"
            a(f'<td><div class="cell {res}{extra}" title="{html_escape(title)}">{glyph}</div></td>')
        a("</tr>")
    a("</tbody></table></div>")
    a('<div class="legend">'
      '<span><i class="cell pass"></i>pass</span>'
      '<span><i class="cell fail"></i>fail</span>'
      '<span><i class="cell warn"></i>warning</span>'
      '<span><i class="cell none"></i>not probed</span>'
      '<span><i class="cell pass asym"></i>&#8644; direction differs</span>'
      "</div>")
    return "\n".join(out)


# Units and semantics are derived from the key suffix rather than requiring a
# typed payload, so this works with metrics the roles already emit. A metric a
# tool was TOLD to produce (iperf3 --bandwidth) is not evidence of capacity, so
# configured values are labelled to stop them being read as measurements.
METRIC_UNITS = {
    "_mbps": "Mbit/s",
    "_ms": "ms",
    "_pct": "%",
}
CONFIGURED_METRICS = {"offered_mbps"}
# Derived from other measurements rather than measured directly. flent's
# bidirectional total is download+upload, which exceeds any single link rate and
# reads as impossible unless it says so.
DERIVED_METRICS = {"bidir_total_mbps"}


def metric_unit(key: str) -> str:
    for suffix, unit in METRIC_UNITS.items():
        if key.endswith(suffix):
            return unit
    return ""


def metric_label(key: str) -> str:
    """Human column header carrying the unit and, where it matters, the semantics."""
    base = key
    for suffix in METRIC_UNITS:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    label = base.replace("_", " ").strip()
    unit = metric_unit(key)
    if key in CONFIGURED_METRICS:
        label += " (configured)"
    elif key in DERIVED_METRICS:
        label += " (derived)"
    return f"{label} [{unit}]" if unit else label


def metric_value(raw: Any) -> Any:
    """Accept a bare number or a typed {value, unit} object."""
    if isinstance(raw, dict):
        return raw.get("value")
    return raw


def _metric_columns(group: list[dict[str, Any]]) -> list[str]:
    """Union of metric keys in a pair group, in first-seen order."""
    columns: list[str] = []
    for probe in group:
        for key in (probe.get("metrics") or {}):
            if key not in columns:
                columns.append(key)
    return columns


def _format_metric(value: Any) -> str:
    """Render a metric value without giving a float spurious precision."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def _render_pair_metrics_table(group: list[dict[str, Any]], columns: list[str]) -> str:
    """Render measured pair results as a table.

    Characterisation probes (iperf3, flent) produce numbers, and a pass/fail
    matrix throws the numbers away -- the throughput figure IS the result. Worst
    first, so a slow path is not buried among healthy ones.
    """
    out: list[str] = []
    a = out.append
    has_protocol = any(p.get("protocol") for p in group)
    a('<div class="table-scroll"><table>')
    a("<thead><tr><th>Status</th><th>Source</th><th>Target</th>"
      + ("<th>Proto</th>" if has_protocol else "")
      + "".join(f'<th title="{html_escape(c)}">{html_escape(metric_label(c))}</th>' for c in columns)
      + "<th>Artifacts</th></tr></thead><tbody>")
    for probe in sorted(group, key=lambda p: (-_sev(p["result"]), p.get("source", ""),
                                              p.get("target", ""), p.get("protocol", ""))):
        metrics = probe.get("metrics") or {}
        a(f'<tr data-result="{probe["result"]}"><td>{status_badge(probe["result"])}</td>')
        a(f'<td class="name">{html_escape(probe.get("source"))}</td>')
        a(f'<td class="name">{html_escape(probe.get("target"))}</td>')
        if has_protocol:
            a(f'<td class="val"><code class="mono">{html_escape(probe.get("protocol") or "-")}</code></td>')
        for col in columns:
            value = metric_value(metrics.get(col))
            cls = "val muted-metric" if col in CONFIGURED_METRICS | DERIVED_METRICS else "val"
            a(f'<td class="{cls}">{html_escape(_format_metric(value)) if value is not None else "&mdash;"}</td>')
        artifacts = probe.get("artifacts") or []
        if artifacts:
            a('<td class="val">'
              + "<br>".join(f'<code class="mono">{html_escape(p)}</code>' for p in artifacts)
              + "</td>")
        else:
            a('<td class="val">&mdash;</td>')
        a("</tr>")
    a("</tbody></table></div>")
    return "\n".join(out)


def _render_pair_sections(a: Any, subset: dict[tuple[str, str], list[dict[str, Any]]],
                         hosts: list[str]) -> None:
    """Render one collapsible per (network, kind), in diagnosis order."""
    for (net, kind), group in sorted(
        subset.items(), key=lambda kv: (kv[0][0], _pair_kind_rank(kv[0][1]))
    ):
        g_pass = sum(1 for g in group if g["result"] == "pass")
        g_fail = len(group) - g_pass
        # data-* carries the group's tally and searchable text so the filter can
        # act on a matrix-only section too: a matrix has no per-check <tr>, so
        # row-level filtering alone would leave ICMP and MTU unfilterable.
        a(f'<details class="card group"{" open" if g_fail else ""}'
          f' data-count="{len(group)}" data-fail="{g_fail}"'
          f' data-search="{html_escape(f"{net} {kind} {network_label(net)}")}">')
        target_note = group_target_note(group)
        a('<summary>'
          f'<span class="group-name">{pair_group_label(net, kind)}'
          + (f' <span class="group-target">{html_escape(target_note)}</span>' if target_note else "")
          + f' <code class="mono group-key">{html_escape(net)}/{html_escape(kind)}</code></span>'
          f'<span class="group-meter">{_meter(g_pass, len(group))}</span>'
          f'<span class="group-tally">{g_pass}/{len(group)}</span>'
          f'{status_badge(_worst(group))}</summary>')
        metric_columns = _metric_columns(group)
        if metric_columns:
            # Measured rather than asserted: show the numbers, not a grid of ticks.
            a(_render_pair_metrics_table(group, metric_columns))
        elif len(hosts) <= MATRIX_HOST_LIMIT:
            a('<div style="padding:12px 16px">')
            a(_render_matrix(hosts, group, net, kind))
            a("</div>")
        else:
            failing = [g for g in group if g["result"] != "pass"]
            a('<div class="table-scroll"><table>')
            a("<thead><tr><th>Status</th><th>Source</th><th>Target</th><th>Actual</th></tr></thead><tbody>")
            for g in (failing or group[:1]):
                a(f'<tr data-result="{g["result"]}"><td>{status_badge(g["result"])}</td>')
                a(f'<td class="name">{html_escape(g.get("source"))}</td>')
                a(f'<td class="name">{html_escape(g.get("target"))}</td>')
                a(f'<td class="val">{html_escape(g.get("actual"))}</td></tr>')
            if not failing:
                a(f'<tr><td colspan="4" class="empty">All {len(group)} probes passed.</td></tr>')
            a("</tbody></table></div>")
        a("</details>")


def render_environment_html(payload: dict[str, Any]) -> str:
    """Render the v2 environment payload as one self-contained HTML page."""
    run = payload.get("run") or {}
    cluster = payload.get("cluster") or {}
    summary = payload.get("summary") or {}
    checks = payload.get("checks") or {}
    hosts: list[str] = cluster.get("hosts") or []
    networks: list[str] = cluster.get("networks") or []
    by_scope = summary.get("by_scope") or {}

    total = summary.get("total", 0)
    passed = summary.get("passed", 0)
    failed = summary.get("failed", 0)
    warned = summary.get("warned", 0)
    pct = _pct(passed, total)
    bad = failed > 0

    failures = [c for c in checks.get("fleet", [])]
    failures += [c for host in checks.get("nodes", {}).values() for c in host]
    failures += [c for c in checks.get("pairs", [])]
    failures = [c for c in failures if c["result"] != "pass"]
    failures.sort(key=lambda c: (-_sev(c["result"]), c["id"]))

    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    title = f"PVE Phase 13 network validation — {run.get('environment', 'environment')}"

    out: list[str] = []
    a = out.append
    a("<!doctype html>")
    a('<html lang="en">')
    a("<head>")
    a('<meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width, initial-scale=1">')
    a(f"<title>{html_escape(title)}</title>")
    a(f"<style>{HTML_STYLE}{ENVIRONMENT_EXTRA_STYLE}</style>")
    a("</head><body><div class=\"wrap\">")

    a('<header class="masthead"><div>')
    a(f"<h1>{html_escape(title)}</h1>")
    a(f'<p class="subtitle">{len(hosts)} nodes &middot; {len(networks)} network segments &middot; '
      f"run <code class=\"mono\">{html_escape(run.get('id', ''))}</code></p>")
    a("</div>")
    a('<button class="theme-toggle" id="theme-toggle" type="button">Toggle theme</button>')
    a("</header>")

    a('<section class="card hero"><div>')
    a(f'<div class="hero-figure">{pct}%</div>')
    a(f'<div class="hero-label">{passed} of {total} checks passed</div>')
    a('</div><div class="hero-meter">')
    a(f'<div class="meter{" has-fail" if bad else ""}"><span style="width:{pct}%"></span></div>')
    a(f'<div class="hero-label">{status_badge("fail" if bad else "pass")}&nbsp;'
      f'{"FAILED" if bad else "SUCCESS"}')
    if failed:
        hosts_hit = sorted({c.get("host") or c.get("source") or "" for c in failures if c["result"] == "fail"} - {""})
        nets_hit = sorted({c.get("network") or "" for c in failures if c["result"] == "fail"} - {""})
        # Blast radius up front: how many, on which hosts, on which segments.
        a(f'<br><span class="dev">{failed} failure(s)</span> across '
          f"{len(hosts_hit)} host(s){' on ' + ', '.join(nets_hit) if nets_hit else ''}")
    a("</div></div></section>")

    # "Total checks" is the denominator the other three tiles are read against.
    # Dropping it for "Nodes" broke that arithmetic -- an operator had to add
    # passed+failed+warned to recover it. Scope keeps its own tile instead.
    a('<section class="kpis">')
    for value, label, cls in (
        (passed, "Passed", "is-good" if passed else ""),
        (failed, "Failed", "is-critical" if failed else ""),
        (warned, "Warnings", "is-warning" if warned else ""),
        (total, "Total checks", ""),
        (f"{len(hosts)} / {len(networks)}", "Nodes / segments", ""),
    ):
        a(f'<div class="card kpi {cls}"><div class="v">{value}</div><div class="k">{label}</div></div>')
    a("</section>")

    a('<dl class="card meta">')
    for label, value in (
        ("Environment", run.get("environment")),
        ("Site", run.get("site")),
        ("Run id", run.get("id")),
        ("Tool", run.get("tool")),
        ("Hosts reporting", f"{run.get('hosts_reporting', len(hosts))} of {len(hosts)}"),
        ("Fleet checks", (by_scope.get("fleet") or {}).get("total", 0)),
        ("Node checks", (by_scope.get("node") or {}).get("total", 0)),
        ("Pair probes", (by_scope.get("pair") or {}).get("total", 0)),
    ):
        if value not in (None, ""):
            a(f"<div><dt>{html_escape(label)}</dt><dd>{html_escape(value)}</dd></div>")
    a("</dl>")

    if summary.get("missing_hosts"):
        a('<div class="card" style="padding:14px 18px;margin-bottom:18px">'
          f'{status_badge("fail")} <strong>Incomplete run.</strong> No fragment reported for: '
          f'<span class="dev">{html_escape(", ".join(summary["missing_hosts"]))}</span>. '
          "Results below cover only the hosts that reported.</div>")

    # Restored from v1, which had it at 61 checks while this page dropped it at
    # 372. Ctrl+F is not a substitute: most checks live inside collapsed <details>
    # and the browser will not find text it cannot see. Sticky, because a filter
    # you have to scroll back up to reach stops being used.
    a('<div class="controls is-sticky">')
    a('<input type="search" id="filter-text" '
      'placeholder="Filter checks by name, host, network, expected or actual value">')
    a('<label><input type="checkbox" id="filter-fail"> Failures only</label>')
    a('<span class="count-note" id="filter-count"></span>')
    a("</div>")

    # Failures first: the reason the page is open.
    a("<h2>Failures and warnings</h2>")
    if failures:
        a('<div class="card"><div class="table-scroll"><table>')
        a("<thead><tr><th>Status</th><th>Check</th><th>Expected</th><th>Actual</th></tr></thead><tbody>")
        for c in failures:
            a(f'<tr data-result="{c["result"]}"><td>{status_badge(c["result"])}</td>')
            a(f'<td class="name"><code class="mono">{html_escape(c["id"])}</code>')
            if c.get("command"):
                a(f'<br><code class="mono">{html_escape(c["command"])}</code>')
            a("</td>")
            a(f'<td class="val">{html_escape(c.get("expected"))}</td>')
            a(f'<td class="val">{html_escape(c.get("actual"))}</td></tr>')
        a("</tbody></table></div></div>")
    else:
        # Nothing to triage, so the space says what WAS covered instead of
        # rendering an empty table where the failures would have been.
        a('<div class="card" style="padding:18px 20px">')
        a(f'<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">'
          f'{status_badge("pass")}'
          f'<strong>Clean bill of health.</strong>'
          f'<span class="count-note">{total} checks passed across '
          f'{len(hosts)} node(s) and {len(networks)} network segment(s).</span></div>')
        coverage = []
        for scope, label in (("fleet", "fleet-wide"), ("node", "node-local"), ("pair", "pair probes")):
            n = (by_scope.get(scope) or {}).get("total", 0)
            if n:
                coverage.append(f"{n} {label}")
        if coverage:
            a(f'<div class="count-note" style="margin-top:8px">Coverage: '
              # Escape the parts, then join with the separator: escaping AFTER the
              # join turned the &middot; entity into literal "&middot;" on screen.
              f'{" &middot; ".join(html_escape(c) for c in coverage)}.</div>')
        a("</div>")

    variance = payload.get("variance") or []
    if variance:
        a("<h2>Fleet variance &mdash; node-local properties</h2>")
        a('<p class="count-note" style="margin:-4px 0 12px">One row per property across the whole fleet, '
          "rather than one near-identical table per node. Deviating hosts are named.</p>")
        a('<div class="card"><div class="table-scroll"><table>')
        a("<thead><tr><th>Status</th><th>Property</th><th>Fleet norm</th>"
          "<th>Compliance</th><th>Deviations</th></tr></thead><tbody>")
        for row in sorted(variance, key=lambda r: (r["result"] == "pass", r["item"])):
            devs = row.get("deviations") or []
            a(f'<tr data-result="{row["result"]}"><td>{status_badge(row["result"])}</td>')
            a(f'<td class="name">{html_escape(row["item"])}</td>')
            a(f'<td class="norm-cell">{html_escape(row.get("norm"))}</td>')
            a('<td class="val" style="min-width:130px">'
              f'{_meter(row["passed"], row["observed"])}'
              f'<span class="group-tally">{row["passed"]}/{row["observed"]}</span></td>')
            if devs:
                a('<td class="val"><span class="dev">'
                  + html_escape(", ".join(f"{d['host']} ({d['result']})" for d in devs))
                  + "</span></td>")
            else:
                a('<td class="val">&mdash;</td>')
            a("</tr>")
        a("</tbody></table></div></div>")

    if checks.get("fleet"):
        fl = checks["fleet"]
        f_pass = sum(1 for c in fl if c["result"] == "pass")
        a("<h2>Fleet-wide checks</h2>")
        a(f'<details class="card group"{" open" if f_pass < len(fl) else ""}>')
        a('<summary><span class="group-name">Firmware and version consistency</span>'
          f'<span class="group-meter">{_meter(f_pass, len(fl))}</span>'
          f'<span class="group-tally">{f_pass}/{len(fl)}</span>'
          f'{status_badge(_worst(fl))}</summary>')
        a('<div class="table-scroll"><table>')
        a("<thead><tr><th>Status</th><th>Component</th><th>Expected</th><th>Actual</th></tr></thead><tbody>")
        for c in fl:
            a(f'<tr data-result="{c["result"]}"><td>{status_badge(c["result"])}</td>')
            a(f'<td class="name">{html_escape(c.get("component") or c["name"])}</td>')
            a(f'<td class="val">{html_escape(c.get("expected"))}</td>')
            a(f'<td class="val">{html_escape(c.get("actual"))}</td></tr>')
        a("</tbody></table></div></details>")

    pairs = checks.get("pairs") or []
    if pairs:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for p in pairs:
            grouped.setdefault((p.get("network", ""), p.get("kind", "")), []).append(p)
        integrity = {k: v for k, v in grouped.items() if k[1] not in PAIR_KIND_IS_MEASUREMENT}
        telemetry = {k: v for k, v in grouped.items() if k[1] in PAIR_KIND_IS_MEASUREMENT}

        for heading, subtitle, subset in (
            ("Path integrity &mdash; reachability and MTU",
             "Assertions: each probe either passed or did not.", integrity),
            ("Transport telemetry &mdash; throughput and latency under load",
             "Measurements, not verdicts. Values in grey are configured inputs or "
             "derived sums rather than measured results.", telemetry),
        ):
            if not subset:
                continue
            a(f"<h2>{heading}</h2>")
            a(f'<p class="count-note" style="margin:-4px 0 12px">{subtitle}</p>')
            if subset is integrity and len(hosts) > MATRIX_HOST_LIMIT:
                a(f'<p class="count-note" style="margin:-4px 0 12px">{len(hosts)} hosts exceeds the '
                  f"{MATRIX_HOST_LIMIT}-host matrix limit, so each segment is summarised. "
                  "Failures are listed in full above.</p>")
            _render_pair_sections(a, subset, hosts)

    nodes = checks.get("nodes") or {}
    if nodes:
        a("<h2>Per-node detail</h2>")
        for host in sorted(nodes):
            hc = nodes[host]
            h_pass = sum(1 for c in hc if c["result"] == "pass")
            h_fail = len(hc) - h_pass
            a(f'<details class="card group"{" open" if h_fail else ""}>')
            a(f'<summary><span class="group-name">{html_escape(host)}</span>'
              f'<span class="group-meter">{_meter(h_pass, len(hc))}</span>'
              f'<span class="group-tally">{h_pass}/{len(hc)}</span>'
              f'{status_badge(_worst(hc))}</summary>')
            a('<div class="table-scroll"><table>')
            a("<thead><tr><th>Status</th><th>Property</th><th>Expected</th><th>Actual</th></tr></thead><tbody>")
            for c in hc:
                a(f'<tr data-result="{c["result"]}"><td>{status_badge(c["result"])}</td>')
                a(f'<td class="name">{html_escape(c.get("item") or c["name"])}</td>')
                a(f'<td class="val">{html_escape(c.get("expected"))}</td>')
                a(f'<td class="val">{html_escape(c.get("actual"))}</td></tr>')
            a("</tbody></table></div></details>")

    a(f'<footer>Generated {html_escape(generated)} by pve-deploy report tool v{VERSION} '
      f"from schema <code class=\"mono\">{html_escape(payload.get('schema_version'))}</code>.</footer>")
    a("</div>")
    a(f"<script>{HTML_SCRIPT}</script>")
    a("</body></html>")
    return "\n".join(out) + "\n"


def environment_basename(payload: dict[str, Any]) -> str:
    """Filename stem for an environment report, without extension."""
    run = payload.get("run") or {}
    env = sanitize_slug(str(run.get("environment") or "environment")) or "environment"
    run_id = sanitize_slug(str(run.get("id") or "")) or "run"
    return f"phase13-network-validation-{env}-{run_id}"


def get_json_schema() -> dict[str, Any]:
    """Generate JSON schema dictionary for input report JSON validation."""
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "pve-deploy Report Input Schema",
        "type": "object",
        "required": ["kind", "slug", "title", "outcome"],
        "properties": {
            "kind": {
                "type": "string",
                "description": "Report type (e.g. change, validation, assessment, investigation, config, evidence, etc.)",
            },
            "slug": {
                "type": "string",
                "description": "Kebab-case descriptive slug for the report",
            },
            "title": {
                "type": "string",
                "description": "Human readable report title",
            },
            "outcome": {
                "type": "string",
                "enum": ["success", "partial", "failed"],
            },
            "element": {
                "type": "string",
                "description": "Target element family (pve, pbs, switches, firewall, bmc)",
            },
            "environment": {"type": "string", "description": "Environment name (e.g. example)"},
            "operator": {"type": "string", "description": "User or system that executed the action"},
            "tool": {"type": "string", "description": "Automation tool or playbook name"},
            "started_at": {
                "type": "string",
                "format": "date-time",
                "description": "ISO 8601 execution start timestamp (must include 24h time)",
            },
            "finished_at": {
                "type": "string",
                "format": "date-time",
                "description": "ISO 8601 execution finish timestamp (must include 24h time)",
            },
            "summary": {"type": "string", "description": "High-level summary paragraph"},
            "targets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["host"],
                    "properties": {
                        "host": {"type": "string"},
                        "address": {"type": "string"},
                        "role": {"type": "string"},
                    },
                },
            },
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "description": {"type": "string"},
                        "before": {"type": "string"},
                        "after": {"type": "string"},
                        "commands": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "verification": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "command": {"type": "string"},
                        "expected": {"type": "string"},
                        "actual": {"type": "string"},
                        "result": {
                            "type": "string",
                            "enum": ["pass", "fail", "warn"],
                        },
                    },
                },
            },
            "artifacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["path"],
                    "properties": {
                        "path": {"type": "string"},
                        "sha256": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
            },
            "notes": {
                "type": "array",
                "items": {"type": "string"},
            },
            "references": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title", "url_or_path"],
                    "properties": {
                        "title": {"type": "string"},
                        "url_or_path": {"type": "string"},
                    },
                },
            },
        },
    }


def load_input_json(input_arg: str) -> tuple[Any, str | None]:
    """Read JSON from file or stdin ('-'). Returns (parsed_json, error_message)."""
    try:
        if input_arg == "-":
            raw = sys.stdin.read()
        else:
            if not os.path.exists(input_arg):
                return None, f"Input file not found: '{input_arg}'."
            with open(input_arg, encoding="utf-8") as f:
                raw = f.read()

        if not raw.strip():
            return None, "Input JSON content is empty."
        parsed = json.loads(raw)
        return parsed, None
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON input: {e}."
    except Exception as e:
        return None, f"Failed to read input JSON: {e}."


def render_environment(payload: dict[str, Any], args: argparse.Namespace) -> int:
    """Handle validate/name/render for a v2 environment payload."""
    stem = environment_basename(payload)
    filename = f"{stem}.md"

    if args.action == "validate":
        summary = payload.get("summary") or {}
        if args.json:
            print(json.dumps({"valid": True, "filename": filename, "schema": ENVIRONMENT_SCHEMA,
                              "checks": summary.get("total", 0)}))
        else:
            print(f"Validation successful ({ENVIRONMENT_SCHEMA}, {summary.get('total', 0)} checks). "
                  f"Generated filename: {filename}")
        return 0

    if args.action == "name":
        print(json.dumps({"filename": filename}) if args.json else filename)
        return 0

    markdown_content = render_environment_markdown(payload)

    if getattr(args, "stdout", False):
        if args.json:
            print(json.dumps({"status": "success", "filename": filename, "content": markdown_content}))
        else:
            sys.stdout.write(markdown_content)
        return 0

    out_dir = args.out
    target_path = os.path.join(out_dir, filename)
    if os.path.exists(target_path) and not args.force:
        msg = f"Report file exists: '{target_path}'. Use --force to overwrite."
        if args.json:
            print(json.dumps({"status": "aborted", "error": msg, "path": target_path}))
        else:
            sys.stderr.write(f"ERROR: {msg}\n")
        return 1

    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(markdown_content)
    except OSError as e:
        msg = f"Failed to write report file to '{target_path}': {e}"
        if args.json:
            print(json.dumps({"status": "error", "error": msg, "path": target_path}))
        else:
            sys.stderr.write(f"ERROR: {msg}\n")
        return 3

    html_path: str | None = None
    html_error: str | None = None
    if not args.no_html:
        html_path = os.path.join(out_dir, f"{stem}.html")
        try:
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(render_environment_html(payload))
        except OSError as e:
            html_error = f"Failed to write HTML companion to '{html_path}': {e}"
            html_path = None

    if args.json:
        result: dict[str, Any] = {"status": "success", "filename": filename, "path": target_path,
                                  "schema": ENVIRONMENT_SCHEMA}
        if html_path:
            result["html_path"] = html_path
        if html_error:
            result["html_error"] = html_error
        print(json.dumps(result))
    else:
        print(f"Environment report rendered to {target_path}")
        if html_path:
            print(f"HTML companion written to {html_path}")
        if html_error:
            sys.stderr.write(f"WARNING: {html_error}\n")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON results")
    parser.add_argument("--no-color", action="store_true", help="Disable colorized console output")

    subparsers = parser.add_subparsers(dest="action", required=True)

    # render subcommand
    render_parser = subparsers.add_parser("render", help="Read JSON evidence and write Markdown report")
    render_parser.add_argument("input_file", nargs="?", default="-", help="Input JSON file path or '-' for stdin")
    render_parser.add_argument(
        "--out",
        default=os.getenv("PVE_REPORT_DIR", "reports"),
        help="Output directory (default: reports/ or PVE_REPORT_DIR)",
    )
    render_parser.add_argument(
        "--stdout", action="store_true", help="Print report to stdout instead of writing to disk"
    )
    render_parser.add_argument(
        "--force", action="store_true", help="Overwrite existing report file if present"
    )
    render_parser.add_argument(
        "--no-html",
        action="store_true",
        help="Skip the HTML companion (Markdown only)",
    )

    # validate subcommand
    val_parser = subparsers.add_parser("validate", help="Validate input JSON against schema and naming rules")
    val_parser.add_argument("input_file", nargs="?", default="-", help="Input JSON file path or '-' for stdin")

    # name subcommand
    name_parser = subparsers.add_parser("name", help="Print the filename that would be generated")
    name_parser.add_argument("input_file", nargs="?", default="-", help="Input JSON file path or '-' for stdin")

    # schema subcommand
    subparsers.add_parser("schema", help="Print input JSON schema")

    args = parser.parse_args(argv)

    # Action: schema
    if args.action == "schema":
        schema_dict = get_json_schema()
        print(json.dumps(schema_dict, indent=2))
        return 0

    # Actions: render, validate, name
    raw_data, load_err = load_input_json(args.input_file)
    if load_err:
        if args.json:
            print(json.dumps({"valid": False, "error": load_err}))
        else:
            sys.stderr.write(f"ERROR: {load_err}\n")
        return 2

    # v2 environment payloads have a different shape from the v1 per-node report and
    # deliberately do not go through validate_and_parse_input, which enforces the
    # flat v1 contract. Detected by schema_version so both keep working.
    if isinstance(raw_data, dict) and raw_data.get("schema_version") == ENVIRONMENT_SCHEMA:
        return render_environment(raw_data, args)

    report_data, filename, val_errors = validate_and_parse_input(raw_data)
    if val_errors or not report_data or not filename:
        if args.json:
            print(json.dumps({"valid": False, "errors": val_errors}))
        else:
            sys.stderr.write("ERROR: Input validation failed:\n")
            for err in val_errors:
                sys.stderr.write(f"  - {err}\n")
        return 2

    # Action: validate
    if args.action == "validate":
        if args.json:
            print(json.dumps({"valid": True, "filename": filename}))
        else:
            print(f"Validation successful. Generated filename: {filename}")
        return 0

    # Action: name
    if args.action == "name":
        if args.json:
            print(json.dumps({"filename": filename}))
        else:
            print(filename)
        return 0

    # Action: render
    if args.action == "render":
        markdown_content = render_markdown(report_data)

        if args.stdout:
            if args.json:
                print(json.dumps({
                    "status": "success",
                    "filename": filename,
                    "content": markdown_content
                }))
            else:
                sys.stdout.write(markdown_content)
            return 0

        # Writing file to disk
        out_dir = args.out
        target_path = os.path.join(out_dir, filename)

        if os.path.exists(target_path) and not args.force:
            msg = f"Report file exists: '{target_path}'. Use --force to overwrite."
            if args.json:
                print(json.dumps({"status": "aborted", "error": msg, "path": target_path}))
            else:
                sys.stderr.write(f"ERROR: {msg}\n")
            return 1

        try:
            os.makedirs(out_dir, exist_ok=True)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(markdown_content)
        except OSError as e:
            msg = f"Failed to write report file to '{target_path}': {e}"
            if args.json:
                print(json.dumps({"status": "error", "error": msg, "path": target_path}))
            else:
                sys.stderr.write(f"ERROR: {msg}\n")
            return 3

        # HTML companion, written beside the Markdown with the same stem. Failing to
        # write it must not fail the run: the Markdown is the contractual artifact
        # and is already on disk by this point.
        html_path: str | None = None
        html_error: str | None = None
        if not args.no_html:
            html_path = os.path.splitext(target_path)[0] + ".html"
            try:
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(render_html(report_data))
            except OSError as e:
                html_error = f"Failed to write HTML companion to '{html_path}': {e}"
                html_path = None

        if args.json:
            result: dict[str, Any] = {"status": "success", "filename": filename, "path": target_path}
            if html_path:
                result["html_path"] = html_path
            if html_error:
                result["html_error"] = html_error
            print(json.dumps(result))
        else:
            print(f"Report successfully rendered to {target_path}")
            if html_path:
                print(f"HTML companion written to {html_path}")
            if html_error:
                sys.stderr.write(f"WARNING: {html_error}\n")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
