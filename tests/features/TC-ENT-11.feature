Feature: TC-ENT-11
  Scenario: One resolver, no inference
    Then every path that decides an entry is finished calls the single resolver
    And no path infers terminality from a result string, an absence, a cursor feed, a raw journal scan, or a leg list
    And only the resolver journals a terminal

  Scenario: UNKNOWN is first-class and never collapsed
    Given evidence insufficient to decide
    Then the resolver returns UNKNOWN
    And no terminal is journaled, nothing renders green, and the entry stays visible

  Scenario: Evidence is ranked and positive
    Then broker positions decide and order/fill feeds are advisory
    And the absence of a record is never treated as proof of no position

  Scenario: Broker-primitive parity is enforced
    Then every fake answers each broker primitive identically to the live adapter
    And a divergence (e.g. a leg predicate that ignores fill status) fails CI

  Scenario: The resolver answers PER LEG (v1.91 scope correction)
    Given a still-open entry with one leg already flat
    When a close is attempted on that flat leg
    Then the resolver answers for THAT LEG, returns TERMINAL_NO_POSITION, and no order reaches the wire
    And a resolver that discards the caller's leg symbol fails this test

  Scenario: Protective stop placement is NOT an exit (v1.91, ORD-12 scope)
    Given stop placement on a filled entry while positions() lags
    Then ORD-12's predicate does not apply to kind="stop"
    And stop placement is never refused as an exit
    And no path can reach "unhedged condor with no stops, journaled as closed"

  Scenario: Refusals raise, never return a sentinel (v1.91)
    Given any refusal, no-op id, or UNKNOWN on an order path
    Then it propagates as an exception callers cannot ignore
    And no sentinel is ever journaled as a real order id
    And SideClosed/EntryClosed are never appended after a refused submit

  Scenario: Quantity is filled, not ordered (v1.91)
    Given a 2-lot condor that filled 1
    Then a close acts on 1, never 2, and no surplus Buy-to-Open occurs
    And a cancelled-after-partial order still reports its filled legs

  Scenario: Git state operations are single-step on a real-money tree (v2.03, NFR-10)
    Then no documented procedure chains a state-changing git step onto an unverified prior step
    And recovery procedures move files aside rather than deleting them
    # Process rule; enforced by review and by the documented procedures, not by runtime code.

  Scenario: Liveness predicates fail toward present, never absent (v1.92, NFR-09)
    Given a broker order status the predicate does not recognise
    Then it resolves UNKNOWN and is logged loudly, never treated as gone
    And the working classification includes received, live, routed, contingent, in flight, cancel requested, replace requested, partially removed
    And the dead classification is exactly cancelled, rejected, expired, removed, filled
    And no allow-list of known-live states may gate a destructive action

  Scenario: Parity is observation-based and catches the Routed divergence (v1.91)
    Given the recorded observation of a resting stop at status "Routed"
    Then the live working_orders filter reports it as working
    And stop confirmation counts it, so a protected position is never auto-flattened as UNPROTECTED
    And a stub-vs-stub parity check that misses this divergence fails

  Scenario: Filled quantity is derived, bounded, and never decides a close (v1.97, ENT-11(9))
    Given a leg at status Received with quantity 1 and remaining_quantity 1
    Then filled_qty derives as 0
    And given remaining_quantity absent or unparsable, filled_qty is UNKNOWN — never zero, never filled
    And given a CANCELLED order with remaining_quantity 0, filled_qty is UNKNOWN — never "fully filled"
    And given a PARTIALLY REMOVED order, filled_qty is UNKNOWN (remaining reduced by removal, not filling)
    And partially removed is simultaneously WORKING for liveness and UNKNOWN for fill-derivation — different questions
    And a partially-filled order at a working status derives correctly, since no Partially Filled status exists
    And fills_since is corrected for its REAL defects: ignored cursor, orders-not-fills sim/live parity divergence, and live-window scoping that silently drops an aged-out fill
    And no close order's quantity is decided by the derivation: broker positions decide (ENT-11(3))
    And a wrong derivation can produce a wrong report but never a wrong order

  Scenario: A close can never open a position (ORD-12)
    Given a leg the resolver reports TERMINAL_NO_POSITION
    Then no exit order is submitted for it — the close is a no-op
    And every submitted exit order carries an explicit close designation
    And a replayed or restart-revived close never reaches the broker (ORD-04)
    And UNKNOWN authorizes re-resolution and an alert, never a close order

  Scenario: Alert sinks are wired and thrown evaluations are heard (NFR-08)
    Then no live or paper composition constructs an alert-raising component with a None sink
    And an exception inside a monitor's evaluation raises an alert rather than silently no-opping
