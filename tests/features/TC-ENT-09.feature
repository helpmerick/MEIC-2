Feature: TC-ENT-09
  Scenario: A floor filters the walk without changing it
    Given SPX at 7480 and a manual fire with put floor 7450
    And the probe walk would normally match the 7460 put
    Then strikes inside the floor are excluded and the walk selects at or beyond 7450
    And the call side runs default behaviour when no call floor is set

  Scenario: Credit rules are never weakened by a floor
    Given floors that leave no strike satisfying 1.00 gross and 2.00 total net
    Then the fire skips with reason "no_valid_strikes" and no order is placed

  Scenario: Refuse and re-pick when spot crosses a floor
    Given the dialog opened with SPX 7480 and call floor 7500 selected
    When SPX is 7505 at OK time
    Then the fire is REFUSED with reason "floor_inside_spot"
    And the operator must re-select before any order can be placed

  Scenario: Dropdowns come from the validated universe only
    Then every selectable strike has fresh two-sided quotes at dialog population
    And each row shows strike, points from spot, and live mid

  Scenario: Floors are evented for audit
    Given a manual fire with put floor 7450 and no call floor
    Then the entry events record the floors and the day report shows them
