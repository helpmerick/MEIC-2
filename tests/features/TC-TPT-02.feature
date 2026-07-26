Feature: TC-TPT-02
  Scenario: A target is caught on a sub-minute spike
    Given an armed target of 60 percent on an entry with net credit 4.00
    And profit rises above the target at t=0ms and falls back at t=700ms
    Then CloseEntry runs with initiator "take_profit_target" at t=500ms

  Scenario: The disarm still wins at any cadence
    Given an entry whose put stop has filled
    And profit rises above the armed target
    Then no close fires
