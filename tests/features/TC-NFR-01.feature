Feature: TC-NFR-01
  Scenario: A hung broker call cannot freeze the bot
    Given a broker REST call that hangs for 30 seconds (injected)
    When the next scheduled entry time arrives during the hang
    Then the entry attempt begins on time
    And the session probe, quote consumption and UI stream continue uninterrupted
