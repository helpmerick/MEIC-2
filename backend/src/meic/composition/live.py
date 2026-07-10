"""Live-mode composition root — EC-RSK-04.

"Paper and live are structurally separate wirings, not a flag." This root
binds the BrokerGateway to the real TastytradeAdapter and MarketDataFeed to
the DXLinkAdapter; the SimulatedBroker is never imported or constructed here.
Same services, same event log — the domain is unaware of the mode.

Every real fill flows through the adapter's allocation reconciler (STP-02d);
per_side selection stays config-gated. Construction is I/O-free; connect()
opens the sessions (the issuer guard refuses a non-cert token before any
network call when is_test=True).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from meic.adapters.dxlink.adapter import DXLinkAdapter
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.adapters.tastytrade.adapter import TastytradeAdapter
from meic.application.close_entry import CloseEntry
from meic.application.execute_entry import ExecuteEntryAttempt
from meic.application.persistent_state import PersistentState
from meic.application.event_log import EventLog
from meic.application.leg_book import LegBook
from meic.application.protect_position import LegsUnrecorded, ProtectPosition, ShortLeg
from meic.application.recover_long import RecoverLong
from meic.application.run_trading_day import RunTradingDay
from meic.composition.close_assembly import DEFAULT_CLOSE_PRICE, assemble_close_inputs
from meic.domain.stop_policy import StopBasis
from meic.domain.ticks import TickTable


class _NullAlerts:
    def alert(self, *a, **k):
        pass


@dataclass
class LiveComposition:
    clock: object
    ticks: TickTable
    provider_secret: str
    refresh_token: str
    is_test: bool = True  # cert unless explicitly wired to production
    stop_basis: StopBasis = StopBasis.TOTAL_CREDIT
    # v1.44: an EventLog stamps every appended event with config_version.
    events: list = field(default_factory=EventLog)
    # RSK-04: entry_id -> structural worst case of each FILLED entry. Live had no
    # such record, so RSK-04 could not have been enforced here even if asked.
    worst_case: dict = field(default_factory=dict)
    state_store: object = None  # inject a SqliteStateStore for durable state (REC-07)

    def __post_init__(self) -> None:
        # BrokerGateway -> live adapter (SimulatedBroker is NOT constructed here)
        self.broker = TastytradeAdapter(self.provider_secret, self.refresh_token, is_test=self.is_test)
        self.feed = DXLinkAdapter(session=None, clock=self.clock)  # session set on connect()
        self.state = PersistentState(self.state_store or InMemoryStateStore())
        self.state.trading_mode = "live"  # DAY-05
        self.alerts = _NullAlerts()
        self.execute = ExecuteEntryAttempt(self.broker, self.clock, self.events, self.ticks,
                                           stop_basis=self.stop_basis)
        # STP-04 AUTO-FLATTEN: `self._auto_flatten_entry` is a bound method, not
        # evaluated until called, so it is safe to hand to ProtectPosition here
        # even though `self.close` (CloseEntry) is constructed a line later —
        # the closure resolves `self.close` at CALL time, not at construction.
        self.protect = ProtectPosition(self.broker, self.clock, self.alerts, self.events, self.ticks,
                                       close_entry=self._auto_flatten_entry)
        self.recover = RecoverLong(self.broker, self.events, self.ticks)
        self.close = CloseEntry(self.broker, self.events)
        self.day = RunTradingDay(self.clock, self.state, self.execute, self.events,
                                 on_filled=self._on_filled)

    async def connect(self, account_number: str | None = None) -> None:
        await self.broker.connect(account_number)
        self.feed._session = self.broker._session  # share the authenticated session

    async def _auto_flatten_entry(self, entry_id: str, initiator: str) -> None:
        """STP-04 AUTO-FLATTEN — ProtectPosition's `close_entry` callback.

        For weeks this hook existed on ProtectPosition (accepted a
        `close_entry` callback and called it on both the STP-02c post-fill
        infeasible path and STP-04 UNPROTECTED escalation) but NOTHING wired
        it here — an unconfirmed/undersized stop raised a critical alert and
        then did nothing further. This assembles the close inputs exactly the
        way PanelCommands.close() does for a manual close (ORD-09
        broker-truth legs from LegBook, stop ids correlated per side from the
        broker's own working orders — see composition/close_assembly.py,
        shared with the panel so there is exactly one assembly, not two) and
        routes through the ONE canonical CloseEntry (CLS-01/02).

        OPEN ITEM — side-scoped flatten (reported to the operator, not
        resolved here): `config.unprotected_action` (doc 06) distinguishes
        `flatten_side` (close only the unprotected side) from
        `flatten_condor` (close the whole entry). `ProtectPosition._go_unprotected`
        knows the side but the `close_entry` callback it invokes carries only
        `(entry_id, initiator)` — no side — and `CloseEntry.close` closes an
        entry's full `live_legs` in one call; there is no side-scoped
        variant. BOTH settings therefore produce a WHOLE-ENTRY close here.
        Honouring `flatten_side` narrowly needs a `CloseEntry` extension
        (e.g. an optional side filter on `live_legs`/`resting_stop_ids`) —
        deliberately NOT bolted on as a second close path (CLS-02 forbids a
        second implementation of the close procedure).

        Initiator note: CLS-02's operator-ratified initiator list is
        `{manual, manual_flatten, take_profit, eod, decay, infeasible_stop}`
        — `unprotected` is not in it, though `CloseEntry.VALID_INITIATORS`
        already carries it. STP-04 demands the flatten and `unprotected` is
        the honest, distinct label for why it happened, so it is kept as-is;
        the list discrepancy is flagged here for operator ratification, not
        silently patched around.
        """
        inputs = await assemble_close_inputs(self.events, self.broker, entry_id)
        if inputs is None or not inputs[0]:
            self.alerts.alert(
                "critical", "STP-04 auto-flatten: no broker-reported legs recorded for "
                "this entry; cannot close (ORD-09) — operator must intervene",
                entry_id=entry_id, initiator=initiator)
            return
        live_legs, stop_ids = inputs
        await self.close.close(entry_id, initiator, resting_stop_ids=stop_ids,
                               live_legs=live_legs, close_price=DEFAULT_CLOSE_PRICE)

    def _shorts(self, entry_id: str, condor) -> list[ShortLeg]:
        """ORD-09: the stops name the symbols the BROKER reported filling.

        No strike fallback (v1.46, operator-ratified hard refusal). If the broker
        recorded no legs we raise rather than reconstruct a symbol at action time:
        a stop resting on an instrument the broker never filled protects nothing.
        """
        book = LegBook.from_events(self.events)
        shorts = book.shorts(entry_id)
        if len(shorts) != 2:
            raise LegsUnrecorded(
                f"{entry_id}: broker reported {len(shorts)} short leg(s), expected 2 (ORD-09)")
        mids = {"PUT": condor.put_short_mid, "CALL": condor.call_short_mid}
        return [ShortLeg(l.side, mids[l.side], Decimal("0.50"), symbol=l.symbol) for l in shorts]

    async def _on_filled(self, entry_id: str, condor, stop=None, fill_credit=None) -> None:
        # RSK-04: record what this entry can lose, so later entries see the headroom.
        self.worst_case[entry_id] = ExecuteEntryAttempt.worst_case(condor)
        await self.protect.protect(
            entry_id=entry_id,
            # doc 06 section 37: this row's stop settings, falling back to the globals.
            basis=(stop.basis if stop else self.stop_basis),
            pct=(stop.pct if stop else self.execute.default_stop.pct),
            markup=(stop.markup if stop else self.execute.default_stop.markup),
            shorts=self._shorts(entry_id, condor),
            # BUG FIX (STP-02, 2026-07-09 incident): "trigger = pct x net credit"
            # means the ACTUAL fill credit, not the mid estimate — on the incident
            # day the stop rested at pct x mid instead of pct x the real 3.60
            # fill. `fill_credit` is the caller's ExecuteEntryAttempt.attempt()
            # outcome (None only when no fill is known, e.g. older call sites).
            total_net_credit=(fill_credit if fill_credit is not None else condor.mid_credit),
            # ENT-04 (v1.44): each stop is sized to the condor it protects.
            contracts=condor.contracts)
