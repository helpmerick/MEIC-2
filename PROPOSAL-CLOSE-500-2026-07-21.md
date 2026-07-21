# Proposal: Close-button 500 — diagnosis + proposed fix (2026-07-21)

**Status:** PROPOSAL — nothing implemented. Awaiting reviewer approval and one
missing artifact (the tester's server traceback).
**Author:** lead session (debug run 2026-07-21), for operator Ash + reviewer.
**Incident:** external tester, 2026-07-20 ~10:29 AM, entry `2026-07-20#7`
(FILLED, Protected, settlement-pending). Clicking **Close** showed
`Close failed: Internal Server Error`. A second card (`2026-07-20#104`) shows
the same Close control; at least one close failed with the identical toast.

---

## 1. What was reproduced (fact, not hypothesis)

The toast text is FastAPI/Starlette's **default unhandled-exception response**
echoed verbatim by the frontend:

- Frontend: `frontend/src/App.tsx:61` — `` flash(`Close failed: ${e.detail}`) ``.
- Endpoint: `backend/src/meic/adapters/api/app.py:1212-1216`:

  ```python
  @app.post("/close/{entry_id}")
  async def close_entry(entry_id: str) -> dict[str, Any]:
      return await commands.close(entry_id)   # no try/except; no app-level handler
  ```

- `create_app` registers **no exception handler**; the only middleware is the
  NFR-06 origin/token gate. Any exception inside `commands.close()` therefore
  surfaces as `500` with body `Internal Server Error` (text/plain).

A reproduction harness (appendix B) drove the **real** `/close/{entry_id}`
endpoint over a real `PaperComposition` + `PanelCommands`, with a broker
scripted to raise the way the live `TastytradeAdapter` raises on a rejected
order (`TastytradeError`, e.g. `tif_no_after_hours_market_orders` — observed in
this repo's own contract-test output). Results:

| Scenario | Result |
|---|---|
| Healthy broker (baseline) | `200 {"result":"closed","initiator":"manual"}` |
| `broker.working_orders()` raises | `500`, body `Internal Server Error` — **byte-identical to the incident toast** |
| `broker.submit()` (long-leg sell) raises | `500`, body `Internal Server Error` — same |

### The two unguarded broker-call sites in the close path

1. **`await broker.working_orders()`** in `assemble_close_inputs`
   (`backend/src/meic/composition/close_assembly.py:87`) — runs on **every**
   filled-entry close, before any order is placed. A failure here is
   *pre-action*: nothing has been sent to the broker yet.
2. **`await self._broker.submit(...)`** for remaining long legs
   (`backend/src/meic/application/close_entry.py:199`; also the no-resting-stop
   short branch at `:154`) — runs **mid-sequence**, after short legs may already
   have been replaced/closed. The short-leg replace path is fully guarded
   (`_replace_stop` catches everything, ORD-08); the long-leg sell and
   direct-submit branches are not.

Contrast: the adjacent TPF/TPT endpoints catch their failure modes and return
structured `422 {detail: {reason}}` (UI-03 pattern). Close is the only
mutating per-entry command that can escape as a raw 500.

## 2. What is NOT yet established

The **underlying broker exception** in the tester's session. Their server-side
traceback is not on this machine (local `logs/` end 2026-07-16). Leading
hypotheses, explicitly unproven:

- H1: tastytrade rejected the close order (equity-option session rules,
  qty/position mismatch, or order-type rule) — repro site 2.
- H2: transient API/session failure on `working_orders()` — repro site 1.

Diagnostic split available *before* the traceback arrives: **fails every
click** → site 1 (pre-action); **intermittent / failed once** → site 2
(per-order rejection). The tester has been asked for the traceback and the
per-click `POST /close/` status history.

## 3. Proposed changes (for approval — not yet built)

### Fix C1 — structured failure reporting on the close path (approvable now)

Make Close fail **loud, specific, and safe**, mirroring the TPF/TPT pattern:

1. In `PanelCommands.close_as()` (or the endpoint), catch exceptions from
   `assemble_close_inputs` and `CloseEntry.close()` and return a structured
   outcome instead of leaking a 500:
   - Pre-action failure (site 1, nothing sent):
     `{"result": "close_failed", "stage": "pre_submit", "reason": "<exception message>"}`
     → toast: “Close failed before any order was sent — position unchanged:
     &lt;reason&gt;”.
   - Mid-sequence failure (site 2, some legs may already be closed):
     `{"result": "close_partial", "stage": "in_flight", "reason": ...,
     "sides_closed": [...], "sides_remaining": [...]}`
     → toast marked **critical**: “Close PARTIALLY executed — check the book:
     &lt;reason&gt;”. Also raise an RSK-06 critical alert.
2. HTTP mapping: prefer a `200` with the structured body (the frontend already
   renders non-`closed` results) or `502` with the same detail — reviewer’s
   call; `200`+body is the smaller frontend change.
3. Frontend: render `close_failed` / `close_partial` distinctly
   (`frontend/src/App.tsx` closeEntry handler); `close_partial` must never look
   like a clean failure OR a clean close.

### C1 safety invariant — the part that needs real review

`CloseEntry.close()` journals per-side `SideClosed`/`ShortStopped` **as it
goes** and `EntryClosed` **last**. If the long-leg submit raises mid-sequence
today, the per-side events already appended **stay journaled** (append-only
list), but no `EntryClosed` lands and the UI says only “Internal Server
Error” — a **partially-closed entry presented as a failed close**. That is the
real money-risk in this bug, worse than the opaque toast. Requirements on C1:

- Never swallow-and-retoast a mid-sequence failure as a generic “failed”:
  the response must say *partial* and *which sides*.
- Never re-raise past the per-side events (losing the response) — catch at the
  boundary that can still read what was journaled.
- The already-journaled per-side truth is authoritative (REC-01); C1 adds no
  new close logic, no second close path (CLS-02) — reporting only.
- Idempotent follow-up: after a partial, the projection shows the remaining
  open side(s); a second Close click walks the SAME canonical path for the
  remainder. This already works structurally (close derives open sides from
  the projection) — a test must prove it.

### Fix C2 — the underlying broker failure (BLOCKED on tester's traceback)

Not proposed yet. Whatever the traceback shows (H1/H2), the response may range
from “C1 already handles it correctly” to a spec conversation (e.g. if closes
are attempted in a session state the broker refuses). No improvisation around
the spec: if the broker contradicts assumed close semantics, that goes to the
operator as a spec amendment per the contract.

### Spec-compliance notes

- CLS-01/CLS-02 untouched: no change to the close procedure or its one
  implementation; C1 is boundary error-*reporting*.
- UI-16 (Close is instant, failures as toast, never a modal) — preserved.
- UI-03 (backend authoritative, precise reasons) — this brings Close up to the
  standard TPF/TPT already meet.
- New result strings (`close_failed`, `close_partial`) are additive API
  surface; flagged to the operator for ratification alongside this proposal.
- Rule IDs for traceability in the implementation: CLS-01, CLS-02, UI-03,
  UI-16, ORD-08, RSK-06, REC-01.

## 4. Acceptance criteria (definition of done for C1)

1. Repro harness scenario 1 (pre-action failure) returns the structured
   `close_failed/pre_submit` body — no 500, position untouched, no per-side
   events journaled.
2. Repro harness scenario 2 (mid-sequence failure) returns
   `close_partial/in_flight` naming the closed and remaining sides; the
   journal contains the per-side events for the closed sides and **no**
   `EntryClosed`; a critical alert fired.
3. After scenario 2, a second Close (healthy broker) closes ONLY the remaining
   side(s) and journals exactly one `EntryClosed` (idempotency preserved).
4. Baseline unchanged: healthy close still `200 {"result":"closed"}`;
   `already_closed` / `unknown_entry` / CLS-03 cancel paths byte-identical.
5. Frontend renders the two new results distinctly; existing tests green;
   traceability checker green.

## 5. Artifacts

- Repro script: scratchpad `repro_close_500.py` (session-local; reproduced
  both 500 sites and the baseline). Its content is appendix B and should be
  ported into `tests/` as the C1 failing tests during implementation.
- Tester ask (sent): full traceback block(s) from their 2026-07-20 logs +
  count/status of every `POST /close/` that day.

---

## Appendix A — incident evidence

- Toast: `Close failed: Internal Server Error` on `2026-07-20#7`
  (~10:29 AM), card FILLED/Protected/settlement-pending; Close (not
  Cancel-entry) rendered, so the filled-entry path was taken.
- Local logs end 2026-07-16 → tester ran their own instance; traceback pending.

## Appendix B — reproduction harness (verbatim)

```python
"""REPRODUCTION (read-only, no real broker): recreate the operator's
"Close failed: Internal Server Error" toast.

Drives the REAL /close/{entry_id} endpoint (create_app + PanelCommands +
PaperComposition) with a broker scripted to raise like the live
TastytradeAdapter does on a rejected order. Baseline must stay 200/closed.
"""
import asyncio
from datetime import datetime
from decimal import Decimal as D

from fastapi.testclient import TestClient

from meic.adapters.api.app import create_app
from meic.composition.paper import PaperComposition
from meic.composition.panel_commands import PanelCommands
from meic.domain.events import CondorFilled, FilledLeg
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_clock import ET, FakeClock
from tests.harness.intents import stop_intent

PANEL = "http://127.0.0.1"
SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


def _legs(prefix="SPXW  260720"):
    return (FilledLeg(f"{prefix}P07385000", "P", "long", 1),
            FilledLeg(f"{prefix}P07435000", "P", "short", 1),
            FilledLeg(f"{prefix}C07505000", "C", "short", 1),
            FilledLeg(f"{prefix}C07555000", "C", "long", 1))


def _seed_protected_entry():
    """A FILLED, PROTECTED condor -- matches the incident card (shield badge,
    two resting stops), dated 2026-07-20 like the tester's entries."""
    comp = PaperComposition(clock=FakeClock(datetime(2026, 7, 20, 10, 29, tzinfo=ET)), ticks=SPX)
    comp.events.append(CondorFilled(entry_id="2026-07-20#7", net_credit=D("3.60"), legs=_legs()))
    asyncio.run(comp.broker.submit(stop_intent("PUT", "3.80", entry_id="2026-07-20#7")))
    asyncio.run(comp.broker.submit(stop_intent("CALL", "3.80", entry_id="2026-07-20#7")))
    return comp


class _BrokerRaisesLikeLiveAdapter:
    """Simulate the LIVE TastytradeAdapter raising on one method (its documented
    behaviour on a rejected/after-hours order -- TastytradeError
    tif_no_after_hours_market_orders seen in this repo's contract-test output).
    Everything else delegates to the real simulated broker."""
    def __init__(self, real, failing_method):
        self._real = real
        self._failing = failing_method

    def __getattr__(self, name):
        if name == self._failing:
            async def _boom(*a, **k):
                raise RuntimeError(
                    "tif_no_after_hours_market_orders: Market orders with a time in "
                    "force of Day cannot be placed when the market is closed.")
            return _boom
        return getattr(self._real, name)


def _http_close(comp):
    cmd = PanelCommands(comp)
    app = create_app(comp.state, comp.events, commands=cmd, panel_origin=PANEL)
    client = TestClient(app, raise_server_exceptions=False)  # behave like a real server
    return client.post("/close/2026-07-20%237", headers={"origin": PANEL})


def _show(r):
    ctype = r.headers.get("content-type", "")
    detail = r.json().get("detail") if ctype.startswith("application/json") else r.text
    print(f"  status={r.status_code}  content-type={ctype!r}  body={r.text!r}")
    print(f"  --> frontend toast: \"Close failed: {detail}\"")


def main():
    print("BASELINE: healthy broker -> close succeeds")
    _show(_http_close(_seed_protected_entry()))

    print("REPRO 1: broker.working_orders() raises (assemble_close_inputs, close_assembly.py:87)")
    comp = _seed_protected_entry()
    comp.broker = _BrokerRaisesLikeLiveAdapter(comp.broker, "working_orders")
    _show(_http_close(comp))

    print("REPRO 2: broker.submit() raises (CloseEntry long-leg sell, close_entry.py:199)")
    comp = _seed_protected_entry()
    wrapped = _BrokerRaisesLikeLiveAdapter(comp.broker, "submit")
    comp.broker = wrapped
    comp.close._broker = wrapped   # CloseEntry captured its own ref at construction (paper.py:74)
    _show(_http_close(comp))


if __name__ == "__main__":
    main()
```

Observed output (2026-07-21, this machine):

```
BASELINE  status=200  body='{"result":"closed","initiator":"manual"}'
REPRO 1   status=500  body='Internal Server Error'   -> toast "Close failed: Internal Server Error"
REPRO 2   status=500  body='Internal Server Error'   -> toast "Close failed: Internal Server Error"
```
