"""Domain events — the event-sourced core (REC-01, doc 05 §4).

Every aggregate mutation is one of these, appended to the log before any side
effect. Events are immutable, deterministically ordered by their stream
sequence, and round-trip through a stable dict form so the log survives
process death and replays identically (REC-01 / TC-REC-01).

Money fields are Decimal end to end; serialization keeps them exact (str),
never float — a replayed P&L must equal the original to the cent.

This module is pure (no I/O). The store that persists these lives in
adapters/persistence; the fold that projects them lives in projection.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from decimal import Decimal
from typing import Any, ClassVar


class Event:
    """Base for all domain events. Subclasses are frozen dataclasses.

    `type` is the stable wire name (class name); the registry maps it back for
    deserialization. Subclasses declare only data fields — no behavior.
    """

    type: ClassVar[str]
    _registry: ClassVar[dict[str, type["Event"]]] = {}

    # v1.44 (operator-ratified: build now, not debt). The config version in force
    # when this event was recorded. It is NOT a dataclass field: making it one
    # would force every event's __eq__ and every constructor to carry it, and two
    # events that differ only in the config version are still the same fact. It
    # round-trips through to_dict/from_dict, so a replayed log knows which rules
    # produced each event.
    config_version: str = ""

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        cls.type = cls.__name__
        Event._registry[cls.__name__] = cls

    def stamped(self, config_version: str) -> "Event":
        """Return this event carrying `config_version`. Frozen, so set it through
        object.__setattr__ on a copy — the caller's event is never mutated."""
        import copy

        out = copy.copy(self)
        object.__setattr__(out, "config_version", config_version)
        return out

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        for f in fields(self):  # type: ignore[arg-type]
            v = getattr(self, f.name)
            if isinstance(v, Decimal):
                out[f.name] = str(v)
            elif isinstance(v, tuple) and v and isinstance(v[0], FilledLeg):
                out[f.name] = [leg.to_dict() for leg in v]   # ORD-09 legs
            elif isinstance(v, tuple):
                out[f.name] = list(v)
            else:
                out[f.name] = v
        if self.config_version:
            out["config_version"] = self.config_version
        return out

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Event":
        cls = Event._registry[data["type"]]
        kwargs: dict[str, Any] = {}
        for f in fields(cls):  # type: ignore[arg-type]
            if f.name not in data:
                # Field absent in an older log entry — fall back to its default
                # (schema evolution: e.g. `fee` added after early events).
                continue
            raw = data[f.name]
            if f.name == "legs":
                kwargs[f.name] = tuple(FilledLeg.from_dict(d) for d in raw)
            elif f.type in ("Decimal", Decimal):
                kwargs[f.name] = Decimal(raw)
            else:
                kwargs[f.name] = raw
        event = cls(**kwargs)
        if data.get("config_version"):
            object.__setattr__(event, "config_version", data["config_version"])
        return event


# --- TradingDay (doc 05 §3) --------------------------------------------------

@dataclass(frozen=True)
class DayArmed(Event):
    date: str
    entry_count: int


@dataclass(frozen=True)
class EntryWindowOpened(Event):
    date: str
    entry_number: int


@dataclass(frozen=True)
class EntrySkipped(Event):
    date: str
    entry_number: int
    reason: str
    # ENT-09b v1.57 (optional/additive, None for every pre-v1.57 skip): the
    # manual-fire minimum short-strike floors in force for THIS attempt, so a
    # `floor_inside_spot` refusal (or any other skip while floors were set) is
    # auditable from the event log alone.
    put_floor: Decimal | None = None
    call_floor: Decimal | None = None


@dataclass(frozen=True)
class DayCompleted(Event):
    date: str


@dataclass(frozen=True)
class ModeSwitchStaged(Event):
    target: str       # "paper" | "live"
    effective: str    # DAY-05: "next_day" — never intraday (UC-10 audit trail)


# --- CondorEntry (doc 05 §3) -------------------------------------------------

@dataclass(frozen=True)
class CondorProposed(Event):
    entry_id: str
    put_short: Decimal
    call_short: Decimal


# `fee` on every fill-bearing event: the per-contract commissions/fees (PNL-01)
# incurred by THAT fill, RECORDED AT FILL TIME from the FeeModel then in force.
# Recording (not recomputing) keeps replay deterministic (PNL-03) and lets the
# EOD pass reconcile recorded fees against broker truth (PNL-04). Default 0.00
# is the seam only — the FeeModel that populates it lands with the code that
# produces each fill (stop fills: slice 2; entry fills: slice 3).

@dataclass(frozen=True)
class FilledLeg:
    """ORD-09 (v1.45): one leg of a fill, AS THE BROKER REPORTED IT.

    `symbol` is the broker's own instrument symbol, byte-identical to its payload.
    Every later order action on this leg — stop, LEX, decay buyback, close,
    flatten, watchdog escalation — uses THIS string. Nothing re-derives it from
    strike/expiry/right at action time: re-running symbology math on every use is
    the same drift class as the intent-translation defect. Reconstruction is only
    ever a cross-check that alerts on mismatch (see `crosscheck_leg_symbols`).

    `price` is the broker-ALLOCATED fill price for this leg — the data source for
    the STP-02d reconciliation records and the OWN fill-derived ledger. Paper
    records simulator-assigned symbols and prices in the same fields (SIM-05).
    """

    symbol: str
    right: str                    # "P" | "C"
    role: str                     # "short" | "long"
    qty: int
    # Broker-ALLOCATED price for this leg — never the net credit divided by four.
    # None means the broker reported no allocation, which is the honest paper case:
    # a simulator has none, and fabricating one would poison the exact field
    # STP-02d exists to reconcile (hence "real fills only").
    price: Decimal | None = None

    @property
    def side(self) -> str:
        return "PUT" if self.right == "P" else "CALL"

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "right": self.right, "role": self.role,
                "qty": self.qty, "price": None if self.price is None else str(self.price)}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "FilledLeg":
        raw = d.get("price")
        return FilledLeg(symbol=d["symbol"], right=d["right"], role=d["role"],
                         qty=int(d["qty"]), price=None if raw is None else Decimal(raw))


@dataclass(frozen=True)
class CondorFilled(Event):
    entry_id: str
    net_credit: Decimal  # actual net fill credit (STK-02a) — the P&L basis
    fee: Decimal = Decimal("0")  # entry fees, all four legs (PNL-01)
    short_premium: Decimal = Decimal("0")  # gross premium on the shorts (UI-14 label)
    legs: tuple[FilledLeg, ...] = ()  # ORD-09: broker-reported identity + allocations
    # ENT-09: how this entry came to be. "schedule" | "manual_entry". Reports tag
    # manual actions (UC-08); the default keeps every pre-v1.44 log entry replayable.
    initiator: str = "schedule"
    # UI card feature (2026-07-09): the wall-clock fill time, ISO, so the operator
    # can see WHEN an entry actually filled. Optional/additive with a None default
    # so a log entry recorded before this field existed still replays (from_dict's
    # generic "field absent -> fall back to default" path handles it, same as `fee`).
    at: str | None = None
    # ENT-09b v1.57 (optional/additive): the manual-fire minimum short-strike
    # floors in force for this entry, if any -- "recorded in the entry's
    # events and day report" (e.g. "manual fire, put floor 7450 / call floor
    # 7500"). None/None for every non-floor and pre-v1.57 fill.
    put_floor: Decimal | None = None
    call_floor: Decimal | None = None


@dataclass(frozen=True)
class StopPlaced(Event):
    entry_id: str
    side: str
    trigger: Decimal  # STP-01/02: broker-resting buy-to-close stop-market
    # Additive (v1.60): the broker's own order id for this resting stop, so a
    # later live catch-up pass can match a fill to THIS stop precisely instead
    # of by symbol inference. Optional/None for every pre-v1.60 recorded event
    # (from_dict's generic "field absent -> default" path handles replay) and
    # for any caller that hasn't threaded the broker's id through yet.
    broker_order_id: str | None = None
    # RPT-07 long recovery (2026-07-11, operator ruling): the STP-02b
    # stop_rebate_markup buffer IN FORCE when this stop's trigger was computed
    # -- journaled so a realized long-sale recovery can later be compared
    # against it (NLE-06's "every calibration record stores the markup in
    # force"). Optional/additive, None for every pre-stamping recorded event
    # (event-store codec's runtime-value tagging handles absent-on-decode,
    # same as `broker_order_id` above).
    markup: Decimal | None = None


@dataclass(frozen=True)
class StopReplaced(Event):
    entry_id: str
    side: str  # REC-04(3): stop re-placed on recovery (trigger recomputed at placement)


@dataclass(frozen=True)
class ReconciliationMismatch(Event):
    detail: str  # REC-02: broker vs internal disagreement -> RSK-03 gate


@dataclass(frozen=True)
class StopConfirmed(Event):
    entry_id: str
    side: str  # STP-04: working-order confirmation from broker


@dataclass(frozen=True)
class SideUnprotected(Event):
    entry_id: str
    side: str
    action: str  # STP-04: flatten_side | flatten_condor after retries exhausted


@dataclass(frozen=True)
class WatchdogEscalated(Event):
    entry_id: str
    side: str
    mark_at_breach: Decimal   # calibration evidence (STP-03b / TC-STP-17)
    elapsed_seconds: Decimal
    fill_price: Decimal


@dataclass(frozen=True)
class EntryClosedInfeasible(Event):
    entry_id: str  # STP-02c post-fill: closed via CLS, initiator infeasible_stop


@dataclass(frozen=True)
class ShortStopped(Event):
    entry_id: str
    side: str  # "PUT" | "CALL"
    fill: Decimal  # buy-to-close fill price paid
    slippage: Decimal
    fee: Decimal = Decimal("0")  # buy-to-close fee (PNL-01)
    initiator: str = "resting_stop"  # resting_stop | watchdog_escalation (STP-03b)


@dataclass(frozen=True)
class DecayBuybackPlaced(Event):
    """DCY-02 / STP-08a (v1.61): the decay watcher placed its limit buy-to-close
    and this is the broker's own order id for it — journaled AT PLACEMENT so the
    live stop-fill detection pass (application/stop_fill_watch.py) can recognise
    the buyback's fill BY ORDER ID and classify the side SIDE_CLOSED_DECAY
    (ShortStopped initiator="decay" + EntryClosed initiator="decay", the exact
    shape decay_watcher.complete() journals) instead of misreading it as a
    stop-out via the symbol fallback — "a fill that is in fact the DCY buyback
    ... never as a stop-out" (STP-08a). Additive/replay-safe: an old log simply
    contains none of these, and every field round-trips through the generic
    to_dict/from_dict paths like `StopPlaced.broker_order_id` (v1.60)."""
    entry_id: str
    side: str
    broker_order_id: str
    price: Decimal  # the buyback limit (the decay trigger, DCY-01)


@dataclass(frozen=True)
class LongSold(Event):
    entry_id: str
    side: str
    recovery: Decimal  # credit received selling the orphaned long (LEX)
    fee: Decimal = Decimal("0")  # long-sale fee (PNL-01)


@dataclass(frozen=True)
class SideClosed(Event):
    entry_id: str
    side: str


@dataclass(frozen=True)
class SideExpired(Event):
    entry_id: str
    side: str  # cash-settled worthless (EOD-01) — no cash movement


@dataclass(frozen=True)
class EntryClosed(Event):
    entry_id: str
    initiator: str  # CLS-02/04: manual | manual_flatten | take_profit | eod | decay | infeasible_stop


@dataclass(frozen=True)
class LongSaleStarted(Event):
    entry_id: str
    side: str
    # RPT-07 long recovery (2026-07-11, operator ruling): the long's market
    # state at ladder start -- this IS the mark-at-stop for a push-detected
    # fill (~1s after the stop) and the honest best-available for a
    # fallback-detected one. mark_bid/mark_ask are the Quote
    # RecoverLong.recover() already receives; intrinsic is LEX-04's own
    # floor value for the same call. Optional/additive, None for every
    # pre-stamping recorded event (event-store codec's runtime-value tagging
    # handles absent-on-decode, same as StopPlaced.broker_order_id).
    mark_bid: Decimal | None = None
    mark_ask: Decimal | None = None
    intrinsic: Decimal | None = None


@dataclass(frozen=True)
class LongSaleRepriced(Event):
    entry_id: str
    side: str
    step: int
    price: Decimal


@dataclass(frozen=True)
class LexOrderPlaced(Event):
    """LEX-01 order-id journaling (v1.62, operator-ratified from the EOD-03
    wiring flag): every LEX ladder order journals its broker order id AT
    PLACEMENT, like all other orders (ORD-09 philosophy — an unjournaled
    order is unauditable). Follows the `DecayBuybackPlaced` (v1.61)
    precedent. Appended by RecoverLong on the initial rung submit, on EVERY
    cancel/replace (a replace mints a NEW broker id — each one is journaled),
    and on the LEX-05 marketable fallback submit — so the journal always
    names every broker id a LEX order ever carried, and the EOD-03 day-end
    sweep audits them via `_journaled_own_order_ids` (server.py), which reads
    `broker_order_id` generically off any event carrying the field.
    Additive/replay-safe: an old log simply contains none of these, and
    every field round-trips through the generic to_dict/from_dict paths."""
    entry_id: str
    side: str
    broker_order_id: str
    price: Decimal
    kind: str  # "ladder" (LEX-03 rung: initial submit or replace) | "fallback" (LEX-05)
               # | "floor" (EC-LEX-08 no-bid intrinsic-floor rest, v1.63)


@dataclass(frozen=True)
class ForeignDetected(Event):
    symbol: str  # OWN-03: FOREIGN quarantine, alert-only


@dataclass(frozen=True)
class ForeignReduction(Event):
    symbol: str  # OWN-06: broker shows less than ledger -> SUSPEND + write down
    from_qty: int
    to_qty: int


@dataclass(frozen=True)
class EntryCompleted(Event):
    entry_id: str


@dataclass(frozen=True)
class DayBrokerConfirmed(Event):
    """RPT-15 (doc 10): the EOD broker reconciliation found the day's
    bot-computed numbers (flat check, fill count, cash delta, fees) matched
    broker truth exactly. Stamps the day broker-confirmed for UI-25's trust
    badge. `checked` is a snapshot of the field->bot-value pairs that were
    compared, recorded for the drill-down (str values -- see ReportReconciler,
    application/report_reconciler.py, which is the ONLY writer of this event)."""
    date: str
    at: str  # ISO wall-clock timestamp of the reconciliation
    checked: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CorrectionRecord(Event):
    """RPT-15 (doc 10, operator's zero-drift rule): the EOD broker
    reconciliation found ONE field of a day's bot-computed numbers disagreed
    with broker truth. Both values and the diff are recorded -- the dashboard
    corrects to `broker_value` (reporting/corrections.py), but NEVER silently:
    this event is the sole permission slip for a rendered number to differ
    from the plain projection fold, and it is always visible in the drill-down.
    Written only by application/report_reconciler.py."""
    date: str
    field: str
    bot_value: str
    broker_value: str
    diff: str
    at: str


@dataclass(frozen=True)
class EntryMarkSample(Event):
    """RPT-12/D8 (doc 10): one per-open-entry mark sample, journaled at the
    ~1-minute health-tick cadence (server.py `_sample_marks_once`) so the day
    drill-down timeline and MAE/MFE can be computed LATER from recorded
    samples only — D10: absent is absent, NEVER interpolated or fabricated.
    Every mark field is optional independently: a leg outside the subscribed
    band, or a stale/absent chain snapshot, records that field as None rather
    than guessing (mirrors the live-P/L enricher's honesty rule, server.py
    `_live_pnl_enricher`)."""
    entry_id: str
    at: str  # ISO wall-clock timestamp of the sample
    spot: Decimal | None = None
    put_short_mid: Decimal | None = None
    put_long_mid: Decimal | None = None
    call_short_mid: Decimal | None = None
    call_long_mid: Decimal | None = None


@dataclass(frozen=True)
class ExternalFillImported(Event):
    """RPT-16 (proposed amendment, AMENDMENT-PROPOSAL-historical-backfill.md):
    one broker fill leg imported, one-time and operator-triggered, from
    broker history for a day that predates the durable event journal
    (REC-01, 2026-07-10). Deliberately NEVER `CondorFilled` -- imported
    history is data, not intent: the bot recorded no decision for these
    fills, only the broker's own record of what happened (REC-02: the log
    is authoritative for INTENT, and there is none to record here).

    OWN-03: `order_id` is always one of the operator-supplied bot/agent
    order ids -- application/backfill.py enforces this at import time; a
    fill outside that list is never represented by this event at all (it is
    counted `skipped_foreign` and dropped).

    Written only by application/backfill.py's `backfill_day`, which reads
    each field straight off the broker's own `Transaction` record (see that
    module's docstring for the exact SDK field mapping) -- never re-derived.

    `value` (operator ruling 2026-07-10, after the 2026-07-09 C7540 cash
    assignment showed an entry-credit-only view masking a real loss):
    the broker's own NET cash effect (`Transaction.net_value`) for a
    Receive-Deliver settlement row (cash-settled assignment / expiration /
    the paired zero-value removal) -- None for an ordinary Trade-style fill
    leg, which keeps computing its dollar effect from `price` x `quantity`
    (see reporting/folds.py `imported_fill_dollars`). Always signed and
    already net of that row's own fee; never fabricated for a Trade row.
    """
    day: str            # YYYY-MM-DD, the ET trading day this fill belongs to
    at: str              # ISO -- the fill's OWN broker-reported timestamp (Transaction.executed_at)
    order_id: str
    symbol: str
    action: str          # broker's own action string, e.g. "Sell to Open" | "Buy to Close",
                          # or -- for a settlement row -- the transaction_sub_type string,
                          # e.g. "Cash Settled Assignment" | "Expiration" | "Assignment"
    quantity: int
    price: Decimal | None    # broker-allocated fill price; None if the broker reported none
    fee: Decimal | None      # this leg's total fees (regulatory + clearing + commission +
                             # proprietary index option), as a POSITIVE cost -- None only if
                             # the broker reported no fee data at all for this leg
    imported_at: str     # ISO wall-clock timestamp of the IMPORT itself (audit trail, RPT-16(5))
    source: str          # e.g. "tastytrade_history"
    value: Decimal | None = None  # settlement rows only -- see docstring above


@dataclass(frozen=True)
class EodSweepCompleted(Event):
    """EOD-03: the end-of-day order-audit sweep ran to completion for `date`.

    Journaled ONCE per trading day by the live wiring's health tick (see
    adapters/api/server.py `_maybe_eod_sweep_once`) AFTER
    `application/eod_sweep.EndOfDaySweep.sweep()` returned. The presence of
    this event for a date is what makes the sweep once-per-day and idempotent
    across restarts -- the same journal-gating shape RPT-15's reconcile uses
    (DayBrokerConfirmed/CorrectionRecord).

    Counts only: the NAMES of any uncancellable/raced orders are already in
    the critical alerts EndOfDaySweep itself raised. EOD-03's own text is
    satisfied either way -- "the day does not end until the bot confirms zero
    working orders remain (or logs a critical alert naming each one it could
    not cancel)" -- so a sweep that completed WITH alerts is still complete
    and is not re-run; a sweep that CRASHED (broker unreachable) journals
    nothing and is retried on the next health tick, mirroring the
    reconcile's own retry rule."""
    date: str
    cancelled: int = 0
    uncancellable: int = 0
    raced_fills: int = 0


@dataclass(frozen=True)
class SettlementRecorded(Event):
    """EOD-01 v1.59 (operator-ratified, 2026-07-09 escalation): settlement
    cash is BROKER-JOURNALED, never merely computed. This is the LIVE path's
    ongoing settlement capture -- distinct from `ExternalFillImported`, which
    is RPT-16's ONE-TIME import of PRE-journal broker history. Written only
    by `application/settlement_capture.capture_settlements`, which fetches
    the broker's own Receive-Deliver transaction records (`day_settlements`,
    day+1 window) and attributes each row to a bot entry by symbol against
    that day's `CondorFilled` leg book -- never guessed: a row whose symbol
    is unattributable or falls under the OWN-03 shared-symbol guard is
    withheld entirely (counted `ambiguous_settlement` in the capture
    result), not journaled with a fabricated entry_id.

    `value` is the broker's own signed NET cash effect, real dollars,
    already net of `fee` -- the identical convention as
    `ExternalFillImported.value` on a settlement row (reporting/folds.py
    `imported_fill_dollars`). It flows into the entry's realized P&L at the
    real-dollar layer (reporting/folds.py `entry_dollars`), never inside the
    per-share `EntryProjection.pnl` -- the same reason `imported_fill_dollars`
    never re-scales a settlement row by the contract multiplier.

    Pinned real-day vector (the 2026-07-09 escalation this rule exists for):
    4 legs credit 3.60x100 - fees 4.88 = +355.12; SPX settled ~7543.64
    through the short C7540 -> settlement -369.00 (incl. $5 fee); true
    entry net -13.88."""
    entry_id: str
    day: str             # YYYY-MM-DD, the ET trading day this settlement belongs to
    at: str              # ISO -- the settlement's OWN broker-reported timestamp
    symbol: str
    sub_type: str        # broker's own transaction_sub_type, e.g. "Cash Settled
                          # Assignment" | "Expiration" | "Assignment"
    quantity: int
    price: Decimal | None   # settle strike reference, or None if the broker reported none
    value: Decimal          # signed NET cash effect, real dollars, already net of `fee`
    fee: Decimal | None = None  # this row's own fee, POSITIVE cost -- None only if the
                                 # broker reported no fee data at all for this row
    source: str = "tastytrade_receive_deliver"
