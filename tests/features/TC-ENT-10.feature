Feature: TC-ENT-10
  Scenario: Arming starts the watcher and the entry fires at its time
    Given a composed schedule with one future entry
    When the operator arms successfully
    Then the day task is watching and the entry fires at its time through the full gate chain

  Scenario: Boot restore resumes the watcher
    Given persisted state is ARMED with entries remaining
    When the bot boots
    Then the day task starts automatically without operator action

  Scenario: Disarm stops future entries atomically
    Given an entry attempt is in flight when the operator disarms
    Then the attempt completes or cancels cleanly and is never abandoned mid-flight
    And no further entries fire

  Scenario: Mid-day edits can never renumber or drop an entry (durable ids)
    Given rows A(fired), B(pending 11:15), C(pending 12:35) with durable ids
    When the operator deletes fired row A while ARMED
    Then rows B and C keep their ids, B fires at 11:15, and nothing is skipped or double-fired

  Scenario: A crashed day task alerts and stays down
    Given the day task dies with an error while ARMED
    Then a critical alert is raised and the task is NOT auto-restarted until Disarm then Arm
