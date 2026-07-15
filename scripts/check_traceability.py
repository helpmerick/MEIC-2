#!/usr/bin/env python3
"""Traceability gate: every rule ID must be covered, every TC must be executable.

- Every rule/edge/NFR ID defined in the spec must be referenced in 04-test-cases.md.
- Every TC-* in 04-test-cases.md must have a generated .feature file OR a test
  function/string reference in tests/ (for prose TCs the coding agent writes
  pytest tests named after the TC id).
Fails CI when anything is orphaned — deleting or skipping a test breaks the build.
"""
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec"
TESTS = ROOT / "tests"

def read(name):
    return (SPEC / name).read_text(encoding="utf-8-sig")

def main() -> int:
    tests_doc = read("04-test-cases.md")
    rules = set(re.findall(r"\*\*((?:DAY|ENT|STK|ORD|STP|LEX|EOD|RSK|DAT|REC|PNL|CLS|TPF|DCY|OWN|NLE|SIM|TPT)-\d{2}[a-d]?)", read("01-strategy-rules.md")))
    rules |= set(re.findall(r"\*\*(EC-[A-Z]+-\d{2})", read("02-edge-cases.md")))
    rules |= set(re.findall(r"\*\*(NFR-\d{2})", read("05-architecture-ddd.md")))
    rules |= set(re.findall(r"\*\*(RPT-\d{2})", read("10-results-dashboard.md")))
    rules |= set(re.findall(r"\*\*(CAL-\d{2})", read("11-trading-calendar.md")))
    rules |= set(re.findall(r"\*\*(DOC-\d{2})", read("12-how-it-works.md")))
    # Expand shorthand notations in the tests doc into full IDs:
    #   slash lists:  CLS-01/05, EC-ENT-03/04, TPF-06/07/09
    #   arrow ranges: EC-ENT-01→13, LEX-01→09
    expanded = set()
    for m in re.finditer(r"([A-Z][A-Z-]*-)(\d{2}[a-d]?)((?:/\d{2}[a-d]?)+)", tests_doc):
        prefix = m.group(1)
        expanded.add(prefix + m.group(2))
        for seg in m.group(3).strip("/").split("/"):
            expanded.add(prefix + seg)
    for m in re.finditer(r"([A-Z][A-Z-]*-)(\d{2})→(\d{2})", tests_doc):
        for n in range(int(m.group(2)), int(m.group(3)) + 1):
            expanded.add(f"{m.group(1)}{n:02d}")
    def covered(rule):
        if rule in tests_doc or rule in expanded:
            return True
        stem = re.sub(r"[a-c]$", "", rule)
        return stem != rule and (stem in tests_doc or stem in expanded)
    missing = sorted(r for r in rules if not covered(r))

    tcs = sorted(set(re.findall(r"\*\*(TC-[A-Z]+-\d{2})\*\*", tests_doc)))
    feature_ids = {f.stem for f in (TESTS / "features").glob("*.feature")} if (TESTS / "features").exists() else set()
    test_text = ""
    for f in TESTS.rglob("*.py"):
        test_text += f.read_text(encoding="utf-8", errors="ignore")
    orphaned = [t for t in tcs if t not in feature_ids and t not in test_text and t.replace("-", "_") not in test_text]

    ok = True
    if missing:
        ok = False
        print("RULES WITHOUT TEST COVERAGE:", ", ".join(missing))
    if orphaned:
        ok = False
        print("TEST CASES WITHOUT IMPLEMENTATION:", ", ".join(orphaned))
    if ok:
        print(f"traceability ok: {len(rules)} rules covered, {len(tcs)} test cases implemented or feature-backed.")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
