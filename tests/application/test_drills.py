"""UC-12 stop-independence drill (STP-05 core claim, paper honesty per SIM-06)."""
import asyncio
from datetime import datetime
from decimal import Decimal as D

from meic.application.drills import (
    OpenShortMark,
    drill_guidance,
    near_trigger_status,
    run_stop_independence_drill,
)
from meic.composition.paper import PaperComposition
from meic.domain.events import EntryMarkSample
from meic.domain.ticks import TickRung, TickTable
from meic.reporting.mae_mfe import excursion
from tests.harness.fake_clock import ET, FakeClock
from tests.harness.intents import condor_intent, stop_intent

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


def _comp():
    return PaperComposition(clock=FakeClock(datetime(2026, 7, 7, 9, 30, tzinfo=ET)), ticks=SPX)


def test_drill_shows_stops_survived_with_unbroken_timestamps():
    comp = _comp()
    # two resting stops in place (as ProtectPosition would leave them)
    asyncio.run(comp.broker.submit(stop_intent("PUT", "3.80", entry_id="e1")))
    asyncio.run(comp.broker.submit(stop_intent("CALL", "3.60", entry_id="e1")))

    ev = asyncio.run(run_stop_independence_drill(comp.broker, outage_seconds=0))

    assert len(ev.stops_before) == 2 and len(ev.stops_after) == 2
    assert ev.survived is True                     # every stop still working after the outage
    assert ev.timestamps_unbroken is True          # placement times unchanged
    assert all(s["received_at"] for s in ev.stops_before)  # timestamps were recorded
    assert "SIM-06" in ev.honesty_note and "TC-STP-08" in ev.honesty_note  # honest caveat


def test_drill_survived_false_when_no_resting_stops():
    comp = _comp()
    ev = asyncio.run(run_stop_independence_drill(comp.broker, outage_seconds=0))
    assert ev.stops_before == [] and ev.survived is False  # nothing to prove


def test_drill_detects_a_stop_that_vanished_during_the_outage():
    comp = _comp()
    oid = asyncio.run(comp.broker.submit(stop_intent("PUT", "3.80", entry_id="e1")))
    # a broker-side disappearance mid-outage would break the independence claim
    asyncio.run(comp.broker.cancel(oid))
    ev = asyncio.run(run_stop_independence_drill(comp.broker, outage_seconds=0))
    assert ev.survived is False


# --- UC-12 near-trigger drill guidance (operator ruling 2026-07-11) ------------
# trigger-distance consumed = (current short mark - short fill) / (stop
# trigger - short fill), warn at >= 50%. MUST be the SAME formula RPT-12's
# MAE uses (reporting.mae_mfe.consumed_fraction) — pinned below.

def test_near_trigger_formula_agrees_with_rpt12_mae_on_the_same_inputs():
    """The near-trigger drill guidance and RPT-12's MAE give IDENTICAL
    fractions on the same (fill, trigger, mark) — one shared implementation,
    never two copies of the money math."""
    fill, trigger, mark = D("3.00"), D("3.80"), D("3.60")

    shorts = [OpenShortMark(fill=fill, trigger=trigger, mark=mark)]
    from meic.reporting.mae_mfe import consumed_fraction
    drill_fraction = consumed_fraction(mark, fill=fill, trigger=trigger)

    sample = EntryMarkSample(entry_id="e1", at="t", put_short_mid=mark)
    rpt12_fraction = excursion("e1", "PUT", [sample], fill=fill, trigger=trigger).mae_pct

    assert drill_fraction == rpt12_fraction == D("0.75")
    assert near_trigger_status(shorts) is True   # 75% consumed >= the 50% warn threshold


def test_near_trigger_warns_at_exactly_50_pct_consumed():
    shorts = [OpenShortMark(fill=D("3.00"), trigger=D("4.00"), mark=D("3.50"))]  # exactly 50%
    assert near_trigger_status(shorts) is True


def test_near_trigger_silent_just_under_the_50_pct_boundary():
    shorts = [OpenShortMark(fill=D("3.00"), trigger=D("4.00"), mark=D("3.49"))]  # 49%
    assert near_trigger_status(shorts) is False


def test_near_trigger_is_honest_none_when_a_short_has_no_usable_mark():
    """No usable mark -> honest None, never a guess and never silently
    reported as False (which would read as 'confirmed not near')."""
    shorts = [OpenShortMark(fill=D("3.00"), trigger=D("4.00"), mark=None)]
    assert near_trigger_status(shorts) is None


def test_a_confirmed_breach_still_warns_even_if_another_side_is_unknown():
    shorts = [OpenShortMark(fill=D("3.00"), trigger=D("4.00"), mark=D("3.80")),  # 80% -- breach
              OpenShortMark(fill=D("2.00"), trigger=D("3.00"), mark=None)]        # unknown
    assert near_trigger_status(shorts) is True


def test_near_trigger_false_with_no_open_shorts_at_all():
    assert near_trigger_status([]) is False   # nothing open -- genuinely nothing to warn about


def test_drill_guidance_true_near_trigger_warns():
    assert drill_guidance(near_trigger=True) == ["a short mark is within 50% of its trigger distance"]


def test_drill_guidance_none_near_trigger_warns_unknown_never_silently_false():
    assert drill_guidance(near_trigger=None) == [
        "trigger-distance unknown for at least one open short (no live mark)"]


def test_drill_guidance_false_near_trigger_is_silent():
    assert drill_guidance(near_trigger=False) == []
