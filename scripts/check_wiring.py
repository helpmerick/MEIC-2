#!/usr/bin/env python3
"""NFR-07 wiring-audit CLI (v1.67; ARMED v1.70 -- hash-locked, operator-owned).

Placed into locked `scripts/` by the operator 2026-07-15 (NFR-07: "a locked
guard, operator-maintained like the traceability checker"). CI enforcement
runs the SAME registry via the pytest gate (suite job); this CLI is the
ON-BOX operational check -- it boots a real live_app() with real credentials
and therefore MUST NEVER run in CI.

It imports the SAME registry (`meic.composition.wiring_registry`) the pytest
gate (tests/bdd/test_tc_nfr_07_wiring_registry.py) uses, so the two can never
silently drift apart -- one source of truth, two callers.

What this prints, in order:
  1. every registry entry: component, rule ids, constructed?, ticked?
  2. the heuristic self-policing cross-check: any spec rule id the keyword
     scan flags that is in NEITHER the registry NOR the curated
     false-positive list (a real finding either way -- see
     wiring_registry.py's own docstring for the heuristic's honest limits)
  3. a final PASS/FAIL line and a matching process exit code (0/1)

Usage:
    python ops/check_wiring.py

Boots a REAL `live_app()` (real broker credentials from .env/env, exactly
like running the panel) to prove construction+ticking against the actual
production composition -- this is an operational health check, not a unit
test, and it does perform a real broker connection attempt.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))


def main() -> int:
    from fastapi.testclient import TestClient

    from meic.adapters.api.server import live_app
    from meic.composition.wiring_registry import check_all, unaccounted_rule_ids

    print("NFR-07 wiring audit -- booting live_app()...")
    app = live_app()

    ok = True
    with TestClient(app):
        results = check_all(app.state)
        print()
        print(f"{'component':45} {'rule ids':30} {'constructed':12} {'ticked':8}")
        print("-" * 100)
        for entry, constructed, ticked in results:
            status_ok = constructed and ticked
            ok = ok and status_ok
            print(f"{entry.component[:45]:45} {','.join(entry.rule_ids)[:30]:30} "
                  f"{str(constructed):12} {str(ticked):8}"
                  f"{'' if status_ok else '   <-- FAIL'}")
            if not status_ok:
                print(f"    proof expected: {entry.proof}")

    print()
    unaccounted = sorted(unaccounted_rule_ids())
    if unaccounted:
        ok = False
        print("SELF-POLICING FINDING -- spec rule id(s) the runtime-component keyword "
              "heuristic flags that are in NEITHER the registry NOR the curated "
              "false-positive list (wiring_registry.KNOWN_FALSE_POSITIVE_RULE_IDS):")
        for rid in unaccounted:
            print(f"    {rid}")
        print("    -> either register the missing component, or read the rule and add it "
              "to KNOWN_FALSE_POSITIVE_RULE_IDS with a one-line reason.")
    else:
        print("self-policing check: no unaccounted spec rule ids found by the heuristic.")

    print()
    print("NFR-07 wiring audit: PASS" if ok else "NFR-07 wiring audit: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
