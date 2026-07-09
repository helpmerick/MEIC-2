Feature: TC-STK-07
  Scenario: Holey near-ATM chain blocks selection, heals, entry proceeds
    Given only 75% of strikes within the ATM band have marks at fire time
    Then no strike selection occurs and the gate retries every chain_retry_seconds
    When the chain completes at T+20s (within the entry window)
    Then selection proceeds normally

  Scenario: Persistent holes skip the entry at window expiry
    Given the entry's trade-relative reachable strike set never reaches chain_completeness_pct within entry_window_seconds
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

  Scenario: Far-OTM dead strikes never block (v1.51 regression, live 2026-07-09)
    Given every strike in the entry's reachable set has fresh two-sided marks
    And calls 55+ points OTM outside the reachable set are listed but never quoted
    Then the STK-10 gate PASSES and selection proceeds

  Scenario: A dead long wing is caught upfront
    Given the reachable set includes the wing strike and its quote is missing
    Then the gate counts it against completeness (no later wing_unmarked surprise)

  Scenario: chain_atm_band_pts is retired
    Given config contains chain_atm_band_pts
    Then config validation rejects it as an unknown retired key
