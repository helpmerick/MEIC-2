Feature: TC-OWN-10
  Scenario: Operator cancels the bot's stop in the app, keeps the position
    Given the stop order shows cancelled with no bot-initiated cancel in the event log
    Then the bot does NOT re-place it
    And the side is marked USER_UNPROTECTED with a critical alert
    And the UI banner offers a one-click Re-protect action which places a fresh stop when clicked

  Scenario: Bot-side absence still auto-protects
    Given a short whose stop was never confirmed (crash before placement)
    Then REC-04 re-places the stop automatically   # OWN-11 applies only to non-bot cancels
