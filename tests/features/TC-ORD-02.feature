Feature: TC-ORD-02
  Scenario: Submit timeout does not cause duplicate orders
    Given the broker accepts the order but the submit response times out
    When the bot queries by idempotency key
    Then it discovers the existing order and does NOT resubmit
