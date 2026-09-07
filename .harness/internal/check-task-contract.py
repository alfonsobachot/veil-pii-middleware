#!/usr/bin/env python3
# vendored from agent-practices@1.3.0 — do not edit, update via bundle
"""Check task/objective files for the agent-practices routing contract.

Read-only. Reports missing or invalid contract fields. Field names resolve
through an optional repo adapter (e.g. BIA renders `Execution lane` as
`Autonomy`); enum values are identical across repos unless the adapter's
enum-map says otherwise.

Usage:
    check-task-contract.py [--adapter ADAPTER_YAML] FILE [FILE ...]

Exit codes: 0 ok, 1 findings, 2 usage/parse error.
"""

import argparse
import re
import sys

GENERIC_FIELDS = {
    "execution-lane": "Execution lane",
    "human-review-trigger": "Human review trigger",
    "delegation-mechanism": "Delegation mechanism",
    "verification-contract": "Verification contract",
    "risk-class": "Risk class",
}

ENUMS = {
    "execution-lane": {"agent-owned", "agent-with-checkpoint", "human-owned"},
    "human-review-trigger": {
        "none", "before-code", "before-external-write", "before-merge",
        "before-deploy", "before-destructive-command", "before-policy-change",
    },
    "delegation-mechanism": {
        "main-session", "skill", "subagent", "worktree-agent", "team",
        "workflow", "mcp-cli",
    },
    "risk-class": {"low", "medium", "high"},
}

# Fields where multiple comma-separated values are allowed.
MULTI = {"human-review-trigger"}


def _fallback_adapter_maps(text):
    """Line-based subset parser for adapter files when PyYAML is missing.

    Reads `field-map`, `enum-map`, and `required-sections` — the keys this
    checker uses. Supports the shapes the bundle's adapters actually write:
    flat `key: value` maps, two-level nested maps, inline-empty `{}`, and a
    top-level list of section titles.
    """
    field_map, enum_map, required_sections = {}, {}, []
    section = sub = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # strip trailing comments (adapter values never contain ` #`)
        line = re.sub(r"\s+#.*$", "", stripped)
        indent = len(raw) - len(raw.lstrip())
        if indent == 0:
            key = line.partition(":")[0].strip()
            section = key if key in ("field-map", "enum-map", "required-sections") else None
            sub = None
            continue
        if section == "required-sections":
            item = re.match(r"-\s+(.+)$", line)
            if item:
                required_sections.append(item.group(1).strip().strip("'\""))
            continue
        if section is None or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if section == "field-map" and indent == 2:
            field_map[key] = value
        elif section == "enum-map":
            if indent == 2:
                sub = key
                if value in ("", "{}"):
                    enum_map[sub] = {}
            elif indent >= 4 and sub is not None:
                enum_map[sub][key] = value
    return field_map, enum_map, required_sections


def load_adapter(path):
    try:
        import yaml
    except ImportError:
        yaml = None
    with open(path) as f:
        text = f.read()
    if yaml is not None:
        data = yaml.safe_load(text) or {}
        field_map = {**GENERIC_FIELDS, **(data.get("field-map") or {})}
        enum_map = data.get("enum-map") or {}
        required_sections = list(data.get("required-sections") or [])
    else:
        fm, em, rs = _fallback_adapter_maps(text)
        field_map = {**GENERIC_FIELDS, **fm}
        enum_map = em
        required_sections = rs
    return field_map, enum_map, required_sections


def check_file(path, field_map, enum_map, required_sections):
    findings = []
    try:
        text = open(path).read()
    except OSError as e:
        return [f"{path}: unreadable: {e}"]

    values = {}
    for key, label in field_map.items():
        # `Label: value` / `**Label:** value` lines, markdown field-table rows
        # `| Label | value |`, or a `## Label` section heading.
        m = re.search(
            rf"^(?:(?:\*\*)?{re.escape(label)}:?(?:\*\*)?:?\s+(.+)|\|\s*{re.escape(label)}\s*\|\s*(.*?)\s*\||#{{1,6}}\s*{re.escape(label)}\b.*())\s*$",
            text, re.MULTILINE,
        )
        if not m:
            findings.append(f"{path}: missing field `{label}` ({key})")
            continue
        raw = (m.group(1) or m.group(2) or "").strip()
        if m.group(3) is not None:
            # Heading-only match: the field exists as a section title but
            # carries no value — say so directly instead of failing the enum
            # check with a confusing `(section present)` message.
            findings.append(
                f"{path}: field `{label}` appears as a section heading with "
                f"no inline value — add `{label}: <value>` on the same line "
                "or in a table row"
            )
            continue
        # Table cells may append a rationale after an em dash.
        if key in ENUMS and "—" in raw:
            raw = raw.split("—", 1)[0].strip()
        values[key] = raw

    for key, raw in values.items():
        if key not in ENUMS:
            if not raw:
                findings.append(f"{path}: `{field_map[key]}` is empty")
            continue
        if not raw:
            findings.append(f"{path}: `{field_map[key]}` has no value")
            continue
        # Translate repo-specific enum values back to generic.
        reverse = {v: k for k, v in (enum_map.get(key) or {}).items()}
        parts = [p.strip() for p in raw.split(",")] if key in MULTI else [raw]
        for part in parts:
            generic = reverse.get(part, part)
            if generic not in ENUMS[key]:
                findings.append(
                    f"{path}: `{field_map[key]}` value `{part}` not in "
                    f"{sorted(ENUMS[key])}"
                )

    # before-merge invariant: the merge decision is a protected action on
    # every lane, so every trigger list must include it.
    if "human-review-trigger" in values:
        trig_reverse = {v: k for k, v in (enum_map.get("human-review-trigger") or {}).items()}
        trig_parts = {
            trig_reverse.get(p.strip(), p.strip())
            for p in values["human-review-trigger"].split(",")
        }
        if "before-merge" not in trig_parts:
            findings.append(
                f"{path}: HIGH: `before-merge` is required on every lane — "
                f"`{field_map['human-review-trigger']}` must include it "
                "(the merge decision is a protected action)"
            )

    # Adapter-declared required sections. The generic contract requires none;
    # an adapter may demand repo-specific sections (e.g. the harness-blueprint
    # adapter declares `Tooling And Approach Assessment`). Promoted out of a
    # hardcoded generic check so blueprint vocabulary no longer leaks into the
    # generic validator.
    for sec in required_sections:
        if not re.search(rf"^#{{1,6}}\s*{re.escape(sec)}\b", text, re.MULTILINE):
            findings.append(f"{path}: missing required section `## {sec}`")

    # All-placeholder Intake Grill: a grill section whose every table row is
    # still a placeholder means the interview never happened. Skipped for
    # not-started files (same skip the wrapper scripts use).
    if "## Intake Grill" in text and not re.search(r"^not started\b", text, re.MULTILINE):
        grill = re.search(
            r"^##\s*Intake Grill\s*$(.*?)(?=^#{1,2}\s|\Z)", text,
            re.MULTILINE | re.DOTALL,
        )
        if grill:
            rows = [
                ln for ln in grill.group(1).splitlines()
                if ln.lstrip().startswith("|")
                and not re.match(r"^\s*\|[\s\-:|]+\|?\s*$", ln)   # separator
                and not re.match(r"^\s*\|\s*Question\b", ln)       # header
            ]
            if rows and all("<PLACEHOLDER" in r for r in rows):
                findings.append(
                    f"{path}: Intake Grill table contains only placeholder "
                    "rows — complete the grill before implementing"
                )

    lane_reverse = {v: k for k, v in (enum_map.get("execution-lane") or {}).items()}
    lane = lane_reverse.get(values.get("execution-lane", ""), values.get("execution-lane", ""))
    verification = values.get("verification-contract", "")
    if lane == "agent-owned" and (not verification or verification.startswith("<")):
        findings.append(
            f"{path}: HIGH: lane is agent-owned but `{field_map['verification-contract']}` "
            "is empty/placeholder — verification must be named before coding"
        )
    risk_reverse = {v: k for k, v in (enum_map.get("risk-class") or {}).items()}
    risk = risk_reverse.get(values.get("risk-class", ""), values.get("risk-class", ""))
    if lane == "agent-owned" and risk == "high":
        findings.append(
            f"{path}: HIGH: `{field_map['risk-class']}` is high but lane is "
            "agent-owned — high risk is never agent-owned"
        )
    # agent-owned work answers to its acceptance criteria, so there must be
    # at least two measurable ones.
    if lane == "agent-owned":
        ac = re.search(
            r"^##\s*Acceptance Criteria\s*$(.*?)(?=^#{1,2}\s|\Z)", text,
            re.MULTILINE | re.DOTALL,
        )
        bullets = (
            [ln for ln in ac.group(1).splitlines() if ln.lstrip().startswith(("-", "*"))]
            if ac else []
        )
        if len(bullets) < 2:
            findings.append(
                f"{path}: agent-owned lane requires at least 2 measurable "
                "Acceptance Criteria bullets"
            )
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--adapter", help="repo adapter.yaml for field/enum names")
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()

    field_map, enum_map, required_sections = (
        load_adapter(args.adapter) if args.adapter else (GENERIC_FIELDS, {}, [])
    )

    all_findings = []
    for f in args.files:
        all_findings += check_file(f, field_map, enum_map, required_sections)

    if all_findings:
        print("\n".join(all_findings))
        print(f"\n{len(all_findings)} finding(s) in {len(args.files)} file(s)")
        sys.exit(1)
    print(f"ok: {len(args.files)} file(s) carry a complete task contract")


if __name__ == "__main__":
    main()
