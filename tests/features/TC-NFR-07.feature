Feature: TC-NFR-07
  Scenario: Every spec-mandated live component is constructed and ticked
    Given the registry of runtime components the spec mandates
    Then each is provably constructed AND ticked inside live_app()
    And a registered component absent from the live composition fails CI
    And DecayWatcher — found built, tested, race-guarded, and never constructed — is the pinned regression

  Scenario: stop_limit has no construction path (STP-03 tombstone)
    Then no code constructs a stop_limit order and the config loader rejects stop_order_type
