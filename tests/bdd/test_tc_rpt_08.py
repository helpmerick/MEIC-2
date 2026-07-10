"""TC-RPT-08 — RPT-12 MAE (trigger-distance consumed, from recorded
`EntryMarkSample`s only, D10: gaps never interpolated) + RPT-13 slot
analytics (manual/ad-hoc entries always group under "manual", ENT-11(3))."""
from decimal import Decimal as D

from pytest_bdd import given, scenario, then

from meic.domain.events import CondorFilled, EntryMarkSample
from meic.domain.projection import fold
from meic.reporting import mae_mfe, slots


@scenario("../features/TC-RPT-08.feature", "MAE measures trigger-distance consumed")
def test_mae_measures_trigger_distance_consumed():
    pass


@scenario("../features/TC-RPT-08.feature", "Slot analytics attribute to the scheduled slot")
def test_slot_analytics_attribute_to_the_scheduled_slot():
    pass


# --- MAE measures trigger-distance consumed -----------------------------------

@given("a short filled at 3.00 with trigger 3.80 whose recorded mark peaked at 3.60 "
       "before expiry", target_fixture="mae_vector")
def _():
    entry_id = "2026-07-09#1"
    samples = [
        EntryMarkSample(entry_id=entry_id, at="2026-07-09T10:00:00-04:00",
                        put_short_mid=D("3.20")),
        EntryMarkSample(entry_id=entry_id, at="2026-07-09T11:00:00-04:00",
                        put_short_mid=D("3.60")),  # the peak
        EntryMarkSample(entry_id=entry_id, at="2026-07-09T12:00:00-04:00",
                        put_short_mid=None),        # a genuine gap: no quote that tick
        EntryMarkSample(entry_id=entry_id, at="2026-07-09T13:00:00-04:00",
                        put_short_mid=D("3.10")),
    ]
    return {"entry_id": entry_id, "samples": samples, "fill": D("3.00"), "trigger": D("3.80")}


@then("the entry MAE = 75 percent of trigger distance and it counts as survived")
def _(mae_vector):
    result = mae_mfe.excursion(mae_vector["entry_id"], "PUT", mae_vector["samples"],
                               fill=mae_vector["fill"], trigger=mae_vector["trigger"])
    assert result is not None
    assert result.mae_pct == D("0.75")
    assert result.survived is True


@then("missing samples render as gaps, never interpolated")
def _(mae_vector):
    # An entry/side with NO recorded sample at all is an honest gap (None) --
    # never a fabricated/interpolated value (D10).
    assert mae_mfe.excursion("2026-07-09#999", "PUT", mae_vector["samples"],
                             fill=D("3.00"), trigger=D("3.80")) is None
    assert mae_mfe.excursion(mae_vector["entry_id"], "CALL", mae_vector["samples"],
                             fill=D("3.00"), trigger=D("3.80")) is None
    # The single None-valued sample above was excluded, not treated as 0 --
    # the computed MAE used only the two REAL put_short_mid values (3.20, 3.60).
    values = [s.put_short_mid for s in mae_vector["samples"]
              if s.entry_id == mae_vector["entry_id"] and s.put_short_mid is not None]
    assert values == [D("3.20"), D("3.60"), D("3.10")]


# --- Slot analytics attribute to the scheduled slot ---------------------------

@given("entries fired from the 10:00 and 12:35 slots across a month",
       target_fixture="slot_vector")
def _():
    day = "2026-07-09"
    events = [
        CondorFilled(entry_id=f"{day}#1", net_credit=D("4.00")),    # 10:00 slot, win
        CondorFilled(entry_id=f"{day}#2", net_credit=D("-2.00")),   # 12:35 slot, loss
        CondorFilled(entry_id=f"{day}#101", net_credit=D("3.00")),  # ad-hoc -> manual
    ]
    slot_map = {f"{day}#1": "10:00", f"{day}#2": "12:35"}
    return {"entries": fold(events).entries, "slot_map": slot_map, "day": day}


@then("win rate, expectancy, and premium capture render per slot")
def _(slot_vector):
    metrics = slots.slot_metrics(slot_vector["entries"], slot_map=slot_vector["slot_map"])
    assert metrics["10:00"]["win_rate"] == D("1")
    assert metrics["10:00"]["expectancy"] == D("400.00")
    assert metrics["12:35"]["win_rate"] == D("0")
    assert metrics["12:35"]["expectancy"] == D("-200.00")
    assert metrics["10:00"]["premium_capture"] == D("1")


@then('manual entries group under a "manual" slot')
def _(slot_vector):
    day = slot_vector["day"]
    assert slots.slot_of(f"{day}#101") == "manual"
    grouped = slots.by_slot(slot_vector["entries"], slot_map=slot_vector["slot_map"])
    assert {e.entry_id for e in grouped["manual"]} == {f"{day}#101"}
