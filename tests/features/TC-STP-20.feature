Feature: TC-STP-20
  Scenario: Wakes carry no data and one path decides
    Given a push event and a poll tick arrive for the same fill
    Then exactly one decision path reads broker truth and the journal and acts once
    And the fill is processed exactly once regardless of wake source

  Scenario: A sold long is never re-sold
    Given the journal shows the side's long already sold
    When any wake detects the historical stop fill again
    Then no order is placed and the wake is a no-op

  Scenario: Poll skips when busy, push waits
    Given the decision path is mid-action
    Then a poll tick SKIPS (its next tick catches up) and a push WAITS for the lock

  Scenario: Stream outage lifecycle
    Given the order-event stream drops
    Then reconnection backs off with a cap, exactly ONE alert fires for the outage
    And the fallback poll is authoritative until resumption re-arms push

  Scenario: A decay buyback fill is never a stop-out
    Given a side's fill is identified as the DCY buyback rather than the stop
    Then the side classifies SIDE_CLOSED_DECAY and the long is left to expire
    And no LEX ladder starts
