Feature: TC-FLT-01
  Scenario: Flatten all with mixed entry states
    Given entry 1 OPEN (both sides), entry 2 with put side mid-LEX, entry 3 with a WORKING entry order, entry 4 OPEN with an armed TPF floor
    When the operator confirms Flatten all
    Then entry 3's order is cancelled (CLS-03), no close orders placed for its legs
    And entries 1, 2, 4 close via CloseEntry with initiator "manual_flatten"
    And entry 2's LEX ladder is superseded by an immediate marketable-limit close
    And entry 4's TPF floor is cleared
    And a scheduled entry arriving WHILE the flatten executes is SKIPPED (flatten_in_progress)
    And with the Stop Trading checkbox OFF, the next scheduled entry after completion fires normally into the clean book
    And with the checkbox ON, subsequent entries are blocked until reset (Stop Trading persisted across restart)
