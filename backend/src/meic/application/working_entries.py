"""Working-entry order registry — the CLS-03 seam (2026-07-11 wiring).

`ExecuteEntryAttempt`'s reprice ladder is the ONLY code that ever knows a
pre-fill entry's broker order id: it lives in a ladder-local variable and is
journaled nowhere (CondorProposed carries strikes only). CLS-03 — "close" of
a PENDING/WORKING entry means cancel the entry order (UC-14/TC-CLS-02) —
therefore needs this seam:

  * the ladder RECORDS its current working order id here while the order
    works (and re-records after every reprice, since a replace mints a new
    id), then CLEARS it when the attempt ends however it ends;
  * the panel's cancel path (PanelCommands -> ManualClose.cancel_working)
    reads the id and RAISES the cancel flag FIRST, which the ladder checks so
    it stands down instead of repricing — a replace racing the cancel is at
    best a spurious error and at worst (the live adapter's cancel-then-submit
    replace fallback) a RESUBMISSION of the very order the operator just
    cancelled.

Pure in-memory state, no I/O. Deliberately NOT durable: a working entry
order cannot outlive the process that is laddering it — after a crash the
boot reconcile's stale-entry-order path (ORD-06) owns the cleanup instead.
"""
from __future__ import annotations


class WorkingEntryOrders:
    def __init__(self) -> None:
        self._orders: dict[str, str] = {}
        self._cancel_requested: set[str] = set()

    def record(self, entry_id: str, order_id) -> None:
        """The ladder's current working order for `entry_id` (re-recorded on
        every reprice — a replace mints a new broker order id)."""
        self._orders[entry_id] = str(order_id)

    def clear(self, entry_id: str) -> None:
        """The attempt ended (fill, skip, cancel or error): nothing is
        working for this entry any more, and any stand-down flag is spent."""
        self._orders.pop(entry_id, None)
        self._cancel_requested.discard(entry_id)

    def get(self, entry_id: str) -> str | None:
        return self._orders.get(entry_id)

    def request_cancel(self, entry_id: str) -> None:
        """CLS-03: the operator cancelled this WORKING entry. The broker-side
        cancel itself is ManualClose.cancel_working's job (the one ratified
        path); this flag only tells the ladder to stand down."""
        self._cancel_requested.add(entry_id)

    def cancel_requested(self, entry_id: str) -> bool:
        return entry_id in self._cancel_requested

    def order_ids(self) -> set[str]:
        """Every currently-working entry order id — merged into the EOD-03
        sweep's OWN-order set (these ids are journaled nowhere else)."""
        return set(self._orders.values())
