"""TC-LEX-06 (LEX-08/EC-LEX-03 race), TC-LEX-07 (LEX-09 late fill), TC-LEX-09
(EC-LEX-06 restart mid-ladder). Pure LEX reconciliation decisions."""
from decimal import Decimal as D

from meic.application.lex_reconcile import (
    correct_pnl_for_late_fill,
    resolve_replace_race,
    resume_ladder_on_restart,
)


# --- TC-LEX-06: fill-during-replace race, and the double-fill short ----------

def test_tc_lex_06_race_adopts_broker_truth_and_buys_back_double_fill():
    """TC-LEX-06 (LEX-08/EC-LEX-03): a fill during cancel/replace supersedes the
    ladder (broker truth adopted); a double fill creates a short ⇒ immediate
    buy-back of the excess + critical alert."""
    # only the OLD order filled while the replace was in flight -> adopt old
    r = resolve_replace_race(old_filled_qty=1, new_filled_qty=0, intended_qty=1)
    assert r.adopt == "old" and r.buy_back_qty == 0 and r.alert is None

    # only the NEW (replacement) filled -> adopt new
    r = resolve_replace_race(old_filled_qty=0, new_filled_qty=1, intended_qty=1)
    assert r.adopt == "new" and r.buy_back_qty == 0 and r.alert is None

    # BOTH filled -> short position created -> buy back the excess + alert
    r = resolve_replace_race(old_filled_qty=1, new_filled_qty=1, intended_qty=1)
    assert r.adopt == "both" and r.buy_back_qty == 1
    assert r.alert == ("critical", "lex_double_fill_short_position")


# --- TC-LEX-07: late fill after presumed cancel -> P&L corrected -------------

def test_tc_lex_07_late_fill_corrects_pnl_from_broker_records():
    """TC-LEX-07 (LEX-09): the bot presumed the sell cancelled (no recovery
    recorded), but a late fill landed at the broker ⇒ adopt broker truth and
    correct the P&L by the difference."""
    out = correct_pnl_for_late_fill(recorded_recovery=D("0"), broker_recovery=D("1.20"))
    assert out["recovery"] == D("1.20")
    assert out["pnl_delta"] == D("1.20") and out["corrected"] is True

    # no late fill -> no correction
    same = correct_pnl_for_late_fill(recorded_recovery=D("1.20"), broker_recovery=D("1.20"))
    assert same["corrected"] is False and same["pnl_delta"] == D("0")


# --- TC-LEX-09: restart mid-ladder ------------------------------------------

def test_tc_lex_09_restart_resumes_step_and_rediscovers_order_by_key():
    """TC-LEX-09 (EC-LEX-06/REC-03): on restart the ladder resumes from the
    persisted step and rediscovers the working sell order by idempotency key;
    if the key is gone, it resubmits."""
    key = "lex:e1:PUT"
    r = resume_ladder_on_restart(persisted_step=2, order_key=key,
                                 broker_working_keys={key, "lex:e2:CALL"})
    assert r["resume_step"] == 2 and r["rediscovered"] is True and r["resubmit"] is False

    gone = resume_ladder_on_restart(persisted_step=3, order_key=key,
                                    broker_working_keys=set())
    assert gone["rediscovered"] is False and gone["resubmit"] is True
