#!/usr/bin/env python3
"""Extract Gherkin blocks from spec/04-test-cases.md into tests/features/*.feature.

The spec is the single source of truth: this script is run by CI on every build,
so editing generated .feature files is pointless. Only the operator may change
the spec (enforced by spec.lock.json + verify_spec_lock.py).
"""
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec" / "04-test-cases.md"
OUT = ROOT / "tests" / "features"

def main() -> int:
    text = SPEC.read_text(encoding="utf-8-sig")
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.feature"):
        old.unlink()
    # Find each **TC-XXX-NN** marker and any ```gherkin block that follows it
    # before the next marker.
    markers = [(m.start(), m.group(1)) for m in re.finditer(r"\*\*(TC-[A-Z]+-\d{2})\*\*", text)]
    count = 0
    for i, (pos, tc) in enumerate(markers):
        end = markers[i + 1][0] if i + 1 < len(markers) else len(text)
        section = text[pos:end]
        blocks = re.findall(r"```gherkin\n(.*?)```", section, re.DOTALL)
        if not blocks:
            continue
        body = "\n".join(blocks)
        lines = [f"Feature: {tc}"]
        for line in body.splitlines():
            lines.append(("  " + line) if line.strip() else line)
        (OUT / f"{tc}.feature").write_text("\n".join(lines) + "\n", encoding="utf-8")
        count += 1
    print(f"extracted {count} feature files from {SPEC.name} into {OUT}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
