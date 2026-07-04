Feature: TC-ENT-02
  Scenario Outline: Pre-entry gate blocks entry
    Given <gate_condition> is true at 10:30 ET
    Then entry 2 is SKIPPED with reason <reason>
    And no order is submitted

    Examples:
      | gate_condition            | reason              |
      | Stop Trading active       | stop_trading        |
      | a Flatten All executing   | flatten_in_progress |
      | a market halt             | market_halted       |
      | market data stale         | data_unavailable    |
      | broker session invalid    | invalid_session     |
      | insufficient buying power | insufficient_bp     |
