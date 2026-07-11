Feature: TC-UI-07
  Scenario: Entry money renders as position dollars with one consistency
    Given an entry with contracts = 2 and per-contract net credit 4.00
    Then displays show 800 dollars and side displays sum exactly to the total
    And aggregates sum per-entry dollars via the single aggregation path

  Scenario: Exemptions stay native
    Then quoted prices, ticks, and trigger prices render per-share
    And slippage renders in both ticks and position dollars
    And no displayed cash number passes through binary float

  Scenario: Markup dial discloses per row
    Given a schedule row sets stop_rebate_markup 0.50 with contracts 2
    Then the row shows the shortfall sentence AND "worst case rises by $200" (0.50 x 100 x 2 x 2)
    And out-of-grid values are rejected, never clamped

  Scenario: Heatmap honesty
    Given an imported day and a day with no data
    Then the imported day shows its imported values and the empty day shows "no data"
    And a fabricated 0-0 never renders
    And weekends render visually distinct from zero-P&L trading days

  Scenario: The local label reads "local", zone from the browser, no geolocation
    Then the echo and countdown label converted times "local" and never a city name
    And the zone derives from the browser Intl setting and no location lookup ever occurs
    And the shortfall tooltip is styled, focus- and tap-capable, never a native title attribute
