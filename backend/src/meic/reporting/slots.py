"""RPT-13 slot analytics -- win rate / expectancy / premium capture per
scheduled slot, with manual (ad-hoc) entries grouped under "manual".

An entry id is `"{day}#{n}"` (folds.entry_day); ENT-11(3) reserves n >= 101
for the ad-hoc lane, so ANY entry numbered there is manual by construction --
no lookup needed. A scheduled entry's actual slot label ("10:00", "12:35",
...) is not itself a durable per-entry fact in the event log (the schedule
config that assigns a row number to a time is mutable and lives in
PersistentState, not the immutable log) -- this module accepts that mapping
from the caller (`slot_map`: entry_id -> label) rather than reaching for any
config/state dependency of its own, keeping it pure and I/O-free like every
other module in this package.
"""
from __future__ import annotations

from decimal import Decimal

from meic.domain.projection import EntryProjection

from .folds import entry_credit_dollars, entry_dollars

MANUAL = "manual"
_ADHOC_FLOOR = 101


def slot_of(entry_id: str, *, slot_map: dict[str, str] | None = None) -> str:
    """ENT-11(3): n >= 101 is always the ad-hoc lane -> "manual". A scheduled
    entry (n < 101) takes its label from `slot_map`, or "unknown" if the
    caller supplied no mapping for it (never fabricated)."""
    n = int(entry_id.rsplit("#", 1)[1])
    if n >= _ADHOC_FLOOR:
        return MANUAL
    if slot_map and entry_id in slot_map:
        return slot_map[entry_id]
    return "unknown"


def by_slot(entries: dict[str, EntryProjection], *,
            slot_map: dict[str, str] | None = None) -> dict[str, tuple[EntryProjection, ...]]:
    out: dict[str, list[EntryProjection]] = {}
    for entry_id, entry in entries.items():
        out.setdefault(slot_of(entry_id, slot_map=slot_map), []).append(entry)
    return {slot: tuple(es) for slot, es in out.items()}


def slot_metrics(entries: dict[str, EntryProjection], *,
                  slot_map: dict[str, str] | None = None) -> dict[str, dict[str, Decimal | None]]:
    """Win rate / expectancy / premium capture per slot (RPT-13), filled
    entries only -- an entry that never filled contributes nothing to any of
    these three (identical convention to `folds.core_results`)."""
    out: dict[str, dict[str, Decimal | None]] = {}
    for slot, es in by_slot(entries, slot_map=slot_map).items():
        filled = [e for e in es if e.net_credit != 0]
        if not filled:
            out[slot] = {"win_rate": None, "expectancy": None, "premium_capture": None}
            continue
        pnls = [entry_dollars(e) for e in filled]
        total_credit = sum((entry_credit_dollars(e) for e in filled), Decimal("0"))
        net = sum(pnls, Decimal("0"))
        out[slot] = {
            "win_rate": Decimal(sum(1 for p in pnls if p > 0)) / len(pnls),
            "expectancy": net / len(pnls),
            "premium_capture": (net / total_credit) if total_credit else None,
        }
    return out
