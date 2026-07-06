"""TC-STP-01 — BLOCKED on a proposed spec amendment (round_to_tick vs floor).

TC-STP-01's trigger assertions use `round_to_tick(...)`, but STP-02 (v1.39,
Ash-ratified) mandates the trigger floors DOWN to tick, and TC-STP-16 pins
floor. The two produce DIFFERENT triggers, e.g.:
  total_credit:  round_to_tick(0.95*2.30)=2.20   vs   floor -> 2.15
  short_premium: round_to_tick(1.35*1.95)=2.65    vs   floor -> 2.60
  per_side:      round_to_tick(1.35+0.95*1.20)=2.50 vs floor -> 2.45

Implementing against `round_to_tick` would contradict STP-02 and the pinned
TC-STP-16 vectors (both green). Per the contract, this is a proposed
amendment, not an improvisation: TC-STP-01's three `round_to_tick(...)`
assertions should read `floor_to_tick(...)`. Full diff is in the slice-2 PR /
operator report. The trigger math itself is implemented and verified against
STP-02/TC-STP-16; only this stale-wording feature is held.
"""


def test_tc_stp_01_blocked_on_round_vs_floor_amendment():
    raise NotImplementedError(
        "TC-STP-01 asserts round_to_tick triggers; STP-02 v1.39 + TC-STP-16 "
        "mandate floor. Proposed amendment: round_to_tick(...) -> floor_to_tick(...) "
        "in all three TC-STP-01 trigger steps. Held pending operator ratification."
    )
