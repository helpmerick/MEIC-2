Feature: TC-CLS-01
  Scenario: Manual close and TPF close are byte-identical
    Given two identical open entries A and B (same fills, same stops)
    When entry A is closed via the UI "Close trade" button
    And entry B is closed via a TPF floor trigger
    Then the sequence of broker requests (replaces, close orders, prices, quantities) is identical
    And only the recorded initiator differs: "manual" vs "take_profit"

  Scenario: The close replaces stops, never cancels them bare
    Given an open entry with both stops resting
    When CloseEntry runs
    Then each short's stop is cancel/replaced with a marketable buy-to-close of ledger quantity
    And at no point does a short leg have zero working buy orders
    And at no point does a short leg have two working buy orders

  Scenario: Replace races are terminal-safe
    Given the put stop fills while its replace is in flight
    Then the replace is classified FILLED (ORD-08a) and the side routes to SIDE_STOPPED + LEX
    And given the call replace fails transient
    Then the original call stop is still resting and the replace is retried per ORD-08

  Scenario: No ad-hoc closes exist
    Then CloseEntry is the only module with close-order submission paths
    And no agent or tooling path can submit a broker order outside the application services
