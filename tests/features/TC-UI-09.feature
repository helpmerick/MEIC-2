Feature: TC-UI-09
  Scenario: Days are visually separated, never continuous
    Given activity spanning 2026-07-13 and 2026-07-14
    Then a sticky ET date header renders between the two days
    And each row shows its own ET time
    And days without activity render no header

  Scenario: Every activity explains itself on hover
    Given any rendered activity row
    Then a styled focus- and tap-capable tooltip explains the event in plain English
    And the wording uses the doc-12 chapter vocabulary
    And no native title attribute carries the explanation

  Scenario: An unexplained event type is a test failure
    Given an event type renderable by the feed with no explanation entry
    Then the suite fails naming the event type
