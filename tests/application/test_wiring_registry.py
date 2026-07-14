"""meic.composition.wiring_registry -- unit tests for the NFR-07 registry's
own machinery (the callables' logic, not the real live_app() composition,
which tests/bdd/test_tc_nfr_07_wiring_registry.py covers)."""
from types import SimpleNamespace

from meic.composition.wiring_registry import (
    REGISTRY,
    SPEC_DIR,
    all_rule_ids,
    check_all,
    spec_runtime_component_rule_ids,
    unaccounted_rule_ids,
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
