Feature: TC-ENT-05
  Scenario: Working entry order cancelled before next entry
    Given entry 2's order is still WORKING at 11:00 ET
    When entry 3's scheduled time arrives
    Then entry 2's order is cancelled and cancellation confirmed
    And any partial fill is resolved per EC-ENT-06 before entry 3 begins
