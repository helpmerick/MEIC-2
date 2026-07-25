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

  Scenario: A close can never open a position (ORD-12)
    Given a leg the resolver reports TERMINAL_NO_POSITION
    Then no exit order is submitted for it — the close is a no-op
    And every submitted exit order carries an explicit close designation
    And a replayed or restart-revived close never reaches the broker (ORD-04)
    And UNKNOWN authorizes re-resolution and an alert, never a close order

  Scenario: Alert sinks are wired and thrown evaluations are heard (NFR-08)
    Then no live or paper composition constructs an alert-raising component with a None sink
    And an exception inside a monitor's evaluation raises an alert rather than silently no-opping
