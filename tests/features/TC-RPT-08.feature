Feature: TC-RPT-08
  Scenario: MAE measures trigger-distance consumed
    Given a short filled at 3.00 with trigger 3.80 whose recorded mark peaked at 3.60 before expiry
    Then the entry MAE = 75 percent of trigger distance and it counts as survived
    And missing samples render as gaps, never interpolated

  Scenario: Slot analytics attribute to the scheduled slot
    Given entries fired from the 10:00 and 12:35 slots across a month
    Then win rate, expectancy, and premium capture render per slot
    And manual entries group under a "manual" slot
