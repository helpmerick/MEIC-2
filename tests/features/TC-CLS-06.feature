Feature: TC-CLS-06
  Scenario: A pre-action failure reports close_failed, position untouched
    Given the close path's working_orders() call raises before any order is sent
    Then the response is 200 {"result":"close_failed","stage":"pre_submit","reason":...}
    And no per-side event is journaled and the position is unchanged
    And no raw 500 ever reaches the frontend

  Scenario: A mid-sequence failure reports close_partial, naming the sides
    Given some sides already closed-and-journaled and the long-leg submit then raises
    Then the response is 200 {"result":"close_partial","stage":"in_flight", sides_closed, sides_remaining}
    And the journal holds the per-side events for the closed sides and NO EntryClosed
    And an RSK-06 critical alert fired
    And the result is never presented as a generic failure

  Scenario: The remainder closes idempotently on a second click
    Given a prior close_partial left one side open
    When Close is clicked again against a healthy broker
    Then only the remaining side closes, via the same canonical path, journaling exactly one EntryClosed

  Scenario: Baseline and existing results are byte-identical
    Then a healthy close still returns 200 {"result":"closed"}
    And already_closed, unknown_entry, and the CLS-03 cancel path are unchanged
