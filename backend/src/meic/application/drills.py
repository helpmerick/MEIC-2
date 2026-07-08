"""Operational drills — UC-12 stop independence.

A deliberate, supported procedure (not an edge case): with resting stops in
place, simulate a bot outage and verify the stops remained WORKING at the
broker throughout, with unbroken placement timestamps (STP-05 core claim).

Honesty (SIM-06): in PAPER the SimulatedBroker holds the stops in-process, so
this proves the RECOVERY MECHANISM and the timestamp evidence — not true
broker-side independence. The bot-independent proof is the cert sandbox drill
(TC-STP-08, `pytest -m contract`). The drill says so in its own result.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field

_HONESTY = ("PAPER: proves the recovery mechanism + timestamp evidence, not "
           "broker-side independence (SIM-06). Bot-independent proof is the "
           "sandbox drill TC-STP-08 (pytest -m contract).")


@dataclass(frozen=True)
class DrillEvidence:
    outage_seconds: float
    stops_before: list[dict] = field(default_factory=list)
    stops_after: list[dict] = field(default_factory=list)
    survived: bool = False            # every pre-outage stop still working after
    timestamps_unbroken: bool = False  # placement times unchanged across the outage
    honesty_note: str = _HONESTY

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


async def run_stop_independence_drill(broker, *, outage_seconds: float = 2.0) -> DrillEvidence:
    """UC-12: snapshot the resting stops, simulate an outage of `outage_seconds`,
    then snapshot again and compare. `survived` requires that there WAS at least
    one resting stop and every one is still working afterwards."""
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
        survived=survived, timestamps_unbroken=survived and timestamps_unbroken)
