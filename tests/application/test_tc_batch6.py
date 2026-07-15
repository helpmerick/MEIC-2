"""Sixth batch: ENT-04 (ENT-06 optional filters), RSK-05 (RSK-07 clock drift).
Real decisions against the entry-gate module."""
import inspect
from decimal import Decimal as D

import pytest

from meic.application import protect_position, watchdog
from meic.application.entry_gates import (
    SKIP_LEVEL,
    FilterSnapshot,
    clock_drift_blocks_entry,
    evaluate_filters,
)


# --- TC-ENT-04: ENT-06 optional filters --------------------------------------

def test_tc_ent_04_vix_above_max_and_blackout_skip_info_level():
    """TC-ENT-04 (ENT-06/EC-ENT-10): VIX above vix_max ⇒ skip (info-level only);
    a blackout date ⇒ skip; filters off or satisfied ⇒ no skip."""
    # VIX above the max -> skip, and the skip is info-level (not an alarm)
    r = evaluate_filters(FilterSnapshot(vix=D("25"), vix_max=D("20"), date="2026-07-06"))
    assert r == "vix_above_max"
    assert SKIP_LEVEL[r] == "info"

    # explicit blackout date (e.g. FOMC) -> skip
    r = evaluate_filters(FilterSnapshot(date="2026-07-29", skip_dates=("2026-07-29",)))
    assert r == "blackout_date" and SKIP_LEVEL[r] == "info"

    # each filter independently toggleable: VIX filter OFF (vix_max None) never skips
    assert evaluate_filters(FilterSnapshot(vix=D("99"), vix_max=None)) is None

    # all filters satisfied -> no skip
    assert evaluate_filters(FilterSnapshot(
        vix=D("15"), vix_max=D("20"), date="2026-07-06", skip_dates=("2026-07-29",),
        total_credit=D("4.00"), min_total_credit=D("2.00"))) is None

    # below the minimum total credit -> skip (STK-06)
    assert evaluate_filters(FilterSnapshot(
        total_credit=D("1.50"), min_total_credit=D("2.00"))) == "below_min_credit"


def test_cal_05_dynamic_blackout_reasons_classify_info_level():
    """CAL-05 (v1.71): `evaluate_filters` returns DYNAMIC `blackout:<label>`
    reasons whose label varies per tag, so they can never be exact-match
    SKIP_LEVEL keys -- the lookup must classify them via the prefix (info,
    identical to the static `blackout_date` beside them), and any OTHER
    unknown reason must still raise, never guess a level."""
    r = evaluate_filters(FilterSnapshot(date="2026-07-15", blackout_label="FOMC"))
    assert r == "blackout:FOMC"
    assert SKIP_LEVEL[r] == "info"                       # the dynamic reason itself
    assert SKIP_LEVEL["blackout:any label at all"] == "info"
    with pytest.raises(KeyError):
        SKIP_LEVEL["some_future_unclassified_reason"]    # never silently guessed


# --- TC-RSK-05: RSK-07 clock drift -------------------------------------------

def test_tc_rsk_05_clock_drift_blocks_entries_management_continues():
    """TC-RSK-05 (RSK-07/EC-RSK-06): clock drift beyond max blocks new entries;
    existing management (resting stops) is unaffected — it lives at the broker."""
    assert clock_drift_blocks_entry(drift_ms=500, max_drift_ms=250) is True   # blocks
    assert clock_drift_blocks_entry(drift_ms=-500, max_drift_ms=250) is True  # either sign
    assert clock_drift_blocks_entry(drift_ms=100, max_drift_ms=250) is False  # within tolerance

    # structural: the drift gate is entry-only. The protection/watchdog paths do
    # not consult it, so management continues regardless of drift (RSK-07).
    assert "clock_drift_blocks_entry" not in inspect.getsource(protect_position)
    assert "clock_drift_blocks_entry" not in inspect.getsource(watchdog)
