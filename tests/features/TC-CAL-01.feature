Feature: TC-CAL-01
  Scenario: A tagged day blocks scheduled entries and nothing else
    Given today is tagged NO-TRADE with label "FOMC"
    Then every scheduled entry skips with reason "blackout:FOMC" shown on its card
    And stops, LEX, TPF, TPT, decay, EOD, and reconcile run untouched

  Scenario: A standing category rule auto-tags imported events
    Given a standing rule "always block FOMC" and a fresh FOMC schedule import
    Then every imported FOMC day is auto-tagged, visually distinct, and individually removable
    And removing one day leaves the rule and other days intact

  Scenario: Empty calendar means trade; staleness is shown, never blocking
    Given no imports and no tags
    Then no entry is blocked by the calendar
    And an import older than cal_stale_after_days banners the calendar as stale without blocking

  Scenario: Tags and rules survive a reboot
    Given tags and a standing rule exist
    When the bot restarts
    Then both restore exactly per REC-07

  Scenario: Tier-2 events are never trusted silently
    Given imported Fed-speaker events
    Then they render visually distinct as tier-2 and days without data show no fabricated events
