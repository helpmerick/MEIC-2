"""EOD-01 v1.59 (operator-ratified, 2026-07-09 escalation): "Settlement cash
is BROKER-JOURNALED, never merely computed." This is the LIVE path's ONGOING
settlement capture -- distinct from `application/backfill.py`'s RPT-16
one-time import of PRE-journal broker history. It reuses that module's
Transaction-field readers (`_at_of` / `_symbol_of`) rather than duplicating
them: both modules read the identical tastytrade SDK Receive-Deliver shape
off the SAME `day_settlements(day)` broker method (see
adapters/tastytrade/adapter.py and its `end_date = day + 1` note -- a
settlement can post to the broker's ledger the day AFTER the trading day it
settles).

Attribution is by SYMBOL against THIS day's own `CondorFilled` leg book
(built from the durable event log via `domain.projection.fold` -- never an
operator-supplied order-id set like RPT-16's backfill, since the live path
already knows its own entries from the log). A settlement row is withheld
(never journaled, never guessed) when its symbol:
  - belongs to none of today's entries -- simply not ours (not counted
    ambiguous, mirrors backfill_day's identical "not ours to import" case), or
  - is claimed by MORE than one of today's own entries (never happens in the
    ordinary one-strike-per-entry case, but never guessed at either), or
  - carries an OWN-03 `ForeignDetected` quarantine -- broker lot-matching is
    ambiguous for that symbol, so its settlement cash is genuinely
    unattributable from the log alone.
The latter two are counted `ambiguous_settlements` in the return value.

Idempotent at transaction level -- `(at, symbol, sub_type)` against existing
`SettlementRecorded` events for `day` -- exactly RPT-16(5)'s rework: a
settlement can post on a LATER run, so a day-level "already captured"
short-circuit would silently drop it. Re-running always re-fetches and
appends only rows whose key isn't already present; a fully-captured day
becomes a true no-op.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Protocol

from meic.application.backfill import _at_of, _symbol_of
from meic.domain.events import Event, ForeignDetected, SettlementRecorded
from meic.domain.projection import fold


class SettlementBrokerFacade(Protocol):
    """The ONLY broker surface live settlement capture may touch -- one read
    fetch, the SAME method RPT-16's `BackfillBrokerFacade` also uses. No
    submit/replace/cancel method exists on this type at all."""

    async def day_settlements(self, day: str) -> list[Any]: ...


def _leg_book(events: list[Event], day: str) -> dict[str, str | None]:
    """symbol -> owning entry_id, for every leg of every `CondorFilled`
    recorded for `day`'s own entries (entry_id prefix match, folds.entry_day's
    convention). A symbol claimed by more than one of today's own entries
    maps to None -- itself ambiguous, never guessed."""
    book: dict[str, str | None] = {}
    for entry_id, entry in fold(events).entries.items():
        if entry_id.split("#", 1)[0] != day:
            continue
        for leg in entry.legs:
            if leg.symbol not in book:
                book[leg.symbol] = entry_id
            elif book[leg.symbol] not in (None, entry_id):
                book[leg.symbol] = None  # claimed by more than one entry today
    return book


async def capture_settlements(
    events: list[Event],
    broker_reads: SettlementBrokerFacade,
    day: str,
    *,
    now_iso: Callable[[], str],
    computed_settle: Callable[[str, str], Decimal | None] | None = None,
    alerts: Any = None,
) -> dict[str, Any]:
    """Fetch `day`'s Receive-Deliver settlement rows, attribute each to a bot
    entry, and append a `SettlementRecorded` for every unambiguous, not-yet
    -captured one.

    `computed_settle`, if supplied, is the bot's OWN cross-check settle value
    for `(symbol, day)` -- EOD-01 v1.59: "the bot's own settle computation is
    retained ONLY as a cross-check that alerts on mismatch." No such
    computation exists anywhere in this codebase today (checked:
    application/execute_entry.py, domain/risk.py; the only "_settle" in the
    tree is SimulatedBroker's PAPER fill bookkeeping in
    adapters/sim/simulated_broker.py, an unrelated concept -- it settles a
    SIMULATED fill, not an expiring position). So with no argument supplied
    (every current caller), this cross-check is a deliberate, honest no-op --
    never a fabricated computation invented to have something to compare.

    Returns `{"result": "captured", "captured": N, "ambiguous_settlements": M}`,
    the same shape as `application.backfill.backfill_day`'s result.
    """
    leg_book = _leg_book(events, day)
    foreign_symbols = {e.symbol for e in events if isinstance(e, ForeignDetected)}

    existing_keys = {(e.at, e.symbol, e.sub_type)
                     for e in events if isinstance(e, SettlementRecorded) and e.day == day}

    settlements = await broker_reads.day_settlements(day)
    captured = 0
    ambiguous = 0

    for t in settlements:
        symbol = _symbol_of(t)
        if symbol not in leg_book:
            continue  # not one of today's own entries -- not ours to capture

        entry_id = leg_book[symbol]
        if entry_id is None or symbol in foreign_symbols:
            # OWN-03 shared-symbol guard: claimed by >1 of our own entries, or
            # under a standing FOREIGN quarantine -- unattributable, never guessed.
            ambiguous += 1
            continue

        sub_type = getattr(t, "transaction_sub_type", None)
        sub_type_str = "" if sub_type is None else str(sub_type)
        at = _at_of(t)
        key = (at, symbol, sub_type_str)
        if key in existing_keys:
            continue

        net_value = getattr(t, "net_value", None)
        if net_value is None:
            # The broker's own settlement rows always carry net_value (see
            # backfill.py's SDK field-mapping notes) -- if one somehow
            # doesn't, there is nothing honest to record; skip rather than
            # fabricate a 0.
            continue
        raw_value = getattr(t, "value", None)
        value = Decimal(str(net_value))
        fee = None if raw_value is None else abs(value - Decimal(str(raw_value)))
        price = getattr(t, "price", None)
        quantity = getattr(t, "quantity", None)

        if computed_settle is not None and alerts is not None:
            expected = computed_settle(symbol, day)
            if expected is not None and expected != value:
                alerts.alert(
                    "critical",
                    f"EOD-01: settlement cross-check disagreement for {symbol} on {day}",
                    computed=str(expected), broker=str(value))

        events.append(SettlementRecorded(
            entry_id=entry_id, day=day, at=at, symbol=symbol, sub_type=sub_type_str,
            quantity=0 if quantity is None else int(quantity),
            price=None if price is None else Decimal(str(price)),
            value=value, fee=fee, source="tastytrade_receive_deliver"))
        existing_keys.add(key)
        captured += 1

    return {"result": "captured", "captured": captured, "ambiguous_settlements": ambiguous}
