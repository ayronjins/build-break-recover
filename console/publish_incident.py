#!/usr/bin/env python3
"""Publish a problem to the BBR Console's public record.

This is how a critical error becomes visible on the website automatically,
rather than only in a log nobody reads.

SCOPE RULE (enforced, not just documented)
------------------------------------------
The website publishes TEST-SYSTEM problems only. Anything identifying the
controller machine is rejected before it can be written. Automation is
exactly where a scope leak would slip in unnoticed, so the check runs here
rather than relying on the caller to be careful.

Usage:
    publish-incident.py --title "..." --severity critical \
        --symptom "..." --root-cause "..." --fix "..." \
        --evidence "..." --evidence "..." --verified "..."
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

STORE = Path(__file__).resolve().parent / "data" / "resolutions.json"

# Generic controller-side concepts are always rejected. Deployments can add
# hostnames, addresses, or organization-specific terms with a comma-separated
# BBR_SCOPE_FORBIDDEN_REGEX environment variable without committing them.
FORBIDDEN = [
    r"controller host",
    r"controller server",
    r"\bbbr[- ]console\b",
    r"console api",
    r"console service",
    r"\bcollector service\b",
    r"\bsystemd\b.*\bcontroller\b",
]
FORBIDDEN.extend(
    p.strip() for p in os.environ.get("BBR_SCOPE_FORBIDDEN_REGEX", "").split(",")
    if p.strip()
)


def scope_violation(text):
    t = (text or "").lower()
    for p in FORBIDDEN:
        m = re.search(p, t)
        if m:
            return m.group(0)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--category", default="Operational")
    ap.add_argument("--severity", default="warning",
                    choices=["critical", "high", "medium", "low", "warning", "info"])
    ap.add_argument("--impact", default="")
    ap.add_argument("--symptom", required=True)
    ap.add_argument("--how-found", dest="how_found", default="")
    ap.add_argument("--evidence", action="append", default=[])
    ap.add_argument("--root-cause", dest="root_cause", required=True)
    ap.add_argument("--fix", dest="fix", required=True)
    ap.add_argument("--fix-scope", dest="fix_scope", default="")
    ap.add_argument("--security", default="")
    ap.add_argument("--verified", action="append", default=[])
    ap.add_argument("--status", default="RESOLVED")
    a = ap.parse_args()

    blob = " ".join([a.title, a.impact, a.symptom, a.how_found, a.root_cause,
                     a.fix, a.fix_scope, a.security,
                     " ".join(a.evidence), " ".join(a.verified)])
    bad = scope_violation(blob)
    if bad:
        print(f"REFUSED: text mentions '{bad}', which identifies the controller "
              f"machine. The website publishes test-system problems only.",
              file=sys.stderr)
        return 2

    if not STORE.exists():
        print(f"store not found: {STORE}", file=sys.stderr)
        return 1
    d = json.loads(STORE.read_text())

    entry = {
        "title": a.title,
        "found_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "category": a.category,
        "severity": a.severity,
        "symptom": a.symptom,
        "root_cause": a.root_cause,
        "fix_applied": a.fix,
        "status": a.status,
        "auto_published": True,
    }
    if a.impact:
        entry["impact"] = a.impact
    if a.how_found:
        entry["how_found"] = a.how_found
    if a.evidence:
        entry["key_evidence"] = a.evidence
    if a.fix_scope:
        entry["fix_scope"] = a.fix_scope
    if a.security:
        entry["security_impact"] = a.security
    if a.verified:
        entry["verified_by"] = a.verified

    items = d.setdefault("real_problems_found", [])
    # Idempotent: replace an existing entry with the same title rather than
    # accumulating duplicates on repeated runs.
    for i, ex in enumerate(items):
        if ex.get("title") == a.title:
            items[i] = entry
            break
    else:
        items.insert(0, entry)

    d["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    STORE.write_text(json.dumps(d, indent=2))
    print(f"published: {a.title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
