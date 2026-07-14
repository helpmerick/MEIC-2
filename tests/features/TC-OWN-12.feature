Feature: TC-OWN-12
  Scenario: A standdown is an event, not an alert
    Given a catch-up finds a bot leg disposed of at the broker
    Then a standdown event enters the journal naming entry, leg, reason, and broker finding
    And the out-of-band P&L stays the operator's, absent from the bot's ledger

  Scenario: The LEX-07 watchdog is never taught to trust standdowns
    Given a standdown explanation exists for a missing long
    Then the LEX-07 watchdog still raises its alert for the operator to dismiss
    And no suppression rule keyed on standdown framing exists
