#!/usr/bin/env python3
# vendored from agent-practices@1.3.0 (commit 20dc5b9) — do not edit; update via bundle re-vendor
"""Check a skill against the bundle's skill-authoring schema.

Read-only. The schema is bundle policy (`.harness/docs/skill-authoring.md`), adopted
from Matt Pocock's `write-a-skill` pattern. Enforces:

  - frontmatter carries `name` and `description`,
  - `name` is kebab-case and matches the skill directory,
  - `description` is <=1024 chars, third person (no second-person "you"),
    and contains a "Use when ..." trigger clause,
  - `SKILL.md` is <=100 lines (split detail into reference files),
  - progressive disclosure is one level deep: `SKILL.md` references direct
    sibling files only, and those reference files do not chain to further
    skill-local files. Cross-bundle links (`../../wiki/...`) are doctrine
    navigation, not progressive disclosure, and are ignored.

Usage:
    check-skill.py SKILL_DIR [SKILL_DIR ...]
    check-skill.py path/to/SKILL.md ...

Exit codes: 0 ok, 1 findings, 2 usage/parse error.
"""

import os
import re
import sys

KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SECOND_PERSON = re.compile(r"\b(you|your|yours|yourself)\b", re.IGNORECASE)
MD_LINK = re.compile(r"\]\(\s*([^)\s]+\.md)(?:#[^)\s]*)?\s*\)")


def _frontmatter(text):
    m = re.match(r"\s*---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
    if not m:
        return None
    data = {}
    for line in m.group(1).splitlines():
        fm = re.match(r"([A-Za-z0-9_-]+):\s*(.*)$", line)
        if fm:
            data[fm.group(1)] = fm.group(2).strip()
    return data


def _is_skill_local(target):
    """A link that resolves inside the skill's own directory tree."""
    return (
        not target.startswith(("/", "http://", "https://"))
        and ".." not in target
    )


def _links(text):
    return [m.group(1) for m in MD_LINK.finditer(text)]


def check_skill(skill_md):
    findings = []
    skill_dir = os.path.dirname(os.path.abspath(skill_md))
    dir_name = os.path.basename(skill_dir)
    try:
        text = open(skill_md).read()
    except OSError as e:
        return [f"{skill_md}: unreadable: {e}"]

    # --- frontmatter --------------------------------------------------------
    fm = _frontmatter(text)
    if fm is None:
        return [f"{skill_md}: missing YAML frontmatter (--- name/description ---)"]

    name = fm.get("name", "")
    desc = fm.get("description", "")
    if not name:
        findings.append(f"{skill_md}: frontmatter `name` missing")
    else:
        if not KEBAB.match(name):
            findings.append(f"{skill_md}: `name` `{name}` is not kebab-case")
        if name != dir_name:
            findings.append(
                f"{skill_md}: `name` `{name}` does not match directory `{dir_name}`"
            )

    # --- description --------------------------------------------------------
    if not desc:
        findings.append(f"{skill_md}: frontmatter `description` missing")
    else:
        if len(desc) > 1024:
            findings.append(
                f"{skill_md}: `description` is {len(desc)} chars (max 1024)"
            )
        if "use when" not in desc.lower():
            findings.append(
                f"{skill_md}: `description` has no `Use when ...` trigger clause"
            )
        m = SECOND_PERSON.search(desc)
        if m:
            findings.append(
                f"{skill_md}: `description` uses second person `{m.group(0)}` — "
                "write in third person (say `the user`, not `you`)"
            )

    # --- length -------------------------------------------------------------
    n_lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    if n_lines > 100:
        findings.append(
            f"{skill_md}: SKILL.md is {n_lines} lines (max 100) — move detail "
            "into reference files"
        )

    # --- progressive disclosure: one level deep -----------------------------
    for t in _links(text):
        if _is_skill_local(t) and "/" in t.strip("/"):
            findings.append(
                f"{skill_md}: reference `{t}` is nested — SKILL.md must "
                "reference direct sibling files only (one level deep)"
            )

    for ref in sorted(os.listdir(skill_dir)):
        if not ref.endswith(".md") or ref == os.path.basename(skill_md):
            continue
        ref_path = os.path.join(skill_dir, ref)
        try:
            ref_text = open(ref_path).read()
        except OSError:
            continue
        for t in _links(ref_text):
            if _is_skill_local(t):
                findings.append(
                    f"{ref_path}: reference file links to skill-local `{t}` — "
                    "references must be one level deep (SKILL.md -> ref, not "
                    "ref -> ref); link doctrine via ../../wiki/ instead"
                )
    return findings


def resolve(path):
    if os.path.isdir(path):
        return os.path.join(path, "SKILL.md")
    return path


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    all_findings = []
    skills = [resolve(p) for p in sys.argv[1:]]
    for s in skills:
        if not os.path.isfile(s):
            all_findings.append(f"{s}: not found (expected a SKILL.md)")
            continue
        all_findings += check_skill(s)
    if all_findings:
        print("\n".join(all_findings))
        print(f"\n{len(all_findings)} finding(s) across {len(skills)} skill(s)")
        sys.exit(1)
    print(f"ok: {len(skills)} skill(s) conform to the skill-authoring schema")


if __name__ == "__main__":
    main()
