Feature: TC-STK-06
  Scenario: Long at the desired short strike forces one shift
    Given entry 3's target short put strike 5990 holds an existing long
    Then the short shifts to 5985 and the wing moves with it (width preserved)

  Scenario: Three blocked strikes abort the entry
    Given existing longs at 5990, 5985 and 5980 (the original and both shift targets)
    Then the entry is SKIPPED with reason "strike_collision" and no order is submitted

  Scenario: Same type stacks - shorts on shorts
    Given entry 1 is short 5990 and entry 3's selection also lands on 5990
    Then no shift occurs and the order is submitted
    And both entries' fills and stops attribute correctly by order ID

  Scenario: Same type stacks - longs on longs
    Given the wing target already holds another entry's long
    Then no shift occurs

  Scenario: Long shifts alone when its target holds a short (width widens)
    Given the short places at its original strike
    But the wing target 5940 holds an existing short position
    Then the long shifts alone to 5935 (spread now 5 points wider)
    And RSK-04 evaluates the widened worst case before submission
    And five failed long shifts abort the entry with "strike_collision"

  Scenario: In-flight opposite-type orders count as occupied
    Given an unfilled working order includes a long at 5990
    When the next entry wants a short at 5990
    Then 5990 is treated as blocked
    But an unfilled SHORT at 5990 does not block a new short there  # same type never blocks

  Scenario: Gates re-run on final strikes
    Given the shifted short's premium falls below min_short_premium (or total net < min_total_credit)
    Then the entry is SKIPPED with reason "insufficient_credit"
