Feature: TC-ENT-07
  Scenario: Disarmed means nothing fires, ever
    Given 3 entries are composed in the UI but the operator never pressed Arm
    Then no entry attempt occurs at any scheduled time
    And existing positions remain fully managed (stops, LEX, TPF)

  Scenario: Arming an empty schedule is rejected
    Given zero entries are composed
    Then the Arm action fails validation with an explanatory error

  Scenario: The operator's count is the count
    Given the operator composed exactly 4 entries and armed
    Then exactly 4 entry attempts run, at exactly the composed times

  Scenario: Disarm mid-day stops future entries only
    Given 4 entries armed, 2 already filled
    When the operator disarms at 11:45
    Then the remaining 2 entries never fire
    And the 2 open condors keep their stops and full management

  Scenario: Armed state persists across days (standing schedule)
    Given the operator armed 6 entries on Monday
    When Tuesday's market opens with no operator action
    Then the day self-initializes (calendar, reconcile, warm-up)
    And all 6 entries fire at their times on Tuesday, and every trading day after, until the operator disarms

  Scenario: Disarmed state equally persists
    Given the operator disarmed on Monday afternoon
    Then no entries fire on Tuesday, Wednesday, or any day until re-armed

  Scenario: Docker/process restart restores the armed state
    Given the system was ARMED with 6 entries and the container dies at 10:47
    When the container recovers at 10:52
    Then the bot boots ARMED (state restored from the durable store)
    And the 10:30 entry (window missed while down) is SKIPPED missed_window
    And the 11:00 and later entries fire normally
    And a restart while DISARMED boots DISARMED

  Scenario: Confirm Live is the third required state (ENT-01b)
    Given the system is ARMED with Stop Trading off
    But Confirm Live is OFF
    Then no entry fires at any scheduled time
    And the dashboard states which gate is blocking

  Scenario: The full persistent-state inventory survives Docker recovery (REC-07)
    Given ARMED = on, Stop Trading = on, Confirm Live = on, trading_mode = paper, a standing 6-entry schedule, an armed TPF floor, and a paper cash ledger
    When the container dies and recovers
    Then every item is restored exactly as it was
    And entries remain blocked (Stop Trading is on) until the operator resumes
    And the paper ledger balance is unchanged

  Scenario: Fresh install defaults safe
    Given a first-ever boot with no persisted state
    Then DISARMED, Stop Trading off, Confirm Live OFF
