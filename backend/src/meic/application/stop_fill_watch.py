"""Live stop-fill catch-up — EC-STP-06, run every health tick, not just boot.

THE GAP (found investigating the 2026-07-10 11:56 incident — the C7565 stop,
order 482621556, filled at 11:56:15 ET and the bot never noticed): a resting
stop CAN fill while the bot is up and running, and nothing notices. `reconcile.py`
already implements the EC-STP-06/REC-04 triage precisely ("a short with no
resting stop: did the stop fill? -> run LEX") and `reconcile_boot.py` already
calls it -- but only from `_boot_reconcile()`, on process start/`/broker/
connect`. The health tick that runs every ~60s the rest of the day never
re-ran it. So the ONLY way a live stop fill got noticed was a restart -- the
exact "pieces exist, nothing drives them" pattern already found in RSK-04
(the day loop had no risk ceiling wired), the day supervisor (ENT-10, no
auto-restart wiring), and TPF/TPT (the monitor existed wired to nothing).
This is the fourth member of that class.

Fix: reuse the SAME `Reconcile`/`TrackedShort` frame `reconcile_boot.py`
already drives at boot (operator ruling 2026-07-10: "the ratified frame for
catch-up, not an ad-hoc scan") -- just build its `tracked_shorts` input from
the CURRENT event log + broker truth on every tick, not only at boot. Calling
`Reconcile.plan()`/`.execute()` synthesizes the missed `ShortStopped` (with
slippage, EC-STP-06); this module's own job is to actually DRIVE `RecoverLong`
for the stopped side afterward (nothing in the codebase, in ANY mode, ever
called `RecoverLong.recover()` reactively before this; even paper's demo
runtime calls it from a hardcoded script step, not from an event reaction).
`plan.run_lex` is stripped before `execute()` here: its only effect is
appending a `LongSaleStarted` marker, and `RecoverLong.recover()` already
journals that marker itself when -- and only when -- a ladder genuinely
starts, so leaving both in would duplicate the activity-feed line
mid-incident (2026-07-10 review finding 2). Boot's `reconcile.py` semantics
are untouched.

Scope, deliberately narrow: this tick answers ONLY "did an open short's
placed stop fill" (EC-STP-06). A short with NO placed stop at all, or one
cancelled by someone other than the bot (REC-04(2)/(3)), stays boot-only for
now -- extending live-tick coverage to those is a larger change (full OWN-09
external-close disambiguation, see `_resolve_by_symbol` below) not asked for
here.

OWN standdown (operator ruling 2026-07-10): a stop fill caught up LATE may be
old news by the time this tick notices it -- the operator could have sold the
orphaned long directly at the broker in the meantime. Before any LEX hand-off
for a CAUGHT-UP fill, `_long_still_held` confirms the long is still actually
there; if not, the side is still recorded `ShortStopped` (honest — the stop
DID fire), but LEX is never invoked and no order is submitted (OWN-09/10:
an operator action at the broker stands down automation, no compensating
order) — an info alert notes the disposition.

FLAGGED LATENT HAZARDS (on record for the wiring slice; both components are
NOT wired into the live composition today, no code change here):
  * watchdog.py `escalate()` journals `ShortStopped` BEFORE its marketable
    buy-back confirms — if the watchdog is ever wired live, a tick landing in
    that window would see the side pending and could start a LEX ladder while
    the buy-back is still working (the guards check the LONG's orders, never
    the short's buy-back).
  * decay_watcher.py's buyback fill (RESOLVED, STP-08a v1.61): the buyback's
    broker order id is now journaled AT PLACEMENT (`DecayBuybackPlaced`,
    decay_watcher.buyback()), and this module's detection pass recognises a
    fill matching that id — up-front per side AND inside the symbol fallback —
    and classifies the side SIDE_CLOSED_DECAY (the exact ShortStopped
    initiator="decay" + EntryClosed initiator="decay" shape `complete()`
    journals; long left to expire, DCY-03) instead of misreading it as a
    stop-out. See `build_tracked_shorts` / `detect_and_recover_stop_fills`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

from meic.domain.events import DecayBuybackPlaced, EntryClosed, LexOrderPlaced, ShortStopped, StopPlaced
from meic.domain.projection import fold

from .execute_entry import _fill_matches
from .reconcile import Reconcile, TrackedShort
from .reconcile_boot import _order_id, _symbol_and_signed_qty

# R3-F3: after this many CONSECUTIVE quote-guard deferrals for one side, raise
# a one-time critical alert (at the 60s health cadence, 5 ticks ~= 5 minutes
# of an orphaned long we cannot price a sale for). Reached only by the NO-BID
# case since v1.62 (a side with a bid falls back to LEX-05 at the bounded
# window instead — see `_try_recover`); a bid-less side keeps deferring — a
# price is never guessed — and the alert IS the fix.
QUOTE_DEFERRAL_ALERT_TICKS = 5

# LEX-02 (v1.62 STP-08a reconciliation): "Invalid quote ⇒ skip to LEX-05
# fallback after `config.lex_quote_wait_seconds`" — doc 06 default 5 (range
# 1–30). The bounded deferral window for a side whose quote is stale-invalid
# but still carries a BID.
LEX_QUOTE_WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class StaleQuote:
    """STP-08a (v1.62) invalid-quote routing: the long HAS a bid on record,
    but the snapshot is too old to price a ladder (DAT-02 staleness = LEX-02's
    age criterion). Distinct from `None` (NO bid at all): once the bounded
    `lex_quote_wait_seconds` deferral window lapses, a StaleQuote can still
    price the LEX-05 marketable-at-bid fallback — "a naked-side recovery never
    waits indefinitely" — while a None cannot price ANY order and keeps
    deferring with the critical alert standing (a price is never invented).

    `quote` is a recover_long.Quote; `intrinsic` the LEX-04 floor input
    computed off the same (stale) snapshot — the honest best available."""

    quote: object
    intrinsic: Decimal


@dataclass(frozen=True)
class NoBidFloor:
    """EC-LEX-08 (v1.63): the long's strike is UNMARKED (no bid at all) but a
    DAT-02-fresh underlying mark exists, so the LEX-04 intrinsic floor is
    computable. `_try_recover` rests an intrinsic-floor sell (one-time critical
    alert on placement); a usable quote arriving later supersedes it."""

    intrinsic: Decimal


def _stop_specs(events) -> dict[tuple[str, str], StopPlaced]:
    """The latest recorded `StopPlaced` per (entry_id, side) -- carries the
    trigger (needed for EC-STP-06 slippage) and, for every stop placed since
    v1.60, the broker's own order id (precise matching; see
    `_resolve_by_order_id`)."""
    specs: dict[tuple[str, str], StopPlaced] = {}
    for e in events:
        if isinstance(e, StopPlaced):
            specs[(e.entry_id, e.side)] = e
    return specs


def _decay_buyback_specs(events) -> dict[tuple[str, str], DecayBuybackPlaced]:
    """STP-08a (v1.61): the latest journaled decay-buyback order per
    (entry_id, side) — `DecayBuybackPlaced` is appended by DecayWatcher.buyback()
    AT PLACEMENT, so by the time its fill can appear in the fills feed the id is
    already on the log. A fill matching one of these ids is the DCY buyback,
    NEVER a stop-out."""
    specs: dict[tuple[str, str], DecayBuybackPlaced] = {}
    for e in events:
        if isinstance(e, DecayBuybackPlaced):
            specs[(e.entry_id, e.side)] = e
    return specs


def _open_short_legs(events):
    """Every (entry_id, side, leg, spec) this tick's EC-STP-06 triage
    considers: a still-open short (not stopped/closed/expired, entry not
    closed) with a `StopPlaced` on record. No StopPlaced at all is REC-04(2)/
    (3) territory -- boot's job, not this tick's."""
    day = fold(events)
    specs = _stop_specs(events)
    out = []
    for entry_id, e in day.entries.items():
        if e.close_initiator is not None:
            continue
        shorts = {leg.side: leg for leg in e.legs if leg.role == "short"}
        for side, leg in shorts.items():
            if side in e.sides_stopped or side in e.sides_closed or side in e.sides_expired:
                continue
            spec = specs.get((entry_id, side))
            if spec is not None:
                out.append((entry_id, side, leg, spec))
    return out


def _order_legs(order):
    if isinstance(order, dict):
        return order.get("legs") or ()
    return getattr(order, "legs", None) or ()


def _leg_symbol(leg) -> str | None:
    return leg.get("symbol") if isinstance(leg, dict) else getattr(leg, "symbol", None)


def _leg_action(leg) -> str:
    raw = leg.get("action") if isinstance(leg, dict) else getattr(leg, "action", "")
    return str(raw).lower().replace(" ", "_").split(".")[-1]


async def _still_resting(broker, symbol: str) -> bool:
    """Is a buy-to-close order for `symbol` still WORKING at the broker? If
    so there is nothing to do this tick -- the stop is doing its job."""
    for order in await broker.working_orders():
        for leg in _order_legs(order):
            if _leg_symbol(leg) == symbol and _leg_action(leg) == "buy_to_close":
                return True
    return False


async def _sell_still_working(broker, symbol: str) -> bool:
    """Double-ladder guard: is a sell-to-close order for `symbol` (a prior
    LEX rung, or its LEX-05 marketable fallback) still WORKING at the broker?
    If so, this tick must NOT start a second ladder beside it -- a 60s tick
    loop re-entering RecoverLong while a fallback rests would submit a
    duplicate sell every minute, the exact incident-#2 class this whole
    package exists to kill."""
    for order in await broker.working_orders():
        for leg in _order_legs(order):
            if _leg_symbol(leg) == symbol and _leg_action(leg) == "sell_to_close":
                return True
    return False


async def _resolve_by_order_id(broker, order_id: str) -> tuple[bool, Decimal | None]:
    """Precise path: `StopPlaced.broker_order_id` is recorded (every stop
    placed since v1.60). Filled iff it appears in `fills_since` -- matched by
    the SAME object-shape-safe normalizer execute_entry's ladder fix uses
    (`_fill_matches`; a raw `.get(...)` here would crash on a live SDK fill,
    the identical trap that normalizer was built to close)."""
    for f in await broker.fills_since(None):
        if _fill_matches(f, order_id):
            legs = await broker.fill_legs(order_id)
            price = next((l.price for l in legs if l.price is not None), None)
            return True, price
    return False, None


async def _resolve_by_symbol(broker, symbol: str) -> tuple[bool, Decimal | None, str | None]:
    """Fallback path for a stop placed BEFORE `broker_order_id` existed --
    every stop on record up to and including the missed 2026-07-10 C7565
    fill. Matched by the short's own broker-reported symbol (ORD-09) among
    buy-to-close fills.

    KNOWN LIMITATION, flagged rather than silently perfected: this cannot
    distinguish a resting-stop fill from some OTHER buy-to-close fill on the
    same symbol that this process never itself recorded (e.g. a genuinely
    external/manual close of the SHORT, OWN-09). `close_entry.py`'s own
    in-process closes already emit `ShortStopped`/`SideClosed` synchronously,
    so they never reach this fallback (the side is no longer "open" by the
    time this tick runs) -- the residual gap is a close performed by some
    OTHER process entirely while this stop-order-id-less bot was also up. A
    future slice should route this decision through the existing OWN-09
    `external_close.py`/`classify_side` machinery (grace period + two-
    consecutive-reconcile confirmation) instead of a bare symbol match.

    Returns (filled, price, matched_order_id): the matched fill's own order id
    is surfaced so the caller can recognise a journaled DECAY BUYBACK (STP-08a:
    a buy-to-close on the short's own symbol that is NOT a stop-out) before ever
    treating the match as a stop fill.
    """
    for order in await broker.fills_since(None):
        for leg in _order_legs(order):
            if _leg_symbol(leg) == symbol and _leg_action(leg) == "buy_to_close":
                oid = _order_id(order)
                legs = await broker.fill_legs(oid)
                price = next((l.price for l in legs if l.symbol == symbol and l.price is not None), None)
                return True, price, (None if oid is None else str(oid))
    return False, None, None


async def build_tracked_shorts(broker, events) -> tuple[list[TrackedShort], list[tuple[str, str, Decimal]]]:
    """The EC-STP-06 candidates for THIS tick: an open, stop-placed short
    whose stop is no longer resting and DID fill. The first list is reused
    directly by `Reconcile.plan()` -- the SAME frame boot reconciliation
    drives.

    The second list is STP-08a's decay classification (v1.61): sides whose
    matched fill is, by journaled order id, the DCY BUYBACK -- (entry_id,
    side, fill_price). These are NEVER TrackedShorts (never a stop-out, never
    LEX); the caller journals them with decay_watcher.complete()'s exact
    SIDE_CLOSED_DECAY shape. Two recognition points, both by order id against
    `_decay_buyback_specs`:
      * up-front per side: a journaled buyback for THIS side whose id shows
        filled resolves the side as decay-closed before either stop-resolution
        path runs (covers the order-id-era stop, whose own id would simply
        read "not filled" after decay cancelled it);
      * inside the symbol fallback: the matched fill's own order id equals ANY
        journaled buyback id (the misread the docstring used to flag as a
        latent hazard -- a decay buyback IS a buy-to-close on the short's own
        symbol)."""
    tracked: list[TrackedShort] = []
    decay_closed: list[tuple[str, str, Decimal]] = []
    decay_specs = _decay_buyback_specs(events)
    decay_ids = {spec.broker_order_id: spec for spec in decay_specs.values()}
    for entry_id, side, leg, spec in _open_short_legs(events):
        if await _still_resting(broker, leg.symbol):
            continue  # covers the resting stop AND a still-working decay buyback
        dspec = decay_specs.get((entry_id, side))
        if dspec is not None:
            filled, price = await _resolve_by_order_id(broker, dspec.broker_order_id)
            if filled:
                decay_closed.append((entry_id, side,
                                     price if price is not None else dspec.price))
                continue  # SIDE_CLOSED_DECAY -- never a stop-out (STP-08a)
        if spec.broker_order_id:
            filled, price = await _resolve_by_order_id(broker, spec.broker_order_id)
        else:
            filled, price, matched_oid = await _resolve_by_symbol(broker, leg.symbol)
            if filled and matched_oid is not None and matched_oid in decay_ids:
                bspec = decay_ids[matched_oid]
                decay_closed.append((entry_id, side,
                                     price if price is not None else bspec.price))
                continue  # the "stop fill" was in fact the DCY buyback
        if not filled:
            continue  # neither resting nor fillable -- ambiguous; leave for boot
        tracked.append(TrackedShort(
            entry_id=entry_id, side=side, symbol=leg.symbol, stop_order_id=None,
            stop_filled=True, stop_fill_price=(price if price is not None else spec.trigger),
            stop_trigger=spec.trigger, contracts=leg.qty))
    return tracked, decay_closed


async def _long_still_held(broker, long_symbol: str) -> bool:
    """OWN standdown (operator ruling 2026-07-10), for a fill CAUGHT UP late:
    is the orphaned long the ladder is about to sell still actually held?

    Deliberately checks BROKER POSITION TRUTH (REC-02: broker is authoritative
    for positions), not `OwnershipLedger`/`state.own_ledger` literally --
    that persisted ledger is never actually populated by a fill-apply call
    anywhere in production (`OwnershipLedger.apply_fill` is exercised only by
    domain unit tests today; `reconcile_on_boot` restores it but never calls
    it either), so trusting it here would stand down every hand-off, not
    just a genuine operator disposition. This is the honest substitute and
    matches the same authority rule OWN-04 relies on; the ledger gap itself
    is flagged separately, not silently patched here.
    """
    for position in await broker.positions():
        symbol, qty = _symbol_and_signed_qty(position)
        if symbol == long_symbol and qty != 0:
            return True
    return False


def _long_leg_for(events, entry_id: str, side: str):
    day = fold(events)
    e = day.entries.get(entry_id)
    if e is None:
        return None
    for leg in e.legs:
        if leg.side == side and leg.role == "long":
            return leg
    return None


def _pending_lex_sides(events) -> list[tuple[str, str]]:
    """LEX-01: a stopped side's orphaned long is ALWAYS sold. Pending = the
    short is stopped (`ShortStopped` on the log -- a fresh detection this
    tick, a boot EC-STP-06 synthesis, a watchdog escalation, or a stop that
    raced a CLS replace to FILLED) with no terminal `SideClosed`/`SideExpired`
    for the side. Keyed off `sides_stopped`, NOT the `LongSaleStarted` marker:
    the marker is only journaled when a ladder actually STARTS, and the whole
    point of this set is the side whose ladder could not start yet -- e.g.
    detection landed on a stale-snapshot tick and the quote guard deferred.
    Without this, that side would leave `_open_short_legs` forever
    (ShortStopped is recorded) and the orphaned long would stay unsold until
    the next process restart (boot REC-03) -- an unhedged long bleeding theta
    for hours on live money (2026-07-10 lead-review finding).

    A CLOSED entry does NOT exempt its stopped sides (R3-F1): CLS-01(2)'s
    ORD-08a race path (close_entry.py `_replace_stop` -> "FILLED") journals
    `ShortStopped` and deliberately never `SideClosed` -- the raced side's
    long is EXCLUDED from CLS's own sells because "LEX owns that side's long
    sale" -- and then `EntryClosed` lands. A blanket entry-closed skip
    orphaned that long forever, on EVERY close initiator, with zero signal.

    The ONE deliberate exemption is DECAY: DCY-03 requires a decay-closed
    side's long be LEFT TO EXPIRE, never LEX-sold (decay_watcher.py
    `complete()` journals ShortStopped(initiator="decay") + EntryClosed
    atomically). Excluded via the SIDE's own ShortStopped initiator ONLY --
    decay always journals that initiator for its own side, so the side-level
    check is precise. The entry-level `close_initiator == "decay"` half was
    removed (final review 2026-07-10): it over-exempted EVERY stopped side on
    a decay-closed entry, so a CALL stopped earlier by a resting_stop (quote-
    deferred, ladder not yet started) would be silently stranded the moment
    the PUT decayed -- the exact R3-F1 orphan class, re-opened."""
    day = fold(events)
    out = []
    for entry_id in sorted(day.entries):
        e = day.entries[entry_id]
        # side -> every initiator that stopped it (parallel tuples in the fold)
        initiators: dict[str, set[str]] = {}
        for side, init in zip(e.sides_stopped, e.stop_initiators):
            initiators.setdefault(side, set()).add(init)
        seen: set[str] = set()
        for side in e.sides_stopped:
            if side in seen or side in e.sides_closed or side in e.sides_expired:
                continue
            seen.add(side)
            if "decay" in initiators.get(side, ()):
                continue  # DCY-03: a decay long is left to expire, never LEX-sold
            out.append((entry_id, side))
    return out


def _now_epoch(comp) -> float:
    """Epoch seconds for the bounded deferral window (STP-08a v1.62). The
    composition's own clock when it has one (live/paper both do -- and the
    FakeClock in tests), so the window honours simulated time; wall clock
    otherwise. Epoch (`.timestamp()`), never wall-clock arithmetic -- the
    same instant discipline UI-24 v1.62 ratified."""
    clock = getattr(comp, "clock", None)
    if clock is not None:
        return clock.now().timestamp()
    return time.time()


def _alert_once(comp, alerts, key: tuple, level: str, message: str, **ctx) -> None:
    """Alert once per (entry_id, side, reason), not once per 60s tick: an
    unresolvable side re-enters the pending set every tick, and re-alerting
    each time would bury the operator. Deduped on the composition (survives
    across ticks, dies with the process -- boot re-evaluates from scratch)."""
    seen = getattr(comp, "_stop_fill_watch_alerted", None)
    if seen is None:
        seen = set()
        comp._stop_fill_watch_alerted = seen
    if key in seen:
        return
    seen.add(key)
    alerts.alert(level, message, **ctx)


async def _try_recover(comp, alerts, quote_provider, entry_id: str, side: str,
                       *, lex_quote_wait_seconds: float = LEX_QUOTE_WAIT_SECONDS) -> None:
    """Drive ONE pending side's LEX hand-off through the shared guards --
    fresh catch-ups and deferred retries take the IDENTICAL path, in order:

      1. ORD-09: no broker-reported long recorded -> cannot name the
         instrument to sell; critical alert (once), operator must intervene.
      2. EC-LEX-08 (v1.63) resting-floor lookup: has a prior tick already
         rested an intrinsic-floor sell for this side? If so, check FIRST
         whether it has itself filled between ticks (the floor order is never
         inside a `recover()` call, so nothing else watches it) -- filled ⇒
         terminal append via `RecoverLong.record_floor_sold`, done.
      3. Double-ladder guard: a sell-to-close for this long still WORKING at
         the broker (last tick's rung, or its LEX-05 fallback) -> SKIP; never
         start a second ladder beside it. Skipped when a floor is tracked --
         that floor IS the resting sell, and steps 5/6 below manage or
         supersede it explicitly rather than being blanket-skipped here.
      4. OWN standdown (operator ruling 2026-07-10): the long is no longer
         held at the broker -> the operator disposed of it directly; no LEX
         order, info alert (once).
      5. Quote guard, BOUNDED (STP-08a v1.62 -- "on invalid quotes LEX
         follows its own ratified path: deferral only within the retry
         cadence, then the marketable-at-bid fallback; a naked-side recovery
         never waits indefinitely"). Distinct cases:
           * `NoBidFloor` (EC-LEX-08 v1.63 -- NO bid at all, but a DAT-02-fresh
             underlying mark makes the LEX-04 floor computable): already
             resting ⇒ nothing to do; else rest it now via
             `RecoverLong.rest_floor` and fire the one-time critical alert AT
             PLACEMENT (announced, not discovered).
           * `StaleQuote` (a bid EXISTS but is stale-invalid, LEX-02's age
             criterion): defer while the total elapsed since this side's
             FIRST deferral is under `lex_quote_wait_seconds`; at/past the
             window, stop deferring and start LEX via the LEX-05 fallback
             (`RecoverLong.recover(quote_stale=True)` -- marketable limit at
             that bid, never a ladder off stale prices). A resting floor is
             left alone: a stale bid is no improvement over the floor.
           * `None` (NO bid at all AND no fresh underlying mark -- EC-LEX-08
             case (c)): a marketable order cannot be priced, so the side
             keeps deferring, forever if need be, with the R3-F3 one-time
             critical alert standing. A price is never invented. A resting
             floor is left alone (still hedging; no spam).
         A USABLE quote arriving at any point -- including mid-window --
         clears the deferral state and starts the normal ladder; if a floor
         was resting, it is SUPERSEDED via the raced-fill-guarded
         cancel/replace (LEX-08 -- `RecoverLong.recover(adopt_order_id=...,
         adopt_price=...)`), resuming LEX-03 pricing.

    `RecoverLong.recover()` itself appends `LongSaleStarted` when -- and only
    when -- a ladder genuinely (re)starts past all guards, so a retry tick
    that skips at any guard journals nothing (no marker spam)."""
    broker, events = comp.broker, comp.events
    long_leg = _long_leg_for(events, entry_id, side)
    if long_leg is None:
        # ORD-09: no broker-reported long on record for this side. Silent
        # skip would strand it invisibly (it re-enters the pending set every
        # tick forever) -- this is unrecoverable without the operator, so say
        # so, loudly, once (2026-07-10 review finding 4).
        _alert_once(comp, alerts, (entry_id, side, "no_long_recorded"), "critical",
                    "EC-STP-06 catch-up: this side's short stopped but NO broker-reported "
                    "long leg is recorded for it (ORD-09) -- the bot cannot identify the "
                    "instrument to sell and will not guess. Operator must dispose of the "
                    "orphaned long manually.",
                    entry_id=entry_id, side=side)
        return

    key = (entry_id, side)
    # EC-LEX-08 (v1.63): this side's resting intrinsic-floor order, if a
    # prior tick placed one. Looked up BEFORE the double-ladder guard -- with
    # a floor tracked, THIS function manages/supersedes it explicitly below
    # instead of being blanket-skipped by that guard.
    floor_orders = getattr(comp, "_stop_fill_floor_orders", None)
    if floor_orders is None:
        floor_orders = {}
        comp._stop_fill_floor_orders = floor_orders
    floor = floor_orders.get(key)

    if floor is not None:
        # The resting floor may itself have FILLED between ticks -- nothing
        # else watches it (it is never inside a `recover()` ladder call).
        filled, price = await _resolve_by_order_id(broker, floor[0])
        if filled:
            comp.recover.record_floor_sold(entry_id, side,
                                           price if price is not None else floor[1])
            floor_orders.pop(key, None)
            getattr(comp, "_stop_fill_quote_deferrals", {}).pop(key, None)
            getattr(comp, "_stop_fill_quote_deferred_since", {}).pop(key, None)
            return

    if floor is None and await _sell_still_working(broker, long_leg.symbol):
        return  # a prior rung/fallback is still resting -- never a second ladder

    if not await _long_still_held(broker, long_leg.symbol):
        floor_orders.pop(key, None)
        _alert_once(comp, alerts, (entry_id, side, "standdown"), "info",
                    "EC-STP-06 catch-up: the short's stop had already filled, but the "
                    "long was no longer held at the broker -- standing down (operator disposed "
                    "of it directly, OWN-09/10). No LEX order submitted.",
                    entry_id=entry_id, side=side, symbol=long_leg.symbol)
        return

    got = await quote_provider(long_leg.symbol, side)

    if isinstance(got, NoBidFloor):
        # EC-LEX-08(a)/(b): no bid at all, but the floor is computable off a
        # fresh underlying mark. Already resting -> nothing new to place;
        # not yet resting -> rest it now, critical alert AT PLACEMENT
        # (operator addition: a degraded-liquidity recovery is announced,
        # not discovered).
        if floor is not None:
            return
        order_id, price = await comp.recover.rest_floor(
            entry_id=entry_id, side=side, long_symbol=long_leg.symbol,
            intrinsic=got.intrinsic, qty=long_leg.qty)
        floor_orders[key] = (order_id, price)
        _alert_once(comp, alerts, (entry_id, side, "lex_floor_rested"), "critical",
                    f"EC-LEX-08: no bid for {long_leg.symbol}; resting intrinsic-floor sell "
                    f"at {price} (a degraded-liquidity recovery, announced not discovered).",
                    entry_id=entry_id, side=side, symbol=long_leg.symbol)
        getattr(comp, "_stop_fill_quote_deferrals", {}).pop(key, None)
        getattr(comp, "_stop_fill_quote_deferred_since", {}).pop(key, None)
        return

    if got is None or isinstance(got, StaleQuote):
        if floor is not None:
            # EC-LEX-08: the floor is resting and hedging already -- a
            # stale bid is no improvement over the intrinsic floor, and no
            # bid at all changes nothing either. Leave it resting, no spam.
            return
        # Deferral bookkeeping, shared by both invalid-quote flavours: the
        # R3-F3 consecutive-tick count (alert threshold) and, since v1.62,
        # the epoch instant of this side's FIRST deferral (the bounded
        # `lex_quote_wait_seconds` window -- STP-08a/LEX-02). Elapsed is
        # measured across REAL INSTANTS (`.timestamp()`), the same epoch
        # discipline UI-24 v1.62 ratified for the countdown.
        deferrals = getattr(comp, "_stop_fill_quote_deferrals", None)
        if deferrals is None:
            deferrals = {}
            comp._stop_fill_quote_deferrals = deferrals
        since = getattr(comp, "_stop_fill_quote_deferred_since", None)
        if since is None:
            since = {}
            comp._stop_fill_quote_deferred_since = since
        now = _now_epoch(comp)
        first = since.setdefault(key, now)
        deferrals[key] = deferrals.get(key, 0) + 1
        if isinstance(got, StaleQuote) and (now - first) >= lex_quote_wait_seconds:
            # STP-08a (v1.62): the bounded window has lapsed and a BID exists
            # -- stop deferring; LEX starts via its LEX-05 fallback (recover()
            # routes quote_stale=True straight to the marketable limit at this
            # bid, never a ladder priced off stale marks). "A naked-side
            # recovery never waits indefinitely."
            deferrals.pop(key, None)
            since.pop(key, None)
            await comp.recover.recover(entry_id=entry_id, side=side,
                                       long_symbol=long_leg.symbol, quote=got.quote,
                                       intrinsic=got.intrinsic, qty=long_leg.qty,
                                       quote_stale=True)
            return
        # R3-F3: deferral is correct (never guess a price), but UNBOUNDED
        # SILENT deferral is not -- a long whose strike never gets marked
        # would defer every tick to expiry with no operator signal and never
        # reach any LEX-05 fallback. Count consecutive deferrals per side and
        # say so, loudly, once, past the threshold. Still defers after the
        # alert -- with NO bid at all there is honestly nothing else to do.
        if deferrals[key] >= QUOTE_DEFERRAL_ALERT_TICKS:
            _alert_once(comp, alerts, (entry_id, side, "quote_deferred"), "critical",
                        f"EC-STP-06 catch-up: cannot start LEX -- no usable quote for "
                        f"{long_leg.symbol} after {deferrals[key]} consecutive ticks. Still "
                        "deferring (a price is never guessed); operator attention needed.",
                        entry_id=entry_id, side=side, symbol=long_leg.symbol)
        return
    # a usable quote resets the deferral state for this side (count + window)
    getattr(comp, "_stop_fill_quote_deferrals", {}).pop(key, None)
    getattr(comp, "_stop_fill_quote_deferred_since", {}).pop(key, None)
    quote, intrinsic = got
    if floor is not None:
        # EC-LEX-08(b): a usable quote SUPERSEDES the resting floor via the
        # raced-fill-guarded cancel/replace (LEX-08) -- recover() adopts the
        # floor's own order id/price as the ladder's starting working order.
        await comp.recover.recover(entry_id=entry_id, side=side, long_symbol=long_leg.symbol,
                                   quote=quote, intrinsic=intrinsic, qty=long_leg.qty,
                                   adopt_order_id=floor[0], adopt_price=floor[1])
        floor_orders.pop(key, None)
    else:
        await comp.recover.recover(entry_id=entry_id, side=side, long_symbol=long_leg.symbol,
                                   quote=quote, intrinsic=intrinsic, qty=long_leg.qty)


async def detect_and_recover_stop_fills(comp, alerts, quote_provider, *,
                                        lex_quote_wait_seconds: float = LEX_QUOTE_WAIT_SECONDS) -> None:
    """One health-tick pass (60s cadence -- the honest poll `live_app`'s
    `MEIC_HEALTH_INTERVAL_S` already runs everything else on; no new
    streaming infrastructure). Idempotent: a side already resolved
    (`ShortStopped`/`SideClosed`/`SideExpired` recorded, or the entry closed)
    is never a DETECTION candidate again (`_open_short_legs` excludes it) --
    but a stopped side whose ladder never TERMINATED stays in the
    `_pending_lex_sides` set and is re-driven through the same guards until
    it does (the double-ladder guard is what keeps that retry loop from ever
    stacking a second sell beside a resting rung/fallback).

    CATCH-UP: this is the SAME pass on every tick, including the very first
    one after boot/deploy -- there is no separate "startup" mode. A stop that
    filled while the bot was up and simply missed (2026-07-10's C7565) is
    caught the first time this runs after it ships, exactly like a fill that
    happens between two ticks going forward.

    `quote_provider(long_symbol, side) -> (Quote, intrinsic) | StaleQuote |
    None` supplies the live market data LEX needs to start its ladder;
    injected so this module stays I/O-light and unit-testable. A tuple is a
    fresh, priceable quote; `StaleQuote` (v1.62) means "a bid exists but is
    stale-invalid (LEX-02 age)" -- deferred within the bounded
    `lex_quote_wait_seconds` window, then routed to the LEX-05 fallback;
    `None` means "NO bid at all this tick" -- retried next tick, never
    guessed.
    """
    broker, events = comp.broker, comp.events

    # Phase 1 -- DETECTION: journal any stop fill the broker knows about that
    # the log doesn't (EC-STP-06 synthesis, via the ratified Reconcile frame).
    tracked, decay_closed = await build_tracked_shorts(broker, events)
    if tracked or decay_closed:
        # R3-F2: build_tracked_shorts folded the log ONCE at its start, then
        # awaited the broker repeatedly -- a concurrent close for the same
        # side can complete in between (its SideClosed now journaled, and its
        # own buy-to-close fill would read as a "stop fill" to the symbol
        # fallback), and executing on that stale view would synthesize a
        # FALSE ShortStopped (corrupt sides_stopped, dashboard taxonomy,
        # slippage stats). Re-apply _open_short_legs' own predicate against
        # the CURRENT log, synchronously: no await can interleave between
        # this check and execute()'s synthesis appends (which sit at its
        # top, before its first await).
        open_now = {(entry_id, side) for entry_id, side, _leg, _spec in _open_short_legs(events)}
        tracked = [t for t in tracked if (t.entry_id, t.side) in open_now]
        decay_closed = [d for d in decay_closed if (d[0], d[1]) in open_now]
    # STP-08a (v1.61): a fill identified (by journaled order id) as the DCY
    # buyback classifies SIDE_CLOSED_DECAY -- journaled with the EXACT shape
    # decay_watcher.complete() uses (ShortStopped initiator="decay" +
    # EntryClosed initiator="decay", atomically), so the projection and the
    # `_pending_lex_sides` decay exemption below keep working unchanged. The
    # long is LEFT TO EXPIRE (DCY-03): the decay initiator excludes the side
    # from the pending set, so no LEX ladder ever starts. Appended
    # synchronously (no await between the R3-F2 re-check and these appends).
    for entry_id, side, fill_price in decay_closed:
        events.append(ShortStopped(entry_id=entry_id, side=side, fill=fill_price,
                                   slippage=Decimal("0"), initiator="decay"))
        events.append(EntryClosed(entry_id=entry_id, initiator="decay"))
    if tracked:
        rec = Reconcile(broker, events)
        plan = rec.plan(tracked_shorts=tracked, broker_working_order_ids=set(),
                        mid_lex_sides=(), stale_entry_order_ids=())
        # run_lex's only effect in execute() is a LongSaleStarted marker --
        # RecoverLong.recover() below journals that itself when a ladder
        # genuinely starts, so leaving both would duplicate the activity-feed
        # line (review finding 2). Stripped HERE only; boot's semantics are
        # byte-identical (reconcile.py untouched).
        plan.run_lex.clear()
        await rec.execute(plan)   # EC-STP-06: synthesizes the missed ShortStopped

    # Phase 2 -- RECOVERY: drive LEX for EVERY stopped-but-unresolved side,
    # whether it was detected this tick, deferred by a guard on a previous
    # tick, synthesized at boot, or stopped by the watchdog. One set, one
    # guard path -- no fresh/resume split to drift apart.
    for entry_id, side in _pending_lex_sides(events):
        await _try_recover(comp, alerts, quote_provider, entry_id, side,
                           lex_quote_wait_seconds=lex_quote_wait_seconds)


def _latest_lex_order_placed(events) -> dict[tuple[str, str], LexOrderPlaced]:
    """The latest journaled `LexOrderPlaced` per (entry_id, side) -- iterate
    in order, keep last. Used by `readopt_resting_floors` (EC-LEX-08(d),
    v1.64) to tell a still-RESTING floor from one already superseded by a
    ladder/fallback rung: only the LAST order this side ever carried tells
    the truth about what is (or was) working at the broker."""
    latest: dict[tuple[str, str], LexOrderPlaced] = {}
    for e in events:
        if isinstance(e, LexOrderPlaced):
            latest[(e.entry_id, e.side)] = e
    return latest


async def readopt_resting_floors(comp, broker) -> None:
    """EC-LEX-08(d)/(e) (v1.64/v1.65): the floor registry is in-memory and lost
    on restart. On boot, for each pending side whose LATEST journaled order is
    a `LexOrderPlaced(kind="floor")`, reconcile that floor against broker truth:

      (d) STILL WORKING (id in `working_orders()`): re-adopt it into
          comp._stop_fill_floor_orders via its journaled id (REC-05 pattern) --
          so supersession resumes and a later fill records LongSold (never
          misread as OWN standdown). REC-03's 'resume' applies to floor orders
          exactly as to ladders.
      (e) ABSENT from working orders but its broker FILL record shows FILLED:
          the floor filled while the bot was down -- synthesize its LongSold +
          SideClosed at the BROKER-ACTUAL fill price (ORD-09), closing the side
          terminally. This is EC-STP-06's synthesize-the-missed-event principle
          applied to a floor order: never leave it perpetual-pending, never let
          `_try_recover`'s OWN-standdown path misclassify a genuine fill, never
          leave the EOD-03 audit one event short. The broker-actual price is
          resolved via the SAME shape-safe `_resolve_by_order_id` normalizer
          the fill-detection paths use (never a new one); the journaled floor
          price is used only if the broker fill record carries no price.
      ABSENT and NOT filled (externally cancelled): do NOTHING here -- neither
          re-adopt nor synthesize. `_try_recover`'s OWN-standdown path handles
          the genuinely-gone-without-a-fill case on the next tick.

    Only the LATEST `LexOrderPlaced` per (entry_id, side) is considered, and
    only if its `kind == "floor"`: a side whose latest journaled order is a
    "ladder"/"fallback" rung already SUPERSEDED whatever floor once rested
    there -- treating a stale floor id as this side's floor would corrupt the
    double-ladder guard (two orders both believed "the resting sell").

    Deliberately silent for (d): no alert. The placement alert already fired
    (once) when the floor was first rested, pre-restart; re-alerting on every
    boot would double-count a single degraded-liquidity event. (e)'s terminal
    appends are the same `record_floor_sold` a live catch-up would make."""
    floor_orders = getattr(comp, "_stop_fill_floor_orders", None)
    if floor_orders is None:
        floor_orders = {}
        comp._stop_fill_floor_orders = floor_orders

    latest = _latest_lex_order_placed(comp.events)
    working_ids = {_order_id(o) for o in await broker.working_orders()}

    for entry_id, side in _pending_lex_sides(comp.events):
        lop = latest.get((entry_id, side))
        if lop is None or lop.kind != "floor":
            continue
        if lop.broker_order_id in working_ids:
            # (d): genuinely still resting -- re-adopt for supersession/fill watch.
            floor_orders[(entry_id, side)] = (lop.broker_order_id, Decimal(str(lop.price)))
            continue
        # (e): gone from working orders -- did it FILL while we were down?
        filled, price = await _resolve_by_order_id(broker, lop.broker_order_id)
        # R3-F2 guard (final review v1.65): there is an await between this side's
        # pending snapshot and the append below, so two concurrent readopt runs
        # (overlapping /broker/connect POSTs) could each pass the check and
        # double-JOURNAL the synthesis (record_floor_sold is not idempotent).
        # Re-derive pending SYNCHRONOUSLY here -- no await before the append --
        # so whichever run appends first closes the side and the other skips.
        # Mirrors the identical Phase-1 detection guard elsewhere in this module.
        if filled and (entry_id, side) in set(_pending_lex_sides(comp.events)):
            comp.recover.record_floor_sold(
                entry_id, side, price if price is not None else Decimal(str(lop.price)))
        # else: gone WITHOUT a fill (externally cancelled) -- leave it for the
        # OWN external-cancel path; do not synthesize, do not re-adopt.
