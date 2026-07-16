"""DAT-04a halt-signal provider (v1.69, operator-ratified — NFR-07's ninth
finding, now CLOSED).

The ninth finding: DAT-04 ("market open and not halted") had no live signal
source anywhere in the codebase, AND the one place a `halted` reading was
consulted (`LiveMarketGates._safe(self.halted, default=False)`) defaulted
UNMEASURED to "not halted" — permissive, inverted from its three siblings
(`data_fresh`/`session_valid`/`buying_power_ok`), which all default an
unmeasured reading to the BLOCKING answer.

DAT-04a's ruling, implemented here + at the one call-site that binds it
(`server.py`'s `_wire_live_day`, the `halted=` kwarg to
`LiveMarketGates.for_live`):

  1. POLARITY (fixed in `composition/live_gates.py`, not here): unmeasured
     is now uniformly blocked, same shape as the other three inputs.
  2. PROVIDER (this module): the underlying's trading-status via the
     EXISTING DXLink connection — a dxfeed Profile subscription piggybacked
     onto the SAME streamer session `chain_snapshot.snapshot_chain` already
     opens for quotes (see its `on_trading_status` parameter). No new
     connection, no new dependency — the DAY-03 pattern of reusing an
     existing feed (there, the broker's Date header on an existing session
     probe; here, Profile on the existing DXLink socket).
  3. The freshness gates (DAT-02/STK-04/STK-10) are untouched — an
     independent second layer, not this module's concern.
  4. CONTINGENCY, pre-ruled: if trading-status proves unusable for the
     underlying in cert/live, this module + its one wiring call-site are
     the WHOLE seam to delete (the `stop_limit` retirement precedent) —
     retiring it is a deletion, never surgery. Halt protection would then
     be formally carried by the freshness gates alone, never a fake flag
     pretending to be a feed.

A halt reading is an INSTANT — stamped from the injected clock the caller
passes in, never `datetime.now()` (DAY-03 discipline).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

# DAT-04a: the ratified staleness bound for a trading-status reading. This is
# NOT a spec/06-configuration.md dial — DAT-04a's own text states the number
# as a fixed threshold ("stale > 300 s"), never a configurable row, so it is
# a named constant citing the rule rather than an invented config key.
HALT_READING_STALE_AFTER_SECONDS = 300

# dxfeed Profile.trading_status's tradeable values. ACTIVE means actively
# trading. UNDEFINED means the broker cannot determine status (e.g., pre-market
# data not yet available) — treat as trading-allowed. HALTED means halted.
# Anything else (e.g., "HALTED") is treated as halted per DAT-04a clause 2.
ACTIVE_STATUS = "ACTIVE"
TRADEABLE_STATUSES = frozenset(("ACTIVE", "UNDEFINED"))


@dataclass(frozen=True)
class TradingStatusReading:
    status: str
    at: datetime


class TradingStatusStore:
    """The bounded provider seam DAT-04a's contingency clause names: one
    module, one object, fed by `chain_snapshot.snapshot_chain`'s piggybacked
    Profile subscription, and read by exactly one predicate (`halted`) that
    the live `halted` gate-input provider (`server.py`) calls.

    `record()` is the ONLY writer — called with every Profile event seen,
    each stamped with the injected clock's instant at the moment it was
    captured. `halted()` is the ONLY reader.
    """

    def __init__(self) -> None:
        self._reading: TradingStatusReading | None = None

    def record(self, status: str, at: datetime) -> None:
        """Store the latest Profile trading_status reading. `at` MUST be a
        tz-aware instant off the injected clock (DAY-03) — never
        `datetime.now()`. A reading older than the one already held is
        dropped (never move the store backward in time — the same
        never-regress discipline `QuoteHub.apply_tick` uses)."""
        if at.tzinfo is None:
            raise ValueError("TradingStatusStore.record requires a tz-aware instant")
        if self._reading is not None and at < self._reading.at:
            logger.debug(f"TradingStatusStore.record: ignoring older reading {status} at {at}, current is {self._reading.status} at {self._reading.at}")
            return
        status_upper = str(status).strip().upper()
        self._reading = TradingStatusReading(status=status_upper, at=at)
        logger.info(f"TradingStatusStore.record: updated to status={status_upper} at {at}")

    @property
    def last(self) -> TradingStatusReading | None:
        return self._reading

    def halted(self, now: datetime) -> bool:
        """DAT-04a: no reading at all, a non-tradeable status, or a reading
        stale beyond `HALT_READING_STALE_AFTER_SECONDS` all mean halted —
        unmeasured = unverified = blocked (RSK-07), uniform with the other
        three ENT-03 gate inputs `LiveMarketGates` sources. UNDEFINED is
        treated as tradeable (broker cannot determine status, assume open)."""
        reading = self._reading
        if reading is None:
            logger.warning(f"TradingStatusStore.halted: NO READING AT ALL — reporting halted (at {now})")
            return True
        if reading.status not in TRADEABLE_STATUSES:
            logger.warning(f"TradingStatusStore.halted: status={reading.status} (not in {TRADEABLE_STATUSES}) — reporting halted (at {now}, reading from {reading.at})")
            return True
        age_seconds = (now - reading.at).total_seconds()
        is_stale = age_seconds > HALT_READING_STALE_AFTER_SECONDS
        logger.info(f"TradingStatusStore.halted: status={reading.status}, age={age_seconds:.1f}s, stale={is_stale} (at {now}, reading from {reading.at})")
        return is_stale
