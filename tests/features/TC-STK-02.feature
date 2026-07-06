Feature: TC-STK-02
  Scenario: target_premium reads the SHORT leg only
    Given the probe walk matches a short strike
    Then the long wing is placed at wing_width regardless of its own cost  # STK-03

  Scenario: Expensive wing aborts on the total NET floor
    Given both shorts match their probes and wings cost 2.10 each (total net = 1.90)
    Then the entry is SKIPPED with reason "insufficient_credit"  # STK-06: total NET < 2.00 aborts

  Scenario: A thin side trades when the total floor passes (accepted by design)
    Given the put side nets 0.10 after an expensive wing and the call side nets 2.20
    And both shorts collected >= min_short_premium
    Then the entry proceeds (total net 2.30 >= 2.00)   # per-side NET floor deliberately does not exist

  Scenario: Stops and P&L use net fill credit, never target_premium
    Given the condor fills with short put 3.00 and long put 1.00
    Then stop math uses the actual net credit (not 3.00)
    And the day report shows short premium and net credit as separate labelled figures  # UI-14
