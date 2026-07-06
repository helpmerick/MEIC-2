Feature: TC-STK-07
  Scenario: Holey near-ATM chain blocks selection, heals, entry proceeds
    Given only 75% of strikes within the ATM band have marks at fire time
    Then no strike selection occurs and the gate retries every chain_retry_seconds
    When the chain completes at T+20s (within the entry window)
    Then selection proceeds normally

  Scenario: Persistent holes skip the entry at window expiry
    Given the chain never reaches chain_completeness_pct within entry_window_seconds
    Then the entry is SKIPPED with reason "incomplete_chain" and no order is submitted

  Scenario: Probe-match integrity invariant (STK-11, v1.39)
    Given the probe walk selects a strike
    Then its raw mid is within 0.025 of the matched probe price
    And the day report records the matched probe number

  Scenario: Missing wing retries within the window
    Given the wing strike has no mark at fire time but appears at T+15s
    Then the entry proceeds with the correct wing (no guessing, no immediate skip)

  Scenario: Far-OTM emptiness never trips the gate
    Given strikes outside the ATM band have no bids
    Then the chain-integrity gate still passes
