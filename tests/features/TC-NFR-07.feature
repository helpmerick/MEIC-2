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
    And DAT-04's halt input (no provider, inverted polarity) is the ninth-finding pinned regression

  Scenario: The halt gate blocks when unmeasured (DAT-04a)
    Given no trading-status reading, or one stale beyond 300 seconds
    Then entries are blocked with reason "market_halted"
    And a status of not-active blocks identically
    And all four gate inputs share False-means-block polarity

  Scenario: stop_limit has no construction path (STP-03 tombstone)
    Then no code constructs a stop_limit order and the config loader rejects stop_order_type
    And stop_limit_offset_ticks is likewise rejected (v1.68 sweep completion)
