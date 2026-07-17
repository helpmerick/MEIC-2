Feature: TC-CAL-04
  Scenario: Monthly and quarterly OpEx compute correctly
    Then 2026-07-17 is an OPEX_MONTHLY event (third Friday of July 2026)
    And 2026-09-18 is a QUAD_WITCH event, badged distinctly from monthly OpEx
    And no weekly or daily expiration ever renders as a calendar event

  Scenario: A holiday third Friday shifts to the preceding trading day
    Given a month whose third Friday is an exchange holiday per the DAY-01a calendar (real vector: April 2000, Good Friday the 21st)
    Then the OpEx event lands on the preceding trading day, never on the holiday

  Scenario: Computed events are taggable but never auto-blocked and never stale
    Given a standing rule "always block QUAD_WITCH"
    Then quad-witch days auto-tag while monthly OpEx days stay untagged and tradeable
    And computed events carry no staleness banner and trigger no fetch
