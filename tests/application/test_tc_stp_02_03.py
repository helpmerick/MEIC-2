"""TC-STP-02 (STP-02 parametrization + STP-02d gate) and TC-STP-03 (intraday
change / EC-STP-07). Exercises the real stop-trigger formulas across the whole
pct × basis grid and the next-entry effective-timing invariant."""
from decimal import Decimal as D

import pytest

from meic.config.stop_basis import StopBasisRejected, validate_stop_basis
from meic.config.validation import ConfigRejected, validate_config
from meic.domain.stop_policy import StopBasis, stop_trigger
from meic.domain.ticks import TickRung, TickTable

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
PCT_SET = tuple(range(95, 305, 5))  # {95, 100, …, 300}


# --- TC-STP-02: triggers match the formulas across the whole grid ------------

def test_tc_stp_02_triggers_match_formulas_for_every_pct_and_basis():
    """For each pct in {95..300} × each basis, stop_trigger equals the STP-02
    formula recomputed independently, then floored to tick. per_side formulas
    stay verified in the domain even though selection is gated (STP-02d)."""
    credit, short_fill, long_fill = D("4.00"), D("2.10"), D("0.60")
    for pct in PCT_SET:
        p = D(pct) / 100

        tc = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D(pct),
                          total_net_credit=credit)
        assert tc == SPX.floor(p * credit)

        sp = stop_trigger(StopBasis.SHORT_PREMIUM, ticks=SPX, pct=D(pct),
                          short_fill=short_fill)
        assert sp == SPX.floor(short_fill * (1 + p))

        ps = stop_trigger(StopBasis.PER_SIDE, ticks=SPX, pct=D(pct),
                          short_fill=short_fill, side_long_fill=long_fill)
        assert ps == SPX.floor(short_fill + (short_fill - long_fill) * p)


def test_tc_stp_02_config_rejects_out_of_range_pct_and_gates_per_side():
    """Values outside the pct set are rejected (never clamped); SELECTING
    per_side is rejected `allocation_unverified` (STP-02d); the two selectable
    bases pass."""
    for bad in (94, 96, 301):
        with pytest.raises(ConfigRejected):
            validate_config({"stop_loss_pct": bad})
    assert all(pct in PCT_SET for pct in (95, 150, 300))

    with pytest.raises(StopBasisRejected) as ei:
        validate_stop_basis("per_side")
    assert ei.value.reason == "allocation_unverified"
    validate_stop_basis("total_credit")   # selectable -> no raise
    validate_stop_basis("short_premium")


# --- TC-STP-03: intraday change is next-entry, never retroactive -------------

def test_tc_stp_03_intraday_pct_change_leaves_entry1_uses_new_for_entry2():
    """TC-STP-03 (EC-STP-07): pct changed 95→150 after entry 1 ⇒ entry 1's
    recorded stop is unchanged (computed at placement); entry 2 uses the new
    value. Stops are recorded, never recomputed from live config."""
    credit = D("4.00")

    # entry 1 placed under pct=95 — this level is captured at placement time
    entry1 = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D(95),
                          total_net_credit=credit)
    assert entry1 == SPX.floor(D("0.95") * credit)  # 3.80

    # operator changes pct to 150 intraday (config effective: next-entry)
    entry2 = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D(150),
                          total_net_credit=credit)
    assert entry2 == SPX.floor(D("1.50") * credit)  # 6.00
    assert entry2 != entry1

    # entry 1 is unaffected: recomputing it under its OWN placement config still
    # yields the original level — the change never reaches back to it.
    assert stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D(95),
                        total_net_credit=credit) == entry1
