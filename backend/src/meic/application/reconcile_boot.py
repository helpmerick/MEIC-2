"""Reconcile-on-boot — REC-02/03/04 + EC-API-04 + OWN-03/06.

Before a live bot may trade it must adopt broker truth. On boot:

  1. Restore the durable OWN ledger (REC-07 item 9). Empty ledger => the bot has
     no recorded fills, so EVERY broker position is FOREIGN.
  2. Read broker positions and classify each against the ledger (OWN-02/03/05/06):
       OWNED     -> the bot's own; manage normally (crash orphan adopted).
       FOREIGN   -> not the bot's. QUARANTINE: never stop it, never close it,
                    never count it. Critical alert. Even a naked short is
                    alert-only (OWN-03).
       SHORTFALL -> broker shows less than the ledger: SUSPEND, write down (OWN-06).
  3. Any FOREIGN/SHORTFALL is a ReconciliationMismatch: it BLOCKS NEW ENTRIES
     until the operator resolves it (REC-02 -> RSK-03). Protection and recovery
     still run — blocking entries is not blocking safety.
  4. Run the REC-04 stop triage over the tracked shorts (re-place genuinely
     missing stops, resume LEX, cancel stale entry orders), idempotency-keyed.

The bot never fires a compensating order automatically for a discrepancy it did
not create.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from meic.application.reconcile import Reconcile, TrackedShort
from meic.domain.events import Event, ReconciliationMismatch
from meic.domain.ownership import Ownership, OwnershipLedger


@dataclass(frozen=True)
class BootReconcileResult:
    adopted: list[str] = field(default_factory=list)      # symbols the bot owns
    foreign: list[str] = field(default_factory=list)      # quarantined (OWN-03)
    shortfall: list[str] = field(default_factory=list)    # suspended (OWN-06)
    stops_placed: list[tuple[str, str]] = field(default_factory=list)
    lex_resumed: list[tuple[str, str]] = field(default_factory=list)
    cancelled_orders: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    entries_blocked: bool = False


def _symbol_and_signed_qty(p: Any) -> tuple[str, int]:
    """Normalize a broker position. Handles the tastytrade shape (symbol,
    quantity, quantity_direction) and plain dicts used by fakes/tests."""
    if isinstance(p, dict):
        sym = str(p.get("symbol"))
        if "signed_qty" in p:
            return sym, int(p["signed_qty"])
        qty = int(p.get("quantity", 0))
        direction = str(p.get("quantity_direction", "") or "")
        return sym, (-qty if direction.lower().startswith("short") else qty)
    sym = str(getattr(p, "symbol", ""))
    qty = int(getattr(p, "quantity", 0) or 0)
    direction = str(getattr(p, "quantity_direction", "") or "")
    return sym, (-qty if direction.lower().startswith("short") else qty)


def _order_id(o: Any) -> str:
    for attr in ("order_id", "id"):
        v = getattr(o, attr, None)
        if v is not None:
            return str(v)
    if isinstance(o, dict):
        return str(o.get("order_id") or o.get("id"))
    return str(o)


def entries_blocked_by_reconcile(events: list[Event]) -> bool:
    """REC-02/RSK-03: an unresolved reconciliation mismatch on the durable log
    blocks NEW entries. Derived from the log, so it survives restart."""
    return any(isinstance(e, ReconciliationMismatch) for e in events)


async def reconcile_on_boot(
    *,
    broker,
    events: list,
    state,
    alerts,
    tracked_shorts: Iterable[TrackedShort] = (),
    stale_entry_order_ids: Iterable[str] = (),
    mid_lex_sides: Iterable[tuple[str, str]] = (),
) -> BootReconcileResult:
    ledger = OwnershipLedger.restore(state.own_ledger)  # durable (REC-07 item 9)

    adopted: list[str] = []
    foreign: list[str] = []
    shortfall: list[str] = []
    for position in await broker.positions():
        symbol, broker_net = _symbol_and_signed_qty(position)
        if not symbol:
            continue
        kind = ledger.classify(symbol, broker_net)
        if kind is Ownership.FOREIGN:
            foreign.append(symbol)
        elif kind is Ownership.SHORTFALL:
            shortfall.append(symbol)
        else:  # OWNED or SHARED — the bot's, manage at ledger quantities
            adopted.append(symbol)

    mismatches = [f"FOREIGN position {s}: quarantined — never stopped, closed or counted (OWN-03)"
                  for s in foreign]
    mismatches += [f"ledger shortfall on {s}: entry SUSPENDED, ledger written down (OWN-06)"
                   for s in shortfall]
    for detail in mismatches:
        alerts.alert("critical", detail)
    for s in shortfall:  # OWN-06: adopt broker truth, never fire compensating orders
        ledger.write_down_to(s, 0)

    working_ids = {_order_id(o) for o in await broker.working_orders()}
    rec = Reconcile(broker, events)
    plan = rec.plan(
        tracked_shorts=list(tracked_shorts),
        broker_working_order_ids=working_ids,
        mid_lex_sides=list(mid_lex_sides),
        stale_entry_order_ids=list(stale_entry_order_ids),
        position_mismatches=mismatches,
    )
    await rec.execute(plan)          # places missing stops, resumes LEX, cancels stale
    state.own_ledger = ledger.snapshot()  # persist the adopted truth

    return BootReconcileResult(
        adopted=adopted, foreign=foreign, shortfall=shortfall,
        stops_placed=list(plan.place_stops), lex_resumed=list(plan.run_lex),
        cancelled_orders=list(plan.cancel_orders), mismatches=mismatches,
        entries_blocked=plan.blocks_entries)
