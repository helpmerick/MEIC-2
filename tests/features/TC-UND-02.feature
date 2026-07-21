Feature: TC-UND-02
  Scenario: /ES requires and enforces a pre-16:00 force-close
    Given a /ES entry
    Then config validation refuses it without a valid eod_close_time before 16:00 (default 15:55)
    And at eod_close_time the entry force-closes via the canonical close (initiator eod)
    And no /ES position is ever held into settlement

  Scenario: An unclosed /ES leg raises a critical alert
    Given a /ES leg not confirmed flat by eod_close_deadline
    Then an RSK-06 critical alert fires naming the position (assignment risk)

  Scenario: Cash underlyings are unchanged
    Given an SPX or RUT entry held to expiry
    Then it cash-settles per EOD-01 with no assignment handling and the never-more-than-premium contract holds
