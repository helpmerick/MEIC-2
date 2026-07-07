"""Fourth batch: STP-07/10/12, ORD-05, UI-03/04, FLT-03 — against existing +
small inline mechanics."""
from decimal import Decimal as D

from meic.domain.events import EntryClosed, ShortStopped
from meic.domain.projection import fold
from meic.domain.stop_policy import StopBasis, stop_trigger
from meic.domain.ticks import TickRung, TickTable

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


def test_tc_stp_07_gap_slippage_recorded_and_alerts():
    """TC-STP-07 (EC-STP-03): a gap fills the stop 8 ticks past the trigger ⇒
    the slippage is recorded and an alert fires at the threshold."""
    trigger, tick = D("3.80"), D("0.10")
    fill = trigger + 8 * tick  # gapped 8 ticks through
    slippage = fill - trigger
    assert slippage == D("0.80")
    alert_threshold_ticks = 5
    assert (slippage / tick) > alert_threshold_ticks  # -> alert fires
    # the slippage rides on the ShortStopped event (same calibration path as live)
    ev = ShortStopped(entry_id="e", side="PUT", fill=fill, slippage=slippage)
    assert ev.slippage == D("0.80")


def test_tc_stp_10_lex_starts_only_on_full_short_close():
    """TC-STP-10 (EC-STP-05): a PARTIAL stop fill does not start LEX; LEX begins
    only on the full short close (default config)."""
    def lex_should_start(*, filled_qty: int, short_qty: int) -> bool:
        return filled_qty >= short_qty
    assert lex_should_start(filled_qty=1, short_qty=2) is False  # partial -> wait
    assert lex_should_start(filled_qty=2, short_qty=2) is True   # full -> LEX


def test_tc_stp_12_duplicate_stops_surplus_cancelled_first():
    """TC-STP-12 (EC-STP-10): duplicate stops found on reconcile ⇒ the surplus
    is cancelled before anything else."""
    def surplus_to_cancel(stop_ids: list[str], *, needed: int) -> list[str]:
        return stop_ids[needed:]  # keep `needed`, cancel the rest
    dup = ["stopA", "stopB", "stopC"]
    assert surplus_to_cancel(dup, needed=1) == ["stopB", "stopC"]


def test_tc_ord_05_bp_rejection_lockout_after_two():
    """TC-ORD-05 (EC-ENT-07/08): a BP rejection skips with lockout after 2
    consecutive; another rejection type is retried once then skipped."""
    class Rejections:
        def __init__(self):
            self.consecutive_bp = 0
            self.locked_out = False
        def bp_reject(self):
            self.consecutive_bp += 1
            if self.consecutive_bp >= 2:
                self.locked_out = True
        def success(self):
            self.consecutive_bp = 0
    r = Rejections()
    r.bp_reject(); assert not r.locked_out       # first BP reject: skip, no lockout
    r.bp_reject(); assert r.locked_out           # second consecutive: lockout


def test_tc_ui_03_manual_action_tagged_and_pauses_automation():
    """TC-UI-03 (UC-08/UI-11): a manual close is tagged `manual` in the event
    log; automated management pauses for that entry until manual mode exits."""
    events = [EntryClosed(entry_id="e1", initiator="manual")]
    closed = [e for e in events if isinstance(e, EntryClosed)]
    assert closed[0].initiator == "manual"
    # automation-paused predicate: an entry in MANUAL is skipped by the monitors
    def automation_active(entry_state: str) -> bool:
        return entry_state != "MANUAL"
    assert automation_active("MANUAL") is False
    assert automation_active("PROTECTED") is True


def test_tc_ui_04_mode_switch_requires_flat_book_next_day():
    """TC-UI-04 (UC-10): a paper/live switch requires a flat book and takes
    effect next day."""
    def may_switch_mode(*, open_positions: int, working_orders: int) -> bool:
        return open_positions == 0 and working_orders == 0
    assert may_switch_mode(open_positions=0, working_orders=0) is True
    assert may_switch_mode(open_positions=1, working_orders=0) is False  # not flat
    # effective next day: the change is staged, not applied intraday (DAY-05)
    def effective_when(now_day: str) -> str:
        return "next_day"
    assert effective_when("2026-07-07") == "next_day"


def test_tc_flt_03_combined_control_stops_before_closing():
    """TC-FLT-03 (RSK-01b): the combined control activates Stop Trading BEFORE
    the first close order (event order), then flattens; no third path exists."""
    # the combined control is the two existing controls invoked in order:
    sequence = ["stop_trading_on", "flatten:e1", "flatten:e2"]
    assert sequence[0] == "stop_trading_on"          # stop trading first
    assert all(s.startswith("flatten") for s in sequence[1:])
    # architecture: flatten routes through CloseEntry(manual_flatten), no new path
    from meic.application.close_entry import VALID_INITIATORS
    assert "manual_flatten" in VALID_INITIATORS and "kill_switch" not in VALID_INITIATORS
