"""TC-UND-02 (spec v1.86, UND-03/F3) — the /ES force-close invariant.

/ES is a futures-option underlying: American exercise ASSIGNS a futures
position, which would break the cash-settlement/defined-risk contract EOD-01
relies on for SPX/RUT. UND-03's F3 ruling (operator, 2026-07-21): /ES is
NEVER held to settlement -- every open /ES entry is force-closed via the ONE
canonical close (CLS-01/02, initiator "eod") before its mandatory, pre-16:00
`eod_close_time` (default 15:55 ET), with a hard `eod_close_deadline` after
which an unresolved leg raises an RSK-06 critical alert naming the position
(assignment risk). F3 explicitly scopes this to the SETTLEMENT/EOD window
only -- intraday early-assignment is accepted as documented residual risk,
never built against here.

TEST HARDENING (2026-07-21 safety review): these tests drive the scheduler
with UTC-aware datetimes -- the SAME thing the production clock
(`application/clocks.SystemClock.now`) returns -- at the wall-clock instants
that correspond to the ET policy times (e.g. 19:55 UTC == 15:55 EDT). A
regression to a naive/UTC `now.time()` comparison (the original defect: it
fired /ES ~4h early at 11:55 ET) therefore FAILS these tests. They no longer
inject ET-tzinfo instants that would hide the timezone conversion.

tests/features/TC-UND-02.feature (generated, read-only) is the definition of
done this binds to -- three scenarios: (1) /ES requires and enforces a
pre-16:00 force-close, (2) an unclosed /ES leg raises a critical alert, (3)
cash underlyings (SPX/RUT) are unchanged.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timezone
from decimal import Decimal as D

from meic.application.close_entry import CloseEntry
from meic.application.force_close_scheduler import (
    ForceCloseScheduler,
    UnderlyingEodPolicy,
    default_policies,
)
from meic.config.fee_model import FeeModel
from meic.domain.events import (
    CondorFilled,
    EntryClosed,
    FilledLeg,
    ForceCloseSweepCompleted,
    LongSold,
    ShortStopped,
)
from meic.domain.fees import fee_for_leg, fee_for_legs
from meic.domain.schedule import EntrySpec, ScheduleDefaults, resolve, validate_entry
from meic.domain.underlying import PROFILES
from tests.harness.fake_broker import FakeBroker, Scripted

ET_TZ = None  # resolved lazily below to avoid a hard zoneinfo import at collection time


def _et():
    global ET_TZ
    if ET_TZ is None:
        from zoneinfo import ZoneInfo
        ET_TZ = ZoneInfo("America/New_York")
    return ET_TZ


def _utc_at_et(hour: int, minute: int, *, on=date(2026, 7, 21)) -> datetime:
    """The UTC-AWARE instant (what production `SystemClock.now()` returns)
    for a given ET wall-clock time on `on`. Built by constructing the ET
    datetime and converting to UTC -- so `.time()` on the result is the UTC
    wall clock (e.g. 19:55), NOT the ET one. A naive `now.time()` comparison
    against ET policy times therefore reads the wrong hour and the scheduler
    misbehaves -- exactly the FIX-1 regression these tests must catch."""
    return datetime(on.year, on.month, on.day, hour, minute, tzinfo=_et()).astimezone(timezone.utc)


def _es_legs():
    return (
        FilledLeg(symbol="./ESU6 E3BN6 260721P06250000", right="P", role="short", qty=1),
        FilledLeg(symbol="./ESU6 E3BN6 260721P06200000", right="P", role="long", qty=1),
        FilledLeg(symbol="./ESU6 E3BN6 260721C06350000", right="C", role="short", qty=1),
        FilledLeg(symbol="./ESU6 E3BN6 260721C06400000", right="C", role="long", qty=1),
    )


class _Alerts:
    def __init__(self):
        self.calls = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message, ctx))


class _Comp:
    """A minimal composition-root stand-in: exactly the four attributes
    `ForceCloseScheduler` reads (`events`, `broker`, `close`, `alerts`) --
    mirrors the SAME shape `composition/live.py::LiveComposition` and
    `composition/paper.py` expose, never a looser one (see
    tests/harness/fake_broker.py's own module docstring on why a fake must
    take the real production shape)."""

    def __init__(self, broker, events, alerts, fee_model=None):
        self.broker = broker
        self.events = events
        self.alerts = alerts
        self.close = CloseEntry(broker, events, alerts=alerts, fee_model=fee_model or FeeModel())


def _es_entry(comp, entry_id="2026-07-21#101", *, eod_close_time=None):
    comp.events.append(CondorFilled(
        entry_id=entry_id, net_credit=D("3.00"), legs=_es_legs(), underlying="/ES",
        eod_close_time=eod_close_time))
    return entry_id


# The default /ES policy every scheduler here is built with (15:55 close /
# 15:59 deadline, the doc 06 §38 defaults) -- kept as one named constant so a
# per-test half-day/per-row override reads as a deliberate departure.
def _default_es_policies():
    return {"/ES": UnderlyingEodPolicy(eod_close_time=time(15, 55), eod_close_deadline=time(15, 59))}


# --- 1. config validation: /ES requires a valid pre-16:00 eod_close_time -----

def test_tc_und_02_es_requires_a_valid_pre_1600_eod_close_time():
    """UND-03/F3: a /ES schedule row with NO eod_close_time, or one >= 16:00
    ET, is REFUSED at validation (naming the F3 requirement); a /ES row WITH
    a valid pre-16:00 eod_close_time (e.g. 15:55) is accepted; SPX/RUT rows
    never require it."""
    defaults = ScheduleDefaults()

    def errors_for(underlying, eod_close_time=None):
        spec = EntrySpec(time=time(10, 0), underlying=underlying, eod_close_time=eod_close_time)
        resolved = resolve(spec, defaults)
        return validate_entry(resolved, 0), resolved

    # /ES with NO override resolves cleanly -- the PROFILE's own default
    # (15:55) fills in (doc 06 §38: "15:55 (MANDATORY for /ES)").
    errors, resolved = errors_for("/ES")
    assert errors == []
    assert resolved.eod_close_time == time(15, 55)

    # /ES explicitly overridden to >= 16:00 -- REFUSED, naming UND-03/F3.
    errors, _ = errors_for("/ES", time(16, 0))
    eod_error = next(e for e in errors if e.field == "eod_close_time")
    assert "UND-03" in eod_error.reason.upper() or "und03" in eod_error.reason
    assert "f3" in eod_error.reason.lower()

    # /ES explicitly overridden with a valid at-or-before-15:55 time -- accepted.
    errors, resolved = errors_for("/ES", time(15, 55))
    assert errors == []
    assert resolved.eod_close_time == time(15, 55)

    errors, resolved = errors_for("/ES", time(9, 30))
    assert errors == []  # lower boundary of the range

    # FIX 3 (validation<->runtime alignment): 15:56-15:59 would be SILENTLY
    # CLAMPED to 15:55 by the runtime force-close scheduler, so validation now
    # REFUSES it -- what is accepted is what is honored. 15:57 was previously
    # accepted-then-clamped; it must now be refused.
    for silently_clamped in (time(15, 56), time(15, 57), time(15, 59)):
        errors, _ = errors_for("/ES", silently_clamped)
        clamped_err = next(e for e in errors if e.field == "eod_close_time")
        assert "1555" in clamped_err.reason or "15:55" in clamped_err.reason
    # 15:55 exactly is the last accepted value (the boundary).
    assert errors_for("/ES", time(15, 55))[0] == []

    # SPX/RUT never require eod_close_time at all -- it resolves to the
    # global default (None -- "off", EOD-01 hold-to-expiry unchanged).
    for cash_underlying in ("SPX", "RUT", None):
        errors, resolved = errors_for(cash_underlying)
        assert errors == []
        assert resolved.eod_close_time is None

    # The PROFILE itself carries the mandatory flag + default, straight off
    # domain/underlying.py (UND-03).
    assert PROFILES["/ES"].mandatory_eod_close is True
    assert PROFILES["/ES"].default_eod_close_time == time(15, 55)
    assert PROFILES["SPX"].mandatory_eod_close is False
    assert PROFILES["RUT"].mandatory_eod_close is False

    # Global config-level validation (doc 06 §37/38) enforces the SAME
    # mandatory pairing: /ES named with no eod_close_time in the same patch
    # is rejected naming UND-03/F3; SPX with none is untouched.
    from meic.config.validation import ConfigRejected, validate_config

    try:
        validate_config({"underlying": "/ES"})
        assert False, "expected a ConfigRejected for /ES with no eod_close_time"
    except ConfigRejected as exc:
        assert exc.key == "underlying"
        assert "UND-03" in exc.reason

    validate_config({"underlying": "/ES", "eod_close_time": "15:55"})  # accepted
    validate_config({"underlying": "SPX"})  # untouched, no eod_close_time needed

    try:
        validate_config({"underlying": "/ES", "eod_close_time": "16:00"})
        assert False, "expected a ConfigRejected for an at-16:00 eod_close_time"
    except ConfigRejected:
        pass

    # FIX 3 at the config layer too: 15:57 (silently-clamped range) is REFUSED.
    try:
        validate_config({"underlying": "/ES", "eod_close_time": "15:57"})
        assert False, "expected a ConfigRejected for a 15:57 eod_close_time (would be clamped)"
    except ConfigRejected as exc:
        assert exc.key == "underlying"
        assert "15:55" in exc.reason


# --- 2. the force-close itself: canonical CloseEntry, initiator "eod" -------

def test_tc_und_02_es_force_closes_at_eod_close_time_via_canonical_close():
    """UND-03/F3/EOD-02: an open /ES entry, when the clock reaches
    eod_close_time, is closed via the canonical CloseEntry with
    initiator="eod" (CLS-01/02 -- the ONE close path, never an ad-hoc order),
    and after the force-close no /ES position remains open.

    FIX 1 (hardened): driven with UTC-aware instants (production clock shape),
    so a naive/UTC time-of-day comparison would fire the 15:50-ET pre-check
    early and break the 'before' assertion."""
    broker = FakeBroker()
    events: list = []
    alerts = _Alerts()
    comp = _Comp(broker, events, alerts)
    entry_id = _es_entry(comp)

    scheduler = ForceCloseScheduler(comp, policies=_default_es_policies())

    # Before eod_close_time (15:50 ET == 19:50 UTC): nothing happens. A naive
    # UTC comparison would read 19:50 >= 15:55 and fire here -> this assertion
    # is the FIX-1 tripwire.
    result = asyncio.run(scheduler.run_once(_utc_at_et(15, 50)))
    assert result.closed == [] and result.unresolved == []
    assert not any(isinstance(e, EntryClosed) for e in events)

    # At eod_close_time (15:55 ET == 19:55 UTC): the force-close fires.
    result = asyncio.run(scheduler.run_once(_utc_at_et(15, 55)))

    assert result.closed == [entry_id]
    closed_events = [e for e in events if isinstance(e, EntryClosed) and e.entry_id == entry_id]
    assert len(closed_events) == 1
    assert closed_events[0].initiator == "eod"  # CLS-02: the ONE close path, canonical initiator

    # No /ES position remains open -- never held into settlement (F3): every
    # leg got a real close order submitted, and the projection shows the
    # entry fully closed with no side left open.
    assert len(broker._orders) >= 4
    from meic.domain.projection import fold
    day = fold(events)
    entry = day.entries[entry_id]
    assert entry.close_initiator == "eod"
    assert set(entry.sides_closed) >= {"PUT", "CALL"}  # both sides confirmed closed

    # Idempotent: a second run_once past eod_close_time is a pure no-op --
    # the already-closed entry is skipped (never a second close attempt).
    orders_before = len(broker._orders)
    result2 = asyncio.run(scheduler.run_once(_utc_at_et(15, 55)))
    assert result2.closed == [] and result2.unresolved == []
    assert len(broker._orders) == orders_before  # nothing new submitted

    # A ForceCloseSweepCompleted marker was journaled (EOD-03 audit inclusion).
    sweeps = [e for e in events if isinstance(e, ForceCloseSweepCompleted)]
    assert len(sweeps) == 1 and sweeps[0].closed == 1 and sweeps[0].unresolved == 0


def test_tc_und_02_utc_clock_does_not_fire_es_four_hours_early():
    """FIX 1 (the masked bug, pinned explicitly): at 11:55 ET (== 15:55 UTC)
    the /ES entry must NOT be force-closed. The original naive `now.time()`
    comparison read the 15:55 UTC wall clock as the 15:55 ET policy time and
    flattened /ES ~4 hours early -- so /ES could never be held past late
    morning. The ET conversion in run_once makes 11:55 ET < 15:55 ET, no
    close; this test fails loudly on any regression to a naive comparison."""
    broker = FakeBroker()
    events: list = []
    comp = _Comp(broker, events, _Alerts())
    _es_entry(comp)
    scheduler = ForceCloseScheduler(comp, policies=_default_es_policies())

    # 11:55 ET == 15:55 UTC (EDT). A naive UTC comparison: 15:55 >= 15:55 -> FIRE.
    result = asyncio.run(scheduler.run_once(_utc_at_et(11, 55)))
    assert result.closed == [] and result.unresolved == []
    assert broker._orders == {}, "a UTC-naive comparison force-closed /ES ~4h early (FIX-1 regression)"
    assert not any(isinstance(e, EntryClosed) for e in events)


# --- 2b. FIX 2: half-day early close clamps the force-close before 13:00 -----

def test_tc_und_02_half_day_forces_es_close_before_the_1300_settlement():
    """FIX 2 (half-day): on a 13:00-ET early close, the 15:55/15:59 defaults
    would leave /ES to REACH settlement at 13:00 before the force-close ran
    (the exact assignment F3 prevents). The effective close/deadline must be
    CLAMPED strictly ahead of the calendar session close: ~12:55 close /
    ~12:59 deadline, both before 13:00. Proven both ways -- an entry force
    closes at 12:56 ET on the half day, and (control) the SAME 12:56-ET
    instant on a NORMAL 16:00 day does NOT close (15:55 still governs)."""
    half_day = date(2026, 7, 21)
    half_days = frozenset({half_day})

    # (a) On the half day: at 12:56 ET the effective close (12:55) has passed.
    broker = FakeBroker()
    events: list = []
    comp = _Comp(broker, events, _Alerts())
    entry_id = _es_entry(comp)
    scheduler = ForceCloseScheduler(comp, policies=_default_es_policies(), half_days=half_days)

    # 12:50 ET: before the clamped 12:55 close -> nothing yet.
    result = asyncio.run(scheduler.run_once(_utc_at_et(12, 50, on=half_day)))
    assert result.closed == [] and not any(isinstance(e, EntryClosed) for e in events)

    # 12:56 ET: past the clamped 12:55 close, and BEFORE the 13:00 settlement.
    result = asyncio.run(scheduler.run_once(_utc_at_et(12, 56, on=half_day)))
    assert result.closed == [entry_id]
    from meic.domain.projection import fold
    assert fold(events).entries[entry_id].close_initiator == "eod"

    # (b) Control -- a NORMAL 16:00 day (no half_days): the SAME 12:56-ET
    # instant must NOT close (the clamp is a strict no-op on a full day, where
    # 15:55 governs). This is what proves the half-day branch, not the clock.
    broker2 = FakeBroker()
    events2: list = []
    comp2 = _Comp(broker2, events2, _Alerts())
    _es_entry(comp2, entry_id="2026-07-21#102")
    normal = ForceCloseScheduler(comp2, policies=_default_es_policies())  # no half_days
    result2 = asyncio.run(normal.run_once(_utc_at_et(12, 56, on=half_day)))
    assert result2.closed == [] and broker2._orders == {}


def test_tc_und_02_half_day_deadline_alert_lands_before_1300():
    """FIX 2: a /ES leg stuck open on a 13:00 half-day fires the RSK-06
    deadline alert BEFORE 13:00 (at the clamped ~12:59 deadline), not at the
    normal 15:59 -- otherwise the alert would arrive after the position had
    already settled/assigned."""
    half_day = date(2026, 7, 21)
    broker = FakeBroker()
    broker.script_submit(*(Scripted("timeout") for _ in range(10)))  # close always fails
    events: list = []
    alerts = _Alerts()
    comp = _Comp(broker, events, alerts)
    entry_id = _es_entry(comp)
    scheduler = ForceCloseScheduler(comp, policies=_default_es_policies(),
                                    half_days=frozenset({half_day}))

    # 12:59 ET: past the clamped deadline (12:59) and still before 13:00.
    result = asyncio.run(scheduler.run_once(_utc_at_et(12, 59, on=half_day)))
    assert entry_id in result.unresolved
    assert any("RSK-06" in msg for lvl, msg, _ in alerts.calls if lvl == "critical")


# --- 2c. FIX 4: a per-row eod_close_time override is honoured ----------------

def test_tc_und_02_per_row_eod_close_time_override_is_honoured():
    """FIX 4 (per-row): a /ES entry journaled with its OWN pinned
    eod_close_time ("15:30") force-closes at 15:30 ET, not the 15:55 profile
    default; an entry with no pinned override still uses 15:55. Both derive
    replay-correctly from the journaled `CondorFilled.eod_close_time`."""
    from meic.domain.projection import fold

    broker = FakeBroker()
    events: list = []
    comp = _Comp(broker, events, _Alerts())
    pinned = _es_entry(comp, entry_id="2026-07-21#301", eod_close_time="15:30")
    unset = _es_entry(comp, entry_id="2026-07-21#302", eod_close_time=None)

    # The journaled per-row value survives the fold onto the projection.
    day = fold(events)
    assert day.entries[pinned].eod_close_time == "15:30"
    assert day.entries[unset].eod_close_time is None

    scheduler = ForceCloseScheduler(comp, policies=_default_es_policies())

    # 15:30 ET: the pinned entry is DUE (its own 15:30), the unset one is NOT
    # (still on the 15:55 profile default).
    result = asyncio.run(scheduler.run_once(_utc_at_et(15, 30)))
    assert result.closed == [pinned]
    day = fold(events)
    assert day.entries[pinned].close_initiator == "eod"
    assert day.entries[unset].close_initiator is None  # 15:55 not yet reached

    # 15:55 ET: now the unset entry (profile default) closes too.
    result = asyncio.run(scheduler.run_once(_utc_at_et(15, 55)))
    assert result.closed == [unset]
    assert fold(events).entries[unset].close_initiator == "eod"


def test_tc_und_02_per_row_close_time_round_trips_from_selection_to_journal():
    """FIX 4 (replay-correct): a ResolvedEntry's pinned eod_close_time flows
    through SelectionConfig -> Condor -> the journaled CondorProposed/
    CondorFilled as an "HH:MM" string, and an OLD journal with no field folds
    to None (profile-default fallback) -- byte-identical replay."""
    from meic.composition.live_selection import SelectionConfig
    from meic.domain.events import CondorProposed, Event

    # ResolvedEntry -> SelectionConfig formats the time to "HH:MM".
    resolved = resolve(EntrySpec(time=time(10, 0), underlying="/ES",
                                 eod_close_time=time(15, 30)), ScheduleDefaults())
    config = SelectionConfig.for_entry(resolved)
    assert config.eod_close_time == "15:30"

    # An unset /ES row still resolves to the profile default (15:55) and so
    # carries "15:55" through, never None -> the scheduler never has to guess.
    default_config = SelectionConfig.for_entry(
        resolve(EntrySpec(time=time(10, 0), underlying="/ES"), ScheduleDefaults()))
    assert default_config.eod_close_time == "15:55"

    # A cash row carries None (no force-close).
    spx_config = SelectionConfig.for_entry(
        resolve(EntrySpec(time=time(10, 0), underlying="SPX"), ScheduleDefaults()))
    assert spx_config.eod_close_time is None

    # Codec round-trip: the journaled field survives replay byte-exact, and
    # an OLD event dict WITHOUT the field replays to None (profile fallback).
    proposed = CondorProposed(entry_id="2026-07-21#1", put_short=D("6250"),
                              call_short=D("6350"), underlying="/ES", eod_close_time="15:30")
    revived = Event.from_dict(proposed.to_dict())
    assert revived == proposed and revived.eod_close_time == "15:30"

    legacy_dict = proposed.to_dict()
    del legacy_dict["eod_close_time"]
    legacy = Event.from_dict(legacy_dict)
    assert legacy.eod_close_time is None  # pre-v1.86 log -> None -> profile default


# --- 3. unclosed /ES leg past the deadline -> RSK-06 critical alert ---------

def test_tc_und_02_unclosed_es_leg_raises_rsk06_critical_alert():
    """UND-03/F3/RSK-06: a /ES leg NOT confirmed flat by eod_close_deadline
    fires a "critical" alert naming the position/assignment risk. Driven with
    a broker whose close submit times out (a stuck/unresponsive broker call),
    which leaves the entry genuinely open past the deadline -- never a
    fabricated close. UTC-aware instants (FIX 1)."""
    broker = FakeBroker()
    # The close attempt fails every time it is retried (each run_once triggers
    # one submit() before CloseEntry.close raises -- a short with no resting
    # stop submits, then raises immediately).
    broker.script_submit(*(Scripted("timeout") for _ in range(10)))
    events: list = []
    alerts = _Alerts()
    comp = _Comp(broker, events, alerts)
    entry_id = _es_entry(comp)

    scheduler = ForceCloseScheduler(comp, policies=_default_es_policies())

    # At eod_close_time (15:55 ET): the attempt is made and fails, but it is
    # not yet past the 15:59 deadline -- no RSK-06 alert fires yet.
    result = asyncio.run(scheduler.run_once(_utc_at_et(15, 55)))
    assert result.closed == []
    assert not any("RSK-06" in msg for _, msg, _ in alerts.calls)

    from meic.domain.projection import fold
    assert fold(events).entries[entry_id].close_initiator is None  # still open

    # Past eod_close_deadline (16:00 ET): the leg is STILL not confirmed flat
    # -> RSK-06 critical alert naming the position (assignment risk).
    result = asyncio.run(scheduler.run_once(_utc_at_et(16, 0)))

    assert entry_id in result.unresolved
    critical_alerts = [(lvl, msg, ctx) for lvl, msg, ctx in alerts.calls if lvl == "critical"]
    assert any("RSK-06" in msg for _lvl, msg, _ctx in critical_alerts)
    rsk06 = next((lvl, msg, ctx) for lvl, msg, ctx in critical_alerts if "RSK-06" in msg)
    assert rsk06[2].get("entry_id") == entry_id  # names the position


# --- 3b. FIX 2: the deadline critical + journal LATCH (once, not per tick) ---

def test_tc_und_02_stuck_entry_alerts_once_not_every_tick():
    """FIX 2 (alert latch / alarm fatigue): a /ES entry stuck open past the
    deadline must fire the RSK-06 critical ONCE, and journal ONE
    ForceCloseSweepCompleted, no matter how many ~5s ticks re-check it while
    it stays stuck. The old code re-fired both on EVERY tick -- dozens/
    hundreds of duplicates from 15:59 to 16:00 during a real assignment."""
    from meic.domain.projection import fold

    broker = FakeBroker()
    broker.script_submit(*(Scripted("timeout") for _ in range(50)))  # close always fails
    events: list = []
    alerts = _Alerts()
    comp = _Comp(broker, events, alerts)
    entry_id = _es_entry(comp)
    scheduler = ForceCloseScheduler(comp, policies=_default_es_policies())

    # Ten past-deadline ticks (16:00, 16:00:05, ...) while the entry stays stuck.
    for _ in range(10):
        result = asyncio.run(scheduler.run_once(_utc_at_et(16, 0)))
        assert entry_id in result.unresolved  # still reported unresolved each pass

    rsk06_criticals = [c for c in alerts.calls if c[0] == "critical" and "RSK-06" in c[1]]
    assert len(rsk06_criticals) == 1, (
        f"the RSK-06 deadline critical must latch (fire once), got {len(rsk06_criticals)}")
    # And exactly ONE ForceCloseSweepCompleted marker -- journaled only on the
    # state-CHANGING pass (the first, newly-unresolved), never on idle re-checks.
    sweeps = [e for e in events if isinstance(e, ForceCloseSweepCompleted)]
    assert len(sweeps) == 1, f"ForceCloseSweepCompleted must not duplicate per tick, got {len(sweeps)}"


def test_tc_und_02_no_legs_critical_latches_per_entry():
    """FIX 2: the `_force_close_one` 'no broker-reported legs' critical latches
    the same way -- a /ES entry whose broker reports no legs (can't close)
    fires its critical ONCE across many due ticks, not every ~5s."""
    broker = FakeBroker()  # working_orders/fill_legs return nothing -> assemble yields no legs
    events: list = []
    alerts = _Alerts()
    comp = _Comp(broker, events, alerts)
    # An entry the fold knows as open /ES but whose broker has NO fill legs:
    # CondorFilled with EMPTY legs -> assemble_close_inputs returns None.
    events.append(CondorFilled(entry_id="2026-07-21#401", net_credit=D("3.00"),
                               legs=(), underlying="/ES"))
    scheduler = ForceCloseScheduler(comp, policies=_default_es_policies())

    for _ in range(8):
        asyncio.run(scheduler.run_once(_utc_at_et(15, 55)))

    no_legs = [c for c in alerts.calls
               if c[0] == "critical" and "no broker-reported legs" in c[1]]
    assert len(no_legs) == 1, f"the no-legs critical must latch per entry, got {len(no_legs)}"


# --- 3c. FIX 1: the force-close loop's OWN death is detected (RSK-06) --------

def test_tc_und_02_force_close_task_death_fires_rsk06_critical():
    """FIX 1 (SETTLEMENT-SAFETY): the force-close supervised loop is the only
    safety-critical loop that lacked death detection. Its done-callback must
    fire an RSK-06 CRITICAL when the task dies with a non-cancelled exception
    (a BaseException, or an alert failure inside the loop's own except body,
    that the `while True` guard can't catch) -- otherwise a dead loop rides
    every open /ES into settlement silently. Mirrors the health_task test."""
    from meic.adapters.api.server import _force_close_task_done_callback

    async def scenario():
        async def _boom():
            raise RuntimeError("force-close loop died")

        crashed = asyncio.create_task(_boom())
        await asyncio.sleep(0)  # let it run to completion (exception set)
        assert crashed.done()

        alerts = _Alerts()
        _force_close_task_done_callback(alerts)(crashed)

        assert len(alerts.calls) == 1
        level, message, _ctx = alerts.calls[0]
        assert level == "critical"
        assert "RSK-06" in message
        assert "force-close" in message.lower() and "down" in message.lower()

    asyncio.run(scenario())


def test_tc_und_02_force_close_task_cancellation_is_not_a_crash():
    """A cancelled force-close task (deliberate shutdown, see
    `_stop_force_close_scheduler_loop`) is NOT a crash and must never alert --
    same contract as the health task's done-callback."""
    from meic.adapters.api.server import _force_close_task_done_callback

    async def scenario():
        async def _forever():
            await asyncio.sleep(3600)

        task = asyncio.create_task(_forever())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.cancelled()

        alerts = _Alerts()
        _force_close_task_done_callback(alerts)(task)
        assert alerts.calls == []

    asyncio.run(scenario())


# --- 4. cash underlyings (SPX/RUT) are completely unaffected ----------------

def test_tc_und_02_cash_underlyings_unchanged():
    """UND-03/EOD-01: an SPX or RUT entry held to expiry cash-settles with NO
    force-close and NO assignment handling -- the force-close scheduler must
    completely IGNORE cash underlyings (no policy entry for them), and the
    never-more-than-premium contract holds regardless of how late the clock
    runs."""
    broker = FakeBroker()
    events: list = []
    alerts = _Alerts()
    comp = _Comp(broker, events, alerts)

    spx_entry_id = "2026-07-21#201"
    rut_entry_id = "2026-07-21#202"
    events.append(CondorFilled(entry_id=spx_entry_id, net_credit=D("3.00"),
                               legs=_es_legs(), underlying="SPX"))
    events.append(CondorFilled(entry_id=rut_entry_id, net_credit=D("3.00"),
                               legs=_es_legs(), underlying="RUT"))

    # The REAL policy table this scheduler is wired with in production --
    # built straight off the ratified PROFILES, never a hand-picked subset.
    policies = default_policies()
    assert "SPX" not in policies and "RUT" not in policies  # never policied
    assert "/ES" in policies

    scheduler = ForceCloseScheduler(comp, policies=policies)

    # Run WELL past every conceivable close time, even past 16:00 ET -- SPX/RUT
    # must never be force-closed, no matter how late.
    result = asyncio.run(scheduler.run_once(_utc_at_et(16, 30)))

    assert result.closed == [] and result.unresolved == []
    assert broker._orders == {}  # not one order was ever submitted
    assert not any(isinstance(e, EntryClosed) for e in events)
    assert not any(isinstance(e, ForceCloseSweepCompleted) for e in events)
    assert not alerts.calls  # no alert of any kind -- completely inert

    from meic.domain.projection import fold
    day = fold(events)
    assert day.entries[spx_entry_id].close_initiator is None
    assert day.entries[rut_entry_id].close_initiator is None  # untouched -- EOD-01 hold-to-expiry


# --- 5. FIX 3: /ES fees journal at the TRUE per-contract dollars (round-trip) -

def test_tc_und_02_es_fee_round_trip_recovers_true_per_contract_dollars():
    """FIX 3 (the masked 50%-fee bug): the full pipeline
    `fee_for_leg`/`fee_for_legs` (what the event constructors call, PER-SHARE)
    -> `EntryProjection.fees` (folded) -> `entry_dollars_fees` /
    `entry_trading_fees_dollars` (real dollars, `* multiplier_of * contracts`)
    must recover the TRUE per-contract /ES fee. The bug divided per_share by a
    hardcoded 100 while /ES re-scales by ×50 -> exactly HALF. Asserting
    per_contract_fee alone (as the earlier tests did) could not catch it --
    only the round-trip does."""
    from meic.reporting.folds import entry_dollars_fees, entry_trading_fees_dollars, multiplier_of
    from meic.domain.projection import fold

    model = FeeModel()
    # One /ES contract's true per-contract fee: 1.25 + 0.30 + 0.01 + 1.50 = 3.06.
    per_contract = model.per_contract_fee(role="short", opening=True, underlying="/ES")
    assert per_contract == D("3.06")

    # Build a real 4-leg /ES entry through the SAME fee path the entry pipeline
    # uses (fee_for_legs, opening) and fold it.
    legs = _es_legs()
    entry_fee_per_share = fee_for_legs(model, legs, opening=True, underlying="/ES")
    events = [CondorFilled(entry_id="2026-07-21#1", net_credit=D("3.00"),
                           fee=entry_fee_per_share, legs=legs, underlying="/ES")]
    entry = fold(events).entries["2026-07-21#1"]

    assert multiplier_of(entry) == D("50")
    # The round-trip: 4 legs each 3.06 real dollars = 12.24. The bug produced
    # 6.12 (each leg 3.06/100 * 50 = 1.53). Assert the TRUE value.
    assert entry_dollars_fees(entry) == D("12.24")
    assert entry_trading_fees_dollars(entry) == D("12.24")
    assert entry_dollars_fees(entry) != D("6.12")  # the halved bug value -- explicitly excluded

    # A single-leg round-trip proof at the primitive level too: per-share
    # times the /ES multiplier recovers exactly the per-contract dollar fee
    # (3.06, never 1.53).
    per_share = fee_for_leg(model, role="short", opening=False, underlying="/ES")
    assert per_share * D("50") == D("3.06")
    assert per_share * D("50") != D("1.53")

    # A /ES CLOSE bills commission too (UND-02/F3): the closing per-share fee
    # round-trips to the SAME 3.06 (both open and close pay it).
    close_per_share = fee_for_leg(model, role="long", opening=False, underlying="/ES")
    assert close_per_share * D("50") == D("3.06")


def test_tc_und_02_cash_fee_round_trip_is_byte_identical_to_before():
    """FIX 3 must NOT shift SPX/RUT: their ÷100·×100 round-trip is unchanged.
    An SPX 4-leg entry (2 short-open @1.72 + 2 long-open @0.72) still lands on
    $4.88 real dollars, and a RUT one on its own exchange-fee-based total --
    proving the multiplier divisor change is a strict no-op for cash
    underlyings (both have multiplier 100, so ÷100 is unchanged)."""
    from meic.reporting.folds import entry_dollars_fees, multiplier_of
    from meic.domain.projection import fold

    model = FeeModel()

    def four_leg_entry_dollars(underlying):
        legs = (FilledLeg(symbol="X P short", right="P", role="short", qty=1),
                FilledLeg(symbol="X P long", right="P", role="long", qty=1),
                FilledLeg(symbol="X C short", right="C", role="short", qty=1),
                FilledLeg(symbol="X C long", right="C", role="long", qty=1))
        fee_ps = fee_for_legs(model, legs, opening=True, underlying=underlying)
        entry = fold([CondorFilled(entry_id="2026-07-21#1", net_credit=D("3.00"),
                                   fee=fee_ps, legs=legs, underlying=underlying)]).entries["2026-07-21#1"]
        assert multiplier_of(entry) == D("100")
        return entry_dollars_fees(entry)

    # SPX: 2*(1.00+0.10+0.02+0.60) + 2*(0.10+0.02+0.60) = 2*1.72 + 2*0.72 = 4.88.
    assert four_leg_entry_dollars("SPX") == D("4.88")
    # RUT: exchange fee 0.18 instead of 0.60 -> 2*(1.00+0.10+0.02+0.18) + 2*(0.10+0.02+0.18)
    #   = 2*1.30 + 2*0.30 = 3.20.
    assert four_leg_entry_dollars("RUT") == D("3.20")
