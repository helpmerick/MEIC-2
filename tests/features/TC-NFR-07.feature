Feature: TC-NFR-07
  Scenario: Every spec-mandated live component is constructed and ticked
    Given the registry of runtime components the spec mandates
    Then each is provably constructed AND ticked inside live_app()
    And a registered component absent from the live composition fails CI
    And DecayWatcher — found built, tested, race-guarded, and never constructed — is the pinned regression

  Scenario: Safety-gate inputs are live signals, never constants (v1.68 — the eighth)
    Given the registry of safety-gate inputs
    Then each is bound to a real signal source
    And a gate input bound to a constant or dead default fails the audit
    And RSK-01a's flatten_in_progress wired to lambda False is the pinned regression

  Scenario: stop_limit has no construction path (STP-03 tombstone)
    Then no code constructs a stop_limit order and the config loader rejects stop_order_type
    And stop_limit_offset_ticks is likewise rejected (v1.68 sweep completion)
