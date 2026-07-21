"""Futures-option symbology (CME /ES etc.) — UND-04 (/ES Stage 1).

Broker-reported futures-option leg symbol (PROD probe 2026-07-21, real cert
order payload):

    ./ESU6 E3BN6 260721C7185
    ^^^^^^ front-future contract symbol ("/ESU6") -- what spot subscribes to
           ^^^^^ daily option-root symbol -- VARIES by weekday (E3B/E4C/E4D/
                 EW4), read live off the chain, never constructed here
                 ^^^^^^ expiration YYMMDD
                       ^ right P|C
                        ^^^^ strike, PLAIN POINTS -- NOT scaled x1000 like OCC

This is structurally incompatible with `adapters/occ.py`'s 21-char, root-
padded, x1000-scaled layout (`occ_symbol` stays equity-only, never touched
here). Pure — no SDK import, no network — so `TastytradeAdapter.fill_legs`
can parse a broker-reported futures-option leg symbol without reconstructing
it (ORD-09: symbols are read verbatim off the fill, this module only reads
the RIGHT back out of what the broker already sent).
"""
from __future__ import annotations

import re
from decimal import Decimal

# Matches the LAST whitespace-separated token of a futures-option symbol:
# 6-digit expiration, right (P|C), then a plain (optionally decimal) strike.
_LEG_RE = re.compile(r"(\d{6})([CP])(\d+(?:\.\d+)?)$")


def parse_future_option_symbol(symbol: str) -> tuple[str, str, Decimal]:
    """Return (expiration_yymmdd, right, strike) parsed off a broker-reported
    futures-option symbol, e.g. "./ESU6 E3BN6 260721C7185" ->
    ("260721", "C", Decimal("7185")). Raises ValueError on a symbol that does
    not carry the expected trailing `YYMMDD[C|P]<strike>` token — never
    guesses a right/strike from a shape it cannot parse."""
    token = symbol.strip().split(" ")[-1]
    m = _LEG_RE.search(token)
    if not m:
        raise ValueError(f"cannot parse futures-option symbol {symbol!r}")
    yymmdd, right, strike = m.groups()
    return yymmdd, right, Decimal(strike)
