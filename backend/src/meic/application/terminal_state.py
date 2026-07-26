"""ENT-11 / CLS-08 — THE terminal-state resolver. Asked PER LEG.

There is exactly ONE authoritative answer to "does this hold a position, and is
it finished?", and v1.91 corrected its scope: the question is asked **per leg**.
v1.90 framed it entry-scoped, which leaves the 2026-07-20 incident unfixed — a
close on an already-flat LEG of a still-open ENTRY resolves HOLDS_POSITION at
entry scope and the order reaches the wire, where the broker converts it to a
Buy to Open. **The LEG is the atomic unit — the broker holds positions per leg
— and ENTRY state is DERIVED from its legs, never the reverse.** A resolver
that discards the caller's leg symbol is by definition wrong, so `resolve_leg`
takes the symbol and every answer names it.

THREE states, and `UNKNOWN` is FIRST-CLASS (ENT-11(2)). It may never be
collapsed into either neighbour: it never journals a terminal, never renders
green, and always leaves the entry visible. Collapsing UNKNOWN toward
TERMINAL_NO_POSITION is how a real position becomes invisible; collapsing it
toward HOLDS_POSITION is how a close reaches the wire against nothing.

EVIDENCE IS RANKED AND POSITIVE (ENT-11(3)). Broker `positions()` decides.
Order and fill feeds are ADVISORY and are deliberately NOT consulted here —
this module reads exactly one primitive. **The absence of a record is never
proof of the absence of a position:** every path that could make a position
merely UNSEEN (the call raised, a row was unreadable, the symbology is one we
have never observed) resolves UNKNOWN, never TERMINAL_NO_POSITION.

REFUSALS RAISE (ENT-11(5)). `TerminalStateUnknown` is an exception, not a
sentinel, because "a value nobody is forced to inspect is not a refusal" — the
defect it closes is `SideClosed`/`EntryClosed` being appended after a refused
submit, which is green and terminal while the position is untouched.

OBSERVED CONSTRAINTS (ENT-11(10), v2.02 — from the 2026-07-26 PROD read-only
capture, `tests/contract/observations/06-positions-prod-shape.json`). Every
rule below is an OBSERVED fact, not an inference:

  (a) a broker position symbol is byte-identical to a journaled
      `CondorFilled.legs` symbol for SPX/SPXW — both OCC-21, root
      left-justified to width 6;
  (b) `quantity` carries NO DIRECTION — a short leg and a long leg both report
      `quantity=1`; the sign lives in `quantity_direction`;
  (c) match the FULL OCC symbol, never root+right or `underlying_symbol` — the
      operator's own far-dated SPXW spread is LIVE in the account, so a loose
      match adopts the operator's own book (OWN-01/OWN-03, live data here);
  (d) the list is UNFILTERED AND MIXED — cryptocurrency and equity rows sit
      alongside options with `expires_at: null` and non-OCC symbols, so nothing
      here may parse a row before knowing it is one it understands;
  (e) `restricted_quantity` is observed 0 everywhere, so non-zero is
      UNOBSERVED — `quantity` may NOT be assumed to be the closeable quantity;
  (f) an instrument type we have not observed resolves UNKNOWN, never
      TERMINAL_NO_POSITION — no /ES position is held, so the futures-option
      symbol as returned by `positions()` is unobserved (the recorded /ES
      symbol came from an instrument probe, not a positions row).

NFR-09 runs through all of it: unobserved means UNASSUMED IN BOTH DIRECTIONS.
Every branch below that cannot produce evidence produces UNKNOWN.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum


class LegState(str, Enum):
    """ENT-11(2): exactly three, and UNKNOWN is never collapsed."""

    HOLDS_POSITION = "HOLDS_POSITION"
    TERMINAL_NO_POSITION = "TERMINAL_NO_POSITION"
    UNKNOWN = "UNKNOWN"


class TerminalStateUnknown(RuntimeError):
    """ENT-11(2)/(5): the state could not be established, raised so no caller
    can proceed by ignoring a return value.

    UNKNOWN authorises RE-RESOLUTION AND AN ALERT — never a close order
    (ORD-12), never a journaled terminal, never a green card."""

    def __init__(self, symbol: str, reason: str) -> None:
        super().__init__(f"ENT-11: terminal state UNKNOWN for leg {symbol!r} -- {reason}")
        self.symbol = symbol
        self.reason = reason


class ExitWouldOpen(RuntimeError):
    """ORD-12: an exit order was refused because the leg holds no position.

    Live evidence (external operator, 2026-07-20): two `close:` intents were
    submitted and FILLED as "Buy to Open", re-establishing the very long calls
    the close was meant to remove. Our own PROD dry-run reproduced the
    mechanism verbatim -- the broker answers a close with no position with
    "This order will be updated to a buy to open order when routed".

    Raised, not returned (ENT-11(5)): the defect this closes is `SideClosed`
    and `EntryClosed` being appended after a refused submit -- green and
    terminal on an untouched position, which is strictly worse than no change
    at all."""

    def __init__(self, symbol: str, reason: str) -> None:
        super().__init__(
            f"ORD-12: refusing an exit order on {symbol!r} -- the broker holds no such "
            f"position, so this order would OPEN one ({reason})")
        self.symbol = symbol
        self.reason = reason


@dataclass(frozen=True)
class LegResolution:
    """One leg's answer. `symbol` is echoed back deliberately: a resolver that
    discards the caller's leg symbol is wrong by definition (ENT-11 v1.91), and
    echoing it makes that visible at every call site and in every log line."""

    symbol: str
    state: LegState
    reason: str
    # Populated ONLY when state is HOLDS_POSITION; None otherwise, so a caller
    # that reads a quantity without checking the state gets None rather than a
    # plausible-looking zero it might act on.
    signed_qty: int | None = None
    closeable_qty: int | None = None

    @property
    def holds(self) -> bool:
        return self.state is LegState.HOLDS_POSITION


# ENT-11(10)(f): the instrument types whose `positions()` shape we have
# ACTUALLY OBSERVED. Anything else -- futures options above all, whose symbology
# is different and unobserved in this payload -- resolves UNKNOWN. This is a
# deliberate ALLOW-list and is NOT an NFR-09 violation: NFR-09 forbids
# allow-lists for LIVENESS predicates, where the unrecognised branch must fail
# toward PRESENT. Here the unrecognised branch fails toward UNKNOWN, which
# blocks the destructive action rather than triggering it -- the same direction
# NFR-09 exists to protect, reached by the opposite structure because the
# question is "is this a shape we have evidence for?", not "is this alive?".
_OBSERVED_INSTRUMENT_TYPES = frozenset({"equity option"})

_OCC_LENGTH = 21  # observed: all 12 journaled and all broker equity-option symbols


def _normalize(raw) -> str:
    """Fold an SDK enum, an enum repr, or a wire string onto one comparable
    token: `InstrumentType.EQUITY_OPTION`, `"Equity Option"`, `"equity_option"`
    all -> `"equity option"`. Same folding the order-status token uses."""
    return str(raw or "").strip().lower().split(".")[-1].strip().replace("_", " ")


def is_observed_leg_symbology(symbol: str) -> bool:
    """ENT-11(10)(a)/(f): is this leg symbol in the ONE symbology whose
    `positions()` rows we have observed (OCC-21 equity option)?

    Derived from the capture, not from a standard: root left-justified and
    space-padded to width 6 (`SPXW  `, `SPY   `, `BE    ` -- padding to 6, not
    a fixed suffix), then YYMMDD, then P|C, then strike*1000 zero-padded to 8.

    A futures-option symbol (`./ESU6 E3BN6 260721C7185`) fails this and the
    caller therefore gets UNKNOWN -- which BLOCKS its close and alerts, rather
    than silently reporting the leg flat."""
    if not isinstance(symbol, str) or len(symbol) != _OCC_LENGTH:
        return False
    root, ymd, right, strike = symbol[:6], symbol[6:12], symbol[12], symbol[13:]
    return (bool(root.strip()) and root == root.strip().ljust(6)
            and ymd.isdigit() and right in ("P", "C") and strike.isdigit())


def _row_symbol(row) -> str | None:
    """The row's symbol, or None if it cannot be read.

    ENT-11(10)(d): the list is unfiltered -- crypto (`ETH/USD`) and equity
    (`JOBY`) rows are present -- so this reads the field and NOTHING else. No
    parsing happens here; a row is only interpreted once its symbol has matched
    the caller's leg exactly."""
    raw = getattr(row, "symbol", None)
    return raw if isinstance(raw, str) and raw else None


def _decimal(raw) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ArithmeticError, ValueError, TypeError):
        return None


def _resolve_matched_row(symbol: str, row) -> LegResolution:
    """Interpret the ONE position row whose symbol matched the caller's leg.

    Every unreadable or unobserved field yields UNKNOWN. None of them yield a
    quantity, and none of them yield TERMINAL_NO_POSITION -- a row that EXISTS
    is positive evidence of a position, so the only honest answers here are
    "held, this much" or "held, amount unknown"."""
    instrument = _normalize(getattr(row, "instrument_type", None))
    if instrument not in _OBSERVED_INSTRUMENT_TYPES:
        # ENT-11(10)(f): a matching symbol on a shape we have never observed.
        return LegResolution(symbol, LegState.UNKNOWN,
                             f"position row instrument_type {instrument!r} is unobserved "
                             "-- its quantity and direction fields are unverified")

    # ENT-11(10)(b): the sign is in `quantity_direction`, NEVER in the number.
    # A short leg and a long leg both report quantity=1 (observed on the
    # operator's own SPXW spread: C8010 Short and C8000 Long, both `1`), so a
    # resolver reading the number alone cannot tell them apart.
    direction = _normalize(getattr(row, "quantity_direction", None))
    if direction not in ("long", "short"):
        return LegResolution(symbol, LegState.UNKNOWN,
                             f"quantity_direction {direction!r} is not one of the observed "
                             "values ('Long', 'Short') -- the position's SIGN is unreadable")

    quantity = _decimal(getattr(row, "quantity", None))
    if quantity is None or quantity != quantity.to_integral_value() or quantity <= 0:
        # Observed as an integral Decimal on every option row. A non-integral
        # or non-positive quantity on an option is a shape we have not seen;
        # a zero-quantity row in particular is NOT read as "flat" -- the broker
        # omits flat legs entirely in the capture, so a zero row would be an
        # unobserved shape whose meaning we would be guessing at.
        return LegResolution(symbol, LegState.UNKNOWN,
                             f"quantity {getattr(row, 'quantity', None)!r} is absent, "
                             "unparsable, non-integral or non-positive -- unobserved shape")

    # ENT-11(10)(e): `restricted_quantity` is observed 0 on every row, so its
    # non-zero behaviour is UNOBSERVED. `quantity` may NOT be assumed to be the
    # CLOSEABLE quantity: a restricted position would size a close against
    # contracts that cannot be closed, and the surplus is a Buy to Open.
    restricted = _decimal(getattr(row, "restricted_quantity", None))
    if restricted is None:
        return LegResolution(symbol, LegState.UNKNOWN,
                             "restricted_quantity is absent or unparsable -- the CLOSEABLE "
                             "quantity cannot be established")
    if restricted != 0:
        return LegResolution(symbol, LegState.UNKNOWN,
                             f"restricted_quantity {restricted} is non-zero, a shape never "
                             "observed -- closeable quantity is unverified (ENT-11(10)(e))")

    qty = int(quantity)
    return LegResolution(
        symbol, LegState.HOLDS_POSITION,
        f"broker reports {direction} {qty} at {symbol}",
        signed_qty=qty if direction == "long" else -qty,
        closeable_qty=qty,
    )


class TerminalStateResolver:
    """The ONE resolver. Every path that decides whether a leg is finished
    calls this; no path infers (ENT-11(1)).

    It reads exactly one broker primitive -- `positions()` -- because ENT-11(3)
    ranks that as the deciding evidence and everything else as advisory. Adding
    a second, "helpful" source here is how inference creeps back in.
    """

    def __init__(self, broker, *, alerts=None) -> None:
        self._broker = broker
        self._alerts = alerts

    async def resolve_leg(self, symbol: str) -> LegResolution:
        """The answer for ONE leg, identified by its full OCC symbol.

        Never raises for an UNKNOWN outcome -- it RETURNS `LegState.UNKNOWN`.
        Raising is the ORDER path's job (`require_holds_position`), because a
        reporting caller must be able to ASK without being aborted, while an
        ORDER caller must not be able to proceed by ignoring the answer.
        """
        # ENT-11(10)(f): decided BEFORE the lookup. If the lookup ran first, a
        # futures-option leg would simply fail to match any row and fall
        # through to TERMINAL_NO_POSITION -- reporting a leg we may well hold
        # as flat, which is the v2.01 failure direction exactly.
        if not is_observed_leg_symbology(symbol):
            return LegResolution(symbol, LegState.UNKNOWN,
                                 "leg symbology is not the observed OCC-21 equity-option "
                                 "shape (e.g. a futures option) -- `positions()` rows for it "
                                 "have never been observed (ENT-11(10)(f))")

        try:
            rows = await self._broker.positions()
        except Exception as exc:  # noqa: BLE001 -- ANY failure is "unseen", never "absent"
            # ENT-11(3): the absence of a record is never proof of the absence
            # of a position. A failed read produces NO evidence, so it can only
            # produce UNKNOWN.
            return LegResolution(symbol, LegState.UNKNOWN,
                                 f"positions() failed ({type(exc).__name__}: {exc}) -- "
                                 "no evidence either way")

        if rows is None:
            return LegResolution(symbol, LegState.UNKNOWN,
                                 "positions() returned None rather than a list -- no evidence")

        matches = []
        unreadable = 0
        for row in rows:
            row_symbol = _row_symbol(row)
            if row_symbol is None:
                unreadable += 1
                continue
            # ENT-11(10)(c): FULL-symbol equality. Never root+right, never
            # `underlying_symbol` -- the operator's own far-dated SPXW spread
            # is live in this account and matches on both of those.
            if row_symbol == symbol:
                matches.append(row)

        if len(matches) > 1:
            # Never observed: the broker aggregates a leg into one row. Two
            # rows for one symbol is a shape we cannot interpret, and picking
            # one would be a guess about which is authoritative.
            return LegResolution(symbol, LegState.UNKNOWN,
                                 f"{len(matches)} position rows share symbol {symbol!r} -- "
                                 "aggregation shape unobserved, cannot choose")
        if matches:
            return _resolve_matched_row(symbol, matches[0])

        if unreadable:
            # ENT-11(3) at its sharpest: we did not find the leg, but we also
            # could not read every row. "Not found among rows I could read" is
            # not proof of absence.
            return LegResolution(symbol, LegState.UNKNOWN,
                                 f"no row matched, but {unreadable} row(s) had an unreadable "
                                 "symbol -- absence is not established")

        # The one place TERMINAL_NO_POSITION is returned: the call succeeded,
        # every row was readable, the symbology is one we have observed, and
        # none matched. That is POSITIVE evidence of flatness, not an absence.
        return LegResolution(symbol, LegState.TERMINAL_NO_POSITION,
                             f"positions() returned {len(rows)} readable row(s), none at "
                             f"{symbol}")

    async def require_holds_position(self, symbol: str) -> LegResolution:
        """The ORDER path's gate: return the resolution, or RAISE.

        ENT-11(5): a refusal must propagate as an exception callers cannot
        ignore. Both non-holding states raise, with distinct types so a caller
        can tell "provably flat, so this close is a no-op" from "we do not
        know, so alert and re-resolve" -- ORD-12 treats them differently, and
        collapsing them would lose exactly the distinction ENT-11(2) exists to
        preserve.
        """
        resolution = await self.resolve_leg(symbol)
        if resolution.state is LegState.UNKNOWN:
            self._alert("error", "ENT-11: leg terminal state UNKNOWN -- close refused, "
                                 "re-resolution required", symbol=symbol,
                        reason=resolution.reason)
            raise TerminalStateUnknown(symbol, resolution.reason)
        if resolution.state is LegState.TERMINAL_NO_POSITION:
            raise ExitWouldOpen(symbol, resolution.reason)
        return resolution

    async def resolve_entry(self, symbols) -> tuple[LegState, tuple[LegResolution, ...]]:
        """ENTRY state DERIVED from its legs, never the reverse (ENT-11 v1.91).

        Any UNKNOWN leg makes the ENTRY UNKNOWN -- an entry cannot be more
        certain than its least-certain leg, and an entry declared terminal
        while one leg is merely unseen is the lingering-position defect this
        rule was written for. An entry is TERMINAL_NO_POSITION only when EVERY
        leg is provably flat.
        """
        resolutions = tuple([await self.resolve_leg(s) for s in symbols])
        if not resolutions:
            return LegState.UNKNOWN, resolutions
        if any(r.state is LegState.UNKNOWN for r in resolutions):
            return LegState.UNKNOWN, resolutions
        if any(r.state is LegState.HOLDS_POSITION for r in resolutions):
            return LegState.HOLDS_POSITION, resolutions
        return LegState.TERMINAL_NO_POSITION, resolutions

    def _alert(self, level: str, message: str, **context) -> None:
        if self._alerts is not None:
            self._alerts.alert(level, message, **context)
