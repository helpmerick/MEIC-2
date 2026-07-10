"""TC-OWN-06 (OWN-07): flatten-all closes every bot entry and leaves every
FOREIGN position untouched; no account-level close-all endpoint is ever called.
The recording broker below stands in for the fake broker's endpoint log."""
import asyncio
from decimal import Decimal as D

from meic.application.close_entry import CloseEntry, LiveLeg
from meic.application.flatten_all import FlattenAll, OpenEntry
from meic.domain.events import EntryClosed, SideClosed
from meic.domain.ownership import OwnershipLedger


class RecordingBroker:
    """Records every endpoint the flatten touches and traps any account-wide
    call (OWN-07): the bot must never invoke close-all/flatten-account."""

    def __init__(self) -> None:
        self.submitted_symbols: list[str] = []
        self.cancelled_ids: list[str] = []
        self.account_level_calls: list[str] = []

    async def submit(self, order: dict) -> str:
        self.submitted_symbols.append(order.legs[0].symbol)
        return f"ord-{len(self.submitted_symbols)}"

    async def cancel(self, id: str) -> dict:
        self.cancelled_ids.append(id)
        return {"result": "cancelled"}

    async def replace(self, id: str, new) -> str:
        # CLS-01 v1.50: CloseEntry replaces each short's resting stop in ONE
        # port call. Recorded the same way a bare cancel used to be — the old
        # stop is gone either way — plus the new order actually submitted.
        self.cancelled_ids.append(id)
        return await self.submit(new)

    def __getattr__(self, name: str):
        # any account-wide endpoint is a bug — record it and fail loudly
        if name in ("close_all", "close_all_positions", "flatten_account", "cancel_all"):
            def _trap(*a, **k):
                self.account_level_calls.append(name)
                raise AssertionError(f"account-level endpoint {name!r} called (OWN-07)")
            return _trap
        raise AttributeError(name)


def test_tc_own_06_flatten_leaves_foreign_untouched_no_account_close_all():
    ledger = OwnershipLedger()
    # the bot owns two entries' short legs; a FOREIGN naked short also exists
    for sym in ("SPXW_PUT_A", "SPXW_CALL_A", "SPXW_PUT_B", "SPXW_CALL_B"):
        ledger.apply_fill(sym, -1)
    # FOREIGN: broker shows a position the ledger knows nothing about -> exit cap 0
    assert ledger.cap_exit_qty("FOREIGN_SPY", 5) == 0

    broker = RecordingBroker()
    events: list = []
    flat = FlattenAll(CloseEntry(broker, events, ledger))

    book = [
        OpenEntry("e1",
                  [LiveLeg("SPXW_PUT_A", "PUT", "short", -1),
                   LiveLeg("SPXW_CALL_A", "CALL", "short", -1)],
                  D("0.05"), resting_stop_ids={"PUT": "stopA1", "CALL": "stopA2"}),
        OpenEntry("e2",
                  [LiveLeg("SPXW_PUT_B", "PUT", "short", -1),
                   LiveLeg("SPXW_CALL_B", "CALL", "short", -1)],
                  D("0.05"), resting_stop_ids={"PUT": "stopB1P", "CALL": "stopB1C"}),
    ]
    asyncio.run(flat.flatten(book))

    # (1) every bot entry closed via the single manual_flatten initiator
    closed = {e.entry_id: e.initiator for e in events if isinstance(e, EntryClosed)}
    assert closed == {"e1": "manual_flatten", "e2": "manual_flatten"}
    assert sum(isinstance(e, SideClosed) for e in events) == 4  # all four sides closed

    # (2) FOREIGN position never touched; only the bot's own symbols submitted
    assert "FOREIGN_SPY" not in broker.submitted_symbols
    assert set(broker.submitted_symbols) == {
        "SPXW_PUT_A", "SPXW_CALL_A", "SPXW_PUT_B", "SPXW_CALL_B"}

    # (3) resting stops replaced (CLS-01 v1.50); no account-level close-all
    # endpoint ever called
    assert set(broker.cancelled_ids) == {"stopA1", "stopA2", "stopB1P", "stopB1C"}
    assert broker.account_level_calls == []
