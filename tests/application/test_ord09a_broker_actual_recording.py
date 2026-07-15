"""ORD-09a (v1.74 fix batch) — every recorded execution price is the
BROKER'S actual fill, never the order's limit/rung/intent price. Extends
v1.52's fill-credit rule (TC-ORD-08, entries only) to every other recording
seam: LEX long-sale recoveries, decay buybacks, and watchdog escalations.

INCIDENT CONTEXT: 2026-07-14 reconcile diverged bot -177.76 vs broker -162.76
($15, pure price, fees agreed exactly). Prime suspect: `LongSold.recovery`
journaled the LEX ladder's LIMIT price (the rung), not the broker's actual
fill — a sell limit can fill AT OR BETTER than its rung, so journaling the
rung under-reports every recovery that filled better and diverges the books
in the broker's favor.

Each test below is FAIL-FIRST evidence: before this fix, the seam recorded
the order's own limit/intent/cap price; a broker fill reported deliberately
BETTER than that price proves the seam now records the broker's number, not
its own.
"""
import asyncio
from decimal import Decimal as D

from meic.application.decay_watcher import DecayWatcher
from meic.application.recover_long import Quote, RecoverLong
from meic.application.watchdog import StopWatchdog
from meic.domain.events import FilledLeg, LongSold, ShortStopped, WatchdogEscalated
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker, Scripted
from tests.harness.fake_clock import FastClock
from tests.harness.intents import stop_intent

from datetime import datetime, timezone

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
LONG_SYM = "SPXW  260715P05940000"
SHORT_SYM = "SPXW  260715P05990000"


class _Alerts:
    def __init__(self):
        self.calls = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message, ctx))


# --- RecoverLong._sold: LEX ladder sells -------------------------------------

def test_ord09a_lex_ladder_records_broker_actual_recovery_not_rung():
    """The ladder's rung (mid of bid/ask) is 1.15; the broker reports the
    actual sell filled BETTER, at 1.20. LongSold.recovery must be 1.20, never
    the 1.15 rung (the exact $15-incident vector: recovery journaled at the
    rung when the broker paid better)."""
    broker, events = FakeBroker(), []
    broker.script_submit(Scripted("fill"))  # first rung (the mid) fills immediately

    async def fake_fill_legs(order_id):
        return (FilledLeg(symbol=LONG_SYM, right="P", role="long", qty=1, price=D("1.20")),)
    broker.fill_legs = fake_fill_legs

    result = asyncio.run(RecoverLong(broker, FastClock(NOW), events, SPX).recover(
        entry_id="e1", side="PUT", long_symbol=LONG_SYM,
        quote=Quote(bid=D("1.10"), ask=D("1.20")), intrinsic=D("0")))

    assert result.outcome == "SOLD"
    sold = next(e for e in events if isinstance(e, LongSold))
    assert sold.recovery == D("1.20"), "must be the broker-ACTUAL fill"
    assert sold.recovery != D("1.15"), "must never be the ladder rung (the mid)"


def test_ord09a_lex_ladder_falls_back_to_rung_when_broker_carries_no_price():
    """Honest fallback: paper/simulated fills report no per-leg allocation
    (adapters/occ.py::simulated_fill_legs always sets price=None) -- the
    rung is used, exactly as before, never fabricated."""
    broker, events = FakeBroker(), []
    broker.script_submit(Scripted("fill"))  # default fill_legs -> price=None per leg

    result = asyncio.run(RecoverLong(broker, FastClock(NOW), events, SPX).recover(
        entry_id="e1", side="PUT", long_symbol=LONG_SYM,
        quote=Quote(bid=D("1.10"), ask=D("1.20")), intrinsic=D("0")))

    assert result.outcome == "SOLD"
    sold = next(e for e in events if isinstance(e, LongSold))
    assert sold.recovery == D("1.15")  # the rung, honestly, no allocation existed


# --- DecayWatcher.complete: decay buybacks -----------------------------------

def test_ord09a_decay_buyback_records_broker_actual_fill_not_trigger():
    """The buyback rests at the 0.05 trigger; the broker reports the actual
    buy filled BETTER (lower), at 0.03. ShortStopped.fill must be 0.03, never
    the 0.05 trigger."""
    broker, events = FakeBroker(), []
    resting = asyncio.run(broker.submit(stop_intent("PUT")))
    w = DecayWatcher(broker, events)
    broker.script_submit(Scripted("fill"))  # the buyback order fills immediately

    buyback_id = asyncio.run(w.buyback(
        entry_id="e1", side="PUT", resting_stop_id=resting, symbol=SHORT_SYM))
    assert buyback_id != "STOP_FILLED_RUN_LEX"

    async def fake_fill_legs(order_id):
        return (FilledLeg(symbol=SHORT_SYM, right="P", role="short", qty=1, price=D("0.03")),)
    broker.fill_legs = fake_fill_legs

    asyncio.run(w.complete(entry_id="e1", side="PUT"))
    stopped = next(e for e in events if isinstance(e, ShortStopped) and e.initiator == "decay")
    assert stopped.fill == D("0.03"), "must be the broker-ACTUAL fill"
    assert stopped.fill != w.decay_buyback_trigger, "must never be the buyback's own trigger/limit"


def test_ord09a_decay_buyback_falls_back_to_trigger_when_broker_carries_no_price():
    """Honest fallback: no buyback ever placed on this instance (TC-DCY-04's
    own shape) -- `complete()` must not explode reaching for `_buyback_id`,
    and falls back to the trigger."""
    broker, events = FakeBroker(), []
    w = DecayWatcher(broker, events)
    asyncio.run(w.complete(entry_id="e9", side="CALL"))
    stopped = next(e for e in events if isinstance(e, ShortStopped) and e.initiator == "decay")
    assert stopped.fill == w.decay_buyback_trigger


# --- StopWatchdog.escalate: watchdog escalation ------------------------------

def test_ord09a_watchdog_escalation_records_broker_actual_not_ask_cap():
    """The escalation's marketable buy-to-close is capped at the 3.95 ask;
    the broker reports the actual fill BETTER (lower), at 3.90. Both
    ShortStopped.fill and WatchdogEscalated.fill_price must be 3.90, never
    the 3.95 ask cap."""
    broker = FakeBroker()
    broker.script_submit(Scripted("fill"))  # the escalation order fills immediately
    events: list = []
    wd = StopWatchdog(broker=broker, alerts=_Alerts(), events=events)

    async def fake_fill_legs(order_id):
        return (FilledLeg(symbol=SHORT_SYM, right="P", role="short", qty=1, price=D("3.90")),)
    broker.fill_legs = fake_fill_legs

    asyncio.run(wd.escalate(entry_id="e1", side="PUT", mark_at_breach=D("3.90"),
                           ask=D("3.95"), symbol=SHORT_SYM))

    stopped = [e for e in events if isinstance(e, ShortStopped)]
    assert len(stopped) == 1
    assert stopped[0].fill == D("3.90"), "must be the broker-ACTUAL fill"
    assert stopped[0].fill != D("3.95"), "must never be the ask cap the order was marketable at"

    escalated = [e for e in events if isinstance(e, WatchdogEscalated)]
    assert len(escalated) == 1
    assert escalated[0].fill_price == D("3.90")


def test_ord09a_watchdog_escalation_falls_back_to_ask_when_broker_carries_no_price():
    """Honest fallback: the escalation order has not yet posted a fill (a
    live marketable order may take a beat) -- the ask cap is used, exactly
    as before, never fabricated. Plain FakeBroker (no autofill/script), so
    the marketable_close stays WORKING and never appears in fills_since.
    No clock injected => the poll degrades to a single immediate check
    (legacy/unit shape), so this returns without waiting."""
    broker = FakeBroker()
    events: list = []
    wd = StopWatchdog(broker=broker, alerts=_Alerts(), events=events)

    asyncio.run(wd.escalate(entry_id="e1", side="PUT", mark_at_breach=D("3.90"),
                           ask=D("3.95"), symbol=SHORT_SYM))

    stopped = [e for e in events if isinstance(e, ShortStopped)]
    assert stopped[0].fill == D("3.95")
    escalated = [e for e in events if isinstance(e, WatchdogEscalated)]
    assert escalated[0].fill_price == D("3.95")


def test_ord09a_watchdog_poll_catches_a_fill_that_posts_on_the_second_poll():
    """PIN (v1.74 review finding A): in live, the escalation's fill record is
    NOT visible on the very next fills_since() GET after submit -- a single
    immediate read would fall back to the ask (the intent price) nearly
    always, leaving the seam effectively unfixed in production. The bounded
    poll (`_await_fill_price`, mirroring execute_entry._await_fill's
    first-check-immediate cadence) must catch a fill that posts on the
    SECOND poll and record the broker price, never the ask."""
    broker = FakeBroker()
    broker.script_submit(Scripted("fill"))  # the fill EXISTS at the broker...
    events: list = []
    wd = StopWatchdog(broker=broker, alerts=_Alerts(), events=events,
                      clock=FastClock(NOW))  # clock present => the poll is armed

    # ...but its record is not yet VISIBLE on the first fills_since() read
    # (live posting latency) -- it appears from the second read onward.
    real_fills_since = broker.fills_since
    reads = {"n": 0}

    async def latent_fills_since(cursor):
        reads["n"] += 1
        if reads["n"] == 1:
            return []  # the beat between submit and the fill posting
        return await real_fills_since(cursor)
    broker.fills_since = latent_fills_since

    async def fake_fill_legs(order_id):
        return (FilledLeg(symbol=SHORT_SYM, right="P", role="short", qty=1, price=D("3.90")),)
    broker.fill_legs = fake_fill_legs

    asyncio.run(wd.escalate(entry_id="e1", side="PUT", mark_at_breach=D("3.90"),
                           ask=D("3.95"), symbol=SHORT_SYM))

    assert reads["n"] >= 2, "the poll must have re-read the fills feed after the miss"
    stopped = [e for e in events if isinstance(e, ShortStopped)]
    assert len(stopped) == 1
    assert stopped[0].fill == D("3.90"), "the second-poll fill must record the BROKER price"
    assert stopped[0].fill != D("3.95"), "never the ask (the intent price)"
    escalated = [e for e in events if isinstance(e, WatchdogEscalated)]
    assert escalated[0].fill_price == D("3.90")


def test_ord09a_watchdog_poll_stops_immediately_on_a_priceless_fill_record():
    """A fill record that IS visible but carries no per-leg price (paper/
    simulated fills) must stop the poll at the first check -- more polling
    cannot invent an allocation the broker never reported -- and fall back
    to the ask honestly, even with a clock present."""
    broker = FakeBroker()
    broker.script_submit(Scripted("fill"))  # default fill_legs -> price=None per leg
    events: list = []
    reads = {"n": 0}
    real_fills_since = broker.fills_since

    async def counting_fills_since(cursor):
        reads["n"] += 1
        return await real_fills_since(cursor)
    broker.fills_since = counting_fills_since

    wd = StopWatchdog(broker=broker, alerts=_Alerts(), events=events,
                      clock=FastClock(NOW))

    asyncio.run(wd.escalate(entry_id="e1", side="PUT", mark_at_breach=D("3.90"),
                           ask=D("3.95"), symbol=SHORT_SYM))

    assert reads["n"] == 1, "a visible-but-priceless record must stop the poll immediately"
    stopped = [e for e in events if isinstance(e, ShortStopped)]
    assert stopped[0].fill == D("3.95")  # honest ask fallback
