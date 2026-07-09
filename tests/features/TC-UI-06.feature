Feature: TC-UI-06
  Scenario: The countdown proves the schedule is being watched
    Given the bot is ARMED with a next entry composed
    Then the panel shows the entry's ET time and a ticking countdown
    And the value derives from the backend's seconds_to_next, never the browser clock
    And DISARMED shows "schedule idle" and an exhausted schedule shows "no more entries today"
