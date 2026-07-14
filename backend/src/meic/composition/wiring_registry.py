"""NFR-07 wiring-audit registry (v1.67).

The single source of truth imported by BOTH a pytest test (TC-NFR-07
scenario 1) and a future operator-maintained `scripts/` CLI: every spec rule
that mandates a runtime component (monitor, watcher, sweep, loop, sampler,
reconciler), and how to PROVE, against a real `live_app()`, that the
component is both CONSTRUCTED (a real instance of the right class exists,
reachable off `app.state`) and TICKED (its supervised background task exists
and is alive).

This is deliberately NOT a hardcoded "everything looks fine" list: each entry
carries the two callables a test/CLI actually RUNS against `app.state` --
`constructed(state) -> bool` and `ticked(state) -> bool` -- so the day
`live_app()` stops wiring a component, the SAME check that would otherwise
need a human to notice the grep count changed fails instead.

Self-policing (best-effort, HEURISTIC -- read this honestly): the spec's
prose has no machine-readable "this rule mandates a runtime component" tag,
so a fully automatic proof that the registry is COMPLETE is not achievable
without either (a) a hand-maintained tag in the spec (spec/ is read-only and
owner-maintained -- not this module's to add) or (b) a keyword heuristic over
the spec text. This module implements (b) in `spec_runtime_component_rule_ids`:
it greps the spec for rule ids whose OWN bolded defining line contains one of
NFR-07's own listed keywords ("monitors, watchers, sweeps, loops, samplers,
reconcilers"), and the accompanying test asserts that set is a SUBSET of the
registry's declared rule ids. A rule worded without any of these keywords, or
one whose defining line the regex doesn't match, could still mandate a
runtime component and slip past this scan -- flagged here rather than
silently claimed as a formal proof.
"""
from __future__ import annotations

import asyncio
import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[4]
SPEC_DIR = REPO_ROOT / "spec"

# NFR-07's own words (spec/05-architecture-ddd.md): "monitors, watchers,
# sweeps, loops, samplers, reconcilers" -- plus the synonyms this codebase
# actually uses for the same concept (watchdog, poll, supervisor).
_RUNTIME_KEYWORDS = (
    "monitor", "watch", "sweep", "loop", "sampl", "reconcil", "poll", "supervis",
)

# Rule ids are written **BOLD** at the start of their defining bullet, e.g.
# "**DCY-01 Trigger.**" or "**STP-08a Live stop-fill reaction chain ...**".
_RULE_ID_RE = re.compile(r"\*\*([A-Z]{2,6}-\d{1,3}[a-z]?)\b")


def _task_alive(task: object) -> bool:
    return task is not None and not getattr(task, "done", lambda: True)()


@dataclass(frozen=True)
class WiringEntry:
    rule_ids: tuple[str, ...]
    component: str
    proof: str
    constructed: Callable[[object], bool]   # (app.state) -> True iff really constructed
    ticked: Callable[[object], bool]        # (app.state) -> True iff really ticking


def _isinstance_check(attr: str, cls_path: str) -> Callable[[object], bool]:
    """(app.state) -> bool: the named attribute is a real instance of the
    class at `cls_path` ("dotted.module:ClassName"), imported lazily so this
    registry never pays every adapter's import cost just by being imported."""
    module_name, _, cls_name = cls_path.partition(":")

    def _check(state: object) -> bool:
        obj = getattr(state, attr, None)
        if obj is None:
            return False
        cls = getattr(importlib.import_module(module_name), cls_name)
        return isinstance(obj, cls)

    return _check


def _task_check(attr: str) -> Callable[[object], bool]:
    def _check(state: object) -> bool:
        return _task_alive(getattr(state, attr, None))

    return _check


def _truthy_attr(attr: str) -> Callable[[object], bool]:
    def _check(state: object) -> bool:
        return getattr(state, attr, None) is not None

    return _check


def _all_truthy_attrs(*attrs: str) -> Callable[[object], bool]:
    def _check(state: object) -> bool:
        return all(getattr(state, attr, None) is not None for attr in attrs)

    return _check


REGISTRY: tuple[WiringEntry, ...] = (
    WiringEntry(
        rule_ids=("DCY-01", "DCY-02", "DCY-03", "DCY-04"),
        component="DecayWatcher",
        proof="app.state.decay_watcher is a real DecayWatcher (construction); "
              "app.state.decay_watcher_task is alive (ticking). The pinned "
              "regression (NFR-07): tests/application/test_decay_watcher_wiring.py "
              "drives an actual ask<=trigger fire through the SAME pass and asserts "
              "a DecayBuybackPlaced lands -- not just object presence.",
        constructed=_isinstance_check("decay_watcher", "meic.application.decay_watcher:DecayWatcher"),
        ticked=_task_check("decay_watcher_task"),
    ),
    WiringEntry(
        rule_ids=("STP-03b",),
        component="StopWatchdog",
        proof="app.state.stop_watchdog is a real StopWatchdog; "
              "app.state.stop_watchdog_task is alive",
        constructed=_isinstance_check("stop_watchdog", "meic.application.watchdog:StopWatchdog"),
        ticked=_task_check("stop_watchdog_task"),
    ),
    WiringEntry(
        rule_ids=("LEX-07",),
        component="LexLadderWatchdog",
        proof="app.state.lex_ladder_watchdog is a real LexLadderWatchdog; "
              "app.state.lex_ladder_watchdog_task is alive",
        constructed=_isinstance_check(
            "lex_ladder_watchdog", "meic.application.lex_ladder_watchdog:LexLadderWatchdog"),
        ticked=_task_check("lex_ladder_watchdog_task"),
    ),
    WiringEntry(
        rule_ids=("NFR-04",),
        component="QuoteHub live stream",
        proof="app.state.quote_hub is a real QuoteHub; app.state.quote_stream_task is alive",
        constructed=_isinstance_check("quote_hub", "meic.domain.quote_hub:QuoteHub"),
        ticked=_task_check("quote_stream_task"),
    ),
    WiringEntry(
        rule_ids=("STP-08a",),
        component="Order-event push consumer (live stop/decay-fill detection)",
        proof="app.state.stop_fill_detector is the real detector closure the push "
              "consumer and the fallback poll below share; "
              "app.state.order_event_task is alive",
        constructed=_truthy_attr("stop_fill_detector"),
        ticked=_task_check("order_event_task"),
    ),
    WiringEntry(
        rule_ids=("STP-08a",),
        component="Stop-fill fallback poll",
        proof="app.state.stop_fill_poll_interval_s is the real env-configured "
              "cadence; app.state.stop_fill_poll_task is alive",
        constructed=_truthy_attr("stop_fill_poll_interval_s"),
        ticked=_task_check("stop_fill_poll_task"),
    ),
    WiringEntry(
        rule_ids=("NFR-02", "DAT-02", "RPT-12", "TPF-03", "TPT-04", "EOD-03", "RPT-15"),
        component="Health tick (_probe_once: session probe, DAT-02 snapshot refresh, "
                  "RPT-12 mark sampler, TPF-03/TPT-04 exit monitor, EOD-03 sweep, "
                  "RPT-15 reconcile)",
        proof="app.state.exit_monitor / chain_snapshots / report_reconciler are the "
              "real objects the tick drives; app.state.health_task is alive",
        constructed=_all_truthy_attrs("exit_monitor", "chain_snapshots", "report_reconciler"),
        ticked=_task_check("health_task"),
    ),
    WiringEntry(
        rule_ids=("ENT-10", "DAY-01"),
        component="Day supervisor (auto-starts/crash-latches the trading day task)",
        proof="app.state.day_task_failed/day_supervisor_error are the real crash-latch "
              "state the supervisor maintains; app.state.day_supervisor is alive",
        constructed=lambda state: hasattr(state, "day_task_failed"),
        ticked=_task_check("day_supervisor"),
    ),
)


def all_rule_ids() -> frozenset[str]:
    ids: set[str] = set()
    for entry in REGISTRY:
        ids.update(entry.rule_ids)
    return frozenset(ids)


def spec_runtime_component_rule_ids() -> frozenset[str]:
    """HEURISTIC (see module docstring): every rule id whose own bolded
    defining line, anywhere under spec/*.md, contains one of NFR-07's
    runtime-component keywords. Best-effort cross-check, not a formal proof."""
    ids: set[str] = set()
    if not SPEC_DIR.exists():
        return frozenset(ids)
    for path in sorted(SPEC_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            m = _RULE_ID_RE.search(line)
            if not m:
                continue
            lowered = line.lower()
            if any(kw in lowered for kw in _RUNTIME_KEYWORDS):
                ids.add(m.group(1))
    return frozenset(ids)


def check_all(state) -> list[tuple[WiringEntry, bool, bool]]:
    """(entry, constructed_ok, ticked_ok) for every registry entry, against
    the given `app.state`. The thin CLI (ops/check_wiring.py) and the pytest
    registry test both call this so the two never drift."""
    return [(entry, entry.constructed(state), entry.ticked(state)) for entry in REGISTRY]


# --- NFR-07 constant-signal species (v1.68) ---------------------------------
#
# NFR-07's first pass proved every REGISTRY component above is constructed and
# TICKED. It said nothing about whether a component's own INPUTS are alive:
# `LiveMarketGates.for_live()` was constructed (pass) and ticked (pass) every
# single boot, while its `flatten_in_progress` INPUT was the dead default
# `lambda: False` -- present, called, green forever, RSK-01a's "no Flatten All
# executing" gate silently unable to ever say no. v1.68 names this the
# constant-signal species and requires the audit to also walk every INPUT to
# the ENT-03 safety-gate chain (`application.entry_gates.evaluate_gates`) that
# `LiveMarketGates` sources, and prove each is bound to a signal that actually
# VARIES with real state in the live composition -- never a constant.
#
# Two proof strategies, used honestly (never dressed up as more than they are):
#   * BEHAVIORAL (sound, not a heuristic): flip the REAL underlying object the
#     provider is supposed to read, off the SAME `app.state` a real live_app()
#     exposes, and assert the provider's OUTPUT actually follows. A constant
#     cannot ever pass this -- it is exactly what would have caught the pinned
#     regression, and exactly what test_tc_nfr_07_constant_signal.py's
#     fail-first proof exercises against a reverted `lambda: False`.
#   * STRUCTURAL/HEURISTIC (labelled `heuristic=True`, honestly weaker): for an
#     input this repo cannot flip without a real broker session offline
#     (`session_valid`, `buying_power_ok` -- both make a real authenticated
#     network call), the strongest sound check available is that the bound
#     callable closes over real outer state (a nonempty `__closure__`) or is a
#     bound method on a real object (`__self__ is not None`) -- a bare
#     `lambda: False`/`lambda: True` has NEITHER, so this can never be fooled
#     by the pinned regression's actual shape, but a contrived constant
#     wrapped in a needless closure could in principle still slip past. Read
#     honestly, like the registry's own keyword heuristic above.


@dataclass(frozen=True)
class LiveCheckResult:
    live: bool
    detail: str


def _closes_over_real_state(fn) -> bool:
    """HEURISTIC (see module docstring): true iff `fn` cannot be a bare
    constant lambda/def, because it either captures a free variable
    (`__closure__` non-empty) or is a bound method on a real instance
    (`__self__ is not None`). `lambda: False` has neither."""
    inner = getattr(fn, "__func__", fn)   # unwrap a bound method to inspect its code
    if getattr(inner, "__closure__", None):
        return True
    return getattr(fn, "__self__", None) is not None


def _flatten_in_progress_live_check(state) -> LiveCheckResult:
    """BEHAVIORAL, sound: flips the REAL `PanelCommands._flatten_in_progress`
    flag RSK-01a's flatten() owns and asserts the bound gate input's output
    tracks it both ways. `lambda: False` (the pinned regression) always
    returns off=False, on=False -- this fails it unconditionally, which is
    exactly the point."""
    runtime = getattr(state, "runtime", None)
    gates = getattr(runtime, "market_gates", None)
    commands = getattr(state, "commands", None)
    provider = getattr(gates, "flatten_in_progress", None)
    if provider is None or commands is None:
        return LiveCheckResult(False, "runtime.market_gates.flatten_in_progress or "
                                       "state.commands not reachable off app.state")
    prior = getattr(commands, "_flatten_in_progress", False)
    try:
        commands._flatten_in_progress = False
        off = bool(provider())
        commands._flatten_in_progress = True
        on = bool(provider())
    finally:
        commands._flatten_in_progress = prior
    live = (off is False) and (on is True)
    return LiveCheckResult(live, f"flipping PanelCommands._flatten_in_progress: off->{off}, on->{on}")


def _data_fresh_live_check(state) -> LiveCheckResult:
    """BEHAVIORAL, sound: flips the REAL `_Snapshots.stale` flag (exposed as
    `app.state.chain_snapshots`, DAT-02) and asserts the bound gate input
    tracks it. A constant `lambda: True`/`lambda: False` fails this either
    way -- it cannot track both flips."""
    runtime = getattr(state, "runtime", None)
    gates = getattr(runtime, "market_gates", None)
    snaps = getattr(state, "chain_snapshots", None)
    provider = getattr(gates, "data_fresh", None)
    if provider is None or snaps is None:
        return LiveCheckResult(False, "runtime.market_gates.data_fresh or "
                                       "state.chain_snapshots not reachable off app.state")
    prior = snaps.stale
    try:
        snaps.stale = True
        stale_reads_fresh = asyncio.run(provider())    # data_fresh() should be False when stale
        snaps.stale = False
        fresh_reads_fresh = asyncio.run(provider())    # and True when not stale
    finally:
        snaps.stale = prior
    live = (stale_reads_fresh is False) and (fresh_reads_fresh is True)
    return LiveCheckResult(live, f"flipping chain_snapshots.stale: stale->{stale_reads_fresh}, "
                                  f"fresh->{fresh_reads_fresh}")


def _session_valid_live_check(state) -> LiveCheckResult:
    """STRUCTURAL/HEURISTIC (see module docstring): `session_valid` makes a
    real authenticated broker call (`comp.broker.working_orders()`), which
    this offline audit must never place -- so the strongest sound check
    available is that the bound provider is not a bare constant callable."""
    runtime = getattr(state, "runtime", None)
    gates = getattr(runtime, "market_gates", None)
    provider = getattr(gates, "session_valid", None)
    if provider is None:
        return LiveCheckResult(False, "runtime.market_gates.session_valid not reachable")
    live = _closes_over_real_state(provider)
    return LiveCheckResult(live, f"session_valid provider closes over real state: {live}")


def _buying_power_ok_live_check(state) -> LiveCheckResult:
    """STRUCTURAL/HEURISTIC -- same reasoning as `_session_valid_live_check`:
    a real `buying_power_ok` provider calls the broker for the actual BP,
    which this offline audit must never place."""
    runtime = getattr(state, "runtime", None)
    gates = getattr(runtime, "market_gates", None)
    provider = getattr(gates, "buying_power_ok", None)
    if provider is None:
        return LiveCheckResult(False, "runtime.market_gates.buying_power_ok not reachable")
    live = _closes_over_real_state(provider)
    return LiveCheckResult(live, f"buying_power_ok provider closes over real state: {live}")


def _halted_known_gap_check(state) -> LiveCheckResult:
    """HONEST, DELIBERATELY NOT a pass -- the NINTH finding (2026-07-14
    review, found auditing the OTHER gate inputs alongside the authorized
    RSK-01a fix). DAT-04 ("market open and not halted") has NO live signal
    source anywhere in this codebase: `_wire_live_day` (server.py) never
    passes a `halted=` provider to `LiveMarketGates.for_live()`, so
    `self.halted` stays the dataclass default `None` -- and
    `LiveMarketGates.__call__` reads `market_halted=await
    self._safe(self.halted, default=False) if open_now else True`, which
    makes `market_halted` UNCONDITIONALLY False whenever the market is open
    per the calendar. Worse: `default=False` is itself the PERMISSIVE answer
    for this particular field (`evaluate_gates` blocks when `market_halted`
    is TRUE, so the safe default on an unreadable signal would be `True`,
    not `False`) -- every OTHER `_safe(...)` call in this file correctly
    defaults to the blocking answer; this one is inverted. Not fixed here:
    building a real halt-detection feed (and correcting the default's
    polarity) is outside this change's authorized scope (the RSK-01a flatten
    gate only) and needs an operator decision. Pinned so this module can
    never silently claim DAT-04 is live."""
    runtime = getattr(state, "runtime", None)
    gates = getattr(runtime, "market_gates", None)
    provider = getattr(gates, "halted", "<unreachable>")
    return LiveCheckResult(
        False,
        f"halted provider is {provider!r} -- no DAT-04 halt-detection signal is wired "
        "anywhere in this codebase (NINTH finding, 2026-07-14 review; not fixed, "
        "flagged for the operator; also note _safe(..., default=False) is the "
        "permissive, not the safe, polarity for this field)")


@dataclass(frozen=True)
class SafetyGateInput:
    """One INPUT to `application.entry_gates.evaluate_gates` that
    `LiveMarketGates` sources from outside itself. `live_check(app.state)`
    is the proof this input is bound to a real, varying signal in the ACTUAL
    live composition -- not a unit test of the provider in isolation.

    `known_gap`: non-None means this input is a DOCUMENTED, NOT-YET-FIXED gap
    (reported, never silently fixed or silently omitted) -- its `live_check`
    is expected to report `live=False`, and the self-policing test below
    pins that exact set so a new, unreported gap can never slip in quietly,
    and a fixed gap left listed here would be caught just as loudly.
    """
    rule_ids: tuple[str, ...]
    gate_input: str
    proof: str
    live_check: Callable[[object], LiveCheckResult]
    known_gap: str | None = None


SAFETY_GATE_REGISTRY: tuple[SafetyGateInput, ...] = (
    SafetyGateInput(
        rule_ids=("RSK-01a", "ENT-03"),
        gate_input="flatten_in_progress",
        proof="app.state.runtime.market_gates.flatten_in_progress, flipped against the "
              "real app.state.commands._flatten_in_progress -- the v1.68 pinned regression.",
        live_check=_flatten_in_progress_live_check,
    ),
    SafetyGateInput(
        rule_ids=("DAT-02", "ENT-03"),
        gate_input="data_fresh",
        proof="app.state.runtime.market_gates.data_fresh, flipped against the real "
              "app.state.chain_snapshots.stale.",
        live_check=_data_fresh_live_check,
    ),
    SafetyGateInput(
        rule_ids=("REC-06", "ENT-03"),
        gate_input="session_valid",
        proof="app.state.runtime.market_gates.session_valid closes over real broker "
              "state (heuristic -- a real authenticated call cannot be placed offline).",
        live_check=_session_valid_live_check,
    ),
    SafetyGateInput(
        rule_ids=("ENT-03", "RSK-04"),
        gate_input="buying_power_ok",
        proof="app.state.runtime.market_gates.buying_power_ok closes over real broker "
              "state (heuristic -- a real authenticated call cannot be placed offline).",
        live_check=_buying_power_ok_live_check,
    ),
    SafetyGateInput(
        rule_ids=("DAT-04", "ENT-03"),
        gate_input="halted",
        proof="app.state.runtime.market_gates.halted -- NEVER wired by _wire_live_day; "
              "see _halted_known_gap_check's docstring (NINTH finding).",
        live_check=_halted_known_gap_check,
        known_gap="DAT-04 halt-detection has no live signal source anywhere in this "
                  "codebase (2026-07-14 review); the _safe(..., default=False) polarity "
                  "for this field is also inverted (permissive, not safe). Not fixed -- "
                  "flagged for the operator.",
    ),
)


def check_all_safety_gate_inputs(state) -> list[tuple[SafetyGateInput, LiveCheckResult]]:
    """(entry, result) for every SAFETY_GATE_REGISTRY entry, against the given
    `app.state`. Mirrors `check_all` above for the component registry."""
    return [(entry, entry.live_check(state)) for entry in SAFETY_GATE_REGISTRY]


def unexpectedly_not_live(state) -> list[str]:
    """The gate_input names that FAILED their live_check WITHOUT being a
    documented `known_gap` -- the audit's hard-fail set. Non-empty means a
    genuinely new constant-signal regression (the RSK-01a case this all
    exists for)."""
    return [entry.gate_input for entry, result in check_all_safety_gate_inputs(state)
            if entry.known_gap is None and not result.live]


# --- self-policing: HONEST LIMITATION ---------------------------------------
#
# Run raw, `spec_runtime_component_rule_ids()` returns ~40 ids against this
# repo's actual spec text -- almost all of them false positives: the keyword
# set (deliberately taken verbatim from NFR-07's own wording) also matches
# ordinary English ("retry loop", "watch out", "the decay watcher" inside a
# DIFFERENT rule's prose, "poll the broker") and rules that describe a
# PROCEDURE or a DATA/CONFIG INVARIANT rather than a standalone runtime
# component. A hard "found ⊆ registered" assertion against the raw set would
# fail today, for reasons that have nothing to do with a genuinely missing
# component -- exactly the "flaky gate people learn to ignore" failure mode.
#
# Every one of the ~40 was individually read (spec/01-strategy-rules.md,
# spec/05-architecture-ddd.md, spec/10-results-dashboard.md, spec/README.md)
# before being placed here. None of them mandates a runtime component beyond
# what REGISTRY already covers. This set is therefore a CURATED, JUSTIFIED
# exclusion list, not a silent rubber stamp: the accompanying test
# (`test_nfr07_wiring_registry.py::test_spec_runtime_component_rule_ids_are_all_accounted_for`)
# fails the day the heuristic flags a NEW id that is in neither REGISTRY nor
# here -- which is exactly the "adding such a rule without registering its
# component fails CI" self-policing NFR-07 itself demands. Extending this
# set for a genuinely-false-positive new rule is a deliberate, reviewable
# one-line change; silently growing without a reason recorded below is the
# thing to avoid.
KNOWN_FALSE_POSITIVE_RULE_IDS: frozenset[str] = frozenset({
    # Procedures/config/data invariants (not a standalone runtime component;
    # the keyword appears in the rule's PROSE, describing something else):
    "CLS-01",       # close PROCEDURE, driven synchronously per CloseEntry.close() call
    "ORD-01", "ORD-08", "ORD-09", "ORD-11",   # order-shape/journaling rules, not loops
    "OWN-02", "OWN-09", "OWN-12",              # ownership classification/journaling rules
    "PNL-01", "PNL-04",                        # fee/reconciliation CALCULATIONS, not loops
    "RSK-01", "RSK-03", "RSK-06",               # toggles/gates/alert-routing, not loops themselves
    "STP-01", "STP-02d",                       # entry-time stop PLACEMENT procedure, not a loop
    "NFR-01",                                  # threading constraint, not itself a component
    "NLE-07", "RPT-08", "SIM-05", "UI-24", "UI-25",  # reporting/UI views, computed on read
    "TPF-08", "TPF-09", "TPT-07",               # persistence/interaction RULES for the exit
                                                 # monitor already registered under TPF-03/TPT-04
    "RPT-15a", "RPT-15b", "RPT-15d",            # scoping/semantics rules for the RPT-15
                                                 # reconcile already registered under RPT-15
    "ENT-01a", "ENT-09",                        # durable-state/manual-fire RULES, not loops
    # One-shot boot actions, not a recurring ticked loop (REC-02's own text:
    # "on startup or reconnect" -- already invoked unconditionally inside the
    # `_connect()` startup hook, not a supervised background task):
    "REC-02", "LEX-09",
    # Subsumed by an already-registered component (the SAME underlying
    # stream/loop the keyword scan is (correctly) noticing, just from a
    # different rule's prose):
    "DAT-01",   # DXLink streaming == NFR-04's QuoteHub stream
    "LEX-06",   # the LEX ladder's own retry-until-timeout runs inside the
                # SAME reactive call chain STP-08a's push/poll trigger
    "DAY-01a",  # the COMPUTED calendar the day supervisor (ENT-10/DAY-01)
                # consults -- a pure calculation, not its own loop
    "STP-03",   # deliberately an ABSENCE rule (stop_limit tombstone) --
                # TC-NFR-07 scenario 2 already covers it; the OPPOSITE of
                # "prove constructed"
    "STP-05a",  # the Phase-0 sandbox VERIFICATION GATE (a one-time,
                # build-blocking manual check per doc 05 section 10), not a
                # runtime component `live_app()` constructs and ticks
    # NFR-07 matches its OWN defining line (it literally quotes "monitors,
    # watchers, sweeps, loops, samplers, reconcilers") -- the meta-rule about
    # the registry is not itself a registry entry.
    "NFR-07",
})


def unaccounted_rule_ids() -> frozenset[str]:
    """Heuristic-flagged rule ids that are in NEITHER the registry NOR the
    curated false-positive list above -- the self-policing test's hard-fail
    set. Non-empty means either a genuinely unwired spec-mandated component
    (register it) or a new false positive (read it, then add it above with a
    one-line reason -- never silently)."""
    return spec_runtime_component_rule_ids() - all_rule_ids() - KNOWN_FALSE_POSITIVE_RULE_IDS
