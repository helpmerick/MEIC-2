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
