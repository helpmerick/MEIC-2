"""meic.composition.wiring_registry -- unit tests for the NFR-07 registry's
own machinery (the callables' logic, not the real live_app() composition,
which tests/bdd/test_tc_nfr_07_wiring_registry.py covers)."""
from types import SimpleNamespace

from meic.composition.wiring_registry import (
    REGISTRY,
    SAFETY_GATE_REGISTRY,
    SPEC_DIR,
    _closes_over_real_state,
    all_rule_ids,
    check_all,
    check_all_safety_gate_inputs,
    spec_runtime_component_rule_ids,
    unaccounted_rule_ids,
    unexpectedly_not_live,
)


def test_spec_dir_actually_exists_so_the_heuristic_is_not_silently_scanning_nothing():
    """Review finding (2026-07-14, non-blocking): `spec_runtime_component_rule_ids`
    returns an empty set when `SPEC_DIR` is missing -- which would make
    `unaccounted_rule_ids()` vacuously pass in any layout where spec/ isn't on
    disk. Pin that the directory this repo actually relies on is present."""
    assert SPEC_DIR.exists() and SPEC_DIR.is_dir()
    assert any(SPEC_DIR.glob("*.md")), "no spec markdown files found -- the heuristic would scan nothing"


def test_registry_entries_have_non_empty_rule_ids_and_components():
    assert len(REGISTRY) > 0
    for entry in REGISTRY:
        assert entry.rule_ids, f"{entry.component} declares no rule ids"
        assert entry.component
        assert entry.proof
        assert callable(entry.constructed)
        assert callable(entry.ticked)


def test_decay_watcher_is_registered():
    components = {entry.component for entry in REGISTRY}
    assert "DecayWatcher" in components


def test_isinstance_check_is_false_when_absent_and_true_for_the_real_class():
    entry = next(e for e in REGISTRY if e.component == "DecayWatcher")
    from meic.application.decay_watcher import DecayWatcher

    assert entry.constructed(SimpleNamespace()) is False
    assert entry.constructed(SimpleNamespace(decay_watcher=None)) is False
    assert entry.constructed(SimpleNamespace(decay_watcher=object())) is False
    assert entry.constructed(SimpleNamespace(decay_watcher=DecayWatcher(broker=None, events=[]))) is True


def test_task_check_reads_done_correctly():
    entry = next(e for e in REGISTRY if e.component == "StopWatchdog")

    class _Task:
        def __init__(self, done):
            self._done = done

        def done(self):
            return self._done

    assert entry.ticked(SimpleNamespace()) is False
    assert entry.ticked(SimpleNamespace(stop_watchdog_task=None)) is False
    assert entry.ticked(SimpleNamespace(stop_watchdog_task=_Task(done=True))) is False
    assert entry.ticked(SimpleNamespace(stop_watchdog_task=_Task(done=False))) is True


def test_check_all_returns_one_tuple_per_registry_entry():
    state = SimpleNamespace()
    results = check_all(state)
    assert len(results) == len(REGISTRY)
    # a totally empty state must fail EVERY entry -- never a false pass
    assert all(not (constructed and ticked) for _entry, constructed, ticked in results)


def test_all_rule_ids_flattens_every_entry():
    ids = all_rule_ids()
    assert "DCY-01" in ids and "STP-03b" in ids and "NFR-04" in ids


def test_spec_runtime_component_rule_ids_finds_the_known_ones():
    """Sanity on the heuristic itself (not the curated exclusion list, covered
    by test_nfr07_wiring_registry_is_self_policing below): rules known to
    genuinely mandate a runtime component and use one of the keywords must be
    found by the scan."""
    found = spec_runtime_component_rule_ids()
    assert "DCY-01" in found       # "the watcher is event-driven..."
    assert "STP-03b" in found or "STP-08a" in found


# --- NFR-07 v1.68 constant-signal species: SAFETY_GATE_REGISTRY machinery --
# (the real live_app() proof lives in tests/bdd/test_tc_nfr_07_constant_signal.py;
# this is unit coverage of the standalone helper/registry logic in isolation.)

def test_closes_over_real_state_rejects_a_bare_constant_lambda():
    """The exact shape of the pinned regression: `lambda: False` has no free
    variables and is not a bound method -- the heuristic must reject it."""
    assert _closes_over_real_state(lambda: False) is False
    assert _closes_over_real_state(lambda: True) is False


def test_closes_over_real_state_accepts_a_real_closure():
    captured = {"flag": True}

    def reads_captured():
        return captured["flag"]

    assert _closes_over_real_state(reads_captured) is True


def test_closes_over_real_state_accepts_a_bound_method_on_a_real_instance():
    class _Real:
        def check(self):
            return True

    assert _closes_over_real_state(_Real().check) is True


def test_safety_gate_registry_entries_are_well_formed():
    assert len(SAFETY_GATE_REGISTRY) > 0
    for entry in SAFETY_GATE_REGISTRY:
        assert entry.rule_ids, f"{entry.gate_input} declares no rule ids"
        assert entry.gate_input
        assert entry.proof
        assert callable(entry.live_check)


def test_flatten_in_progress_is_the_named_pinned_regression_in_the_registry():
    entry = next(e for e in SAFETY_GATE_REGISTRY if e.gate_input == "flatten_in_progress")
    assert "RSK-01a" in entry.rule_ids
    assert entry.known_gap is None   # this one must actually be proven live, never excused


def test_no_known_gaps_remain_dat_04a_closes_the_ninth_finding():
    """DAT-04a (v1.69) closed the ninth finding (`halted` was the ONE
    documented known_gap) with a real TradingStatusStore provider. DAT-04a
    v1.80 (market-taught) went further and RETIRED the input outright, so
    there is no `halted` entry left to carry a known_gap at all. The known_gap
    list must still be EMPTY for every remaining entry -- this test's name
    predates the v1.80 retirement but the assertion (no known gaps) stays
    exactly as strict."""
    gaps = {e.gate_input for e in SAFETY_GATE_REGISTRY if e.known_gap}
    assert gaps == set()


def test_halted_is_retired_no_registry_entry_at_all():
    """DAT-04a v1.80 (operator-ruled, market-taught, contingency executed):
    `halted` is not merely a fixed known_gap anymore -- it has NO
    SAFETY_GATE_REGISTRY entry at all. The module that fed it
    (`meic.adapters.dxlink.trading_status`) is deleted."""
    assert not any(e.gate_input == "halted" for e in SAFETY_GATE_REGISTRY)


def test_check_all_safety_gate_inputs_returns_one_result_per_entry_and_fails_closed():
    """A totally empty state (no runtime, no commands, no chain_snapshots
    reachable) must report every entry as NOT live -- never a false pass."""
    state = SimpleNamespace()
    results = check_all_safety_gate_inputs(state)
    assert len(results) == len(SAFETY_GATE_REGISTRY)
    assert all(not result.live for _entry, result in results)


def test_unexpectedly_not_live_flags_every_unproven_input():
    """Against an empty state every entry fails its live_check -- including
    `flatten_in_progress` (unreachable off an empty state, correctly
    flagged). `halted` is no longer a registry entry at all (v1.80), so it
    cannot appear here in either direction."""
    state = SimpleNamespace()
    failing = unexpectedly_not_live(state)
    assert "flatten_in_progress" in failing
    assert "halted" not in failing   # not a registry entry -- retired, not merely unproven


def test_nfr07_wiring_registry_is_self_policing():
    """The hard gate: every spec rule id the keyword heuristic flags must be
    accounted for by EITHER the registry or the curated, justified false-
    positive list -- non-empty here means a genuinely missing component (go
    register it) or an unreviewed new false positive (go read it, then add a
    one-line reason to KNOWN_FALSE_POSITIVE_RULE_IDS). See wiring_registry.py's
    own module docstring for why this can't be a bare, un-curated subset
    check against this repo's real spec text."""
    assert unaccounted_rule_ids() == frozenset(), (
        "a spec rule id mandating a runtime component may be unregistered -- "
        "or a new heuristic false positive needs curating; see "
        "meic.composition.wiring_registry.KNOWN_FALSE_POSITIVE_RULE_IDS")
