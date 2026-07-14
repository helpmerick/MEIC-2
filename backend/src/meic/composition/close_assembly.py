"""Shared close-input assembly — ORD-09 broker-truth legs + stop-id correlation.

The one place that turns "an entry_id" into what `CloseEntry` needs to close
it: `LiveLeg`s built from the broker-reported fill legs (`LegBook`, ORD-09),
signed so CloseEntry's ledger cap (OWN-04) can read short/long correctly, and
`resting_stop_ids` correlated per side from the broker's own working orders
(never invented, never assumed).

`PanelCommands.close()` (the operator's manual Close, UC-14) and
`ProtectPosition`'s STP-04 AUTO-FLATTEN hook (live.py/paper.py `_on_filled`
wiring) both need exactly this assembly — it lives here once so neither
reinvents it (CLS-02: one close path; this is the one input-assembly for it).
"""
from __future__ import annotations

from decimal import Decimal

from meic.application.close_entry import LiveLeg
from meic.application.leg_book import LegBook
from meic.application.order_intent import side_of

# CLS-01: "aggressive cap per LEX fallback rules" — a nominal marketable
# buy/sell price. What actually makes the resulting order marketable is the
# broker adapter's own marketable_limit translation (STK-08 tick rules,
# LEX-04/05 aggressive-cap semantics) — this constant is only the one
# `close_price` value every close-input assembly in the codebase passes
# through (previously a bare `Decimal("0.05")` duplicated in panel_commands).
DEFAULT_CLOSE_PRICE = Decimal("0.05")


async def assemble_close_inputs(
    events: list, broker, entry_id: str, *, open_sides: set[str] | None = None,
) -> tuple[list[LiveLeg], dict[str, str]] | None:
    """Build `(live_legs, resting_stop_ids)` for closing `entry_id`.

    Returns `None` if the broker never reported any legs for this entry
    (ORD-09 hard refusal territory) — the caller decides how to alert; this
    function never invents a symbol.

    `open_sides`, when given, restricts assembly to legs on those sides (the
    manual Close command derives this from the live projection so an
    already-stopped/closed side is not re-touched). Omitted, every recorded
    leg for the entry is assembled — used by the STP-04 AUTO-FLATTEN hook,
    which has no cheaper "what's already closed" view than the recorded legs
    themselves and, per STP-04/CLS-02 (open item, see protect_position.py
    `_go_unprotected`), always closes the WHOLE entry.
    """
    book = LegBook.from_events(events)
    recorded = book.of(entry_id)
    if not recorded:
        return None
    legs = [
        LiveLeg(leg.symbol, leg.side, leg.role, -leg.qty if leg.role == "short" else leg.qty)
        for leg in recorded
        if open_sides is None or leg.side in open_sides
    ]
    # DCY-01/CLS-01 (2026-07-14): a side whose resting protective stop was
    # cancelled and replaced by an in-flight DCY-02 decay buyback (a working
    # `kind="decay"` limit order, application/decay_watcher.py `buyback()`)
    # must ALSO be treated as "the thing to replace" here -- otherwise this
    # entry_id has NO recorded stop for that side, `CloseEntry.close()` reads
    # that as "no resting stop" (CLS-01 (3)) and submits a DIRECT marketable
    # buy-to-close with no cancel/replace at all: two live buy-to-close orders
    # resting on the same short leg at once (the decay limit AND the fresh
    # close), each independently fillable -- a genuine double-fill-into-long
    # race on real money. Folding it into the SAME `stop_ids` correlation
    # routes it through CLS-01's existing race-safe `broker.replace()` path
    # instead, exactly like an ordinary resting stop. (Before this, DecayWatcher
    # was never constructed anywhere, so this interaction was unreachable.)
    def _for_entry(o) -> bool:
        return getattr(getattr(o, "intent", None), "entry_id", None) == entry_id

    def _is_resting_stop(o) -> bool:
        return getattr(o.intent, "order_type", None) == "stop_market"

    def _is_decay_buyback(o) -> bool:
        # Precise on purpose: `kind` alone is an unvalidated free-form string
        # on OrderIntent (application/order_intent.py) -- also requiring the
        # buy-to-close leg shape `decay_watcher.buyback()` always builds means
        # a hypothetical future reuse of the "decay" label for something else
        # cannot silently fold in here too.
        intent = o.intent
        legs = getattr(intent, "legs", None) or ()
        return (getattr(intent, "kind", None) == "decay"
                and bool(legs) and getattr(legs[0], "action", None) == "buy_to_close")

    working = [o for o in await broker.working_orders() if _for_entry(o)]
    # A genuine resting stop always wins over a decay buyback for the SAME
    # side -- structurally the two should never coexist (DCY-02(1) cancels
    # the stop before placing the buyback), but a deterministic preference
    # is safer than whichever `working_orders()` happens to list last.
    stop_ids = {side_of(o.intent.legs[0].right): o.order_id for o in working if _is_decay_buyback(o)}
    stop_ids.update({side_of(o.intent.legs[0].right): o.order_id for o in working if _is_resting_stop(o)})
    return legs, stop_ids
