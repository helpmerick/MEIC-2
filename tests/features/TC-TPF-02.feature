Feature: TC-TPF-02
  Scenario: Selector revalidates continuously while open
    Given the selector is open with profit 25% and level 20 enabled
    When streamed profit falls to 24%
    Then level 20 greys out in place without reopening the selector  # 24 - 20 < 5

  Scenario: Backend is authoritative at arm time
    Given the client submits level 20 based on its rendered profit of 25%
    But the backend's own mark computes profit at 22%
    Then the request is rejected (not clamped) and the UI refreshes  # EC-TPF-04
