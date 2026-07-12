"""reporting.slots — RPT-13 slot analytics; ad-hoc entries always "manual"."""
from decimal import Decimal as D

from meic.domain.events import CondorFilled
from meic.domain.projection import fold
from meic.reporting.slots import MANUAL, by_slot, slot_metrics, slot_of


def _entries():
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00")),
        CondorFilled(entry_id="2026-07-09#2", net_credit=D("-2.00")),
        CondorFilled(entry_id="2026-07-09#101", net_credit=D("3.00")),
    ]
    return fold(events).entries


def test_adhoc_entries_are_always_manual_regardless_of_slot_map():
    assert slot_of("2026-07-09#101", slot_map={"2026-07-09#101": "10:00"}) == MANUAL
    assert slot_of("2026-07-09#101") == MANUAL


def test_scheduled_entries_take_their_label_from_the_slot_map():
    slot_map = {"2026-07-09#1": "10:00"}
    assert slot_of("2026-07-09#1", slot_map=slot_map) == "10:00"


def test_unmapped_scheduled_entries_are_unknown_never_fabricated():
    assert slot_of("2026-07-09#1") == "unknown"


def test_by_slot_groups_manual_separately():
    grouped = by_slot(_entries(), slot_map={"2026-07-09#1": "10:00", "2026-07-09#2": "12:35"})
    assert {e.entry_id for e in grouped[MANUAL]} == {"2026-07-09#101"}
    assert {e.entry_id for e in grouped["10:00"]} == {"2026-07-09#1"}


def test_slot_metrics_win_rate_expectancy_premium_capture():
    slot_map = {"2026-07-09#1": "10:00", "2026-07-09#2": "12:35"}
    metrics = slot_metrics(_entries(), slot_map=slot_map)
    assert metrics["10:00"]["win_rate"] == D("1")
    assert metrics["10:00"]["expectancy"] == D("400.00")
    assert metrics["10:00"]["premium_capture"] == D("1")
    assert metrics["12:35"]["win_rate"] == D("0")


def test_slot_metrics_on_no_entries_is_empty():
    assert slot_metrics({}, slot_map={}) == {}
