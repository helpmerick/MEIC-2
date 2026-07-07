"""TC-NLE-07 (NLE-05/UI-13): the preview returns per-side estimates for a
candidate pct when the market is open, UNAVAILABLE when closed/stale; changing
pct recomputes without submitting anything."""
from decimal import Decimal as D

from meic.application.nle_preview import SideInput, preview_net_loss
from meic.domain.nle import NetLossEstimate
from meic.domain.ticks import TickRung, TickTable

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))

# a generous, monotonic chain so interpolation succeeds for both pcts
_PUT_CHAIN = {D(str(k)): (D(str(k)) - D("5975")) * D("0.30") for k in range(5978, 6001, 2)}
_CALL_CHAIN = {D(str(k)): (D("6075") - D(str(k))) * D("0.30") for k in range(6050, 6073, 2)}

PUT = SideInput(chain_mids=_PUT_CHAIN, short_strike=D("5990"), short_fill=D("3.00"),
                long_strike=D("5980"), long_fill=D("1.20"))
CALL = SideInput(chain_mids=_CALL_CHAIN, short_strike=D("6060"), short_fill=D("2.00"),
                 long_strike=D("6070"), long_fill=D("0.80"))


def _preview(pct, *, market_open=True, data_fresh=True):
    return preview_net_loss(pct=D(pct), ticks=SPX, market_open=market_open,
                            data_fresh=data_fresh, put=PUT, call=CALL,
                            total_net_credit=D("4.00"))


def test_tc_nle_07_available_when_open_unavailable_when_closed_or_stale():
    out = _preview(95)
    assert out["available"] is True
    assert out["trigger"] == D("3.80")                 # 95% × 4.00, floored
    assert isinstance(out["put"], NetLossEstimate) and isinstance(out["call"], NetLossEstimate)

    # market closed -> UNAVAILABLE, never a fabricated number
    closed = _preview(95, market_open=False)
    assert closed == {"available": False, "reason": "market_closed"}

    # stale chain -> UNAVAILABLE
    stale = _preview(95, data_fresh=False)
    assert stale == {"available": False, "reason": "stale_chain"}


def test_tc_nle_07_changing_pct_recomputes_without_submitting():
    """The selector scrubbing from 95 → 150 recomputes the trigger (and thus the
    estimates); nothing is submitted — preview_net_loss has no broker seam."""
    at_95 = _preview(95)
    at_150 = _preview(150)
    assert at_95["trigger"] == D("3.80")
    assert at_150["trigger"] == D("6.00")              # recomputed for the new pct
    assert at_95["trigger"] != at_150["trigger"]

    # purity: the function signature exposes no broker/order sink to submit through
    import inspect
    params = set(inspect.signature(preview_net_loss).parameters)
    assert not params & {"broker", "gateway", "submit", "order"}
