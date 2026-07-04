Feature: TC-RSK-01
  Scenario: Stop Trading blocks entries and nothing else
    Given two open condors and one LEX ladder in progress
    When Stop Trading is activated
    Then no further entries occur
    And resting stops remain working
    And the LEX ladder continues            # risk-reducing work proceeds
    And TPF monitoring and the decay watcher continue

  Scenario: Flatten All does not block trading (orthogonality)
    Given Flatten All is confirmed WITHOUT the Stop Trading checkbox
    Then every bot entry closes via CLS
    And the next scheduled entry fires normally into the clean book

  Scenario: Stop Trading persists across restart
    Given Stop Trading was active
    When the bot restarts
    Then entries remain blocked until the operator resets ("Resume trading")
