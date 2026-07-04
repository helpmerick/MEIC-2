Feature: TC-ENT-06
  Scenario: Near-expiry token renewed at warm-up, entry fires on time
    Given the session token expires in 200 seconds at T-60
    When the warm-up probe runs
    Then the token is renewed before T-30
    And the 10:30 entry begins exactly on schedule with fresh quotes

  Scenario: Dropped stream resubscribed at warm-up
    Given the DXLink chain subscription is silently stale at T-60
    Then the warm-up resubscribes and quotes are fresh (STK-04) at fire time

  Scenario: Warm-up cannot restore the session
    Given token renewal fails repeatedly from T-60
    Then an alert is raised at T-10
    And at fire time the entry is SKIPPED with reason "invalid_session"
    And the entry time itself was never delayed

  Scenario: Bot starts inside the warm-up window
    Given the bot finishes recovery at T-30
    Then the warm-up probe runs immediately (compressed), not skipped
