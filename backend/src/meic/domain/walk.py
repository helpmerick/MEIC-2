"""Premium probe walk — STK-02 (v1.39, Ash's probe walk; supersedes the
v1.4/1.5 ceiling walk), STK-03 wing, STK-07 short-bid validity, STK-11
probe-match integrity.

Every candidate strike's raw mid rounds to the nearest $0.05 — its "probe
price". Starting at target T, probes run in this exact order:
    T, T−0.05, T+0.05, T−0.10, T+0.10, T−0.15, T+0.15, then DOWN ONLY.
Limits: probe_up_max (default 3) probes above target, probe_down_max
(default 25) below; the effective floor is max(T − 0.05×probe_down_max,
min_short_premium) — never sell a short under the hard floor regardless of
depth. The FIRST matching probe wins; ties within a probe go to the raw mid
closest to the probe price, then further OTM. All probes exhausted ⇒ skip
`no_valid_strikes`.

The probe sequence is deterministic and the matched probe number is part of
the selection result (STK-11: the day report logs it). The retired
target_premium_tolerance has no successor here — probe_up_max/probe_down_max
replace it (v1.39).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .chain import ChainSide

_LATTICE = Decimal("0.05")


def lattice_price(mid: Decimal) -> Decimal:
    """Nearest-0.05 rounding: 2.93 -> 2.95, 2.92 -> 2.90 (STK-02 probe price)."""
    steps = (mid / _LATTICE).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (steps * _LATTICE).quantize(_LATTICE)


def probe_prices(
    target: Decimal,
    *,
    floor: Decimal,
    probe_up_max: int = 3,
    probe_down_max: int = 25,
) -> tuple[Decimal, ...]:
    """The exact deterministic probe order (STK-02/STK-11). Probes below the
    effective floor are never taken."""
    seq: list[Decimal] = [target] if target >= floor else []
    for k in range(1, max(probe_up_max, probe_down_max) + 1):
        down = target - _LATTICE * k
        if k <= probe_down_max and down >= floor:
            seq.append(down)
        if k <= probe_up_max:
            seq.append(target + _LATTICE * k)
    return tuple(seq)


@dataclass(frozen=True)
class Selected:
    short_strike: Decimal
    long_strike: Decimal
    short_mid: Decimal
    probe_number: int  # 1-indexed position in the deterministic sequence (STK-11 log)
    probe_price: Decimal


@dataclass(frozen=True)
class Skip:
    reason: str  # "no_valid_strikes" | "incomplete_chain"


@dataclass(frozen=True)
class WingUnmarked:
    """STK-11/STK-07: a missing wing mark retries within the entry window
    rather than skipping immediately — retry policy is application-layer."""

    short_strike: Decimal
    long_strike: Decimal


def floor_inside_spot(spot: Decimal, *, put_floor: Decimal | None,
                      call_floor: Decimal | None) -> bool:
    """ENT-09b v1.57 refuse-and-re-pick: True when spot has crossed a selected
    floor between dialog-open and OK -- call floor <= spot, or put floor >=
    spot -- meaning the operator's chosen floor no longer makes directional
    sense (it would now sit on or across the money). The fire must be REFUSED
    (reason `floor_inside_spot`) rather than silently reinterpreted."""
    if put_floor is not None and put_floor >= spot:
        return True
    if call_floor is not None and call_floor <= spot:
        return True
    return False


def _short_floor_ok(strike: Decimal, short_floor: Decimal | None, otm_direction: Decimal) -> bool:
    """ENT-09b v1.57 manual minimum short-strike floor. FLOOR semantics, never a
    pin: puts select short <= the floor (further OTM/down is fine), calls
    select short >= the floor (further OTM/up is fine). `None` = no floor set
    (default OFF, dialog unchanged) -- every pre-v1.57 caller is unaffected."""
    if short_floor is None:
        return True
    return strike <= short_floor if otm_direction < 0 else strike >= short_floor


def select_side(
    side: ChainSide,
    *,
    target_premium: Decimal,
    wing_width: Decimal,
    otm_direction: Decimal,  # puts: -1 (long below short), calls: +1
    min_short_premium: Decimal = Decimal("1.00"),
    probe_up_max: int = 3,
    probe_down_max: int = 25,
    validated: frozenset[Decimal] | None = None,
    short_floor: Decimal | None = None,
) -> Selected | Skip | WingUnmarked:
    """One side's probe-walk selection. Credit gates (STK-05/06) run
    separately on the result.

    `validated` (STK-10 v1.55, optional -- `None` preserves every pre-v1.55
    caller unchanged): the entry's baseline-captured validated universe.
    Shopping is restricted to it -- a short outside it is simply never a
    candidate, and a candidate whose WING lands outside it is an INFEASIBLE
    candidate: the walk continues to the next probe rather than treating it as
    a whole-selection failure (contrast: a wing missing from `side` entirely is
    still STK-07 `no_valid_strikes`; a wing IN the validated universe but
    merely unmarked THIS instant is still the STK-11 `WingUnmarked` retry --
    only "never validated at baseline" gets the new infeasible-candidate skip).

    `short_floor` (ENT-09b v1.57, optional): a manual-entry floor on the short
    strike itself (see `_short_floor_ok`); candidates violating it are simply
    never proposed, so the walk naturally continues to the next qualifying
    probe -- "the probe walk runs unchanged among qualifying strikes".
    """
    floor = max(target_premium - _LATTICE * probe_down_max, min_short_premium)

    by_probe: dict[Decimal, list[Decimal]] = {}
    for strike in side.strikes_toward_otm:
        mark = side.marks.get(strike)
        if mark is None:
            continue
        if validated is not None and strike not in validated:
            continue  # STK-10 v1.55: shops only inside the validated universe
        if not _short_floor_ok(strike, short_floor, otm_direction):
            continue  # ENT-09b v1.57: inside the floor -- never a candidate
        by_probe.setdefault(lattice_price(mark.mid), []).append(strike)

    for number, probe in enumerate(
        probe_prices(target_premium, floor=floor, probe_up_max=probe_up_max, probe_down_max=probe_down_max),
        start=1,
    ):
        candidates = by_probe.get(probe)
        if not candidates:
            continue
        # tie-break: raw mid closest to the probe, then further OTM
        strike = min(
            candidates,
            key=lambda s: (abs(side.marks[s].mid - probe), -side.strikes_toward_otm.index(s)),
        )
        mark = side.marks[strike]
        if mark.bid <= 0:
            return Skip("no_valid_strikes")  # STK-07: short needs a real bid
        # STK-11 sanity invariant — true by construction of nearest-0.05 rounding
        assert abs(mark.mid - probe) <= Decimal("0.025"), (
            f"probe-match integrity violated: raw mid {mark.mid} vs probe {probe}"
        )
        long_strike = strike + wing_width * otm_direction  # STK-03
        if long_strike not in side.strikes_toward_otm:
            return Skip("no_valid_strikes")  # wing not listed
        if validated is not None and long_strike not in validated:
            continue  # STK-10 v1.55: infeasible candidate -- walk continues, not a failure
        if not side.is_marked(long_strike):
            return WingUnmarked(strike, long_strike)  # retry, don't skip yet
        return Selected(strike, long_strike, mark.mid, number, probe)

    return Skip("no_valid_strikes")  # all probes exhausted (STK-02)
