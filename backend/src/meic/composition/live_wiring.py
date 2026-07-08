"""Live wiring — the one place the live day is assembled.

`live_app()` needs credentials, so it cannot be unit-tested. Everything that
decides WHETHER THE SAFETY RAILS ARE ON therefore lives here, in functions a test
can call. `test_live_wiring.py` asserts on THESE functions, not on a
hand-constructed LiveRuntime — because the defect this module exists to prevent
was exactly that: the tests built a LiveRuntime with rails, and `live_app` built
one without.

Rails assembled here:
  * RSK-04 max exposure   -> max_day_risk from the schedule panel
  * RSK-08 daily order cap -> counted at the broker, not guessed
  * ENT-03 buying power    -> the broker's real derivative BP, priced per condor
  * ENT-04 per-entry rows  -> ScheduledRow carrying each row's ResolvedEntry
  * ENT-09 manual fire     -> ManualEntry, so the panel's fire button is real
  * UC-02 pre-flight       -> live checks, not trivially-passing stubs
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from decimal import Decimal
from typing import Any, Callable

from meic.application.entry_gates import RiskSnapshot
from meic.application.manual_entry import ManualEntry
from meic.application.reconcile_boot import entries_blocked_by_reconcile
from meic.application.schedule_service import ScheduleService
from meic.composition.live_runtime import LiveRuntime, ScheduledRow
from meic.composition.live_selection import SelectionConfig
from meic.domain.projection import fold
from meic.domain.risk import OrderCap

# doc 06: RSK-08 daily order-count rail.
DEFAULT_DAILY_ORDER_CAP = 380
DEFAULT_ORDER_CAP_BUFFER = 10


#: DAY-03 (v1.48): a reading older than this is stale — unverified, blocked.
CLOCK_READING_MAX_AGE_S = 300


class BrokerClockProbe:
    """DAY-03 (v1.48, option B): drift measured against the BROKER's `Date` header.

    The clock that governs entry windows and cutoffs is the broker's, not ours, so
    that is the one to check against. The authenticated session probe already runs
    every ~60 s (NFR-02); each response carries a `Date` header, so drift is
    measured continuously with no new dependency and no new network path. Header
    resolution is ~1 s, hence the 2000 ms default threshold.

    UNMEASURED = UNVERIFIED = BLOCKED, and staleness counts as unmeasured:
      * no reading yet (startup, or the probe can't reach the broker) -> ms() = inf
      * latest reading older than 300 s -> ms() = inf
    Either way every entry skips `clock_drift` and the pre-flight names it. Wiring
    a constant `0.0` would be a rail that can never fire; blocking on absence is the
    only safe default when the probe is the source of truth.

    `now` is injected so staleness is testable without wall-clock sleeps.
    """

    def __init__(self, now: Callable[[], datetime] | None = None,
                 max_age_s: float = CLOCK_READING_MAX_AGE_S) -> None:
        self._now = now or _utcnow
        self._max_age_s = max_age_s
        self._drift_ms: float | None = None
        self._read_at: datetime | None = None

    def record(self, server_time: datetime | None, local_time: datetime | None = None) -> None:
        """Fold in one probe reading. `server_time` None (no `Date` header) clears
        the reading — an unreadable probe must not leave a stale value looking
        fresh."""
        if server_time is None:
            self._drift_ms = None
            self._read_at = None
            return
        local = local_time or self._now()
        self._drift_ms = (local - server_time).total_seconds() * 1000.0
        self._read_at = local

    @property
    def verified(self) -> bool:
        return self.ms() != float("inf")

    def ms(self) -> float:
        """Signed drift in ms (local − broker). Unmeasured or stale reads as
        infinite: it BLOCKS, never passes."""
        if self._drift_ms is None or self._read_at is None:
            return float("inf")
        if (self._now() - self._read_at).total_seconds() > self._max_age_s:
            return float("inf")                    # stale — as good as unmeasured
        return self._drift_ms


def _utcnow() -> datetime:
    from datetime import timezone
    return datetime.now(timezone.utc)


class CountingBroker:
    """Wraps a BrokerGateway and charges every order against the RSK-08 cap.

    RSK-08 counts orders SUBMITTED, "including cancel/replaces — each replace
    counts as a new order". A cap nobody increments is not a cap, and the live
    wiring previously had no counter at all. Exit-side orders are never BLOCKED by
    the cap (that is `OrderCap.allow`'s job); they are still COUNTED, because the
    broker counts them.
    """

    def __init__(self, inner, cap: OrderCap) -> None:
        self._inner = inner
        self.cap = cap

    @property
    def inner(self):
        """The wrapped gateway. Named so a caller never has to reach for `_inner`
        (and so `isinstance` checks have somewhere honest to look)."""
        return self._inner

    def __getattr__(self, name):           # everything else passes straight through
        return getattr(self._inner, name)

    async def submit(self, order):
        self.cap.record()
        return await self._inner.submit(order)

    async def replace(self, order_id, new):
        self.cap.record()                  # a replace IS a new order (RSK-08)
        return await self._inner.replace(order_id, new)


def schedule_rows(state, *, today: date, tz) -> list[ScheduledRow]:
    """The composed schedule -> today's ScheduledRows, each carrying its OWN
    ENT-04 settings.

    The live day used to keep only the times and throw the rows away, so every
    entry traded 1 contract at the global premium/width/stop no matter what the
    panel showed. Pin-at-Save (v1.47) means these values are already concrete.
    """
    rows: list[ScheduledRow] = []
    for entry in ScheduleService(state).resolved():
        when = datetime.combine(today, dtime(entry.time.hour, entry.time.minute), tzinfo=tz)
        rows.append(ScheduledRow(when, entry))
    return sorted(rows, key=lambda r: r.when)


def max_day_risk_of(state) -> Decimal | None:
    """RSK-04's ceiling, as typed into the schedule panel. `None` means no ceiling
    is configured — never 'unlimited'. UC-02's pre-flight refuses a LIVE arm
    without one (doc 06 §169)."""
    return ScheduleService(state).max_day_risk()


def open_worst_cases(comp) -> tuple[Decimal, ...]:
    """Only entries still OPEN count toward the day's exposure. A closed condor
    can no longer lose anything, so the event log decides — not a running total."""
    open_ids = {eid for eid, e in fold(comp.events).entries.items() if not e.close_initiator}
    return tuple(wc for eid, wc in comp.worst_case.items() if eid in open_ids)


def build_live_runtime(
    comp,
    *,
    selector,
    market_gates,
    warmup=None,
    max_entries_per_day: int | None = None,
    daily_order_cap: int = DEFAULT_DAILY_ORDER_CAP,
    order_cap_buffer: int = DEFAULT_ORDER_CAP_BUFFER,
    drift: BrokerClockProbe | None = None,
    max_clock_drift_ms: float = 2000.0,   # DAY-03 v1.48 default
) -> LiveRuntime:
    """Assemble the live day with EVERY rail armed.

    `comp.broker` is wrapped in a CountingBroker in place, so the cap sees the
    orders every service submits — not just the ones this function knows about.
    """
    cap = OrderCap(cap=daily_order_cap, buffer=order_cap_buffer)
    # DAY-03/RSK-07: an unverified clock reads as infinite drift and blocks entries.
    drift = drift or BrokerClockProbe()
    if not isinstance(comp.broker, CountingBroker):
        comp.broker = CountingBroker(comp.broker, cap)

    async def buying_power() -> Decimal:
        """ENT-03: the real options buying power, fetched at gate time."""
        return await comp.broker.buying_power()

    runtime = LiveRuntime(
        comp,
        selector=selector,
        market_gates=market_gates,
        warmup=warmup,
        max_entries_per_day=max_entries_per_day,
        max_day_risk=max_day_risk_of(comp.state),   # RSK-04
        order_cap=cap,                              # RSK-08
        buying_power=buying_power,                  # ENT-03
        measure_drift_ms=drift.ms,                  # RSK-07 / DAY-03
        max_clock_drift_ms=max_clock_drift_ms,
    )
    # RSK-04 counts the entries THIS COMPOSITION filled, wherever they were filled
    # from — the scheduled day or a manual ENT-09 fire. One book, one total.
    runtime._worst_case = comp.worst_case
    return runtime


def build_manual_entry(comp, *, selector, market_gates, max_entries_per_day=None,
                       day: Callable[[], str] | None = None,
                       drift: BrokerClockProbe | None = None,
                       max_clock_drift_ms: float = 2000.0) -> ManualEntry:
    """ENT-09. The manual fire crosses the identical rails as a scheduled entry —
    the SAME max_day_risk, the SAME open worst cases, the SAME reconcile block and
    clock-drift check. Only the ENT-02 window is bypassed."""
    from meic.application.entry_gates import clock_drift_blocks_entry

    drift = drift or BrokerClockProbe()

    def blocks() -> str | None:
        if entries_blocked_by_reconcile(comp.events):          # REC-02 -> RSK-03
            return "reconcile_pending"
        if clock_drift_blocks_entry(drift_ms=drift.ms(),        # RSK-07 / DAY-03
                                    max_drift_ms=max_clock_drift_ms):
            return "clock_drift"
        return None

    async def risk() -> RiskSnapshot:
        return RiskSnapshot(
            new_worst_case=Decimal("0"),          # attempt() re-prices it from the condor
            open_worst_cases=open_worst_cases(comp),
            max_day_risk=max_day_risk_of(comp.state),
            order_cap_allows_entry=(comp.broker.cap.allow(exit_priority=False)
                                    if isinstance(comp.broker, CountingBroker) else True),
            buying_power=await comp.broker.buying_power(),
        )

    return ManualEntry(comp, selector, market_gates,
                       max_entries_per_day=max_entries_per_day, risk=risk, day=day,
                       blocks=blocks)


def live_preflight_checks(comp, *, data_fresh: Callable[[], bool],
                          drift: BrokerClockProbe,
                          max_drift_ms: float = 2000.0,
                          ) -> dict[str, Callable[[], tuple[bool, str]]]:
    """UC-02: real checks, in the spec's order. They were absent, so every item
    passed trivially and the checklist told the operator nothing.

    Every predicate here is SYNCHRONOUS and reads CACHED health — the freshness of
    the last chain snapshot, the last measured drift. The pre-flight route is a
    sync FastAPI handler running in a threadpool: awaiting the broker from there
    would bind its session to a fresh event loop. Live health is kept fresh by the
    health loop and the runtime, and read here.
    """

    def reconcile() -> tuple[bool, str]:
        if entries_blocked_by_reconcile(comp.events):
            return False, "unresolved reconciliation mismatch (REC-02) blocks new entries"
        return True, "no open mismatch"

    def clock() -> tuple[bool, str]:
        if not drift.verified:
            return False, ("clock NOT verified against the broker (DAY-03) — no fresh "
                           "session-probe reading yet, or the last one is stale (> 300s)")
        ms = drift.ms()
        if abs(ms) > max_drift_ms:
            return False, f"clock drift {ms:.0f}ms exceeds {max_drift_ms:.0f}ms (RSK-07)"
        return True, f"drift {ms:.0f}ms"

    def config() -> tuple[bool, str]:
        version = comp.state.config_version
        if not version:
            return False, "no config version — save the schedule first"
        return True, f"config {version}"

    def market_data() -> tuple[bool, str]:
        # DAT-02: never trade stale data. Unknown freshness is STALE, not fresh.
        return (True, "chain fresh") if data_fresh() else (False, "chain data is stale (DAT-02)")

    return {"reconcile": reconcile, "clock": clock, "config": config,
            "market_data": market_data}
