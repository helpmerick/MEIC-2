"""TC-API-03 (EC-API-04 adopt/protect/block) and TC-STP-08 (STP-05/UC-12 stop
independence — the offline fake-broker portion). The bot-independent proof
proper belongs to the sandbox drill; here the FakeBroker stands in for broker
truth surviving a bot restart."""
import asyncio
from decimal import Decimal as D

from meic.application.reconcile import Reconcile, TrackedShort
from meic.domain.events import ReconciliationMismatch, StopReplaced
from meic.domain.ownership import Ownership, OwnershipLedger
from tests.harness.fake_broker import FakeBroker
from tests.harness.intents import stop_intent


class RecordingBroker:
    def __init__(self):
        self.submitted = []

    async def submit(self, order):
        self.submitted.append(order.idempotency_key)
        return f"ord-{len(self.submitted)}"

    async def cancel(self, id):
        return {"result": "cancelled"}


# --- TC-API-03: unknown position adopted, short protected first, entries block -

def test_tc_api_03_adopts_matching_position_protects_short_and_blocks_entries():
    """TC-API-03 (EC-API-04): a broker position matching the bot's own fills is
    adopted (not FOREIGN); a stopless short is protected before anything else;
    the logged mismatch blocks new entries until reconciled."""
    ledger = OwnershipLedger()
    ledger.apply_fill("SPXW_5990P", -1)  # the bot's own recorded fill

    # attribution first (OWN-03): matches the ledger -> OWNED (adopt), not FOREIGN
    assert ledger.classify("SPXW_5990P", broker_net=-1) is Ownership.OWNED
    assert ledger.classify("SPXW_FOREIGN", broker_net=-1) is Ownership.FOREIGN

    events: list = []
    plan = Reconcile(RecordingBroker(), events).plan(
        tracked_shorts=[TrackedShort("e1", "PUT", "SPXW_5990P",
                                     stop_order_id=None, stop_filled=False, stop_trigger=D("3.80"))],
        broker_working_order_ids=set(),
        mid_lex_sides=[], stale_entry_order_ids=[],
        position_mismatches=["adopted crash-orphan short SPXW_5990P"],
    )
    # the adopted stopless short is queued for protection (REC-04(3))
    assert plan.place_stops == [("e1", "PUT")]
    # and an unresolved mismatch blocks new entries (REC-02/RSK-03)
    assert plan.blocks_entries is True

    broker = RecordingBroker()
    asyncio.run(Reconcile(broker, events).execute(plan))
    # the naked short got its stop placed; the mismatch is on the log (gates entries)
    assert any(isinstance(e, StopReplaced) for e in events)
    assert any(isinstance(e, ReconciliationMismatch) for e in events)
    assert broker.submitted == ["stop:e1:PUT"]


# --- TC-STP-08: a resting stop survives a bot restart at the broker -----------

def test_tc_stp_08_working_stop_survives_bot_restart_with_unbroken_timestamp():
    """TC-STP-08 (fake portion): a resting stop placed at the broker is still
    working — with its placement timestamp intact — after the bot process is
    discarded and a fresh one reconnects. (Bot-independence proper is the
    sandbox drill, UC-12.)"""
    broker = FakeBroker()

    async def place_stop():
        return await broker.submit(stop_intent("PUT", "3.80", entry_id="e1"))

    stop_id = asyncio.run(place_stop())
    before = asyncio.run(broker.working_orders())
    assert [o.order_id for o in before] == [stop_id]
    placed_at = before[0].received_at  # broker-recorded, not part of the intent

    # simulate a bot disconnect: drop every bot-side reference, keep ONLY the
    # broker. A fresh bot reconnects and reads broker truth.
    del place_stop
    reconnected = asyncio.run(broker.working_orders())

    assert [o.order_id for o in reconnected] == [stop_id]         # still working
    assert reconnected[0].received_at == placed_at                 # unbroken timestamp
    assert reconnected[0].status == "WORKING"
