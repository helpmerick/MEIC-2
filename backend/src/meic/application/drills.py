"""Operational drills — UC-12 stop independence.

A deliberate, supported procedure (not an edge case): with resting stops in
place, simulate a bot outage and verify the stops remained WORKING at the
broker throughout, with unbroken placement timestamps (STP-05 core claim).

Honesty (SIM-06): in PAPER the SimulatedBroker holds the stops in-process, so
this proves the RECOVERY MECHANISM and the timestamp evidence — not true
broker-side independence. The bot-independent proof is the cert sandbox drill
(TC-STP-08, `pytest -m contract`). The drill says so in its own result.

Live mode (v1.56, operator-ratified): the drill is available in LIVE too, but
ONLY with the operator present, behind a typed DRILL confirmation
(`drill_confirmation_ok` below) — never a silent one-click action against a
real book. Its honesty note differs from paper's: a live run against the REAL
broker session genuinely demonstrates broker-side independence for the
sessions it severed, though it still isn't the full cert-sandbox drill's
end-to-end evidence trail. The dialog SHOULD warn (guidance, not a hard
block — the operator is supervising) if a short mark sits within 50% of its
trigger distance, or an entry fires within 10 minutes (`drill_guidance`).
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field

DRILL_CONFIRMATION = "DRILL"

_PAPER_HONESTY = ("PAPER: proves the recovery mechanism + timestamp evidence, not "
                  "broker-side independence (SIM-06). Bot-independent proof is the "
                  "sandbox drill TC-STP-08 (pytest -m contract).")

_LIVE_HONESTY = ("LIVE: the bot severed its own broker/data sessions for the outage "
                 "window and the resting stops kept working at the broker throughout — "
                 "genuine evidence of broker-side independence for this drill. It is "
                 "still not the full cert-sandbox drill's end-to-end evidence trail "
                 "(TC-STP-08, pytest -m contract), which remains the pre-go-live gate.")

# Backward-compat alias -- some earlier code/tests may still import `_HONESTY`.
_HONESTY = _PAPER_HONESTY


def honesty_note_for(mode: str) -> str:
    """SIM-06 / v1.56: the drill's evidence claim depends on which broker it
    actually severed. `mode` is the trading mode ("paper" | "live"); any other
    value is treated as paper's (the more conservative claim)."""
    return _LIVE_HONESTY if mode == "live" else _PAPER_HONESTY


def drill_confirmation_ok(*, mode: str, confirmation: str) -> bool:
    """UC-12 v1.56: a LIVE-mode drill requires the typed word DRILL (operator
    present, deliberate) — mirroring `mode_switch.CONFIRM_TOKEN`'s LIVE gate.
    Paper drills need no confirmation (SIM-03: they prove less anyway, and
    touch no real broker session)."""
    if mode == "live":
        return confirmation == DRILL_CONFIRMATION
    return True


def drill_guidance(*, near_trigger: bool = False, entry_soon: bool = False) -> list[str]:
    """UC-12 v1.56: advisory warnings for the confirmation dialog — GUIDANCE,
    never a hard block (the operator is supervising and may proceed anyway).
    `near_trigger`: a short mark sits within 50% of its trigger distance.
    `entry_soon`: a scheduled/armed entry fires within 10 minutes."""
    warnings: list[str] = []
    if near_trigger:
        warnings.append("a short mark is within 50% of its trigger distance")
    if entry_soon:
        warnings.append("an entry is scheduled to fire within 10 minutes")
    return warnings


@dataclass(frozen=True)
class DrillEvidence:
    outage_seconds: float
    stops_before: list[dict] = field(default_factory=list)
    stops_after: list[dict] = field(default_factory=list)
    survived: bool = False            # every pre-outage stop still working after
    timestamps_unbroken: bool = False  # placement times unchanged across the outage
    honesty_note: str = _PAPER_HONESTY
    # v1.56: advisory-only, never gates the drill itself.
    guidance: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _snapshot_stops(orders) -> list[dict]:
    out = []
    for o in orders:
        intent = getattr(o, "intent", None)
        if getattr(intent, "order_type", None) == "stop_market":
            out.append({
                "order_id": getattr(o, "order_id", None),
                "received_at": getattr(o, "received_at", None),
                "entry_id": intent.entry_id,
                "leg": "short_put" if intent.legs[0].right == "P" else "short_call",
            })
    return out


async def run_stop_independence_drill(broker, *, outage_seconds: float = 2.0,
                                      mode: str = "paper",
                                      guidance: list[str] | None = None) -> DrillEvidence:
    """UC-12: snapshot the resting stops, simulate an outage of `outage_seconds`,
    then snapshot again and compare. `survived` requires that there WAS at least
    one resting stop and every one is still working afterwards.

    `mode` selects the honesty note (paper vs live, SIM-06/v1.56).
    `guidance` (v1.56): pre-computed advisory warnings for the dialog, carried
    through onto the evidence record (never gates the drill itself)."""
    before = _snapshot_stops(await broker.working_orders())
    await asyncio.sleep(outage_seconds)   # the simulated outage window
    after = _snapshot_stops(await broker.working_orders())

    after_by_id = {s["order_id"]: s for s in after}
    survived = bool(before) and all(s["order_id"] in after_by_id for s in before)
    timestamps_unbroken = all(
        s["received_at"] == after_by_id[s["order_id"]]["received_at"]
        for s in before if s["order_id"] in after_by_id)

    return DrillEvidence(
        outage_seconds=outage_seconds, stops_before=before, stops_after=after,
        survived=survived, timestamps_unbroken=survived and timestamps_unbroken,
        honesty_note=honesty_note_for(mode), guidance=list(guidance or []))
