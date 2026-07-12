"""PanelCommands — the Close/Flatten glue behind the control panel (UC-14/UI-16,
RSK-01a). Drives it against a real PaperComposition."""
import asyncio
from types import SimpleNamespace
from datetime import datetime
from decimal import Decimal as D

from meic.composition.paper import PaperComposition
from meic.composition.panel_commands import PanelCommands
from meic.domain.events import CondorFilled, FilledLeg, EntryClosed
from meic.domain.projection import fold
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_clock import ET, FakeClock
from tests.harness.intents import stop_intent


def _legs(prefix="SPXW  260706"):
    """ORD-09: the broker-reported legs a real fill would have recorded."""
    return (FilledLeg(f"{prefix}P05940000", "P", "long", 1),
            FilledLeg(f"{prefix}P05990000", "P", "short", 1),
            FilledLeg(f"{prefix}C06060000", "C", "short", 1),
            FilledLeg(f"{prefix}C06110000", "C", "long", 1))



SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


def _comp():
    return PaperComposition(clock=FakeClock(datetime(2026, 7, 7, 9, 30, tzinfo=ET)), ticks=SPX)


def test_close_closes_open_entry_cancels_stops_clears_tpf_idempotent():
    comp = _comp()
    comp.events.append(CondorFilled(entry_id="e1", net_credit=D("4.00"), legs=_legs()))
    comp.state.tpf_floors = {"e1": "6.00"}
    stop_id = asyncio.run(comp.broker.submit(stop_intent("PUT", "3.80", entry_id="e1")))

    cmd = PanelCommands(comp)
    res = asyncio.run(cmd.close("e1"))

    assert res == {"result": "closed", "initiator": "manual"}
    closed = [e for e in comp.events if isinstance(e, EntryClosed)]
    assert closed == [EntryClosed(entry_id="e1", initiator="manual")]
    assert comp.state.tpf_floors == {}                       # armed floor cleared
    # the resting stop was cancelled (no longer working)
    working = asyncio.run(comp.broker.working_orders())
    assert stop_id not in [o.order_id for o in working]

    # idempotent: a second close is a no-op (exactly one EntryClosed)
    assert asyncio.run(cmd.close("e1")) == {"result": "already_closed"}
    assert sum(isinstance(e, EntryClosed) for e in comp.events) == 1


def test_close_unknown_entry():
    assert asyncio.run(PanelCommands(_comp()).close("nope")) == {"result": "unknown_entry"}


def test_flatten_requires_typed_confirmation_then_closes_open_entries():
    comp = _comp()
    comp.events.append(CondorFilled(entry_id="e1", net_credit=D("4.00"), legs=_legs()))
    comp.events.append(CondorFilled(entry_id="e2", net_credit=D("4.00"), legs=_legs()))
    cmd = PanelCommands(comp)

    assert asyncio.run(cmd.flatten("")) == {"result": "confirmation_required"}
    assert asyncio.run(cmd.flatten("nope")) == {"result": "confirmation_required"}

    res = asyncio.run(cmd.flatten("FLATTEN"))
    assert res["result"] == "flattened" and set(res["entries"]) == {"e1", "e2"}
    closed = {e.entry_id for e in comp.events if isinstance(e, EntryClosed)}
    assert closed == {"e1", "e2"}


def test_each_press_gets_its_own_id_so_a_second_press_is_not_a_duplicate():
    """ENT-09 is idempotent PER PRESS. The press id was once derived from
    clock.now(), so two separate presses inside one clock tick collided and the
    operator's second, entirely legitimate press came back `duplicate_press`.
    Found by driving the running panel, not by a test — hence this one."""
    from meic.composition.panel_commands import PanelCommands

    class _Manual:
        """Records the id it was handed; the clock never ticks between presses."""
        def preview(self, press_id, entry_number, row):
            return SimpleNamespace(press_id=press_id)

    cmds = PanelCommands(_comp(), manual_entry=_Manual())
    ids = [cmds.fire_preview(1, None).press_id for _ in range(3)]
    assert len(set(ids)) == 3, f"presses shared an id: {ids}"


def test_a_panel_without_manual_entry_wiring_can_never_fire():
    from meic.composition.panel_commands import PanelCommands

    cmds = PanelCommands(_comp())
    assert cmds.can_fire() is False
    out = asyncio.run(cmds.fire(press_id="p1", entry_number=1, row=None, confirmed=True))
    assert out["result"] == "unavailable"


# =============================================================================
# TPF-06/TPT-02: set/raise/lower/clear per entry, server-side gap validation
# (TPF-02/TPT-03: reject, never clamp).
# =============================================================================

def _comp_with_entry(profit_pct=None):
    comp = _comp()
    comp.events.append(CondorFilled(entry_id="e1", net_credit=D("4.00"), legs=_legs()))
    return comp


def _cmd(comp, profit_pct):
    """A PanelCommands whose profit_pct_provider returns a FIXED value for
    every entry — real wiring (server.py) computes this from live marks; the
    command layer only needs the number."""
    return PanelCommands(comp, profit_pct_provider=lambda entry_id: profit_pct)


def test_set_tpf_arms_the_floor_when_the_gap_is_respected():
    comp = _comp_with_entry()
    cmd = _cmd(comp, D("30"))  # profit 30% -> up to 25 armable (30-5)
    res = cmd.set_tpf("e1", 20)
    assert res == {"result": "armed", "entry_id": "e1", "level": 20}
    assert comp.state.tpf_floors == {"e1": 20}


def test_set_tpf_rejects_a_violating_level_never_clamps():
    comp = _comp_with_entry()
    cmd = _cmd(comp, D("22"))  # 22 - 20 < 5 -> 20 violates the gap
    res = cmd.set_tpf("e1", 20)
    assert res["result"] == "rejected"
    assert comp.state.tpf_floors == {}   # never silently clamped to a lower level


def test_set_tpf_rejected_when_profit_unavailable():
    comp = _comp_with_entry()
    cmd = _cmd(comp, None)   # paper/stale: current profit% unknown
    res = cmd.set_tpf("e1", 5)
    assert res["result"] == "rejected"
    assert "unavailable" in res["reason"]


def test_set_tpf_unknown_entry():
    comp = _comp()
    cmd = _cmd(comp, D("30"))
    assert cmd.set_tpf("nope", 5) == {"result": "unknown_entry"}


def test_clear_tpf_removes_the_armed_floor():
    comp = _comp_with_entry()
    comp.state.tpf_floors = {"e1": 20}
    cmd = _cmd(comp, D("30"))
    res = cmd.clear_tpf("e1")
    assert res == {"result": "cleared", "entry_id": "e1"}
    assert comp.state.tpf_floors == {}


def test_set_tpt_arms_the_target_when_the_gap_is_respected():
    comp = _comp_with_entry()
    cmd = _cmd(comp, D("35"))   # 40 is the lowest selectable (35 + 5)
    res = cmd.set_tpt("e1", 40)
    assert res == {"result": "armed", "entry_id": "e1", "level": 40}
    assert comp.state.tp_targets == {"e1": 40}


def test_set_tpt_rejects_a_passed_target_never_treats_as_close_now():
    """TPT-03 operator ruling 1A: profit 35%, submitting 30% is REJECTED."""
    comp = _comp_with_entry()
    cmd = _cmd(comp, D("35"))
    res = cmd.set_tpt("e1", 30)
    assert res["result"] == "rejected"
    assert "target already passed" in res["reason"]
    assert comp.state.tp_targets == {}


def test_clear_tpt_removes_the_armed_target():
    comp = _comp_with_entry()
    comp.state.tp_targets = {"e1": 60}
    cmd = _cmd(comp, D("10"))
    res = cmd.clear_tpt("e1")
    assert res == {"result": "cleared", "entry_id": "e1"}
    assert comp.state.tp_targets == {}


def test_close_as_clears_both_tpf_and_tpt_for_the_entry():
    comp = _comp_with_entry()
    comp.state.tpf_floors = {"e1": 20}
    comp.state.tp_targets = {"e1": 60}
    cmd = PanelCommands(comp)
    res = asyncio.run(cmd.close_as("e1", "take_profit"))
    assert res == {"result": "closed", "initiator": "take_profit"}
    assert comp.state.tpf_floors == {} and comp.state.tp_targets == {}
    closed = [e for e in comp.events if isinstance(e, EntryClosed)]
    assert closed == [EntryClosed(entry_id="e1", initiator="take_profit")]
