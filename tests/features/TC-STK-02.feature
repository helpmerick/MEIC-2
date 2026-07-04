Feature: TC-STK-02
  Scenario: Small overshoot within tolerance is accepted
    Given target_premium = 3.00, target_premium_tolerance = 0.10
    And adjacent put strikes with mids 3.10 and 2.85
    Then the short put strike is the 3.10 strike   # ceiling = 3.10 inclusive; richest qualifying wins
    And the long put strike = short - wing_width   # STK-03, regardless of its cost

  Scenario: Boundary — one tick over the ceiling is rejected
    Given adjacent put strikes with mids 3.11 and 2.85
    Then the short put strike is the 2.85 strike   # 3.11 > 3.10 ceiling

  Scenario: Ceiling beats proximity
    Given adjacent put strikes with mids 3.25 and 2.85
    Then the short put strike is the 2.85 strike   # 3.25 above ceiling even though closer to target

  Scenario: No strike at or below the ceiling
    Given every strike with valid quotes has mid > 3.10
    Then the entry is SKIPPED with reason "no_valid_strikes"

  Scenario: Same short target, expensive wing => net credit gate skips the entry
    Given target_premium = 3.00 and both shorts fill their premium floor
    And in the morning the wings cost 1.00 each (total net = 3.90)
    But at the 12:30 entry the wings cost 2.10 each (total net = 1.90)
    Then the morning entry proceeds
    And the 12:30 entry is SKIPPED with reason "insufficient_credit"  # STK-06: total NET < 2.00 aborts

  Scenario: A thin side trades when the total floor passes (accepted by design)
    Given the put side nets 0.10 after an expensive wing and the call side nets 2.20
    And both shorts collected >= min_short_premium
    Then the entry proceeds (total net 2.30 >= 2.00)   # per-side NET floor deliberately does not exist

  Scenario: Stops and P&L use net fill credit, never target_premium
    Given the condor fills with short put 3.00 and long put 1.00
    Then per_side stop math uses side net credit 2.00 (not 3.00)
    And the day report shows short premium and net credit as separate labelled figures  # UI-14
