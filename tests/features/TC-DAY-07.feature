Feature: TC-DAY-07
  Scenario: Holiday observance quirks compute correctly
    Then New Year's Day falling on Saturday is NOT observed (real vector: 2021-12-31 was a full trading day)
    And Saturday holidays observe Friday, Sunday holidays observe Monday
    And Good Friday derives from the Easter computus for any year
    And July 3 (Mon-Thu), the day after Thanksgiving, and Christmas Eve (Mon-Thu) are 13:00 ET half-days
    And the computed calendar matches published NYSE calendars pinned as vectors

  Scenario: No day task exists on a closed day
    Given the bot is ARMED on a Saturday
    Then the supervisor starts no day task and zero EntrySkipped events enter the journal
    And the ENT-03 fire-time market-open gate remains in force unchanged

  Scenario: The countdown never promises a closed-day entry
    Given a Saturday with the next trading day Monday and first entry 11:56 ET
    Then the panel shows "Mon 11:56 ET" with a day-spanning countdown
    And "no more entries today" appears only for an exhausted schedule on a trading day

  Scenario: An empty calendar is a construction error
    Given live gates constructed with no holiday data
    Then boot fails loudly rather than treating holidays as open days

  Scenario: The local echo is DST-correct across the switch
    Given a next entry lying on the far side of a DST transition
    Then the local echo converts the full instant, not today's offset
