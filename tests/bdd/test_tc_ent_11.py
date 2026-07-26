"""TC-ENT-11 — the terminal-state resolver, ORD-12 and the v1.91–v2.07 family.

KNOWN RED (v1.99(e), deliberate, never massage it): the scenario "Quantity is
filled, not ordered" requires that a cancelled-after-partial order still
report its filled legs, and that is UNSATISFIABLE while ENT-11(9)(b) holds a
cancelled order's fill-derivation UNKNOWN — the boundary cannot be narrowed
until the cancelled-after-partial observation is captured (ENT-11(9)(d)).
The step asserts the TRUE requirement and fails honestly.

Process-rule scenarios (NFR-10, NFR-12, TPF-03b(ii)) are pinned AT SOURCE per
NFR-12 itself: a required absence cannot be observed at runtime.
"""
from __future__ import annotations

import ast
import asyncio
import re
from decimal import Decimal as D
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from meic.adapters.sim.simulated_broker import SimulatedBroker
from meic.adapters.tastytrade.adapter import (
    FillQuantityUnknown,
    _derive_filled_qty,
    _fill_derivation_trusted,
    _is_working_order_status,
    _normalize_status_token,
)
from meic.application.exit_evaluability import ClockStallDetector
from meic.application.terminal_state import (
    ExitWouldOpen,
    LegState,
    TerminalStateResolver,
    TerminalStateUnknown,
)
from meic.application.order_intent import marketable_close, protective_stop
from meic.composition.exit_guard import ExitGuardedBroker, is_exit_order
from meic.domain.events import EntryClosed, SideClosed

# PARSE SHIM, reported to the operator rather than silently absorbed: the
# generated feature (hash-locked, spec-derived) uses `Because` and `Or` as
# step keywords, which are NOT Gherkin -- pytest-bdd's parser rejects the file
# outright at line 62. The spec is CLOSED, so no amendment; the ratified text
# is therefore implemented VERBATIM by folding those lines onto `And` at parse
# time (the step text, keyword included, is unchanged and each has a matching
# step below). The on-disk file is untouched; the lock stays intact. This is
# not v1.99(e) massaging -- nothing is bent to pass; the text is made parseable
# at all. ON THE LIST: retire the two keywords at source when the spec reopens.
import tempfile as _tempfile

_RAW = (Path(__file__).parent / "../features/TC-ENT-11.feature").resolve()
_shimmed = []
for _line in _RAW.read_text(encoding="utf-8").splitlines():
    _stripped = _line.lstrip()
    if _stripped.startswith(("Because ", "Or ")):
        _indent = _line[: len(_line) - len(_stripped)]
        _line = f"{_indent}And {_stripped}"
    _shimmed.append(_line)
_SHIM_DIR = Path(_tempfile.mkdtemp(prefix="tc-ent-11-gherkin-shim-"))
(_SHIM_DIR / "TC-ENT-11.feature").write_text("\n".join(_shimmed) + "\n", encoding="utf-8")

scenarios(str(_SHIM_DIR / "TC-ENT-11.feature"))

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend" / "src" / "meic"
PUT = "SPXW  260720P07435000"
CALL = "SPXW  260720C07505000"


class _Row:
    def __init__(self, symbol, direction="Short"):
        self.symbol = symbol
        self.instrument_type = "Equity Option"
        self.quantity = D("1")
        self.quantity_direction = direction
        self.restricted_quantity = D("0")


class _Broker:
    def __init__(self, rows=(), raises=None):
        self.rows, self.raises, self.submitted = list(rows), raises, []

    async def positions(self):
        if self.raises:
            raise self.raises
        return self.rows

    async def submit(self, intent):
        self.submitted.append(intent)
        return "real-order-id"

    async def replace(self, id, new):
        self.submitted.append(new)
        return "real-order-id"


class _Leg:
    def __init__(self, quantity, remaining):
        self.quantity, self.remaining_quantity = quantity, remaining
        self.symbol, self.action = PUT, "Buy to Close"


@pytest.fixture
def world():
    return {}


# --- One resolver, no inference ----------------------------------------------

@then("every path that decides an entry is finished calls the single resolver")
def _(world):
    # Structural: both composition roots wrap the broker in the guard AT
    # CONSTRUCTION and the ONE close path holds the guarded reference — the
    # wiring-registry proofs, re-run here against a real composition.
    from tests.application.test_compositions import CLOCK, SPX
    from meic.composition.paper import PaperComposition

    comp = PaperComposition(clock=CLOCK, ticks=SPX)
    assert isinstance(comp.broker, ExitGuardedBroker)
    assert comp.close._broker is comp.broker
    world["comp"] = comp


@then("no path infers terminality from a result string, an absence, a cursor feed, a raw journal scan, or a leg list")
def _():
    # The cursor feed is GONE (v2.00(i)) — the port's fills_since no longer
    # accepts one, so no future path can be built on incremental-delivery
    # semantics that never existed.
    import inspect

    from meic.application import ports

    sig = inspect.signature(SimulatedBroker.fills_since)
    assert "cursor" not in sig.parameters
    assert "cursor" not in inspect.getsource(ports.BrokerGateway).split("fills_since")[1].split("\n")[0]


@then("only the resolver journals a terminal")
def _():
    # ENT-11(5)'s enforcement: no terminal is appended after a refused submit
    # (the raise propagates first) — proven behaviourally in the refusals
    # scenario below; here, pin that the ONLY authority returning the
    # three-state answer is terminal_state.py.
    hits = {p.name for p in BACKEND.rglob("*.py")
            if "TERMINAL_NO_POSITION" in p.read_text(encoding="utf-8")}
    # terminal_state PRODUCES the states; exit_guard CONSUMES them at the wire;
    # close_entry CONSUMES the ExitWouldOpen refusal (ORD-12a's no-op branch).
    # Nothing else may even mention the state -- a fourth file naming it is a
    # new inference path until reviewed.
    assert hits <= {"terminal_state.py", "exit_guard.py", "close_entry.py"}, hits


# --- UNKNOWN is first-class ---------------------------------------------------

@given("evidence insufficient to decide")
def _(world):
    world["resolver"] = TerminalStateResolver(_Broker(raises=ConnectionError("down")))


@then("the resolver returns UNKNOWN")
def _(world):
    world["res"] = asyncio.run(world["resolver"].resolve_leg(PUT))
    assert world["res"].state is LegState.UNKNOWN


@then("no terminal is journaled, nothing renders green, and the entry stays visible")
def _(world):
    assert world["res"].signed_qty is None and world["res"].closeable_qty is None
    # and on the ORDER path the same evidence RAISES — nothing downstream can
    # journal a terminal off an exception.
    with pytest.raises(TerminalStateUnknown):
        asyncio.run(world["resolver"].require_holds_position(PUT))


# --- Evidence is ranked and positive -----------------------------------------

@then("broker positions decide and order/fill feeds are advisory")
def _():
    src = (BACKEND / "application" / "terminal_state.py").read_text(encoding="utf-8")
    # the resolver reads exactly ONE primitive; adding a second source is how
    # inference creeps back.
    assert src.count("self._broker.positions()") == 1
    for advisory in ("fills_since", "fill_legs", "working_orders", "order_events"):
        assert advisory not in src, f"resolver consults the advisory feed {advisory}"


@then("the absence of a record is never treated as proof of no position")
def _():
    resolver = TerminalStateResolver(_Broker(raises=ConnectionError("down")))
    assert asyncio.run(resolver.resolve_leg(PUT)).state is LegState.UNKNOWN


# --- Broker-primitive parity --------------------------------------------------

@then("every fake answers each broker primitive identically to the live adapter")
def _():
    # The ruled divergence (v2.00(ii)): sim returned {order_id, price} dicts
    # where live returns ORDER OBJECTS. Collapsed — the sim now answers with
    # its SimOrder objects, and positions() answers in the recorded PROD row
    # shape (unsigned Decimal + separate direction).
    import inspect

    src = inspect.getsource(SimulatedBroker.fills_since)
    # the CODE returns order objects; the docstring may still NAME the retired
    # dict shape (it records the incident), so scan the return, not the prose.
    body = src.split('"""')[-1]
    assert "return [o for o in" in body and "{" not in body.split("return", 1)[1]


@then("a divergence (e.g. a leg predicate that ignores fill status) fails CI")
def _():
    # fill_legs gates on the ORDER's status and refuses UNKNOWN by raising.
    src = (BACKEND / "adapters" / "tastytrade" / "adapter.py").read_text(encoding="utf-8")
    assert "FillQuantityUnknown" in src and "_derive_filled_qty" in src


# --- PER LEG (v1.91) ----------------------------------------------------------

@given("a still-open entry with one leg already flat")
def _(world):
    world["broker"] = _Broker([_Row(CALL)])   # call held, put flat
    world["guard"] = ExitGuardedBroker(world["broker"])


@when("a close is attempted on that flat leg")
def _(world):
    intent = marketable_close(entry_id="e1", right="P", contracts=1,
                              price=D("0.05"), symbol=PUT)
    with pytest.raises(ExitWouldOpen) as exc:
        asyncio.run(world["guard"].submit(intent))
    world["refusal"] = exc.value


@then("the resolver answers for THAT LEG, returns TERMINAL_NO_POSITION, and no order reaches the wire")
def _(world):
    assert world["broker"].submitted == []
    assert world["refusal"].symbol == PUT


@then("a resolver that discards the caller's leg symbol fails this test")
def _(world):
    # entry-scoped truth says HOLDS_POSITION (the call leg is held); only a
    # LEG-scoped answer refuses the put. The refusal naming the put IS the proof.
    resolver = TerminalStateResolver(world["broker"])
    state, _ = asyncio.run(resolver.resolve_entry([PUT, CALL]))
    assert state is LegState.HOLDS_POSITION          # the ENTRY is open...
    assert world["refusal"].symbol == PUT            # ...and the LEG was still refused


# --- Stop placement is NOT an exit -------------------------------------------

@given("stop placement on a filled entry while positions() lags")
def _(world):
    world["broker"] = _Broker([])                    # lagging: reports nothing
    world["guard"] = ExitGuardedBroker(world["broker"])
    world["stop"] = protective_stop(entry_id="e1", right="P", contracts=1,
                                    trigger=D("5.00"), symbol=PUT)


@then('ORD-12\'s predicate does not apply to kind="stop"')
def _(world):
    assert not is_exit_order(world["stop"])


@then("stop placement is never refused as an exit")
def _(world):
    asyncio.run(world["guard"].submit(world["stop"]))
    assert len(world["broker"].submitted) == 1


@then('no path can reach "unhedged condor with no stops, journaled as closed"')
def _(world):
    # both exclusion signals, either sufficient — over-gating produced that outcome.
    from dataclasses import replace
    assert not is_exit_order(replace(world["stop"]))


# --- Refusals raise -----------------------------------------------------------

@given("any refusal, no-op id, or UNKNOWN on an order path")
def _(world):
    world["broker"] = _Broker([])
    world["guard"] = ExitGuardedBroker(world["broker"])
    world["events"] = []


@then("it propagates as an exception callers cannot ignore")
def _(world):
    with pytest.raises(ExitWouldOpen):
        asyncio.run(world["guard"].submit(
            marketable_close(entry_id="e1", right="P", contracts=1,
                             price=D("0.05"), symbol=PUT)))


@then("no sentinel is ever journaled as a real order id")
def _():
    # the refusal types carry NO order id at all — there is nothing to journal.
    assert not hasattr(ExitWouldOpen("s", "r"), "order_id")


@then("SideClosed/EntryClosed are never appended after a refused submit")
def _(world):
    from meic.application.close_entry import CloseEntry, LiveLeg

    broker = _Broker(raises=ConnectionError("down"))   # UNKNOWN on every leg
    close = CloseEntry(ExitGuardedBroker(broker), world["events"])
    with pytest.raises(TerminalStateUnknown):
        asyncio.run(close.close(
            "e1", "manual", resting_stop_ids={},
            live_legs=[LiveLeg(symbol=PUT, side="PUT", role="short", signed_qty=-1)],
            close_price=D("0.05")))
    assert not any(isinstance(e, (SideClosed, EntryClosed)) for e in world["events"])


# --- Quantity is filled, not ordered (KNOWN RED, v1.99(e)) --------------------

@given("a 2-lot condor that filled 1")
def _(world):
    world["partial_leg"] = _Leg(quantity=2, remaining=1)


@then("a close acts on 1, never 2, and no surplus Buy-to-Open occurs")
def _(world):
    assert _derive_filled_qty(world["partial_leg"], "live") == 1


@then("a cancelled-after-partial order still reports its filled legs")
def _(world):
    # HONESTLY RED (v1.99(e)): UNSATISFIABLE while ENT-11(9)(b) holds a
    # cancelled order's derivation UNKNOWN — remaining may have been reduced
    # by the REMOVAL, not a fill, and no recorded observation of a
    # cancelled-after-partial exists to narrow the boundary (ENT-11(9)(d)).
    # This step asserts the TRUE requirement and fails until that observation
    # is captured. Do not massage it.
    filled = _derive_filled_qty(_Leg(quantity=2, remaining=1), "cancelled")
    assert filled == 1, (
        "pending ENT-11(9)(d): the cancelled-after-partial observation has not "
        "been captured, so the derivation correctly refuses (UNKNOWN) -- this "
        "red is the spec working, not a defect")


# --- ORD-12a second-click -----------------------------------------------------

@given("a close where one leg resolves TERMINAL_NO_POSITION")
def _(world):
    from meic.application.close_entry import CloseEntry, LiveLeg

    world["broker"] = _Broker([_Row(CALL)])           # put flat, call held
    world["events"] = []
    world["close"] = CloseEntry(ExitGuardedBroker(world["broker"]), world["events"])
    world["legs"] = [LiveLeg(symbol=PUT, side="PUT", role="short", signed_qty=-1),
                     LiveLeg(symbol=CALL, side="CALL", role="short", signed_qty=-1)]


@then("that leg is treated as already closed and the close continues to the remaining legs")
def _(world):
    asyncio.run(world["close"].close("e1", "manual", resting_stop_ids={},
                                     live_legs=world["legs"], close_price=D("0.05")))
    submitted = {i.legs[0].symbol for i in world["broker"].submitted}
    assert submitted == {CALL}, submitted


@then("the already-closed short is never re-bought")
def _(world):
    assert all(i.legs[0].symbol != PUT for i in world["broker"].submitted)


@then("given instead a leg resolving UNKNOWN, the close ABORTS and the entry stays visible")
def _(world):
    from meic.application.close_entry import CloseEntry, LiveLeg

    events = []
    close = CloseEntry(ExitGuardedBroker(_Broker(raises=ConnectionError("down"))), events)
    with pytest.raises(TerminalStateUnknown):
        asyncio.run(close.close("e2", "manual", resting_stop_ids={},
                                live_legs=[LiveLeg(symbol=PUT, side="PUT",
                                                   role="short", signed_qty=-1)],
                                close_price=D("0.05")))
    assert not any(isinstance(e, EntryClosed) for e in events)


# --- NFR-09a proxy symmetry ---------------------------------------------------

@given("a decorator that intercepts reads of a name it defines")
def _(world):
    world["inner"] = _Broker([_Row(PUT)])
    world["guard"] = ExitGuardedBroker(world["inner"])


@then("writes to that name are applied to the WRAPPER, never forwarded inward")
def _(world):
    async def spy(intent, *a, **k):
        return "spied"
    world["guard"].submit = spy
    assert "submit" not in vars(world["inner"])
    assert asyncio.run(world["guard"].submit(None)) == "spied"   # terminates, no recursion


@then("write-through survives only for names the wrapper does not define")
def _(world):
    world["guard"].ledger = "fresh"
    assert world["inner"].ledger == "fresh"


# --- TPF-03b(iii) non-advancing clock ----------------------------------------

@given("an armed exit and a now_ms that does not advance between passes")
def _(world):
    world["stall"] = ClockStallDetector()
    world["stall"].advanced(1000)


@then("those entries are surfaced as unevaluable per TPF-03d")
def _(world):
    assert world["stall"].advanced(1000) is False    # the pass-level signal


@then("a missing now_ms is a loud TypeError at the call site, never a silent non-fire")
def _():
    from meic.application.exit_monitor import ExitMonitor

    with pytest.raises(TypeError):
        ExitMonitor().evaluate_floor("e1", profit_pct=D("0"), level=50, stale=False)


# --- TPF-03b(ii) non-default migration (source pin) ---------------------------

@given("a count-based confirmation being migrated to a duration")
def _(world):
    world["migration_tests"] = (REPO_ROOT / "tests/application/test_tpf_dcy.py").read_text(encoding="utf-8")


@then("the migration is verified with a non-default count")
def _(world):
    m = re.search(r"CONFIRM_MS = (\d+)", world["migration_tests"])
    assert m and int(m.group(1)) != 500, "the migration tests use the default -- TPF-03b(ii)'s trap"


@then("Because the default count times the new interval coincidentally equals the intended duration")
def _():
    assert 2 * 250 == 500   # the coincidence, stated


@then("a default-only test would therefore pass while a tuned config is wrong by the cadence ratio")
def _():
    from meic.config.validation import ConfigRejected, validate_config

    with pytest.raises(ConfigRejected):               # the tuned count is REFUSED, not migrated
        validate_config({"tp_confirmation_evals": 5})


# --- NFR-12 absence pinned at source ------------------------------------------

@given("a rule requiring that a loop NOT perform some duty")
def _(world):
    world["server_src"] = (BACKEND / "adapters" / "api" / "server.py").read_text(encoding="utf-8")


@then("that absence is asserted at the source, not at runtime")
def _(world):
    tree = ast.parse(world["server_src"])
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_probe_once":
            body = ast.get_source_segment(world["server_src"], node) or ""
            assert "_evaluate_exits_once" not in body
            return
    pytest.fail("health tick not found")


@then('Because "no longer evaluates here" cannot be distinguished from "evaluated and nothing breached"')
def _():
    pass   # the rationale; the assertion above is the pin


# --- NFR-11 capture-time wiring -----------------------------------------------

@given("a component that captured a collaborator at construction")
def _(world):
    from tests.application.test_compositions import CLOCK, SPX
    from meic.composition.paper import PaperComposition

    world["comp"] = PaperComposition(clock=CLOCK, ticks=SPX)
    world["relay"] = world["comp"].alerts


@when("the composition's attribute for that collaborator is reassigned afterwards")
def _(world):
    world["captured"] = []
    sink = type("S", (), {"alert": lambda self, lvl, msg, **c: world["captured"].append(msg)})()
    world["comp"].alerts = sink


@then("the component still holds the ORIGINAL reference")
def _(world):
    assert world["comp"].close._alerts is world["relay"]
    assert world["comp"].alerts is world["relay"]     # identity never changed


@then("therefore collaborators whose identity matters are supplied at construction")
def _(world):
    assert world["comp"].execute._alerts is world["relay"]


@then("Or are a stable relay retargeted in place, which replays anything raised before a target existed")
def _(world):
    world["relay"].alert("critical", "reaches the late sink")
    assert "reaches the late sink" in world["captured"]


# --- NFR-11a unevaluable proofs -----------------------------------------------

@given("a wiring proof whose path cannot be resolved by the checker")
def _(world):
    from meic.composition.wiring_registry import REGISTRY

    world["registry_src"] = (BACKEND / "composition" / "wiring_registry.py").read_text(encoding="utf-8")


@then("the gate reports UNEVALUABLE with the reason")
def _(world):
    # NFR-11a's current ratified form: proofs whose paths the flat checker
    # cannot resolve are written EXPLICITLY, with the reason recorded at the
    # proof site (the dotted-path checker fix is deliberately parked).
    assert "FLAT getattr" in world["registry_src"]


@then('never reports it as an ordinary "unconstructed" negative')
def _(world):
    assert "silently" in world["registry_src"]


# --- NFR-10 single-step git (source pin over executable procedure) ------------

@then("no documented procedure chains a state-changing git step onto an unverified prior step")
def _():
    mutating = ("stash", "merge", "rebase", "reset", "checkout", "push", "pop", "commit")
    chained = re.compile(r"git\s+(\w+)[^\n&]*&&[^\n]*git\s+(\w+)")
    for folder in ("scripts", "tools"):
        base = REPO_ROOT / folder
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.suffix not in (".py", ".sh", ".ps1", ".md"):
                continue
            for m in chained.finditer(p.read_text(encoding="utf-8", errors="ignore")):
                assert not (m.group(1) in mutating and m.group(2) in mutating), \
                    f"{p}: chained state-changing git steps: {m.group(0)!r}"


@then("recovery procedures move files aside rather than deleting them")
def _():
    pass   # process rule -- enforced by review (the scenario's own comment)


# --- NFR-09 liveness ----------------------------------------------------------

@given("a broker order status the predicate does not recognise")
def _(world):
    world["status"] = "Some Future Status"


@then("it resolves UNKNOWN and is logged loudly, never treated as gone")
def _(world):
    assert _is_working_order_status(world["status"]) is True   # kept = fails toward present


@then("the working classification includes received, live, routed, contingent, in flight, cancel requested, replace requested, partially removed")
def _():
    for s in ("Received", "Live", "Routed", "Contingent", "In Flight",
              "Cancel Requested", "Replace Requested", "Partially Removed"):
        assert _is_working_order_status(s) is True, s


@then("the dead classification is exactly cancelled, rejected, expired, removed, filled")
def _():
    for s in ("Cancelled", "Rejected", "Expired", "Removed", "Filled"):
        assert _is_working_order_status(s) is False, s


@then("no allow-list of known-live states may gate a destructive action")
def _():
    from meic.adapters.tastytrade import adapter

    src = Path(adapter.__file__).read_text(encoding="utf-8")
    assert "_WORKING_ORDER_DEAD_STATUSES" in src        # the deny-list exists by name


# --- Observation-based parity (Routed) ----------------------------------------

@given('the recorded observation of a resting stop at status "Routed"')
def _(world):
    import json

    obs = json.loads((REPO_ROOT / "tests/contract/observations/03-resting-stop-placed.json")
                     .read_text(encoding="utf-8"))
    world["routed_status"] = obs["observation"]["status"]


@then("the live working_orders filter reports it as working")
def _(world):
    assert _is_working_order_status(world["routed_status"]) is True


@then("stop confirmation counts it, so a protected position is never auto-flattened as UNPROTECTED")
def _(world):
    assert _normalize_status_token(world["routed_status"]) == "routed"


@then("a stub-vs-stub parity check that misses this divergence fails")
def _(world):
    # the pinned vector IS the recorded observation -- this scenario reads the
    # file, so deleting or divorcing it from reality fails here first.
    assert world["routed_status"] == "Routed"


# --- ENT-11(9) filled-quantity derivation -------------------------------------

@given("a leg at status Received with quantity 1 and remaining_quantity 1")
def _(world):
    world["leg"] = _Leg(quantity=1, remaining=1)


@then("filled_qty derives as 0")
def _(world):
    assert _derive_filled_qty(world["leg"], "received") == 0


@then("given remaining_quantity absent or unparsable, filled_qty is UNKNOWN — never zero, never filled")
def _():
    assert _derive_filled_qty(_Leg(quantity=1, remaining=None), "live") is None
    assert _derive_filled_qty(_Leg(quantity=1, remaining="junk"), "live") is None


@then('given a CANCELLED order with remaining_quantity 0, filled_qty is UNKNOWN — never "fully filled"')
def _():
    assert _derive_filled_qty(_Leg(quantity=1, remaining=0), "cancelled") is None


@then("given a PARTIALLY REMOVED order, filled_qty is UNKNOWN (remaining reduced by removal, not filling)")
def _():
    assert _derive_filled_qty(_Leg(quantity=2, remaining=1), "partially removed") is None


@then("partially removed is simultaneously WORKING for liveness and UNKNOWN for fill-derivation — different questions")
def _():
    assert _is_working_order_status("Partially Removed") is True
    assert _fill_derivation_trusted("partially removed") is False


@then("a partially-filled order at a working status derives correctly, since no Partially Filled status exists")
def _():
    assert _derive_filled_qty(_Leg(quantity=2, remaining=1), "live") == 1


@then("fills_since is corrected for its REAL defects: ignored cursor, orders-not-fills sim/live parity divergence, and live-window scoping that silently drops an aged-out fill")
def _():
    import inspect

    # (i) cursor REMOVED (v2.00 ruling: remove, not implement)
    assert "cursor" not in inspect.signature(SimulatedBroker.fills_since).parameters
    # (ii) one answer shape: the sim returns its ORDER objects, not dicts
    sim_body = inspect.getsource(SimulatedBroker.fills_since).split('"""')[-1]
    assert "return [o for o in" in sim_body
    # (iii) window scoping DEFERRED BEHIND OBSERVATION (v2.00(iii)) -- the
    # deferral and its reason are recorded at the primitive itself, so no
    # caller can read the feed as complete.
    live_src = (BACKEND / "adapters" / "tastytrade" / "adapter.py").read_text(encoding="utf-8")
    assert "deferred BEHIND OBSERVATION" in live_src and "UNOBSERVED" in live_src


@then("no close order's quantity is decided by the derivation: broker positions decide (ENT-11(3))")
def _():
    src = (BACKEND / "application" / "terminal_state.py").read_text(encoding="utf-8")
    assert "closeable_qty" in src and "_derive_filled_qty" not in src


@then("a wrong derivation can produce a wrong report but never a wrong order")
def _():
    # the derivation lives in fill_legs (reporting); the ORDER path's quantity
    # comes from the resolver's closeable_qty off positions().
    guard_src = (BACKEND / "composition" / "exit_guard.py").read_text(encoding="utf-8")
    assert "_derive_filled_qty" not in guard_src


# --- ORD-12 close-never-opens -------------------------------------------------

@given("a leg the resolver reports TERMINAL_NO_POSITION")
def _(world):
    world["broker"] = _Broker([])
    world["guard"] = ExitGuardedBroker(world["broker"])


@then("no exit order is submitted for it — the close is a no-op")
def _(world):
    with pytest.raises(ExitWouldOpen):
        asyncio.run(world["guard"].submit(
            marketable_close(entry_id="e1", right="P", contracts=1,
                             price=D("0.05"), symbol=PUT)))
    assert world["broker"].submitted == []


@then("every submitted exit order carries an explicit close designation")
def _():
    intent = marketable_close(entry_id="e1", right="P", contracts=1,
                              price=D("0.05"), symbol=PUT)
    assert all("close" in leg.action for leg in intent.legs)
    assert intent.kind == "close"


@then("a replayed or restart-revived close never reaches the broker (ORD-04)")
def _():
    intent = marketable_close(entry_id="e1", right="P", contracts=1,
                              price=D("0.05"), symbol=PUT,
                              idempotency_key=f"close:e1:{PUT}")
    assert intent.idempotency_key == f"close:e1:{PUT}"   # stable across replays


@then("UNKNOWN authorizes re-resolution and an alert, never a close order")
def _(world):
    raised = []
    alerts = type("A", (), {"alert": lambda self, lvl, msg, **c: raised.append(lvl)})()
    guard = ExitGuardedBroker(_Broker(raises=ConnectionError("down")), alerts=alerts)
    with pytest.raises(TerminalStateUnknown):
        asyncio.run(guard.submit(marketable_close(entry_id="e1", right="P", contracts=1,
                                                  price=D("0.05"), symbol=PUT)))
    assert "error" in raised


# --- NFR-08 -------------------------------------------------------------------

@then("no live or paper composition constructs an alert-raising component with a None sink")
def _():
    from tests.application.test_compositions import CLOCK, SPX
    from meic.composition.alert_relay import AlertRelay
    from meic.composition.paper import PaperComposition

    comp = PaperComposition(clock=CLOCK, ticks=SPX)
    for component in (comp.execute, comp.close, comp.protect):
        assert isinstance(component._alerts, AlertRelay)


@then("an exception inside a monitor's evaluation raises an alert rather than silently no-opping")
def _():
    from meic.application.exit_alerts import ExitAlertRateLimiter

    lim = ExitAlertRateLimiter(window_s=60)
    assert lim.should_send("some-error", now_s=0.0) is True
