Feature: TC-OWN-07
  Scenario: Operator closes the condor in the tastytrade app
    Given entry 1 is OPEN with stops resting and was seen_open in the positions feed
    When the position disappears and the stop shows NOT filled, on two consecutive reconciles
    Then all automation for the side stands down (no LEX, no TPF, no EOD close)
    And the bot submits NO orders and does NOT cancel its own leftover stop   # operator owns all cleanup
    And the critical alert lists the leftover stop with its open-a-long consequence
    And the alert's one-click Cancel-stop action cancels it when (and only when) clicked
    And the side is marked CLOSED_EXTERNAL

  Scenario: A real stop-out is never mislabeled
    Given the short is gone AND its stop order shows FILLED
    Then the normal stop-out path runs (LEX) and no external-close event is emitted
