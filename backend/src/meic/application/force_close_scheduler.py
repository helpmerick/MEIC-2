"""F3 force-close scheduler -- UND-03/EOD-02/RSK-06 (v1.86 /ES Stage 2).

UND-03 (operator-ruled 2026-07-21): /ES is NEVER held to settlement --
American-exercise assignment would break the cash-settlement/defined-risk
contract EOD-01 relies on for SPX/RUT. Every open entry of a MANDATORY-eod-
close underlying (today: /ES only, `UnderlyingProfile.mandatory_eod_close`)
is therefore force-closed via the ONE canonical close path (CLS-01/02,
initiator "eod") once its `eod_close_time` is reached, with a hard
`eod_close_deadline` after which any leg still open raises an RSK-06 CRITICAL
alert naming the position (assignment risk).

F3 (operator ruling, same session): this covers the SETTLEMENT/EOD window
only. American-exercise INTRADAY early assignment is accepted as documented
residual risk -- this scheduler does not attempt an intraday ITM-flatten.

Modeled on the existing EOD-03 sweep (`application/eod_sweep.EndOfDaySweep`):
a small, supervised, idempotent pass a background tick drives repeatedly.
Nothing here is a NEW close path -- every close routes through the SAME
canonical `CloseEntry`, reusing `assemble_close_inputs` exactly like
`PanelCommands.close_as` and the STP-04 auto-flatten hook
(`composition/live.py::LiveComposition._auto_flatten_entry`).

Idempotent: an entry already closed (`close_initiator` set) or with no open
sides is skipped outright -- the SAME journal-derived check every other
close-idempotency check in this codebase uses (see
`composition/panel_commands.py::_open_sides`), so a restart mid-window simply
re-derives "what's still open" from the projection and resumes exactly where
it left off, never re-closing or double-submitting. Cash-settled underlyings
(SPX, RUT) carry no policy in `ForceCloseScheduler`'s table and are therefore
NEVER touched -- EOD-01's hold-to-expiry default is completely unaffected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as dt_date, datetime, time as dt_time, timedelta

from meic.composition.close_assembly import DEFAULT_CLOSE_PRICE, assemble_close_inputs
from meic.domain.events import ForceCloseSweepCompleted
from meic.domain.projection import EntryProjection, fold

from .market_calendar import ET, RTH_CLOSE, session_close, trading_day_str

_SIDES = ("PUT", "CALL")

# UND-03/F3 half-day clamp (FIX 2): the safety margins the effective
# force-close time and deadline keep AHEAD of the calendar session close.
# Derived from the ratified defaults against the NORMAL 16:00 close so a
# regular day is a strict NO-OP: 16:00 - 5min = 15:55 (== the default
# eod_close_time), 16:00 - 1min = 15:59 (== the default eod_close_deadline).
# On a 13:00-ET early close these same margins clamp the effective close to
# 12:55 and the deadline to 12:59 -- BOTH strictly before the 13:00
# settlement F3 exists to beat.
_CLOSE_SAFETY_MARGIN = timedelta(
    minutes=(RTH_CLOSE.hour * 60 + RTH_CLOSE.minute) - (15 * 60 + 55))   # 5 minutes
_DEADLINE_SAFETY_MARGIN = timedelta(
    minutes=(RTH_CLOSE.hour * 60 + RTH_CLOSE.minute) - (15 * 60 + 59))   # 1 minute

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_hhmm(value: str | None) -> dt_time | None:
    """Parse a journaled "HH:MM" ET string back to a `datetime.time`. None
    (an entry with no pinned override) stays None -> the caller falls back to
    the profile default. An unparseable value is treated as absent rather
    than crashing the scheduler tick (the same reject-the-value convention
    server.py's own env-time parsers use) -- the profile default then
    governs, which is the safe direction (force-close still happens)."""
    if not value:
        return None
    m = _HHMM_RE.match(value.strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return dt_time(hour, minute)


def _minus(t: dt_time, delta: timedelta, on_date: dt_date) -> dt_time:
    """`t - delta` as a wall-clock time on `on_date` (the date only anchors
    the subtraction so it never wraps under midnight in practice)."""
    return (datetime.combine(on_date, t) - delta).time()


def _open_sides(e: EntryProjection) -> list[str]:
    """Mirrors `composition/panel_commands.py::_open_sides` exactly (CLS-02:
    one close PATH, but deriving 'what's open' from the projection is a
    read, not a close implementation -- the same tiny helper is duplicated
    at each call site in this codebase, e.g. `close_assembly.py`'s own
    per-side filtering)."""
    gone = set(e.sides_stopped) | set(e.sides_closed) | set(e.sides_expired)
    return [s for s in _SIDES if s not in gone]


@dataclass(frozen=True)
class UnderlyingEodPolicy:
    """One underlying's mandatory force-close window (UND-03). Only
    underlyings with an entry in `ForceCloseScheduler`'s policy table are
    ever touched -- cash-settled underlyings (SPX, RUT) carry none and hold
    to expiry per EOD-01, completely unaffected by this scheduler existing."""
    eod_close_time: dt_time
    eod_close_deadline: dt_time


@dataclass
class ForceCloseResult:
    closed: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


class ForceCloseScheduler:
    """The supervised component that force-closes every open entry of an
    underlying with a mandatory `eod_close_time` (today: /ES only)."""

    def __init__(self, comp, *, policies: dict[str, UnderlyingEodPolicy],
                 half_days: frozenset = frozenset()) -> None:
        self._comp = comp
        self._policies = policies
        # FIX 2 (half-day): the SAME algorithmic early-close calendar the
        # EOD-03 sweep uses (server.py `eod_half_days`, `half_days_near`) so
        # a 13:00 half-day force-closes /ES before 13:00, never 15:55.
        self._half_days = half_days
        # ALERT LATCHES (2026-07-21 final review, "one alert per outage" --
        # the SAME latching rule day_supervisor_error / _health_tick /
        # STP-08a all follow). run_once ticks every ~5s; without a latch a
        # single stuck /ES entry would fire the RSK-06 critical (and journal
        # a ForceCloseSweepCompleted) on EVERY tick from the deadline to the
        # close and beyond -- dozens/hundreds of duplicates during a real
        # assignment, the exact alarm-fatigue this closes.
        #   * `_alerted_deadline`: entry_ids that have fired the past-deadline
        #     RSK-06 critical; re-set to the CURRENT unresolved set each pass,
        #     so a newly-unresolved entry alerts once and a resolved one
        #     re-arms (drops out) automatically.
        #   * `_alerted_close_failure`: entry_ids that have fired an
        #     assemble-failed / no-legs critical inside `_force_close_one`;
        #     pruned to the still-due set each pass so a resolved entry
        #     re-arms.
        self._alerted_deadline: set[str] = set()
        self._alerted_close_failure: set[str] = set()

    def _effective_times(
        self, entry: EntryProjection, policy: UnderlyingEodPolicy, on_date: dt_date,
    ) -> tuple[dt_time, dt_time]:
        """The effective (close, deadline) ET times for `entry` on `on_date`.

        FIX 4 (per-row): the CLOSE time is the entry's OWN pinned
        `eod_close_time` (journaled, doc 06 §38) when set, else the profile
        default carried on the policy. FIX 2 (half-day): both are then
        CLAMPED strictly ahead of the calendar session close -- a no-op on a
        normal 16:00 day (15:55/15:59), 12:55/12:59 on a 13:00 early close."""
        raw_close = _parse_hhmm(getattr(entry, "eod_close_time", None)) or policy.eod_close_time
        sess_close = session_close(on_date, half_days=self._half_days)
        effective_close = min(raw_close, _minus(sess_close, _CLOSE_SAFETY_MARGIN, on_date))
        effective_deadline = min(policy.eod_close_deadline,
                                 _minus(sess_close, _DEADLINE_SAFETY_MARGIN, on_date))
        return effective_close, effective_deadline

    async def run_once(self, now: datetime) -> ForceCloseResult:
        """One pass: attempt a force-close for every DUE, still-open,
        policied entry, then re-read the journal to see what actually landed
        -- an entry that closed cleanly is reported `closed`; one still open
        at/after its effective `eod_close_deadline` is reported `unresolved`
        and raises the RSK-06 critical alert. Safe to call every tick: an
        already-closed entry is skipped before any broker call, and the
        deadline re-check re-alerts each tick an unresolved position persists
        -- never silently going quiet on a still-unclosed /ES leg.

        FIX 1 (timezone): the production clock (SystemClock) reads UTC, but
        every policy time is ET -- so the time-of-day comparison MUST happen
        in ET, never on `now`'s raw (UTC) wall clock, or /ES force-closes ~4
        hours early (11:55 ET) and can't be held through the afternoon. The
        conversion mirrors `_decay_watcher_pass`'s own `now.astimezone(_ET)`.
        `now` must be tz-aware (SystemClock/FakeClock both are)."""
        now_et = now.astimezone(ET)
        now_t = now_et.time()
        on_date = now_et.date()

        result = ForceCloseResult()
        day_state = fold(self._comp.events)
        due: list[tuple[str, EntryProjection, dt_time]] = []
        for entry_id, e in day_state.entries.items():
            policy = self._policies.get(e.underlying)
            if policy is None:
                continue  # no policy for this underlying -- EOD-01 unchanged, never touched
            if e.close_initiator or not _open_sides(e):
                continue  # already closed / nothing open -- idempotent skip
            effective_close, effective_deadline = self._effective_times(e, policy, on_date)
            if now_t < effective_close:
                continue  # not due yet (in ET)
            due.append((entry_id, e, effective_deadline))

        due_ids = {entry_id for entry_id, _e, _deadline in due}
        for entry_id, e, _deadline in due:
            await self._force_close_one(entry_id, e)

        # Re-fold: a close attempt above may have partially completed (a
        # mid-sequence broker failure) or raised outright -- read the
        # CURRENT journal truth, never the pre-attempt snapshot.
        refreshed = fold(self._comp.events)
        current_unresolved: list[str] = []
        newly_unresolved: list[str] = []
        for entry_id, e, effective_deadline in due:
            current = refreshed.entries.get(entry_id, e)
            if current.close_initiator and not _open_sides(current):
                result.closed.append(entry_id)
                continue
            if now_t >= effective_deadline:
                result.unresolved.append(entry_id)
                current_unresolved.append(entry_id)
                # FIX 2 (per-entry latch): alert ONCE per distinct unresolved
                # entry, not every ~5s tick. `_alerted_deadline` carries the
                # entries already alerted; a re-armed (previously resolved)
                # entry that breaks again is not in it, so it re-alerts.
                if entry_id not in self._alerted_deadline:
                    newly_unresolved.append(entry_id)
                    self._comp.alerts.alert(
                        "critical",
                        "RSK-06: /ES leg not confirmed flat by eod_close_deadline -- "
                        "assignment risk (UND-03/F3)",
                        entry_id=entry_id, underlying=current.underlying,
                        sides_open=_open_sides(current))

        # Re-set the deadline latch to EXACTLY the current unresolved set:
        # an entry that resolved this pass drops out (re-arms), a still-stuck
        # one stays latched (no re-alert next tick).
        self._alerted_deadline = set(current_unresolved)
        # Prune the close-failure latch to entries still due this pass, so a
        # resolved/gone entry re-arms its assemble/no-legs critical too.
        self._alerted_close_failure &= due_ids

        # FIX 2 (state-changed journaling): journal a ForceCloseSweepCompleted
        # ONLY on a pass that actually CHANGED state -- closed something, or
        # newly broke past a deadline -- never on an idle re-check of an
        # already-stuck entry. Otherwise a single stuck entry would append a
        # fresh marker every ~5s tick from the deadline onward, flooding the
        # EOD-03 audit trail with duplicates.
        if result.closed or newly_unresolved:
            self._comp.events.append(ForceCloseSweepCompleted(
                date=trading_day_str(now), closed=len(result.closed),
                unresolved=len(result.unresolved)))
        return result

    async def _force_close_one(self, entry_id: str, e: EntryProjection) -> None:
        open_sides = set(_open_sides(e))
        try:
            inputs = await assemble_close_inputs(
                self._comp.events, self._comp.broker, entry_id, open_sides=open_sides)
        except Exception as exc:  # noqa: BLE001 -- never crash the scheduler's tick
            # FIX 2 (per-entry latch): fire this critical once per distinct
            # entry, not every ~5s tick while the same entry keeps failing.
            if entry_id not in self._alerted_close_failure:
                self._alerted_close_failure.add(entry_id)
                self._comp.alerts.alert(
                    "critical", "UND-03/F3 force-close: could not assemble close inputs",
                    entry_id=entry_id, error=repr(exc))
            return
        if inputs is None or not inputs[0]:
            if entry_id not in self._alerted_close_failure:
                self._alerted_close_failure.add(entry_id)
                self._comp.alerts.alert(
                    "critical", "UND-03/F3 force-close: no broker-reported legs recorded "
                    "for this /ES entry -- cannot close (ORD-09), operator must intervene",
                    entry_id=entry_id)
            return
        # Got past assemble+legs: this entry is no longer in the failure
        # state, so re-arm its failure latch (a later genuine failure alerts
        # again).
        self._alerted_close_failure.discard(entry_id)
        live_legs, stop_ids = inputs
        try:
            await self._comp.close.close(
                entry_id, "eod", resting_stop_ids=stop_ids,
                live_legs=live_legs, close_price=DEFAULT_CLOSE_PRICE)
        except Exception:  # noqa: BLE001
            # A mid-sequence failure already journaled whatever per-side
            # events genuinely landed (CloseEntry appends as it goes) --
            # `run_once`'s post-attempt re-fold reads that partial truth
            # directly; nothing further to do here. The deadline check above
            # is what actually surfaces this to the operator (RSK-06).
            pass


def default_policies(
    *, eod_close_deadline: dt_time | None = None,
) -> dict[str, UnderlyingEodPolicy]:
    """UND-03: build the policy table straight off the ratified underlying
    PROFILES (`domain/underlying.PROFILES`) -- every profile with
    `mandatory_eod_close` (today: /ES only) gets an entry using ITS OWN
    `default_eod_close_time`; every other profile (SPX, RUT) gets none, so
    `ForceCloseScheduler` never touches them. `eod_close_deadline` is the
    ONE global EOD-02 deadline (doc 06 §134, default 15:59) applied to every
    policied underlying -- there is only one mandatory-eod-close underlying
    today, so this is not yet a per-underlying dial."""
    from meic.domain.underlying import PROFILES

    deadline = eod_close_deadline or dt_time(15, 59)
    return {
        profile.name: UnderlyingEodPolicy(
            eod_close_time=profile.default_eod_close_time, eod_close_deadline=deadline)
        for profile in PROFILES.values()
        if profile.mandatory_eod_close and profile.default_eod_close_time is not None
    }
