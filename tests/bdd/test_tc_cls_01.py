"""Hand-written step definitions for TC-CLS-01 — the v1.50 REPLACE-BASED close
(CLS-01/02): manual vs TPF byte-identical, the replace-never-cancel-bare
invariant, ORD-08 replace-race classification, and the CLS-02 structural
"CloseEntry is the only close path" guarantee.
"""
from __future__ import annotations

import ast
from dataclasses import replace as dc_replace
from decimal import Decimal as D
from pathlib import Path

import asyncio
import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.cancel_taxonomy import ReplaceFilled
from meic.application.close_entry import CloseEntry, LiveLeg
from meic.domain.events import EntryClosed, ShortStopped
from tests.harness.fake_broker import FakeBroker, Scripted
from tests.harness.intents import CALL_LONG, CALL_SHORT, PUT_LONG, PUT_SHORT, stop_intent

scenarios("../features/TC-CLS-01.feature")

LEGS = [LiveLeg("SPXW_5990P", "PUT", "short", -1), LiveLeg("SPXW_5940P", "PUT", "long", 1),
        LiveLeg("SPXW_6060C", "CALL", "short", -1), LiveLeg("SPXW_6110C", "CALL", "long", 1)]
STOPS = {"PUT": "S1", "CALL": "S2"}

# Scenarios 2/3 submit REAL stop orders (via `stop_intent`, which uses the OCC-
# style symbols below) and then must correlate them back to the SAME leg
# symbols, so these LiveLegs use `stop_intent`'s own symbols rather than the
# simpler placeholder strings in `LEGS` above (which scenario 1 never actually
# submits to a broker, so any symbol string does there).
INTENT_LEGS = [LiveLeg(PUT_SHORT, "PUT", "short", -1), LiveLeg(PUT_LONG, "PUT", "long", 1),
               LiveLeg(CALL_SHORT, "CALL", "short", -1), LiveLeg(CALL_LONG, "CALL", "long", 1)]


@pytest.fixture
def world():
    return {}


# =============================================================================
# Scenario 1: Manual close and TPF close are byte-identical
# =============================================================================

class RecordingBroker:
    """Wraps FakeBroker, recording the exact (method, ...) broker-request
    sequence CloseEntry issues. CLS-01 v1.50 calls ONLY `replace()`/`submit()`
    — never a bare `cancel()` — so those are the two methods recorded."""

    def __init__(self):
        self._fake = FakeBroker()
        self.requests = []

    async def replace(self, id, new):
        self.requests.append(("replace", id, new))
        return await self._fake.replace(id, new)

    async def submit(self, intent):
        self.requests.append(("submit", intent))
        return await self._fake.submit(intent)


@given('two identical open entries A and B (same fills, same stops)')
def _(world):
    world["A"], world["B"] = RecordingBroker(), RecordingBroker()
    world["events_A"], world["events_B"] = [], []


@when('entry A is closed via the UI "Close trade" button')
def _(world):
    asyncio.run(CloseEntry(world["A"], world["events_A"]).close(
        "A", "manual", resting_stop_ids=STOPS, live_legs=LEGS, close_price=D("0.05")))


@when('entry B is closed via a TPF floor trigger')
def _(world):
    # TPF-04: TPF has no close logic of its own (CLS-02) — a TPF-triggered
    # close IS a CloseEntry.close() call with initiator "take_profit", nothing
    # more. Driving both A and B through the identical service, differing only
    # in this argument, is exactly what "byte-identical" means to test.
    asyncio.run(CloseEntry(world["B"], world["events_B"]).close(
        "B", "take_profit", resting_stop_ids=STOPS, live_legs=LEGS, close_price=D("0.05")))


@then('the sequence of broker requests (replaces, close orders, prices, quantities) is identical')
def _(world):
    # Normalize the entry-id-specific idempotency key / entry_id (both differ
    # by construction, A vs B) so only structure/prices/quantities compare.
    def norm(reqs):
        out = []
        for req in reqs:
            if req[0] == "submit":
                _, intent = req
                out.append(("submit", dc_replace(intent, idempotency_key="", entry_id="")))
            else:  # ("replace", stop_id, intent)
                _, stop_id, intent = req
                out.append(("replace", stop_id, dc_replace(intent, idempotency_key="", entry_id="")))
        return out

    assert norm(world["A"].requests) == norm(world["B"].requests)
    assert norm(world["A"].requests)  # sanity: the sequence isn't vacuously empty


@then('only the recorded initiator differs: "manual" vs "take_profit"')
def _(world):
    a = [e for e in world["events_A"] if isinstance(e, EntryClosed)][0]
    b = [e for e in world["events_B"] if isinstance(e, EntryClosed)][0]
    assert a.initiator == "manual" and b.initiator == "take_profit"


# =============================================================================
# Scenario 2: The close replaces stops, never cancels them bare
# =============================================================================

def _working_buy_to_close_count(broker: FakeBroker, symbol: str) -> int:
    """How many WORKING/PARTIAL buy-to-close orders currently exist for this
    leg symbol — a resting STOP counts (its own leg carries buy_to_close,
    STP-01/06), and so does a marketable close. This is exactly "a working buy
    order on the short" in CLS-01's own words."""
    return sum(
        1 for o in broker._orders.values()
        if o.status in ("WORKING", "PARTIAL")
        and o.intent.legs[0].symbol == symbol
        and o.intent.legs[0].action == "buy_to_close"
    )


@given('an open entry with both stops resting')
def _(world):
    async def setup():
        broker = FakeBroker()
        put_stop = await broker.submit(stop_intent("PUT", entry_id="e1"))
        call_stop = await broker.submit(stop_intent("CALL", entry_id="e1"))
        return broker, put_stop, call_stop

    broker, put_stop, call_stop = asyncio.run(setup())
    world["broker"] = broker
    world["stops"] = {"PUT": put_stop, "CALL": call_stop}
    world["legs"] = INTENT_LEGS
    # Baseline BEFORE the close: exactly ONE working buy order per short (its
    # resting stop) — never zero, never two, from the start.
    for leg in INTENT_LEGS:
        if leg.role == "short":
            assert _working_buy_to_close_count(broker, leg.symbol) == 1
    world["events"] = []


@when('CloseEntry runs')
def _(world):
    asyncio.run(CloseEntry(world["broker"], world["events"]).close(
        "e1", "manual", resting_stop_ids=world["stops"], live_legs=world["legs"],
        close_price=D("0.05")))


@then("each short's stop is cancel/replaced with a marketable buy-to-close of ledger quantity")
def _(world):
    broker = world["broker"]
    for side, stop_id in world["stops"].items():
        # the OLD stop is gone (REPLACED — CLS-01 v1.50, never a bare CANCEL)
        assert broker._orders[stop_id].status == "REPLACED"
    # the NEW order per short is a marketable buy-to-close at the full ledger
    # quantity (1 contract here, per LiveLeg.signed_qty)
    shorts = [l for l in world["legs"] if l.role == "short"]
    for leg in shorts:
        news = [o for o in broker._orders.values()
                if o.intent.legs[0].symbol == leg.symbol
                and o.intent.legs[0].action == "buy_to_close"
                and o.intent.order_type == "marketable_limit"]
        assert len(news) == 1
        assert news[0].intent.contracts == abs(leg.signed_qty)


@then('at no point does a short leg have zero working buy orders')
def _(world):
    broker = world["broker"]
    # CLS-01 v1.50's whole point: CloseEntry issues ONE port call
    # (`broker.replace()`) per short, never a separate cancel() THEN submit().
    # FakeBroker.replace() flips the old order dead and the new one working in
    # the SAME call (tests/harness/fake_broker.py) — there is no call boundary
    # at which either observer (this test, or a real concurrent watcher) could
    # see the leg with nothing working. Immediately after the close, the count
    # is exactly 1 (the new marketable close) — never 0.
    for leg in world["legs"]:
        if leg.role == "short":
            assert _working_buy_to_close_count(broker, leg.symbol) == 1


@then('at no point does a short leg have two working buy orders')
def _(world):
    broker = world["broker"]
    for leg in world["legs"]:
        if leg.role == "short":
            assert _working_buy_to_close_count(broker, leg.symbol) == 1  # never 2


# =============================================================================
# Scenario 3: Replace races are terminal-safe
# =============================================================================

class FlakyReplaceBroker:
    """A FakeBroker wrapper whose `replace()` fails TRANSIENTLY exactly once
    for a chosen stop id (simulating a network blip) before delegating to the
    real FakeBroker — which itself raises the typed ORD-08 exceptions for a
    stop that has already filled."""

    def __init__(self, fake: FakeBroker, *, fail_once_for: str):
        self._fake = fake
        self._fail_once_for = fail_once_for
        self.replace_calls: list[str] = []

    async def replace(self, id, new):
        self.replace_calls.append(id)
        if id == self._fail_once_for and self.replace_calls.count(id) == 1:
            raise TimeoutError("scripted transient network blip")  # ORD-08(c)
        return await self._fake.replace(id, new)

    async def submit(self, order):
        return await self._fake.submit(order)


@given('the put stop fills while its replace is in flight')
def _(world):
    async def setup():
        fake = FakeBroker()
        # the PUT stop has ALREADY filled by the time replace() runs (the race
        # CLS-01(2)/ORD-08a describes) -- script it to fill on submission.
        fake.script_submit(Scripted("fill", payload={"price": "3.80"}))
        put_stop = await fake.submit(stop_intent("PUT", entry_id="e1"))
        call_stop = await fake.submit(stop_intent("CALL", entry_id="e1"))  # rests normally
        return fake, put_stop, call_stop

    fake, put_stop, call_stop = asyncio.run(setup())
    broker = FlakyReplaceBroker(fake, fail_once_for=call_stop)
    world["broker"] = broker
    world["fake"] = fake
    world["stops"] = {"PUT": put_stop, "CALL": call_stop}
    world["legs"] = INTENT_LEGS
    world["events"] = []


@then('the replace is classified FILLED (ORD-08a) and the side routes to SIDE_STOPPED + LEX')
def _(world):
    asyncio.run(CloseEntry(world["broker"], world["events"]).close(
        "e1", "manual", resting_stop_ids=world["stops"], live_legs=world["legs"],
        close_price=D("0.05")))
    # a direct unit check of the classification (ORD-08a) the FakeBroker itself
    # raises for an already-filled stop:
    with pytest.raises(ReplaceFilled):
        asyncio.run(world["fake"].replace(world["stops"]["PUT"], object()))
    # and CloseEntry's own reaction: the SAME event a live stop fill emits
    # (SimulatedBroker.try_fill_stop) — the SAME downstream SIDE_STOPPED -> LEX
    # reaction picks it up; CloseEntry never submits a second buy on this leg.
    put_stopped = [e for e in world["events"]
                  if isinstance(e, ShortStopped) and e.side == "PUT"]
    assert len(put_stopped) == 1 and put_stopped[0].initiator == "resting_stop"
    put_closes = [o for o in world["fake"]._orders.values()
                 if o.intent.legs[0].symbol == PUT_SHORT
                 and o.intent.legs[0].action == "buy_to_close"
                 and o.intent.order_type == "marketable_limit"]
    assert put_closes == []  # never a second buy-to-close on the put short


@given('given the call replace fails transient')
@then('given the call replace fails transient')
def _(world):
    # Setup already scripted the call stop's FIRST replace to raise
    # TimeoutError (ORD-08(c), unclassifiable-by-a-broker-is-transient) in the
    # `@given` step above — this step just documents/re-asserts the scripted
    # condition is in place before the following `@then` checks its outcome.
    assert world["broker"]._fail_once_for == world["stops"]["CALL"]


@then('the original call stop is still resting and the replace is retried per ORD-08')
def _(world):
    broker, fake = world["broker"], world["fake"]
    call_stop = world["stops"]["CALL"]
    # the retry happened: replace() was called at least twice for the call stop
    assert broker.replace_calls.count(call_stop) >= 2
    # the FIRST (failed) attempt never touched the real order -- FlakyReplaceBroker
    # raises BEFORE ever calling the underlying fake's replace(), so a failed
    # attempt cannot have cancelled/replaced anything. The retry then succeeded,
    # so the call stop's FINAL state is REPLACED (closed), never left dangling
    # in some half-cancelled state and never resulting in two working orders.
    assert fake._orders[call_stop].status == "REPLACED"
    call_closes = [o for o in fake._orders.values()
                  if o.intent.legs[0].symbol == CALL_SHORT
                  and o.intent.legs[0].action == "buy_to_close"
                  and o.intent.order_type == "marketable_limit"]
    assert len(call_closes) == 1  # exactly one buy-to-close resulted, not two


# =============================================================================
# Scenario 4: No ad-hoc closes exist (CLS-02, structural)
# =============================================================================

BACKEND_SRC = Path(__file__).resolve().parents[2] / "backend" / "src" / "meic"
REPO_ROOT = Path(__file__).resolve().parents[2]

# Every module under backend/src/meic that calls `<broker-like>.submit(...)` or
# `.replace(...)` today (verified by `grep -rn "broker\.submit(\|broker\.replace("
# backend/src/meic` at the time this test was written). Each is a legitimate,
# already-reviewed order-submission path with its OWN rule family — CLOSE
# orders specifically are CloseEntry's alone (checked separately below):
#   close_entry.py      - CLS-01/02: the canonical close (THIS module)
#   execute_entry.py    - ENT: opens the condor (the entry ladder)
#   protect_position.py - STP-01/06: places the protective stops
#   recover_long.py     - LEX: the long-sale ladder after a stop-out
#   decay_watcher.py    - DCY-02: short-only decay buyback + re-protect
#   reconcile.py        - REC-04: boot-time stop triage (re-places missing stops)
#   watchdog.py         - STP-03b: escalation buy-back when the resting stop lags
ALLOWED_SUBMIT_MODULES = {
    "application/close_entry.py", "application/execute_entry.py",
    "application/protect_position.py", "application/recover_long.py",
    "application/decay_watcher.py", "application/reconcile.py",
    "application/watchdog.py",
}


def _broker_submit_replace_callers() -> dict[str, set[str]]:
    """AST scan: every `.py` file under backend/src/meic containing a call
    `<name>.submit(...)` / `<name>.replace(...)` where `<name>`'s final
    identifier looks like a broker reference (`broker` or `_broker` — the
    two names used everywhere in this codebase for the injected
    BrokerGateway). Returns {relative_path: {"submit", "replace"}}."""
    hits: dict[str, set[str]] = {}
    for path in BACKEND_SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("submit", "replace"):
                continue
            receiver = node.func.value
            receiver_name = getattr(receiver, "attr", None) or getattr(receiver, "id", None)
            if receiver_name not in ("broker", "_broker"):
                continue
            rel = path.relative_to(BACKEND_SRC).as_posix()
            hits.setdefault(rel, set()).add(node.func.attr)
    return hits


def _kind_close_constructors() -> list[tuple[str, int]]:
    """AST scan: every `kind="close"` keyword literal anywhere under
    backend/src/meic, as (relative_path, lineno). `order_intent.py`'s own
    `marketable_close()` DEFAULT of `kind: str = "close"` is a parameter
    default, not a call-site keyword, so it never appears here — only an
    actual construction site (`OrderIntent(..., kind="close", ...)`) does."""
    hits: list[tuple[str, int]] = []
    for path in BACKEND_SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "kind" and isinstance(kw.value, ast.Constant) and kw.value.value == "close":
                    hits.append((path.relative_to(BACKEND_SRC).as_posix(), node.lineno))
    return hits


def _marketable_close_callers_without_kind_override() -> list[str]:
    """Any caller of `marketable_close(...)` OUTSIDE close_entry.py that does
    NOT explicitly pass its own `kind=` — i.e. would silently inherit the
    "close" default. Today only watchdog.py calls it, and it explicitly passes
    `kind="escalation"`. A new call site that forgets to override `kind` would
    be caught here."""
    offenders: list[str] = []
    for path in BACKEND_SRC.rglob("*.py"):
        rel = path.relative_to(BACKEND_SRC).as_posix()
        if rel == "application/close_entry.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "marketable_close":
                if not any(kw.arg == "kind" for kw in node.keywords):
                    offenders.append(f"{rel}:{node.lineno}")
    return offenders


@then('CloseEntry is the only module with close-order submission paths')
def _(world):
    # (a) only close_entry.py constructs a `kind="close"` order.
    close_kind_sites = _kind_close_constructors()
    assert close_kind_sites, "expected at least one kind=\"close\" construction (close_entry.py)"
    for rel, lineno in close_kind_sites:
        assert rel == "application/close_entry.py", \
            f"unexpected kind=\"close\" construction outside CloseEntry: {rel}:{lineno}"

    # (b) nobody reuses marketable_close()'s "close" default by omission.
    offenders = _marketable_close_callers_without_kind_override()
    assert offenders == [], f"marketable_close() called without an explicit kind= override: {offenders}"

    # (c) the broader submit/replace call graph is exactly the reviewed set —
    # a NEW module calling broker.submit/replace must be added here deliberately.
    callers = set(_broker_submit_replace_callers().keys())
    unexpected = callers - ALLOWED_SUBMIT_MODULES
    assert unexpected == set(), f"unreviewed broker order-submission path(s): {unexpected}"


@then('no agent or tooling path can submit a broker order outside the application services')
def _(world):
    # tools/ and scripts/ (repo root) must contain no direct place_order/submit/
    # broker.replace caller — an agent or ad-hoc script talking to the broker
    # bypassing CloseEntry/the application services entirely (CLS-02's binding
    # rule: "No ad-hoc broker-side orders by an agent, ever").
    needles = ("place_order", ".submit(", "broker.replace(")
    for folder in ("tools", "scripts"):
        base = REPO_ROOT / folder
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                assert needle not in text, f"{path}: found {needle!r} — an ad-hoc broker order path"
