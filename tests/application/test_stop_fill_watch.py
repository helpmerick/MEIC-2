"""EC-STP-06 live stop-fill catch-up (application/stop_fill_watch.py).

Covers the 2026-07-10 11:56 gap directly: a resting stop (the C7565 CALL
stop, order 482621556, filled 11:56:15 ET) fills while the bot is up, and
reconcile.py's EC-STP-06 triage ("did the stop fill? -> run LEX") had never
been re-run outside boot. These tests drive `detect_and_recover_stop_fills`
as the live health tick would, against a scripted fake broker -- no FastAPI,
no real market data.
"""
import asyncio
from decimal import Decimal as D

from meic.application.recover_long import Quote
from meic.application.stop_fill_watch import detect_and_recover_stop_fills
from meic.domain.events import (
    CondorFilled,
    DecayBuybackPlaced,
    EntryClosed,
    FilledLeg,
    LongSaleStarted,
    ShortStopped,
    SideClosed,
    StopPlaced,
)

ENTRY_ID = "2026-07-10#1"
PUT_SHORT, PUT_LONG = "SPXW  260710P07525000", "SPXW  260710P07505000"
CALL_SHORT, CALL_LONG = "SPXW  260710C07550000", "SPXW  260710C07570000"


def _condor_filled_events():
    legs = (
        FilledLeg(symbol=PUT_LONG, right="P", role="long", qty=1, price=D("0.10")),
        FilledLeg(symbol=PUT_SHORT, right="P", role="short", qty=1, price=D("1.50")),
        FilledLeg(symbol=CALL_SHORT, right="C", role="short", qty=1, price=D("2.00")),
        FilledLeg(symbol=CALL_LONG, right="C", role="long", qty=1, price=D("0.08")),
    )
    return [CondorFilled(entry_id=ENTRY_ID, net_credit=D("3.32"), legs=legs)]


class _FakeBroker:
    def __init__(self):
        self.working = []
        self.fills = []
        self.fill_legs_by_order = {}
        self.positions_ = []

    async def working_orders(self):
        return list(self.working)

    async def fills_since(self, cursor):
        return list(self.fills)

    async def fill_legs(self, order_id):
        return self.fill_legs_by_order.get(str(order_id), ())

    async def positions(self):
        return list(self.positions_)


class _RecoverSpy:
    def __init__(self):
        self.calls: list[dict] = []

    async def recover(self, **kw):
        self.calls.append(kw)


class _Alerts:
    def __init__(self):
        self.calls: list[tuple] = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message, ctx))


class _Comp:
    def __init__(self, broker, events, recover):
        self.broker = broker
        self.events = events
        self.recover = recover


async def _quote_provider(symbol, side):
    return Quote(bid=D("0.35"), ask=D("0.45")), D("0")


def _run(comp, alerts, quote_provider=_quote_provider):
    asyncio.run(detect_and_recover_stop_fills(comp, alerts, quote_provider))


# --- symbol-fallback path (a stop placed before broker_order_id existed) -------

def test_catch_up_via_symbol_when_no_broker_order_id_recorded():
    """Exactly the 2026-07-10 shape: StopPlaced predates broker_order_id, so
    detection falls back to matching the CALL short's own broker-reported
    symbol among buy-to-close fills."""
    events = list(_condor_filled_events())
    events.append(StopPlaced(entry_id=ENTRY_ID, side="CALL", trigger=D("3.80")))  # no broker_order_id
    broker = _FakeBroker()
    broker.fills = [{"order_id": "STOP-9", "legs": [{"symbol": CALL_SHORT, "action": "buy_to_close"}]}]
    broker.fill_legs_by_order["STOP-9"] = (
        FilledLeg(symbol=CALL_SHORT, right="C", role="short", qty=1, price=D("3.85")),)
    broker.positions_ = [{"symbol": CALL_LONG, "signed_qty": 1}]  # long still held
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    _run(comp, alerts)

    stopped = [e for e in events if isinstance(e, ShortStopped)]
    assert len(stopped) == 1 and stopped[0].side == "CALL" and stopped[0].fill == D("3.85")
    assert len(recover.calls) == 1
    call = recover.calls[0]
    assert call["entry_id"] == ENTRY_ID and call["side"] == "CALL" and call["long_symbol"] == CALL_LONG

    # No duplicate LongSaleStarted marker (review finding 2): the detector
    # strips plan.run_lex before execute() -- the marker belongs to
    # RecoverLong.recover() alone. The spy here journals nothing, so ZERO
    # markers proves detection itself appended none.
    assert not any(isinstance(e, LongSaleStarted) for e in events)

    # idempotent once the ladder TERMINATES (the real RecoverLong journals
    # these on fill; the spy doesn't, so simulate the terminal): a second
    # tick neither re-detects the stop nor re-drives the ladder.
    from meic.domain.events import LongSold, SideClosed
    events.append(LongSold(entry_id=ENTRY_ID, side="CALL", recovery=D("0.40")))
    events.append(SideClosed(entry_id=ENTRY_ID, side="CALL"))
    _run(comp, alerts)
    assert len([e for e in events if isinstance(e, ShortStopped)]) == 1
    assert len(recover.calls) == 1


# --- precise broker_order_id path (every stop placed since v1.60) -------------

def test_catch_up_via_precise_broker_order_id():
    events = list(_condor_filled_events())
    events.append(StopPlaced(entry_id=ENTRY_ID, side="PUT", trigger=D("3.80"),
                             broker_order_id="STOP-1"))
    broker = _FakeBroker()
    broker.fills = [{"order_id": "STOP-1", "partial": False}]
    broker.fill_legs_by_order["STOP-1"] = (
        FilledLeg(symbol=PUT_SHORT, right="P", role="short", qty=1, price=D("3.82")),)
    broker.positions_ = [{"symbol": PUT_LONG, "signed_qty": 1}]
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    _run(comp, alerts)

    stopped = [e for e in events if isinstance(e, ShortStopped)]
    assert len(stopped) == 1 and stopped[0].fill == D("3.82")
    assert len(recover.calls) == 1 and recover.calls[0]["long_symbol"] == PUT_LONG


# --- still resting: nothing to do ---------------------------------------------

def test_still_resting_stop_is_left_alone():
    events = list(_condor_filled_events())
    events.append(StopPlaced(entry_id=ENTRY_ID, side="PUT", trigger=D("3.80"),
                             broker_order_id="STOP-1"))
    broker = _FakeBroker()
    broker.working = [{"order_id": "STOP-1", "legs": [{"symbol": PUT_SHORT, "action": "buy_to_close"}]}]
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    _run(comp, alerts)

    assert not any(isinstance(e, ShortStopped) for e in events)
    assert recover.calls == []


# --- OWN standdown (operator ruling 2026-07-10) --------------------------------

def test_own_standdown_when_long_no_longer_held_at_broker():
    """A CAUGHT-UP fill may be old news: the operator could have sold the
    orphaned long directly at the broker while detection lagged. ShortStopped
    is still recorded (the stop DID fire — honesty), but LEX is never invoked
    and no order is submitted; an info alert notes the disposition."""
    events = list(_condor_filled_events())
    events.append(StopPlaced(entry_id=ENTRY_ID, side="CALL", trigger=D("3.80")))
    broker = _FakeBroker()
    broker.fills = [{"order_id": "STOP-9", "legs": [{"symbol": CALL_SHORT, "action": "buy_to_close"}]}]
    broker.fill_legs_by_order["STOP-9"] = (
        FilledLeg(symbol=CALL_SHORT, right="C", role="short", qty=1, price=D("3.85")),)
    broker.positions_ = []   # the long is GONE -- operator disposed of it directly
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    _run(comp, alerts)

    stopped = [e for e in events if isinstance(e, ShortStopped)]
    assert len(stopped) == 1, "the stop fill is still recorded honestly"
    assert recover.calls == [], "LEX must NOT be invoked -- the long is already gone"
    assert any(level == "info" and "standing down" in msg for level, msg, _ in alerts.calls)


# --- no usable market data yet: defer THEN RETRY (never strand the long) -------

def test_quote_less_detection_tick_retries_lex_on_a_later_tick():
    """The stranded-LEX case (lead review finding, 2026-07-10): detection
    journals ShortStopped, so the side leaves `_open_short_legs` forever --
    if the quote guard then defers, ONLY the `_pending_lex_sides` set can
    ever drive the ladder again before a restart. Tick 1 (stale snapshot):
    stop recorded, no LEX order. Tick 2 (quote back): recover() runs exactly
    once. Tick 3: the pending set skips it (LongSold/SideClosed recorded)."""
    events = list(_condor_filled_events())
    events.append(StopPlaced(entry_id=ENTRY_ID, side="CALL", trigger=D("3.80")))
    broker = _FakeBroker()
    broker.fills = [{"order_id": "STOP-9", "legs": [{"symbol": CALL_SHORT, "action": "buy_to_close"}]}]
    broker.fill_legs_by_order["STOP-9"] = (
        FilledLeg(symbol=CALL_SHORT, right="C", role="short", qty=1, price=D("3.85")),)
    broker.positions_ = [{"symbol": CALL_LONG, "signed_qty": 1}]
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    async def _no_quote(symbol, side):
        return None

    # tick 1: detection lands on a stale-snapshot tick -- record, defer
    _run(comp, alerts, quote_provider=_no_quote)
    assert any(isinstance(e, ShortStopped) for e in events)
    assert recover.calls == []  # deferred, never guessed

    # tick 2: the snapshot is fresh again -- the resume set re-drives LEX
    _run(comp, alerts)
    assert len(recover.calls) == 1, "the deferred side must be recovered on the next usable tick"
    call = recover.calls[0]
    assert call["entry_id"] == ENTRY_ID and call["side"] == "CALL" and call["long_symbol"] == CALL_LONG

    # the real RecoverLong would journal LongSold/SideClosed on fill; simulate
    # the terminal so tick 3 proves the resume set stops retrying
    from meic.domain.events import LongSold, SideClosed
    events.append(LongSold(entry_id=ENTRY_ID, side="CALL", recovery=D("0.40")))
    events.append(SideClosed(entry_id=ENTRY_ID, side="CALL"))
    _run(comp, alerts)
    assert len(recover.calls) == 1, "a terminated side must never be re-driven"


# --- double-ladder guard: a resting rung/fallback blocks a second ladder -------

def test_mid_lex_side_with_a_working_sell_to_close_submits_nothing():
    """The critical guard on the retry loop: a prior tick's LEX rung (or its
    LEX-05 marketable fallback) still WORKING at the broker means this tick
    must submit NOTHING -- a 60s loop re-entering the ladder beside a resting
    sell would stack duplicate sells every minute, the exact incident-#2
    class this package exists to kill."""
    events = list(_condor_filled_events())
    events.append(StopPlaced(entry_id=ENTRY_ID, side="CALL", trigger=D("3.80")))
    events.append(ShortStopped(entry_id=ENTRY_ID, side="CALL", fill=D("3.85"), slippage=D("0.05")))
    events.append(LongSaleStarted(entry_id=ENTRY_ID, side="CALL"))  # mid-LEX
    broker = _FakeBroker()
    # last tick's fallback still resting at the broker
    broker.working = [{"order_id": "LEX-1", "legs": [{"symbol": CALL_LONG, "action": "sell_to_close"}]}]
    broker.positions_ = [{"symbol": CALL_LONG, "signed_qty": 1}]
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    _run(comp, alerts)
    _run(comp, alerts)  # and again -- the loop must stay quiet while it rests

    assert recover.calls == [], "never a second ladder beside a resting sell"


# --- standdown respected on the RETRY path too ----------------------------------

def test_own_standdown_respected_on_the_mid_lex_retry_path():
    """A mid-LEX side whose long is gone from broker positions (operator
    disposed of it directly while the ladder was deferred): the retry path
    stands down exactly like the fresh path -- no order, one info alert
    (deduped: an unresolved side must not re-alert every 60s tick)."""
    events = list(_condor_filled_events())
    events.append(StopPlaced(entry_id=ENTRY_ID, side="CALL", trigger=D("3.80")))
    events.append(ShortStopped(entry_id=ENTRY_ID, side="CALL", fill=D("3.85"), slippage=D("0.05")))
    events.append(LongSaleStarted(entry_id=ENTRY_ID, side="CALL"))  # mid-LEX
    broker = _FakeBroker()
    broker.positions_ = []   # the long is GONE -- operator disposed of it directly
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    _run(comp, alerts)
    _run(comp, alerts)  # second tick: still no order, and no alert spam

    assert recover.calls == [], "LEX must NOT be invoked on the retry path either"
    standdowns = [a for a in alerts.calls if a[0] == "info" and "standing down" in a[1]]
    assert len(standdowns) == 1, "alert once per side, not once per 60s tick"


# --- guard 1: a stopped side with NO recorded long is loud, once ----------------

def test_stopped_side_with_no_recorded_long_alerts_critical_once():
    """Review finding 4: guard 1 used to skip silently, so a side whose long
    was never broker-reported (ORD-09 gap) re-entered the pending set every
    tick forever with zero operator signal. Unrecoverable-without-operator
    -> critical, and exactly once, not once per 60s tick."""
    # CondorFilled with the CALL LONG missing from the broker-reported legs
    legs = (
        FilledLeg(symbol=PUT_LONG, right="P", role="long", qty=1, price=D("0.10")),
        FilledLeg(symbol=PUT_SHORT, right="P", role="short", qty=1, price=D("1.50")),
        FilledLeg(symbol=CALL_SHORT, right="C", role="short", qty=1, price=D("2.00")),
    )
    events = [CondorFilled(entry_id=ENTRY_ID, net_credit=D("3.32"), legs=legs)]
    events.append(StopPlaced(entry_id=ENTRY_ID, side="CALL", trigger=D("3.80")))
    events.append(ShortStopped(entry_id=ENTRY_ID, side="CALL", fill=D("3.85"), slippage=D("0.05")))
    broker = _FakeBroker()
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    _run(comp, alerts)
    _run(comp, alerts)  # second tick: no re-alert

    assert recover.calls == [], "nothing to sell can be identified -- no order, ever"
    criticals = [a for a in alerts.calls if a[0] == "critical" and "NO broker-reported" in a[1]]
    assert len(criticals) == 1, "critical alert exactly once, not once per tick"


# --- SDK object shapes, end to end (review finding 5) ---------------------------

def test_sdk_object_shapes_detection_and_standdown_end_to_end():
    """`_order_legs`/`_leg_symbol`/`_leg_action`/`_long_still_held` were only
    ever exercised against dicts, but the live TastytradeAdapter yields SDK
    OBJECTS (`.symbol`/`.action`/`.quantity_direction`, no `.get`) -- the
    exact shape-mismatch class behind the 2026-07-09 live bugs. This drives
    detection AND standdown through LiveShapedBroker's SDK shapes: orders
    placed through the real submit path, a stop marked traded-through, and
    positions returned as SDK-shaped objects."""
    from datetime import datetime
    from meic.application.order_intent import protective_stop
    from tests.harness.fake_clock import ET, FakeClock
    from tests.harness.live_broker import LiveShapedBroker

    async def scenario():
        clock = FakeClock(datetime(2026, 7, 10, 12, 0, tzinfo=ET))
        broker = LiveShapedBroker(clock)
        events = list(_condor_filled_events())
        call_stop = await broker.submit(protective_stop(
            entry_id=ENTRY_ID, right="C", contracts=1, trigger=D("3.80"),
            symbol=CALL_SHORT, idempotency_key=f"stop:{ENTRY_ID}:CALL"))
        put_stop = await broker.submit(protective_stop(
            entry_id=ENTRY_ID, right="P", contracts=1, trigger=D("3.80"),
            symbol=PUT_SHORT, idempotency_key=f"stop:{ENTRY_ID}:PUT"))
        events.append(StopPlaced(entry_id=ENTRY_ID, side="CALL", trigger=D("3.80"),
                                 broker_order_id=str(call_stop)))
        events.append(StopPlaced(entry_id=ENTRY_ID, side="PUT", trigger=D("3.80"),
                                 broker_order_id=str(put_stop)))
        broker.fill_stop(call_stop)                    # the CALL stop trades through
        broker.set_positions([(CALL_LONG, 1, "Long")])  # SDK-shaped position objects
        recover, alerts = _RecoverSpy(), _Alerts()
        comp = _Comp(broker, events, recover)

        # tick 1: detection via SDK order objects (fills_since/_fill_matches,
        # working_orders legs) and _long_still_held via SDK position objects
        await detect_and_recover_stop_fills(comp, alerts, _quote_provider)
        stopped = [e for e in events if isinstance(e, ShortStopped)]
        assert [e.side for e in stopped] == ["CALL"], "only the traded-through stop detected"
        assert stopped[0].fill == D("3.80")  # no per-leg allocation -> trigger fallback
        assert [(c["entry_id"], c["side"]) for c in recover.calls] == [(ENTRY_ID, "CALL")]
        assert recover.calls[0]["long_symbol"] == CALL_LONG
        # the PUT stop is still WORKING (SDK-shaped) -- untouched

        # tick 2: the operator disposed of the long directly -> standdown,
        # judged off SDK-shaped positions() (empty now)
        broker.set_positions([])
        await detect_and_recover_stop_fills(comp, alerts, _quote_provider)
        assert len(recover.calls) == 1, "no second ladder after the long is gone"
        standdowns = [a for a in alerts.calls if a[0] == "info" and "standing down" in a[1]]
        assert len(standdowns) == 1

    asyncio.run(scenario())


# --- R3-F1: the ORD-08a raced side on a CLOSED entry still gets LEX -------------

def test_ord08a_raced_side_on_a_closed_entry_still_gets_lex():
    """CLS-01(2)'s race path (close_entry.py `_replace_stop` -> "FILLED"):
    the stop beat the close's replace, ShortStopped journaled, NO SideClosed
    for that side, its long excluded from CLS's own sells ("LEX owns that
    side's long sale") -- then EntryClosed lands. The blanket entry-closed
    skip orphaned that long forever, on EVERY close initiator (R3-F1
    BLOCKING). The side must be pending and LEX driven through the guards."""
    events = list(_condor_filled_events())
    # the close: CALL stop raced to FILLED; PUT closed normally; entry closed
    events.append(ShortStopped(entry_id=ENTRY_ID, side="CALL", fill=D("3.85"),
                               slippage=D("0.05"), initiator="resting_stop"))
    events.append(SideClosed(entry_id=ENTRY_ID, side="PUT"))
    events.append(EntryClosed(entry_id=ENTRY_ID, initiator="manual"))
    broker = _FakeBroker()
    broker.positions_ = [{"symbol": CALL_LONG, "signed_qty": 1}]  # the orphaned long
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    _run(comp, alerts)

    assert [(c["entry_id"], c["side"]) for c in recover.calls] == [(ENTRY_ID, "CALL")]
    assert recover.calls[0]["long_symbol"] == CALL_LONG


def test_decay_closed_side_is_never_lex_driven():
    """DCY-03: a decay-closed side's long is LEFT TO EXPIRE, never LEX-sold.
    decay_watcher.complete() journals ShortStopped(initiator="decay") +
    EntryClosed(initiator="decay") atomically; R3-F1's closed-entry widening
    must NOT sweep this side into a ladder -- no order, no alert, ever."""
    events = list(_condor_filled_events())
    events.append(ShortStopped(entry_id=ENTRY_ID, side="CALL", fill=D("0.05"),
                               slippage=D("0"), initiator="decay"))
    events.append(EntryClosed(entry_id=ENTRY_ID, initiator="decay"))
    broker = _FakeBroker()
    broker.positions_ = [{"symbol": CALL_LONG, "signed_qty": 1}]  # long held, left to expire
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    _run(comp, alerts)
    _run(comp, alerts)

    assert recover.calls == [], "a decay long must never be LEX-sold (DCY-03)"
    assert alerts.calls == [], "and it is not an alertable condition either"


def test_resting_stopped_side_on_a_decay_closed_entry_is_still_lex_driven():
    """Final-review finding (2026-07-10): the decay exemption must be SIDE-
    level only. A CALL stopped earlier by its resting stop (ladder not yet
    started -- e.g. quote-deferred) must still be LEX-driven after the PUT
    decays and decay_watcher journals the ENTRY-level EntryClosed
    (initiator="decay"); an entry-level exemption would strand that CALL
    long forever -- the R3-F1 orphan class re-opened. The PUT's own decay
    side stays exempt (DCY-03)."""
    events = list(_condor_filled_events())
    events.append(ShortStopped(entry_id=ENTRY_ID, side="CALL", fill=D("3.85"),
                               slippage=D("0.05"), initiator="resting_stop"))
    events.append(ShortStopped(entry_id=ENTRY_ID, side="PUT", fill=D("0.05"),
                               slippage=D("0"), initiator="decay"))
    events.append(EntryClosed(entry_id=ENTRY_ID, initiator="decay"))
    broker = _FakeBroker()
    broker.positions_ = [{"symbol": CALL_LONG, "signed_qty": 1},
                         {"symbol": PUT_LONG, "signed_qty": 1}]
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    _run(comp, alerts)

    assert len(recover.calls) == 1, "the resting-stopped CALL must be LEX-driven"
    call = recover.calls[0]
    assert call["side"] == "CALL" and call["long_symbol"] == CALL_LONG
    assert not any(c["side"] == "PUT" for c in recover.calls), \
        "the decay PUT long is left to expire (DCY-03)"


# --- STP-08a (v1.61): a DCY buyback fill is NEVER a stop-out --------------------

def test_decay_buyback_fill_classifies_side_closed_decay_never_a_stop_out():
    """STP-08a: DecayWatcher.buyback() journals its order id at placement
    (DecayBuybackPlaced); a detected fill matching that id classifies the side
    SIDE_CLOSED_DECAY -- journaled with decay_watcher.complete()'s exact shape
    (ShortStopped initiator="decay" + EntryClosed initiator="decay") -- the
    long is left to expire (DCY-03) and NO LEX ladder ever starts. Exercises
    the symbol-fallback era (StopPlaced without broker_order_id): without the
    id journal, `_resolve_by_symbol` would have misread this buy-to-close on
    the short's own symbol as a stop-out (the docstring's old latent hazard)."""
    events = list(_condor_filled_events())
    events.append(StopPlaced(entry_id=ENTRY_ID, side="CALL", trigger=D("3.80")))
    events.append(DecayBuybackPlaced(entry_id=ENTRY_ID, side="CALL",
                                     broker_order_id="DCY-1", price=D("0.05")))
    broker = _FakeBroker()
    broker.fills = [{"order_id": "DCY-1", "legs": [{"symbol": CALL_SHORT, "action": "buy_to_close"}]}]
    broker.fill_legs_by_order["DCY-1"] = (
        FilledLeg(symbol=CALL_SHORT, right="C", role="short", qty=1, price=D("0.05")),)
    broker.positions_ = [{"symbol": CALL_LONG, "signed_qty": 1}]  # long held, left to expire
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    _run(comp, alerts)

    stopped = [e for e in events if isinstance(e, ShortStopped)]
    assert len(stopped) == 1 and stopped[0].initiator == "decay", \
        "the DCY buyback fill must classify as decay, never as a stop-out"
    assert stopped[0].fill == D("0.05") and stopped[0].slippage == D("0")
    # PNL-01: a decay buyback synthesized here is still a CLOSE (commission-
    # free), but clearing/ORF/exchange still apply -- never the bare 0
    # default. Per-share: real $0.72 / 100.
    assert stopped[0].fee == D("0.0072")
    closed = [e for e in events if isinstance(e, EntryClosed)]
    assert len(closed) == 1 and closed[0].initiator == "decay"
    assert recover.calls == [], "the decay long is left to expire -- no LEX ladder (DCY-03)"
    assert not any(isinstance(e, LongSaleStarted) for e in events)

    # idempotent: a second tick re-detects nothing and still starts no ladder
    _run(comp, alerts)
    assert len([e for e in events if isinstance(e, ShortStopped)]) == 1
    assert recover.calls == []


def test_decay_buyback_fill_beats_the_order_id_era_ambiguous_path():
    """An order-id-era stop (broker_order_id recorded) that decay CANCELLED
    resolves 'not filled' by its own id -- previously 'ambiguous; leave for
    boot'. With the journaled buyback id, the up-front per-side check
    classifies the side decay-closed instead of leaving it dangling."""
    events = list(_condor_filled_events())
    events.append(StopPlaced(entry_id=ENTRY_ID, side="CALL", trigger=D("3.80"),
                             broker_order_id="STOP-7"))  # cancelled by decay, never filled
    events.append(DecayBuybackPlaced(entry_id=ENTRY_ID, side="CALL",
                                     broker_order_id="DCY-2", price=D("0.05")))
    broker = _FakeBroker()
    broker.fills = [{"order_id": "DCY-2", "legs": [{"symbol": CALL_SHORT, "action": "buy_to_close"}]}]
    broker.positions_ = [{"symbol": CALL_LONG, "signed_qty": 1}]
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    _run(comp, alerts)

    stopped = [e for e in events if isinstance(e, ShortStopped)]
    assert len(stopped) == 1 and stopped[0].initiator == "decay"
    assert stopped[0].fill == D("0.05"), \
        "no per-leg allocation -> the journaled buyback limit, never a guess"
    assert recover.calls == []


# --- R3-F2: a close completing MID-BUILD must not synthesize a false stop -------

def test_close_completing_mid_build_does_not_synthesize_a_false_short_stopped():
    """build_tracked_shorts folds once, then awaits the broker repeatedly; a
    close for the same side can complete in between -- after which the symbol
    fallback reads the CLOSE's own buy-to-close fill as a "stop fill". The
    synchronous re-check right before execute() must drop the side (its
    SideClosed is now on the log) so no FALSE ShortStopped is journaled."""
    events = list(_condor_filled_events())
    events.append(StopPlaced(entry_id=ENTRY_ID, side="CALL", trigger=D("3.80")))  # symbol-fallback era

    class _RacingBroker(_FakeBroker):
        """Simulates a concurrent close completing DURING build_tracked_shorts:
        the first broker await (working_orders) journals the close's SideClosed
        and lands its buy-to-close fill -- which the stale-fold symbol fallback
        then reads as a stop fill."""

        def __init__(self, events):
            super().__init__()
            self._events = events
            self._raced = False

        async def working_orders(self):
            if not self._raced:
                self._raced = True
                self._events.append(SideClosed(entry_id=ENTRY_ID, side="CALL"))
                self.fills.append({"order_id": "CLOSE-1",
                                   "legs": [{"symbol": CALL_SHORT, "action": "buy_to_close"}]})
            return list(self.working)

    broker = _RacingBroker(events)
    broker.positions_ = [{"symbol": CALL_LONG, "signed_qty": 1}]
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    _run(comp, alerts)

    assert not any(isinstance(e, ShortStopped) for e in events), \
        "a close's own buy-to-close fill must never be journaled as a stop fill"
    assert recover.calls == []


# --- R3-F3: unbounded silent quote deferral gets a one-time critical alert ------

def test_persistent_quote_deferral_alerts_critical_once_after_threshold():
    """A pending side whose long strike never gets marked would defer every
    tick to expiry with no signal. After QUOTE_DEFERRAL_ALERT_TICKS
    consecutive deferrals: ONE critical alert; deferral continues (a price is
    never guessed) with no re-alert."""
    from meic.application.stop_fill_watch import QUOTE_DEFERRAL_ALERT_TICKS

    events = list(_condor_filled_events())
    events.append(ShortStopped(entry_id=ENTRY_ID, side="CALL", fill=D("3.85"),
                               slippage=D("0.05"), initiator="resting_stop"))
    broker = _FakeBroker()
    broker.positions_ = [{"symbol": CALL_LONG, "signed_qty": 1}]
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    async def _no_quote(symbol, side):
        return None

    for tick in range(QUOTE_DEFERRAL_ALERT_TICKS - 1):
        _run(comp, alerts, quote_provider=_no_quote)
    assert not any(a[0] == "critical" for a in alerts.calls), "quiet below the threshold"

    _run(comp, alerts, quote_provider=_no_quote)   # tick N: the threshold
    _run(comp, alerts, quote_provider=_no_quote)   # tick N+1: no re-alert
    criticals = [a for a in alerts.calls if a[0] == "critical" and "no usable quote" in a[1]]
    assert len(criticals) == 1, "critical alert exactly once, then keep deferring"
    assert recover.calls == [], "still never guesses a price"


# --- STP-08a (v1.62): BOUNDED deferral, then the LEX-05 fallback -----------------
# "on invalid quotes LEX follows its own ratified path — deferral only within
# the retry cadence, then the marketable-at-bid fallback: a naked-side
# recovery never waits indefinitely" (STP-08a); "Invalid quote ⇒ skip to
# LEX-05 fallback after config.lex_quote_wait_seconds" (LEX-02). The bound is
# read as TOTAL ELAPSED since the side's first deferral (epoch instants off
# comp.clock), not a tick count — at the ~60s live cadence one full tick
# interval already exceeds the doc-06 range (1–30s), so in live this means
# "defer one tick, fall back on the next"; the tests pin the boundary exactly.

class _TickClock:
    def __init__(self, start):
        self._t = start

    def now(self):
        return self._t

    def advance(self, seconds):
        from datetime import timedelta
        self._t += timedelta(seconds=seconds)


def _stopped_side_comp():
    """A pending CALL side (ShortStopped, long still held) with a controllable
    clock — the STP-08a bounded-deferral scenarios all start here."""
    from datetime import datetime, timezone

    events = list(_condor_filled_events())
    events.append(ShortStopped(entry_id=ENTRY_ID, side="CALL", fill=D("3.85"),
                               slippage=D("0.05"), initiator="resting_stop"))
    broker = _FakeBroker()
    broker.positions_ = [{"symbol": CALL_LONG, "signed_qty": 1}]
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)
    comp.clock = _TickClock(datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc))
    return comp, recover, alerts


def test_stale_bid_defers_below_the_bound_and_falls_back_at_it():
    """The bounded-deferral boundary: a STALE quote (bid EXISTS, too old to
    ladder — LEX-02's age criterion) defers while total elapsed since the
    side's first deferral is under lex_quote_wait_seconds, and at the bound
    stops deferring: LEX starts via the LEX-05 fallback path
    (recover(quote_stale=True) — marketable limit at that bid)."""
    from meic.application.recover_long import Quote
    from meic.application.stop_fill_watch import StaleQuote

    comp, recover, alerts = _stopped_side_comp()
    stale = StaleQuote(quote=Quote(bid=D("0.35"), ask=D("0.45")), intrinsic=D("0"))

    async def _stale_quote(symbol, side):
        return stale

    def _tick():
        asyncio.run(detect_and_recover_stop_fills(
            comp, alerts, _stale_quote, lex_quote_wait_seconds=5.0))

    _tick()                       # first deferral: the window opens
    assert recover.calls == [], "never a fallback on the first invalid tick"
    comp.clock.advance(4.9)
    _tick()                       # 4.9s elapsed: still inside the window
    assert recover.calls == [], "defers below the bound"
    comp.clock.advance(0.1)
    _tick()                       # 5.0s elapsed: the bound — fall back NOW
    assert len(recover.calls) == 1, "falls back exactly at the bound"
    call = recover.calls[0]
    assert call["quote_stale"] is True and call["quote"] is stale.quote
    assert call["entry_id"] == ENTRY_ID and call["side"] == "CALL" \
        and call["long_symbol"] == CALL_LONG

    # the deferral window RESET with the hand-off: the side (still pending —
    # the spy journals nothing) re-defers on a fresh window rather than
    # spamming a fallback every subsequent tick (in live, the double-ladder
    # guard additionally skips while the fallback order rests).
    _tick()
    assert len(recover.calls) == 1
    assert not any(a[0] == "critical" for a in alerts.calls), \
        "an acted-on side never reached the R3-F3 alert threshold"


def test_no_bid_at_all_keeps_deferring_forever_with_the_alert_standing():
    """The honest edge the ratified text cannot reach: LEX-05 needs a BID, and
    quote_provider returning None means there is NO bid on record at all — a
    marketable order cannot be priced and a price is never invented. The side
    keeps deferring past ANY window, however much real time passes, and the
    R3-F3 one-time critical alert stands (no re-alert spam)."""
    from meic.application.stop_fill_watch import QUOTE_DEFERRAL_ALERT_TICKS

    comp, recover, alerts = _stopped_side_comp()

    async def _no_quote(symbol, side):
        return None

    for _ in range(QUOTE_DEFERRAL_ALERT_TICKS + 3):
        asyncio.run(detect_and_recover_stop_fills(
            comp, alerts, _no_quote, lex_quote_wait_seconds=5.0))
        comp.clock.advance(60)    # every tick is far past the window

    assert recover.calls == [], \
        "no bid ⇒ no fallback, ever — a price is never invented"
    criticals = [a for a in alerts.calls if a[0] == "critical" and "no usable quote" in a[1]]
    assert len(criticals) == 1, "the one-time critical alert stands (R3-F3 continuity)"


def test_usable_quote_arriving_mid_window_starts_the_normal_ladder():
    """A usable quote arriving before the window lapses clears the deferral
    state and starts the NORMAL ladder — recover() without quote_stale, the
    exact same call shape as an undeferred hand-off."""
    from meic.application.recover_long import Quote
    from meic.application.stop_fill_watch import StaleQuote

    comp, recover, alerts = _stopped_side_comp()
    stale = StaleQuote(quote=Quote(bid=D("0.35"), ask=D("0.45")), intrinsic=D("0"))
    fresh = (Quote(bid=D("0.40"), ask=D("0.50")), D("0"))
    feed = [stale, fresh, stale]

    async def _sequenced(symbol, side):
        return feed.pop(0)

    def _tick():
        asyncio.run(detect_and_recover_stop_fills(
            comp, alerts, _sequenced, lex_quote_wait_seconds=5.0))

    _tick()                       # stale: window opens, defer
    assert recover.calls == []
    comp.clock.advance(1)
    _tick()                       # usable, mid-window: NORMAL ladder
    assert len(recover.calls) == 1
    call = recover.calls[0]
    assert "quote_stale" not in call, "a usable quote starts the normal ladder"
    assert call["quote"] == fresh[0]

    # and the usable tick cleared the window: a later stale tick starts a
    # FRESH window (defers again) instead of falling back off stale state.
    comp.clock.advance(1)
    _tick()
    assert len(recover.calls) == 1, "a fresh window opens — no immediate fallback"
