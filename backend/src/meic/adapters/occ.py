"""OCC option symbology — pure (no SDK, no network).

    SPXW  260707P03000000
    ^^^^^^ root, 6 chars, space-padded
          ^^^^^^ expiration YYMMDD
                ^ right P|C
                 ^^^^^^^^ strike x 1000, zero-padded to 8

Verified against a real cert order payload (tests/contract/observations/
02-trigger-source-evidence.json). Strike is scaled by 1000 exactly — a strike
that is not a whole thousandth is a programming error, not a rounding problem.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal


def occ_symbol(underlying: str, expiration: date, right: str, strike: Decimal) -> str:
    """Build the 21-char OCC symbol the broker expects."""
    if right not in ("P", "C"):
        raise ValueError(f"right must be P or C, got {right!r}")
    if len(underlying) > 6:
        raise ValueError(f"underlying {underlying!r} exceeds the 6-char OCC root")
    scaled = Decimal(strike) * 1000
    if scaled != scaled.to_integral_value():
        raise ValueError(f"strike {strike} is not an exact thousandth")
    return f"{underlying.ljust(6)}{expiration:%y%m%d}{right}{int(scaled):08d}"


def leg_symbol(intent, leg) -> str:
    """The symbol a broker would report for this leg of this intent."""
    return leg.symbol or occ_symbol(intent.underlying, intent.expiration, leg.right, leg.strike)


def simulated_fill_legs(intent) -> tuple:
    """ORD-09 for the simulating brokers: report each leg's symbol, as a real
    broker would (TC-ORD-07: "paper records simulator symbols in the same fields").

    `price` is left None — a simulator has no BROKER-ALLOCATED per-leg price to
    report, and inventing one would poison the very field STP-02d exists to
    reconcile. STP-02d is real-fills-only for exactly this reason.
    """
    from meic.domain.events import FilledLeg

    # On an OPENING condor the shorts are the sold legs; the wings are bought.
    return tuple(
        FilledLeg(symbol=leg_symbol(intent, leg), right=leg.right,
                  role="short" if leg.action == "sell_to_open" else "long",
                  qty=leg.qty, price=None)
        for leg in intent.legs
    )
