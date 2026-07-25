Feature: TC-CLS-07
  Scenario: A cancelled pre-fill entry leaves the open set
    Given a WORKING entry with nothing filled
    When Cancel entry confirms the broker cancel
    Then a terminal EntryClosed with initiator "cancelled" is journaled
    And the entry no longer appears open in the projection
    And Flatten All does not target it and does not warn about it again

  Scenario: A partial fill is never journalled as a plain cancel
    Given a cancel that discovers a filled balanced condor
    Then EC-ENT-06 applies (the fill is kept and protected) and no cancelled-terminal event is journaled

  Scenario: A superseded cancel is never reported as a clean cancel
    Given the ladder mints a new working order id mid-replace while the panel holds an older snapshot
    When the cancel targets the superseded id
    Then the result is "cancel_superseded", the current id is re-resolved, and the attempt retries within its bound
    And no report states "cancelled" for that press

  Scenario: cancelled is never treated as flat
    Then no close or flatten path may show green on "cancelled" alone
    And flatness must be proven from broker truth before any all-clear renders
