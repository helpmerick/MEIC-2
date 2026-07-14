Feature: TC-RPT-17
  Scenario: The reconciler sees only the bot's rows
    Given a day where the account holds the bot's condor (+43.68, fees 6.32) and the operator's own trades (-928.26 futures, -466.00 settlement, +350.12 condor)
    Then RPT-15's cash_delta and fees reflect ONLY the bot's rows
    And the foreign rows never move any bot figure and a clean day emits no correction
