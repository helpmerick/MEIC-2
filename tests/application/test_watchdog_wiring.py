"""STP-03b stop-watchdog WIRING (2026-07-13) — `StopWatchdog` (application/
watchdog.py) was fully written and unit-tested (see tests/bdd/test_tc_stp_17.py
for the pure decision-logic scenarios) but never constructed, ticked, or wired
into the live app — grep confirmed the only references anywhere were a
health-panel counter (reports.py) and an activity-feed icon (app.py).

These tests pin the WIRING seam (`_stop_watchdog_pass`/`_run_stop_watchdog_loop`
in adapters/api/server.py): feeding the watchdog LIVE QuoteHub marks off the
SAME `_open_short_legs`/`_streamer_symbol` frames the stop-fill catch-up loop
and the P&L path already use, and supervising it exactly like every other
live background task (never crashes the app). The pure decision logic itself
(grace/escalate thresholds, DAT-02 pause, ORD-08 abort) is `StopWatchdog`'s
own job and already covered elsewhere; these tests prove the WIRING drives it
correctly and honestly, not a second copy of that logic.
"""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D

from meic.adapters.api.server import _run_stop_watchdog_loop, _stop_watchdog_pass
from meic.application.order_intent import protective_stop
from meic.application.watchdog import StopWatchdog
from meic.domain.events import CondorFilled, FilledLeg, ShortStopped, StopPlaced, WatchdogEscalated
from meic.domain.staleness import StampedQuote
from tests.harness.fake_broker import FakeBroker

ENTRY, SIDE = "e1", "PUT"
SHORT_SYM = "SPXW  260713P07535000"   # ORD-09 broker form (double space)
LONG_SYM = "SPXW  260713P07510000"
STREAMER = ".SPXW260713P7535"
TRIGGER = D("3.80")

NOW = datetime(2026, 7, 13, 15, 0, tzinfo=None)


class _FakeSnap:
    """Minimal stand-in for the held ChainSnapshot -- `_streamer_symbol` only
    ever reads `.streamer_symbols` off it."""

    def __init__(self, streamer_symbols):
        self.streamer_symbols = streamer_symbols


class _Snaps:
    def __init__(self, last):
        self.last = last


class _FakeHub:
    """Minimal stand-in for QuoteHub.mark -- a mutable symbol -> StampedQuote
    map so a test can swap the tick between passes (fresh/stale/absent)."""

    def __init__(self, marks: dict | None = None):
        self.marks = marks or {}

    def mark(self, symbol):
        return self.marks.get(symbol)


class _Alerts:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message, ctx))


class _Comp:
    def __init__(self, events, broker):
        self.events = events
        self.broker = broker


def _snap():
    return _FakeSnap({D("7535"): (STREAMER, ".SPXW260713C7535")})


def _condor_filled():
    return CondorFilled(entry_id=ENTRY, net_credit=D("3.60"), legs=(
        FilledLeg(symbol=SHORT_SYM, right="P", role="short", qty=1, price=D("1.80")),
        FilledLeg(symbol=LONG_SYM, right="P", role="long", qty=1, price=D("0.08")),
    ))


def _stop_placed(events, broker) -> str:
    """Places the resting stop at the FakeBroker AND journals the matching
    StopPlaced (with the broker's own order id, v1.60 shape) -- the SAME
    journaled-intent frame `_open_short_legs` requires."""
    resting_id = asyncio.run(broker.submit(protective_stop(
        entry_id=ENTRY, right="P", contracts=1, trigger=TRIGGER, symbol=SHORT_SYM,
        idempotency_key=f"stop:{ENTRY}:{SIDE}")))
    events.append(StopPlaced(entry_id=ENTRY, side=SIDE, trigger=TRIGGER, broker_order_id=resting_id))
    return resting_id


def _quote(mark: str, *, ask: str | None = None, at: datetime = NOW) -> StampedQuote:
    bid = D(mark) - D("0.05")
    return StampedQuote(STREAMER, bid, D(ask) if ask else D(mark) + D("0.05"), at)


# --- grace alert, no order yet ------------------------------------------------

def test_pass_alerts_at_grace_seconds_and_submits_nothing():
    events = [_condor_filled()]
    broker = FakeBroker()
    resting_id = _stop_placed(events, broker)
    alerts = _Alerts()
    wd = StopWatchdog(broker=broker, alerts=alerts, events=events,
                      grace_seconds=D("10"), escalate_seconds=D("20"))
    hub = _FakeHub({STREAMER: _quote("3.90", at=NOW)})
    comp = _Comp(events, broker)
    last_ticked: dict = {}

    # First sighting: elapsed must be credited as 0, never as time that
    # occurred before the watchdog was watching this short.
    asyncio.run(_stop_watchdog_pass(comp, wd, hub, _Snaps(_snap()), now=NOW,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))
    assert alerts.calls == []

    # 10 real seconds later, still breaching (a FRESH tick at this new
    # instant -- staleness is not what's under test here) -> grace alert.
    t10 = NOW + timedelta(seconds=10)
    hub.marks[STREAMER] = _quote("3.90", at=t10)
    asyncio.run(_stop_watchdog_pass(comp, wd, hub, _Snaps(_snap()), now=t10,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))

    assert any(level == "critical" for level, _msg, _ctx in alerts.calls)
    assert not any(isinstance(e, WatchdogEscalated) for e in events)
    assert not any(isinstance(e, ShortStopped) for e in events)
    marketable = [o for o in broker._orders.values() if o.intent.order_type == "marketable_limit"]
    assert marketable == []
    assert broker._orders[resting_id].status == "WORKING"


# --- escalate at bound: buy-to-close + cancel resting stop + journal --------

def test_pass_escalates_at_bound_cancels_resting_stop_and_journals_calibration():
    events = [_condor_filled()]
    broker = FakeBroker()
    resting_id = _stop_placed(events, broker)
    alerts = _Alerts()
    wd = StopWatchdog(broker=broker, alerts=alerts, events=events,
                      grace_seconds=D("10"), escalate_seconds=D("20"))
    hub = _FakeHub({STREAMER: _quote("3.90", ask="3.95", at=NOW)})
    comp = _Comp(events, broker)
    last_ticked: dict = {}

    asyncio.run(_stop_watchdog_pass(comp, wd, hub, _Snaps(_snap()), now=NOW,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))
    t10 = NOW + timedelta(seconds=10)
    hub.marks[STREAMER] = _quote("3.90", ask="3.95", at=t10)
    asyncio.run(_stop_watchdog_pass(comp, wd, hub, _Snaps(_snap()), now=t10,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))  # -> alert
    t20 = NOW + timedelta(seconds=20)
    hub.marks[STREAMER] = _quote("3.90", ask="3.95", at=t20)
    asyncio.run(_stop_watchdog_pass(comp, wd, hub, _Snaps(_snap()), now=t20,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))  # -> escalate

    marketable = [o for o in broker._orders.values()
                  if o.intent.order_type == "marketable_limit"
                  and o.intent.legs[0].action == "buy_to_close"]
    assert len(marketable) == 1, "the watchdog must submit its OWN marketable buy-to-close"
    assert marketable[0].intent.price == D("3.95"), "must fire at the ask, via order_intent.marketable_close"

    assert broker._orders[resting_id].status == "CANCELLED", (
        "the sleeping stop must be cancelled so it cannot later fire against a now-flat leg")

    stopped = [e for e in events if isinstance(e, ShortStopped)]
    assert len(stopped) == 1 and stopped[0].initiator == "watchdog_escalation"
    # PNL-01: a watchdog-escalated buy-to-close is still a CLOSE (commission-
    # free), but clearing/ORF/exchange still apply -- never the bare 0
    # default. Per-share: real $0.72 / 100.
    assert stopped[0].fee == D("0.0072")
    escalated = [e for e in events if isinstance(e, WatchdogEscalated)]
    assert len(escalated) == 1
    assert escalated[0].mark_at_breach == D("3.90")
    assert escalated[0].elapsed_seconds == D("20")
    assert escalated[0].fill_price == D("3.95")


# --- ORD-08: the resting stop already filled -> abort, submit NOTHING ------

def test_ord08_resting_stop_already_filled_before_escalation_submits_nothing_no_double_buy():
    """THE CATASTROPHIC FAILURE THIS GUARDS AGAINST: if the resting stop wins
    the race and the watchdog does not notice, the short gets bought back
    TWICE. The wiring must re-check broker truth (via StopWatchdog.escalate's
    own ORD-08 pre-check) immediately before submitting and abort cleanly if
    the stop already filled -- no marketable order, no ShortStopped, no
    WatchdogEscalated."""
    events = [_condor_filled()]
    broker = FakeBroker()
    resting_id = _stop_placed(events, broker)
    # The stop WON the race -- filled at the broker, exactly as ORD-08 governs.
    broker._orders[resting_id].status = "FILLED"
    alerts = _Alerts()
    wd = StopWatchdog(broker=broker, alerts=alerts, events=events,
                      grace_seconds=D("10"), escalate_seconds=D("20"))
    hub = _FakeHub({STREAMER: _quote("3.90", ask="3.95", at=NOW)})
    comp = _Comp(events, broker)
    last_ticked: dict = {}

    asyncio.run(_stop_watchdog_pass(comp, wd, hub, _Snaps(_snap()), now=NOW,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))
    t10 = NOW + timedelta(seconds=10)
    hub.marks[STREAMER] = _quote("3.90", ask="3.95", at=t10)
    asyncio.run(_stop_watchdog_pass(comp, wd, hub, _Snaps(_snap()), now=t10,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))
    # the breach clock genuinely reached grace (proving the mark was FRESH,
    # not accidentally paused by staleness) before the ORD-08 guard is what's
    # actually exercised at the escalate bound below.
    assert any(level == "critical" for level, _msg, _ctx in alerts.calls)

    t20 = NOW + timedelta(seconds=20)
    hub.marks[STREAMER] = _quote("3.90", ask="3.95", at=t20)
    asyncio.run(_stop_watchdog_pass(comp, wd, hub, _Snaps(_snap()), now=t20,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))  # would escalate

    marketable = [o for o in broker._orders.values() if o.intent.order_type == "marketable_limit"]
    assert marketable == [], "ORD-08: a filled resting stop must abort the escalation, submitting NOTHING"
    assert not any(isinstance(e, ShortStopped) for e in events)
    assert not any(isinstance(e, WatchdogEscalated) for e in events)
    assert broker._orders[resting_id].status == "FILLED"   # untouched, not double-cancelled


# --- DAT-02: a stale mark pauses the clock, a fresh one resumes it ----------

def test_dat02_stale_mark_pauses_the_clock_and_resumes_on_fresh_data():
    events = [_condor_filled()]
    broker = FakeBroker()
    _stop_placed(events, broker)
    alerts = _Alerts()
    wd = StopWatchdog(broker=broker, alerts=alerts, events=events,
                      grace_seconds=D("10"), escalate_seconds=D("20"))
    comp = _Comp(events, broker)
    last_ticked: dict = {}
    snaps = _Snaps(_snap())

    # t=0: first sighting, fresh, breaching -> 0s credited.
    hub = _FakeHub({STREAMER: _quote("3.90", at=NOW)})
    asyncio.run(_stop_watchdog_pass(comp, wd, hub, snaps, now=NOW,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))
    # t=5: still fresh -> 5s credited (5s elapsed total).
    t5 = NOW + timedelta(seconds=5)
    hub.marks[STREAMER] = _quote("3.90", at=t5)
    asyncio.run(_stop_watchdog_pass(comp, wd, hub, snaps, now=t5,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))
    # t=105: the quote goes STALE (100s old against a 3000ms bar) -- despite
    # 100 real seconds passing, the breach clock must NOT advance.
    t105 = t5 + timedelta(seconds=100)
    # hub.marks[STREAMER] left at its t5 stamp -> now 100s stale.
    asyncio.run(_stop_watchdog_pass(comp, wd, hub, snaps, now=t105,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))
    assert alerts.calls == [], "a stale mark must never contribute to the breach clock"

    # t=106: fresh again, +1s -> total accumulated is 5+1=6s, still < grace(10).
    t106 = t105 + timedelta(seconds=1)
    hub.marks[STREAMER] = _quote("3.90", at=t106)
    asyncio.run(_stop_watchdog_pass(comp, wd, hub, snaps, now=t106,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))
    assert alerts.calls == [], "resuming at 6s must still be below the 10s grace bound"

    # t=110: +4 more fresh seconds -> total 10s -> grace alert fires, proving
    # the clock genuinely resumed (never permanently stuck from the stale tick).
    t110 = t106 + timedelta(seconds=4)
    hub.marks[STREAMER] = _quote("3.90", at=t110)
    asyncio.run(_stop_watchdog_pass(comp, wd, hub, snaps, now=t110,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))
    assert any(level == "critical" for level, _msg, _ctx in alerts.calls)


def test_dat02_an_absent_mark_is_also_treated_as_stale_never_a_guessed_zero():
    """No hub tick at all for this streamer symbol (nothing subscribed yet, or
    the socket dropped) must pause the clock exactly like an aged one -- never
    fabricate a mark of 0 (which would be < trigger and silently reset the
    breach instead of pausing it)."""
    events = [_condor_filled()]
    broker = FakeBroker()
    _stop_placed(events, broker)
    alerts = _Alerts()
    wd = StopWatchdog(broker=broker, alerts=alerts, events=events,
                      grace_seconds=D("10"), escalate_seconds=D("20"))
    comp = _Comp(events, broker)
    last_ticked: dict = {}
    snaps = _Snaps(_snap())

    hub = _FakeHub({STREAMER: _quote("3.90", at=NOW)})
    asyncio.run(_stop_watchdog_pass(comp, wd, hub, snaps, now=NOW,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))
    t5 = NOW + timedelta(seconds=5)
    hub.marks[STREAMER] = _quote("3.90", at=t5)
    asyncio.run(_stop_watchdog_pass(comp, wd, hub, snaps, now=t5,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))

    hub.marks.pop(STREAMER)   # the tick vanishes entirely
    t20 = t5 + timedelta(seconds=15)
    asyncio.run(_stop_watchdog_pass(comp, wd, hub, snaps, now=t20,
                                    max_quote_age_ms=3000, last_ticked=last_ticked))

    assert alerts.calls == [], "an absent mark must pause the clock, not fabricate a below-trigger reset"


# --- mark below trigger never escalates -------------------------------------

def test_mark_below_trigger_never_alerts_or_escalates():
    events = [_condor_filled()]
    broker = FakeBroker()
    resting_id = _stop_placed(events, broker)
    alerts = _Alerts()
    wd = StopWatchdog(broker=broker, alerts=alerts, events=events,
                      grace_seconds=D("10"), escalate_seconds=D("20"))
    comp = _Comp(events, broker)
    last_ticked: dict = {}
    snaps = _Snaps(_snap())

    now = NOW
    for _ in range(6):   # 6 x 10s = 60s of wall time, well past escalate_seconds
        hub = _FakeHub({STREAMER: _quote("3.50", at=now)})   # below TRIGGER (3.80)
        asyncio.run(_stop_watchdog_pass(comp, wd, hub, snaps, now=now,
                                        max_quote_age_ms=3000, last_ticked=last_ticked))
        now += timedelta(seconds=10)

    assert alerts.calls == []
    assert not any(isinstance(e, (ShortStopped, WatchdogEscalated)) for e in events)
    assert broker._orders[resting_id].status == "WORKING"


# --- the loop never crashes the app ------------------------------------------

class _ExplodingHub:
    def mark(self, symbol):
        raise RuntimeError("hub lookup blew up")


def test_run_stop_watchdog_loop_swallows_a_pass_failure_and_keeps_running():
    events = [_condor_filled()]
    broker = FakeBroker()
    _stop_placed(events, broker)
    alerts = _Alerts()
    wd = StopWatchdog(broker=broker, alerts=alerts, events=events)
    comp = _Comp(events, broker)

    class _Clock:
        def now(self):
            return NOW

    async def _drive():
        task = asyncio.create_task(_run_stop_watchdog_loop(
            comp, wd, _ExplodingHub(), _Snaps(_snap()), alerts, clock=_Clock(),
            max_quote_age_ms=3000, idle_seconds=0.01, connected=lambda: True))
        try:
            await asyncio.wait_for(task, timeout=0.1)
        except asyncio.TimeoutError:
            pass   # expected -- the loop runs forever, that's the point
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_drive())

    assert len(alerts.calls) >= 1
    assert all(level == "warning" for level, _msg, _ctx in alerts.calls)
    assert all("stop watchdog" in msg for _level, msg, _ctx in alerts.calls)


def test_run_stop_watchdog_loop_idles_without_touching_the_hub_when_not_connected():
    events = [_condor_filled()]
    broker = FakeBroker()
    _stop_placed(events, broker)
    alerts = _Alerts()
    wd = StopWatchdog(broker=broker, alerts=alerts, events=events)
    comp = _Comp(events, broker)

    class _Clock:
        def now(self):
            return NOW

    async def _drive():
        task = asyncio.create_task(_run_stop_watchdog_loop(
            comp, wd, _ExplodingHub(), _Snaps(_snap()), alerts, clock=_Clock(),
            max_quote_age_ms=3000, idle_seconds=0.01, connected=lambda: False))
        try:
            await asyncio.wait_for(task, timeout=0.05)
        except asyncio.TimeoutError:
            pass
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_drive())

    assert alerts.calls == []   # never touched the (exploding) hub -- nothing failed
