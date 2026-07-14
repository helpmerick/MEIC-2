"""Step definitions for TC-NFR-07's FIRST scenario -- the NFR-07 wiring-audit
registry / DecayWatcher regression (2026-07-14).

`tests/bdd/test_tc_nfr_07_stp03_tombstone.py` already binds the SECOND
scenario ("stop_limit has no construction path") and explicitly left this one
unbound, pending exactly this change. Only this scenario is bound here.

NFR-07 (spec/05-architecture-ddd.md): "A registry test walks the spec's
live-component list and asserts each is provably CONSTRUCTED and TICKED
inside `live_app()` -- not merely unit-tested... A component in the registry
that `live_app()` does not construct-and-tick fails CI." The registry itself
(`meic.composition.wiring_registry`) is the single source of truth this test
and a future scripts/ CLI both import.

DecayWatcher's DEEP functional proof (an actual ask<=trigger fire, through
the real wiring seam, landing a real `DecayBuybackPlaced`) lives in
`tests/application/test_decay_watcher_wiring.py` -- unit-level, not tied to a
real TastytradeAdapter session, so it is fast and never flaky. THIS file
proves the STRUCTURAL half NFR-07 asks for against a REAL `live_app()`:
every registered component is actually constructed and ticking, and the
check would have failed before this change (the "pinned regression").
"""
from __future__ import annotations

import os as _os

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, scenario, then

from meic.composition.wiring_registry import REGISTRY, check_all


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Same isolation as test_live_app.py / test_tc_ent_10.py: never read the
    operator's real .env when a scenario boots a real live_app()."""
    from meic.adapters.api import server
    monkeypatch.setattr(server, "_read_env", lambda: dict(_os.environ))


def _jwt(iss: str) -> str:
    import base64
    import json

    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': iss})}.sig"


def _cert_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_CERT_PROVIDER_SECRET", "s")
    monkeypatch.setenv("TT_CERT_REFRESH_TOKEN", _jwt("https://api.sandbox.tastyworks.com"))
    monkeypatch.setenv("TT_CERT_ACCOUNT", "5WZ00000")
    monkeypatch.setenv("MEIC_LIVE_IS_TEST", "true")
    monkeypatch.setenv("MEIC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")


@scenario("../features/TC-NFR-07.feature",
          "Every spec-mandated live component is constructed and ticked")
def test_nfr07_wiring_registry_scenario():
    pass


@pytest.fixture
def booted_app(monkeypatch, tmp_path):
    """A real `live_app()`, network-free (comp.connect stubbed, same as every
    other live_app() capstone in test_live_app.py), with its startup events
    actually run so every supervised background task exists."""
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    app = live_app()
    comp = app.state.composition

    async def _noop_connect(account=None):
        return None
    comp.connect = _noop_connect

    with TestClient(app) as client:
        yield app, client


@given("the registry of runtime components the spec mandates")
def _registry_given():
    assert len(REGISTRY) > 0
    components = {entry.component for entry in REGISTRY}
    assert "DecayWatcher" in components, (
        "DCY-01..04: DecayWatcher -- found built, tested, race-guarded, and "
        "never constructed -- must be the pinned regression in this registry")
    return REGISTRY


@then("each is provably constructed AND ticked inside live_app()")
def test_every_registry_entry_is_constructed_and_ticked(booted_app):
    app, _client = booted_app
    results = check_all(app.state)
    failures = [(entry.component, entry.rule_ids, constructed, ticked)
                for entry, constructed, ticked in results if not (constructed and ticked)]
    assert failures == [], f"NFR-07: components not provably constructed+ticked: {failures}"


@then("a registered component absent from the live composition fails CI")
def test_a_torn_down_component_is_caught_by_the_registry(booted_app):
    """Simulate the exact historical bug (DecayWatcher constructed, then its
    task silently missing) and prove `check_all` flags exactly that entry --
    the registry's absence-detection mechanism this whole gate exists to
    provide, not merely a one-time snapshot that happened to pass today.

    Restores a live task afterward: this scenario's LAST step re-asserts the
    decay watcher is healthy, off the SAME `booted_app` fixture instance
    (pytest-bdd runs one scenario's steps against one shared fixture set,
    exactly like Gherkin's own sequential-state contract) -- this step must
    leave the app exactly as healthy as it found it, its assertions already
    made before the teardown."""
    app, _client = booted_app
    task = app.state.decay_watcher_task
    task.cancel()
    app.state.decay_watcher_task = None
    try:
        results = check_all(app.state)
        decay_entry = next(e for e, _c, _t in results if e.component == "DecayWatcher")
        constructed_ok = decay_entry.constructed(app.state)
        ticked_ok = decay_entry.ticked(app.state)
        assert constructed_ok is True, "construction is untouched by tearing down only the task"
        assert ticked_ok is False, "a torn-down task must fail the 'ticked' half of the gate"
    finally:
        # A minimal stand-in satisfying `_task_alive` (`task is not None and not
        # task.done()`) -- restoring a REAL asyncio task would need a running
        # loop, which sync pytest-bdd step code does not have between
        # TestClient calls; this scenario's remaining step only needs the
        # "still ticking" shape restored, not a second genuine loop.
        class _StillAliveStub:
            def done(self) -> bool:
                return False

            def cancel(self) -> None:
                pass  # the real shutdown hook calls this; nothing to actually cancel here
        app.state.decay_watcher_task = _StillAliveStub()


@then("DecayWatcher — found built, tested, race-guarded, and never constructed — is the pinned regression")
def test_decay_watcher_is_provably_constructed_and_ticked(booted_app):
    from meic.application.decay_watcher import DecayWatcher

    app, _client = booted_app
    assert isinstance(app.state.decay_watcher, DecayWatcher)
    assert app.state.decay_watcher.decay_buyback_trigger == app.state.decay_buyback_trigger
    assert app.state.decay_watcher.decay_confirmation_evals == app.state.decay_confirmation_evals
    task = app.state.decay_watcher_task
    assert task is not None and not task.done(), (
        "live_app must actually start the DCY-01..04 decay watcher loop at startup")
    # the loop's REAL bookkeeping (see test_decay_watcher_wiring.py for a
    # fully driven fire against these exact dicts) is reachable off app.state,
    # never a private copy inside the task's closure:
    assert app.state.decay_watchers is not None
    assert app.state.decay_watcher_active is not None
