"""PNL-01 fee model — doc 06 `fee_model` ("per-contract fee table", default
"tastytrade SPX schedule (verify at build time)").

THE BUILD-TIME VERIFICATION GATE (non-negotiable, per PNL-01's own text
"verify at build time"): two independently observed, operator-confirmed real
broker days (2026-07-13 and 2026-07-10) both show the SAME shape -- one
entry, 4 legs opened (2 short, 2 long), 1 short stop buyback (close), 1 LEX
long sale (close) = 6 contracts -- and the SAME broker fee both days: $6.32.
`application/report_reconciler.py::_fee_cost_of`'s own docstring derives that
exact number from real broker rows via `value - net_value` and states it
explicitly: "Summed over the day's 6 own condor fills it lands on exactly
6.32, the true fee." This file reproduces that number from the CONFIGURED
fee table alone -- no broker call, no fabricated component.
"""
from decimal import Decimal as D

import pytest

from meic.config.fee_model import (
    DEFAULT_INDEX_OPTION_FEES,
    FeeModel,
    FeeModelRejected,
    fee_model_from_config,
    validate_fee_model,
)
from meic.domain.fees import fee_for_leg, fee_for_legs


class _Leg:
    """FilledLeg-shaped stand-in (`.role`, `.qty`) -- fee_for_legs only reads
    those two fields, so a bare stand-in is honest and doesn't drag in ORD-09
    symbol/price plumbing this test has no use for."""

    def __init__(self, role: str, qty: int = 1):
        self.role = role
        self.qty = qty


# --- THE GATE ------------------------------------------------------------
#
# `CondorFilled.fee` / `ShortStopped.fee` / `LongSold.fee` are PER-SHARE (they
# sit in the exact same formula as `net_credit`/`fill`/`recovery` --
# `domain/projection.py`'s `EntryProjection.pnl`), so `reporting/folds.py`'s
# `entry_dollars` can rescale the WHOLE entry by `* 100 * contracts` exactly
# once. The gate below therefore runs in two steps, matching that pipeline
# exactly: (1) verify FeeModel.per_contract_fee -- the REAL, tastytrade-
# sourced dollar schedule -- against the $6.32 broker truth; (2) verify
# fee_for_leg/fee_for_legs (what the event constructors actually call) is
# per_contract_fee / 100, so summing per-share and rescaling by
# `* 100 * contracts` at the reporting layer recovers that same $6.32.

def test_pnl01_build_time_gate_4_open_2_close_reproduces_6_32_exactly():
    """THE gate. 2026-07-13 AND 2026-07-10, both real, both own-scoped,
    both $6.32 on this exact shape. Must reproduce to the cent, from the
    REAL per-contract dollar schedule (FeeModel.per_contract_fee)."""
    model = FeeModel()  # config.fee_model default -- verified tastytrade schedule

    # 4 entry legs opened: 2 short (sell-to-open), 2 long (buy-to-open) -- one
    # condor, one contract per leg (the observed shape).
    entry_fee = sum(model.per_contract_fee(role=r, opening=True) for r in ("short", "short", "long", "long"))

    # 1 short stop buyback (close) -- buy-to-close.
    stop_fee = model.per_contract_fee(role="short", opening=False)

    # 1 LEX long sale (close) -- sell-to-close.
    lex_fee = model.per_contract_fee(role="long", opening=False)

    total = entry_fee + stop_fee + lex_fee

    assert total == D("6.32"), (
        f"PNL-01 build-time gate FAILED: fee model produced {total}, "
        f"real broker truth (2026-07-10 and 2026-07-13, both own-scoped) is 6.32 exactly. "
        f"(entry={entry_fee}, stop_close={stop_fee}, lex_close={lex_fee})"
    )


def test_pnl01_gate_holds_for_both_observed_days_independently():
    """Both 2026-07-10 and 2026-07-13 showed the identical shape and total --
    the fee table is deterministic and config-driven, so computing it twice
    over the same shape must be idempotent and identical, exactly like the
    two real days were."""
    model = FeeModel()

    def day_total():
        entry = sum(model.per_contract_fee(role=r, opening=True) for r in ("short", "short", "long", "long"))
        return (entry + model.per_contract_fee(role="short", opening=False)
                + model.per_contract_fee(role="long", opening=False))

    assert day_total() == day_total() == D("6.32")


def test_pnl01_gate_end_to_end_through_the_per_share_event_recording_path():
    """THE full pipeline proof: sum the PER-SHARE fee (`fee_for_leg`/
    `fee_for_legs` -- what the event constructors actually call) over the
    exact 4-open/2-close shape, exactly as `EntryProjection.fees` would
    accumulate it from CondorFilled + ShortStopped + LongSold, then rescale
    by `* 100 * contracts` exactly once (`reporting/folds.py::entry_dollars`
    -- 1 contract here) and confirm it STILL lands on $6.32. This is the
    actual code path the operator's live entry card runs, not just the
    component numbers in isolation."""
    model = FeeModel()
    entry_legs = [_Leg("short"), _Leg("short"), _Leg("long"), _Leg("long")]

    condor_filled_fee = fee_for_legs(model, entry_legs, opening=True)      # entry.fees +=
    short_stopped_fee = fee_for_leg(model, role="short", opening=False)   # entry.fees +=
    long_sold_fee = fee_for_leg(model, role="long", opening=False)       # entry.fees +=

    entry_fees_per_share = condor_filled_fee + short_stopped_fee + long_sold_fee
    real_dollars = entry_fees_per_share * D(100) * 1   # entry_dollars' own formula, 1 contract

    assert real_dollars == D("6.32")


# --- component sourcing (verified at build time against tastytrade's own schedule) --

def test_default_table_matches_tastytrade_published_schedule():
    """Every number here is cited in config/fee_model.py's module docstring
    against the actual tastytrade "Commissions & Fees" document (broad-based
    index options + trade-related fees + Single-Listed Exchange Proprietary
    Index Options Fees tables), last updated 2026-07-01 -- not invented."""
    model = FeeModel()
    assert model.commission_open == D("1.00")   # "$1.00 / contract to open"
    assert model.clearing_fee == D("0.10")      # "Clearing Fees - Options"
    assert model.regulatory_fee == D("0.02")    # "Options Regulatory Fee"
    assert model.exchange_fee("SPX") == D("0.60")   # Single-Listed Exchange table
    assert DEFAULT_INDEX_OPTION_FEES["RUT"] == D("0.18")
    assert DEFAULT_INDEX_OPTION_FEES["VIX"] == D("0.35")


def test_commission_applies_only_to_sell_to_open():
    """The part the flat "$1/contract to open" summary elides, verified
    against real broker fill rows (report_reconciler.py::_fee_cost_of's own
    docstring: a buy-to-open long and a close both derive to $0.72/contract;
    only the sell-to-open short carries the extra $1.00)."""
    model = FeeModel()
    # sell-to-open short: commission + clearing + orf + exchange
    assert model.per_contract_fee(role="short", opening=True) == D("1.72")
    # buy-to-open long: no commission
    assert model.per_contract_fee(role="long", opening=True) == D("0.72")


# --- open vs close asymmetry (pinned) -----------------------------------------

def test_a_closing_contract_is_never_charged_the_opening_commission():
    """A closing contract (buy-to-close a short, sell-to-close a long) is
    NEVER charged `commission_open` -- regardless of role."""
    model = FeeModel()
    assert model.per_contract_fee(role="short", opening=False) == D("0.72")
    assert model.per_contract_fee(role="long", opening=False) == D("0.72")
    # both closes are identical -- commission (the only asymmetric component)
    # never applies to a close, so role stops mattering once opening=False.
    assert (model.per_contract_fee(role="short", opening=False)
            == model.per_contract_fee(role="long", opening=False))


def test_opening_a_short_costs_strictly_more_than_opening_a_long():
    model = FeeModel()
    assert (model.per_contract_fee(role="short", opening=True)
            > model.per_contract_fee(role="long", opening=True))
    assert (model.per_contract_fee(role="short", opening=True)
            - model.per_contract_fee(role="long", opening=True)) == model.commission_open


def test_per_share_fee_is_the_real_dollar_fee_divided_by_the_contract_multiplier():
    """`fee_for_leg` (what every event constructor calls) must be
    `per_contract_fee / 100` -- the SAME rescaling `net_credit`/`fill`/
    `recovery` implicitly assume (`reporting/folds.py::entry_dollars` does
    `* 100 * contracts` exactly once, at the reporting layer). Recording the
    real per-contract dollar figure directly (or worse, pre-multiplying it by
    contracts here too) would double- or wildly over-count once that rescale
    ran -- exactly the regression this test pins."""
    model = FeeModel()
    for role, opening in (("short", True), ("long", True), ("short", False), ("long", False)):
        assert fee_for_leg(model, role=role, opening=opening) == (
            model.per_contract_fee(role=role, opening=opening) / D(100)
        )


def test_per_share_fee_is_contracts_invariant():
    """A real per-contract dollar fee is already linear in contracts (each
    contract pays its own commission/clearing/exchange fee) -- exactly the
    linearity `entry_dollars`'s `* 100 * contracts` assumes for every other
    per-share field. So the per-share fee this codebase RECORDS must NOT be
    scaled by contracts again here -- `fee_for_leg` takes no contracts
    parameter at all, by design."""
    import inspect

    from meic.domain.fees import fee_for_leg as _f
    assert "contracts" not in inspect.signature(_f).parameters


# --- config validation (UI-03, reject never clamp) ----------------------------

def test_validate_fee_model_rejects_negative_component():
    with pytest.raises(FeeModelRejected):
        validate_fee_model({"clearing_fee": "-0.10"})


def test_validate_fee_model_rejects_non_numeric():
    with pytest.raises(FeeModelRejected):
        validate_fee_model({"commission_open": "not-a-number"})


def test_validate_fee_model_rejects_negative_index_option_fee():
    with pytest.raises(FeeModelRejected):
        validate_fee_model({"index_option_fees": {"SPX": "-0.60"}})


def test_validate_fee_model_accepts_the_verified_defaults():
    validate_fee_model({
        "commission_open": "1.00", "clearing_fee": "0.10", "regulatory_fee": "0.02",
        "index_option_fees": {"SPX": "0.60"},
    })  # must not raise


def test_fee_model_from_config_falls_back_to_verified_defaults_when_absent():
    model = fee_model_from_config(None)
    assert model == FeeModel()


def test_fee_model_from_config_overrides_only_what_is_given():
    model = fee_model_from_config({"clearing_fee": "0.20"})
    assert model.clearing_fee == D("0.20")
    assert model.commission_open == D("1.00")  # untouched default


# --- UND-02/F3 (v1.86 /ES Stage 2): futures-option fees -----------------------
#
# tastytrade's FUTURES OPTIONS schedule is structurally different from the
# index-option table above: commission bills on BOTH open AND close, on
# EVERY leg (short or long alike) -- never short-open-only, never
# close-free. Published figures (2026-07 schedule, F3 build): $1.25/contract
# commission (open AND close) + $0.30 clearing + $0.01 NFA + the CME
# per-product exchange fee (ESTIMATED pending exact verification on the
# first real /ES trade, operator ruling).

def test_es_futures_option_bills_commission_on_both_open_and_close():
    """CRITICAL (UND-02/F3): unlike an index option, a /ES close is NOT
    commission-free -- both `per_contract_fee(opening=True)` and
    `per_contract_fee(opening=False)` bill the full futures-option total,
    for EVERY role (short and long alike)."""
    model = FeeModel()
    expected = model.futures_commission + model.futures_clearing_fee \
        + model.futures_nfa_fee + model.futures_exchange_fee("/ES")

    for role in ("short", "long"):
        opening_fee = model.per_contract_fee(role=role, opening=True, underlying="/ES")
        closing_fee = model.per_contract_fee(role=role, opening=False, underlying="/ES")
        assert opening_fee == expected, f"role={role} opening fee"
        assert closing_fee == expected, f"role={role} closing fee"
        assert opening_fee > 0 and closing_fee > 0
        # both directions bill the SAME total -- neither is commission-free,
        # the whole point of this test (a round trip pays commission TWICE).
        assert opening_fee == closing_fee


def test_es_futures_option_uses_the_published_component_figures():
    model = FeeModel()
    assert model.futures_commission == D("1.25")
    assert model.futures_clearing_fee == D("0.30")
    assert model.futures_nfa_fee == D("0.01")
    assert model.futures_exchange_fee("/ES") > 0   # ESTIMATE, validate on first real trade


def test_per_share_fee_divides_by_the_underlyings_own_multiplier_not_a_hardcoded_100():
    """FIX 3 (the masked 50%-fee bug): `per_share_fee` must be the exact
    inverse of the reporting layer's `* multiplier_of(entry)` re-scale, so
    `per_share_fee(U) * multiplier(U) == per_contract_fee(U)` for EVERY
    underlying. The original hardcoded ÷100 broke this for /ES (÷100 then ×50
    = HALF). Asserting `per_contract_fee` alone (the tests above) could not
    catch it -- this per-underlying divisor guard does."""
    from meic.config.fee_model import FeeModel as _FM
    from meic.domain.underlying import profile_for

    model = _FM()
    for underlying in ("SPX", "RUT", "/ES"):
        multiplier = profile_for(underlying).multiplier
        for role, opening in (("short", True), ("long", True),
                              ("short", False), ("long", False)):
            per_share = model.per_share_fee(role=role, opening=opening, underlying=underlying)
            per_contract = model.per_contract_fee(role=role, opening=opening, underlying=underlying)
            # per_share * multiplier must recover the real per-contract fee EXACTLY.
            assert per_share * multiplier == per_contract, (
                f"{underlying} {role} opening={opening}: "
                f"{per_share} * {multiplier} != {per_contract}")
    # /ES specifically: the divisor is 50, not 100 -- the direct guard on the
    # exact regression (a ÷100 would make this per_share half of what it is).
    es_ps = model.per_share_fee(role="short", opening=True, underlying="/ES")
    assert es_ps == D("3.06") / D("50")
    assert es_ps != D("3.06") / D("100")  # the halved-fee bug value, excluded


def test_es_is_detected_as_a_futures_option_underlying_never_a_hardcoded_string_check():
    model = FeeModel()
    assert model.is_futures_option("/ES") is True
    assert model.is_futures_option("SPX") is False
    assert model.is_futures_option("RUT") is False


def test_index_options_are_completely_unaffected_by_the_futures_fee_branch():
    """Do NOT change index-option fee behaviour: SPX/RUT stay close-free,
    commission still only on a sell-to-open short -- byte-identical to
    before the /ES futures-option branch existed."""
    model = FeeModel()
    for underlying in ("SPX", "RUT"):
        opening_short = model.per_contract_fee(role="short", opening=True, underlying=underlying)
        expected_opening_short = (model.clearing_fee + model.regulatory_fee
                                  + model.exchange_fee(underlying) + model.commission_open)
        assert opening_short == expected_opening_short
        # closing a short or long is commission-free for an index option,
        # regardless of underlying -- clearing + regulatory + exchange only.
        closing = model.per_contract_fee(role="short", opening=False, underlying=underlying)
        assert closing == model.per_contract_fee(role="long", opening=False, underlying=underlying)
        assert closing == model.clearing_fee + model.regulatory_fee + model.exchange_fee(underlying)


def test_validate_fee_model_accepts_and_rejects_futures_fee_fields():
    validate_fee_model({
        "futures_commission": "1.25", "futures_clearing_fee": "0.30",
        "futures_nfa_fee": "0.01", "futures_exchange_fees": {"/ES": "1.50"},
    })  # must not raise
    with pytest.raises(FeeModelRejected):
        validate_fee_model({"futures_commission": "-1.25"})
    with pytest.raises(FeeModelRejected):
        validate_fee_model({"futures_exchange_fees": {"/ES": "-1.50"}})


def test_fee_model_from_config_overrides_futures_fee_fields():
    model = fee_model_from_config({"futures_commission": "2.00",
                                   "futures_exchange_fees": {"/ES": "3.00"}})
    assert model.futures_commission == D("2.00")
    assert model.futures_exchange_fee("/ES") == D("3.00")
    assert model.futures_clearing_fee == D("0.30")  # untouched default
