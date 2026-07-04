Feature: TC-LEX-01
  Scenario: Ladder from mid toward bid
    Given the short put stop filled and the long put quotes bid 2.00 / ask 2.30
    Then a limit sell at 2.15 is placed within lex_start_latency_ms
    When lex_reprice_seconds elapses without fill
    Then the order is replaced at one tick lower recomputed from the CURRENT quote  # EC-LEX-05
    And after lex_reprice_attempts unfilled replacements the fallback places a marketable limit at the current bid  # LEX-05
