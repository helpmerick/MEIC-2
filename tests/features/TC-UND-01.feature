Feature: TC-UND-01
  Scenario: Dollar math uses the profile multiplier
    Given an SPX entry and a /ES entry with identical width and credit
    Then every SPX dollar figure uses x100 and every /ES figure uses x50
    And RSK-04 sums the day as SPX-worst-case(x100) + ES-worst-case(x50), never a shared multiplier

  Scenario: An unverified underlying is refused, never guessed
    Given a schedule row naming an unsupported or unverified underlying
    Then config validation refuses it and no entry is attempted
