Feature: TC-DAY-06
  Scenario Outline: Non-military formats are rejected per row
    When a schedule row's time is "<bad>"
    Then validation rejects it with reason "not_24h_military"
    Examples:
      | bad    |
      | 1:53pm |
      | 0930   |
      | 24:00  |
      | 11:60  |
      | 11-53  |

  Scenario: Valid formats pass and dots canonicalise
    Then 09:32, 9:32, 15:30 and 23:59 pass the format gate
    And 11.53 persists as 11:53 and 9.32 persists as 09:32

  Scenario: The RTH window is enforced on the value
    Then 08:00 and 16:30 are rejected with reason "outside_market_hours"
    And 09:30 (the open edge) saves
    And the format and window checks are backend-authoritative
