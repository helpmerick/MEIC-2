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
from decimal import Decimal

from meic.reporting.mae_mfe import consumed_fraction

DRILL_CONFIRMATION = "DRILL"

# UC-12 near-trigger drill guidance (operator ruling 2026-07-11): warn when
# trigger-distance consumed is >= 50%.
NEAR_TRIGGER_THRESHOLD = Decimal("0.50")

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


@dataclass(frozen=True)
class OpenShortMark:
    """One open short leg's live trigger-distance inputs, for
    `near_trigger_status` below (operator ruling 2026-07-11): the recorded
    fill and the resting stop's trigger, plus the CURRENT live mark -- `None`
    when this tick has no usable mark for it (never a guess, D10-style
    honesty)."""

    fill: Decimal
    trigger: Decimal
    mark: Decimal | None


def near_trigger_status(shorts: list[OpenShortMark]) -> bool | None:
    """UC-12 near-trigger drill guidance (operator ruling 2026-07-11):
    trigger-distance consumed = (current mark − fill) / (trigger − fill) --
    the SAME shared formula RPT-12's MAE uses
    (`reporting.mae_mfe.consumed_fraction`), evaluated at the CURRENT live
    mark rather than the worst RECORDED sample.

    True: ANY open short's consumed fraction is >= `NEAR_TRIGGER_THRESHOLD`
    (50%) -- a confirmed breach warns even if some OTHER side's mark is
    unusable. False: every short has a usable mark and none reached 50%.
    `None`: at least one short has NO usable mark and none of the computable
    ones already breached -- an honest "unknown", never silently reported as
    False (no warning-suppression claim). An empty `shorts` list (nothing
    open to watch) is `False` -- there is genuinely nothing to warn about."""
    unknown = False
    for s in shorts:
        if s.mark is None:
            unknown = True
            continue
        consumed = consumed_fraction(s.mark, fill=s.fill, trigger=s.trigger)
        if consumed is not None and consumed >= NEAR_TRIGGER_THRESHOLD:
            return True
    return None if unknown else False


def drill_guidance(*, near_trigger: bool | None = False, entry_soon: bool = False) -> list[str]:
    """UC-12 v1.56: advisory warnings for the confirmation dialog — GUIDANCE,
    never a hard block (the operator is supervising and may proceed anyway).
    `near_trigger` (operator ruling 2026-07-11, `near_trigger_status` above):
    True warns explicitly; `None` (no usable live mark for at least one open
    short) warns that trigger-distance is UNKNOWN rather than silently
    reporting no warning at all; False is silent. `entry_soon`: a
    scheduled/armed entry fires within 10 minutes."""
    warnings: list[str] = []
    if near_trigger is True:
        warnings.append("a short mark is within 50% of its trigger distance")
    elif near_trigger is None:
        warnings.append("trigger-distance unknown for at least one open short (no live mark)")
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
