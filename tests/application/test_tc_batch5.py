"""Fifth batch: RSK-02 (tombstone/absence), RSK-06 (paper structural), API-04
(cancel-rejected-because-filled routing). Real behavior against production code.
"""
from datetime import datetime
from decimal import Decimal as D

from meic.adapters.sim.simulated_broker import SimulatedBroker
from meic.application.cancel_routing import route_cancel_outcome
from meic.application.close_entry import VALID_INITIATORS
from meic.composition.paper import PaperComposition
from meic.config.validation import TOMBSTONE_KEYS, ConfigRejected, validate_config
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_clock import ET, FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
CLOCK = FakeClock(datetime(2026, 7, 6, 9, 30, tzinfo=ET))


# --- TC-RSK-02: the daily-loss feature is a tombstone -------------------------

def test_tc_rsk_02_tombstone_keys_rejected_and_no_daily_loss_path():
    """TC-RSK-02 (RSK-02 tombstone, absence test): the removed daily-loss config
    keys are REJECTED as unknown (spec 06 §169); no `daily_loss` close initiator
    exists anywhere; a heavy-loss day has no automatic halt/flatten path to fire."""
    # (1) each removed key is rejected — never silently ignored
    for key in ("daily_max_loss", "daily_loss_also_flatten", "risk_eval_seconds"):
        try:
            validate_config({key: 5000})
            raise AssertionError(f"{key} should be rejected as a tombstone key")
        except ConfigRejected as e:
            assert e.key == key and e.reason == "removed_rsk02"
    assert TOMBSTONE_KEYS == {"daily_max_loss", "daily_loss_also_flatten", "risk_eval_seconds"}

    # (2) architecture assertion: no `daily_loss` close initiator exists
    assert "daily_loss" not in VALID_INITIATORS

    # (3) the close procedure refuses a daily_loss initiator outright — there is
    #     no automatic loss-triggered close path to reach (deliberate, RSK-02)
    comp = PaperComposition(clock=CLOCK, ticks=SPX)
    import asyncio
    try:
        asyncio.run(comp.close.close(entry_id="e1", initiator="daily_loss",
                                     resting_stop_ids=[], live_legs=[], close_price=D("0")))
        raise AssertionError("a daily_loss close must not exist")
    except ValueError as e:
        assert "daily_loss" in str(e)


# --- TC-RSK-06: paper mode never instantiates the live adapter (structural) ---

def test_tc_rsk_06_paper_wiring_never_constructs_live_adapter(monkeypatch):
    """TC-RSK-06 (EC-RSK-04): in paper mode the live adapter is NOT instantiated.
    Structural (assert wiring, not flags): guard the live adapter constructor so
    it explodes if the paper path ever reaches it, then build + arm a paper day."""
    import meic.adapters.tastytrade.adapter as adapter_mod

    def guard(self, *a, **k):
        raise AssertionError("paper wiring constructed the live adapter (EC-RSK-04)")

    monkeypatch.setattr(adapter_mod.TastytradeAdapter, "__init__", guard)

    comp = PaperComposition(clock=CLOCK, ticks=SPX)
    comp.compose_and_arm(["10:00", "11:00"])   # exercises the full paper assembly

    assert isinstance(comp.broker, SimulatedBroker)
    assert type(comp.broker).__name__ != "TastytradeAdapter"

    # the paper composition module does not even import the live adapter
    import inspect

    import meic.composition.paper as paper_mod
    src = inspect.getsource(paper_mod)
    assert "TastytradeAdapter" not in src
    assert "tastytrade" not in src.lower()


# --- TC-API-04: cancel rejected because already filled routes as a fill --------

def test_tc_api_04_cancel_rejected_because_filled_routes_as_fill():
    """TC-API-04 (EC-API-06): a cancel the broker rejects because the order
    already filled is treated as a fill (broker truth) and routed to the fill
    handler; any other rejection is a genuine cancel failure."""
    assert route_cancel_outcome(rejected=True, reason="already_filled") == "route_as_fill"
    assert route_cancel_outcome(rejected=True, reason="ORDER_FILLED") == "route_as_fill"
    assert route_cancel_outcome(rejected=True, reason="order_not_cancellable") == "route_as_fill"
    assert route_cancel_outcome(rejected=False) == "cancelled"
    assert route_cancel_outcome(rejected=True, reason="rate_limited") == "cancel_failed"
