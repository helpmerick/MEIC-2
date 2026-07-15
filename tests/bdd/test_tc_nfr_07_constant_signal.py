"""Step definitions for TC-NFR-07's SECOND scenario -- the v1.68
constant-signal species (2026-07-14).

`tests/bdd/test_tc_nfr_07_wiring_registry.py` binds the FIRST scenario
(component construction/ticking) and `tests/bdd/test_tc_nfr_07_stp03_tombstone.py`
binds the THIRD (stop_limit tombstone). Only THIS scenario is bound here.

NFR-07's first pass proved every registered component is constructed and
ticked. It said nothing about whether a component's INPUTS are alive: RSK-01a's
flatten gate was constructed (pass) and ticked (pass) every boot, while its
`flatten_in_progress` INPUT was the dead default `lambda: False` -- present,
called, green forever, never once able to say "yes, a flatten is executing".
v1.68 names this the constant-signal species and requires the audit to prove
every safety-gate INPUT is bound to a live signal.

This file proves BOTH halves the ratified fix demands:
  1. FAIL-FIRST (condition 1): the flatten gate's live_check is RED when the
     provider is (re)bound to `lambda: False`, and GREEN against the real,
     now-wired signal -- the exact regression this whole gate exists to catch.
  2. The registry-level audit (`wiring_registry.unexpectedly_not_live`) is a
     genuine structural check across every safety-gate input, not a
     hardcoded single case, and correctly distinguishes the ONE documented,
     honest known-gap (DAT-04 `halted`, the NINTH finding) from a real
     regression.
"""
from __future__ import annotations

import os as _os

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, scenario, then

from meic.composition.wiring_registry import (
    SAFETY_GATE_REGISTRY,
    check_all_safety_gate_inputs,
    unexpectedly_not_live,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Same isolation as the sibling TC-NFR-07 step files: never read the
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
          "Safety-gate inputs are live signals, never constants (v1.68 — the eighth)")
def test_nfr07_constant_signal_scenario():
    pass


@pytest.fixture
def booted_app(monkeypatch, tmp_path):
    """A real `live_app()`, network-free (comp.connect stubbed), with startup
    events run -- identical fixture shape to the sibling wiring-registry test."""
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    app = live_app()
    comp = app.state.composition

    async def _noop_connect(account=None):
        return None
    comp.connect = _noop_connect

    with TestClient(app) as client:
        yield app, client


@given("the registry of safety-gate inputs")
def _registry_given():
    assert len(SAFETY_GATE_REGISTRY) > 0
    inputs = {entry.gate_input for entry in SAFETY_GATE_REGISTRY}
    assert "flatten_in_progress" in inputs, (
        "RSK-01a's flatten_in_progress must be a registered safety-gate input "
        "-- it is the v1.68 pinned regression")
    return SAFETY_GATE_REGISTRY


@then("each is bound to a real signal source")
def test_every_non_gap_safety_gate_input_is_live(booted_app):
    """Every entry WITHOUT a documented `known_gap` must resolve to a real,
    varying signal in the ACTUAL live composition -- not merely "some
    callable exists". DAT-04a (v1.69) closed the ninth finding (`halted` used
    to be the ONE documented gap): the known_gap set must now be EMPTY, so a
    new, silent gap can never slip in unreported."""
    app, _client = booted_app
    results = check_all_safety_gate_inputs(app.state)

    live_failures = [(entry.gate_input, result.detail)
                      for entry, result in results
                      if entry.known_gap is None and not result.live]
    assert live_failures == [], f"NFR-07 constant-signal audit: {live_failures}"

    gap_inputs = {entry.gate_input for entry in SAFETY_GATE_REGISTRY if entry.known_gap}
    assert gap_inputs == set(), (
        "DAT-04a (v1.69): the ninth finding (halted) is fixed -- no known_gap "
        "should remain documented; a new gap here needs the same loud "
        "documentation, not silent inclusion")
    # halted itself (formerly the gap) must now positively resolve live too.
    halted_result = next(result for entry, result in results if entry.gate_input == "halted")
    assert halted_result.live is True, (
        f"DAT-04a's halted provider must be live against the real wiring: {halted_result.detail}")


@then("a gate input bound to a constant or dead default fails the audit")
def test_rebinding_flatten_to_a_constant_fails_the_audit(booted_app):
    """The audit's OWN teeth: simulate the exact historical bug (the real
    live wiring, then someone rebinds the provider back to `lambda: False`)
    and prove `unexpectedly_not_live` flags exactly that input -- not merely
    a one-time snapshot that happened to look fine today.

    Restores the real provider afterward: this scenario's LAST step
    re-asserts the flatten gate is live, off the SAME `booted_app` fixture
    instance (pytest-bdd's sequential-state contract, matching the sibling
    wiring-registry scenario's own teardown discipline)."""
    app, _client = booted_app
    gates = app.state.runtime.market_gates
    real_provider = gates.flatten_in_progress
    assert app.state.commands is not None

    gates.flatten_in_progress = lambda: False   # THE pinned regression, reintroduced
    try:
        failing = unexpectedly_not_live(app.state)
        assert "flatten_in_progress" in failing, (
            "rebinding RSK-01a's flatten gate to a constant must fail the audit")
    finally:
        gates.flatten_in_progress = real_provider


@then("RSK-01a's flatten_in_progress wired to lambda False is the pinned regression")
def test_flatten_in_progress_is_provably_live_against_the_real_wiring(booted_app):
    """The GREEN side of the fail-first proof (condition 1): against the
    REAL, current (fixed) wiring -- no monkeypatching -- the flatten gate's
    live_check passes, and the audit's hard-fail set is empty for it."""
    app, _client = booted_app
    entry = next(e for e in SAFETY_GATE_REGISTRY if e.gate_input == "flatten_in_progress")
    assert "RSK-01a" in entry.rule_ids

    result = entry.live_check(app.state)
    assert result.live is True, (
        f"RSK-01a's flatten_in_progress must resolve to a live signal against the "
        f"real live_app() wiring: {result.detail}")
    assert "flatten_in_progress" not in unexpectedly_not_live(app.state)


@then("DAT-04's halt input (no provider, inverted polarity) is the ninth-finding pinned regression")
def test_halted_ninth_finding_closed_and_still_pinned(booted_app):
    """DAT-04a (v1.69) CLOSES the ninth finding: `halted` is now a bound,
    live SAFETY_GATE_REGISTRY entry (`known_gap is None`), and its live_check
    passes against the REAL wiring -- no monkeypatching. The regression stays
    pinned exactly like `flatten_in_progress`'s own step above: reverting to
    the historically broken shape (no provider bound at all) must fail the
    audit."""
    app, _client = booted_app
    entry = next(e for e in SAFETY_GATE_REGISTRY if e.gate_input == "halted")
    assert {"DAT-04", "DAT-04a"} <= set(entry.rule_ids)
    assert entry.known_gap is None, "DAT-04a: the ninth finding is fixed -- no longer an excused gap"

    result = entry.live_check(app.state)
    assert result.live is True, (
        f"DAT-04a's halted must resolve to a live signal against the real "
        f"live_app() wiring: {result.detail}")
    assert "halted" not in unexpectedly_not_live(app.state)

    # THE historically broken shape, reintroduced: no provider bound at all
    # (the dataclass default `None`, exactly as `_wire_live_day` left it
    # before DAT-04a) must fail the audit, never silently pass.
    gates = app.state.runtime.market_gates
    real_provider = gates.halted
    try:
        gates.halted = None
        failing = unexpectedly_not_live(app.state)
        assert "halted" in failing, (
            "no halted provider bound must fail the audit -- the ninth finding, reintroduced")
    finally:
        gates.halted = real_provider
