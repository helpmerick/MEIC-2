Feature: TC-NFR-04
  Scenario: One connection all day
    Given a full simulated trading day
    Then the fake transport counts exactly 1 persistent connection in the happy path

  Scenario: Zombie ticks never land (generation guard property test)
    Given the hub replaced socket generation 2 with generation 3
    When late ticks from generation 2 arrive interleaved with generation 3 ticks
    Then no generation-2 tick reaches the marks table and prices never move backwards in time

  Scenario: Single writer (architecture test)
    Then only the hub manager writes the marks table
    And the one-shot fetcher's data path returns to its caller only

  Scenario: Decision moment - demand-reconnect heals
    Given the hub is sick and in an 8s backoff wait when an entry fires
    When the demand-reconnect succeeds within feed_demand_reconnect_seconds
    Then the entry proceeds on the healed hub (no fetcher used)

  Scenario: Decision moment - fetcher path
    Given the demand-reconnect fails
    Then a one-shot fetcher returns a chain snapshot directly to the entry attempt
    And the snapshot passes chain-integrity gates before any selection
    And the marks table is untouched by the fetcher

  Scenario: Decision moment - give up safely
    Given demand-reconnect and fetcher both fail
    Then the entry skips "data_unavailable", a LEX ladder freezes with its limit still working, TPF/DCY pause, an informational alert fires, and everything resumes on heal
