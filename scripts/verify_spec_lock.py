#!/usr/bin/env python3
"""Verify (or, operator-only, update) the SHA-256 lock over spec/ and the guard files.

CI runs this FIRST. Any mismatch fails the build: the spec was modified by someone
other than the operator. Only the operator runs `--update` after deliberate changes.
"""
import hashlib, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "spec.lock.json"
LOCKED = ["spec", "scripts", ".github", "CLAUDE.md", "CODEOWNERS"]

def digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()

def collect() -> dict:
    out = {}
    for item in LOCKED:
        path = ROOT / item
        if path.is_file():
            out[item] = digest(path)
        elif path.is_dir():
            for f in sorted(path.rglob("*")):
                if f.is_file():
                    out[f.relative_to(ROOT).as_posix()] = digest(f)
    return out

def main() -> int:
    current = collect()
    if "--update" in sys.argv:
        LOCK.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        print(f"spec.lock.json updated over {len(current)} files. Commit it (operator only).")
        return 0
    if not LOCK.exists():
        print("FAIL: spec.lock.json missing. Operator must run --update.")
        return 1
    recorded = json.loads(LOCK.read_text(encoding="utf-8"))
    problems = []
    for path, h in recorded.items():
        if path not in current:
            problems.append(f"DELETED: {path}")
        elif current[path] != h:
            problems.append(f"MODIFIED: {path}")
    for path in current:
        if path not in recorded:
            problems.append(f"ADDED (unlocked path): {path}")
    if problems:
        print("SPEC LOCK VIOLATION — the contract was altered without the operator:")
        for p in problems:
            print("  " + p)
        return 1
    print(f"spec lock verified: {len(recorded)} files intact.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
