"""fee_model config (PNL-01, doc 06 configuration reference: `fee_model` --
"per-contract fee table", default "tastytrade SPX schedule (verify at build
time)", effectivity next-day).

SOURCE (verified at build time, per doc 06's instruction): tastytrade's
official "Commissions & Fees" schedule (fetched directly from tastytrade's
own asset CDN, "Last updated July 1, 2026") plus the "Single-Listed Exchange
Proprietary Index Options Fees" table on the same document:

  * Broad-Based Index Options: "$1.00 / contract to open, $0.00 commission
    to close ... clearing fee of $0.10 per contract + regulatory fee ...
    + Single-Listed Exchange Proprietary Index Options Fee".
  * Trade-related fees table: Options Regulatory Fee (ORF) $0.02/contract;
    Clearing Fees - Options $0.10/contract.
  * Single-Listed Exchange Proprietary Index Options Fees table: SPX $0.60
    per contract (RUT $0.18, VIX $0.35, OEX/XEO $0.40, DJX $0.18, XSP
    $0.00/$0.07, CBTX $0.50, MBTX $0.25, NDX $0.25).

WHERE COMMISSION APPLIES (the part the flat "$1/contract to open" summary
elides, and the part this table gets exactly right -- verified against real
broker rows, see `application/report_reconciler.py::_fee_cost_of`'s
docstring and the PNL-01 build-time gate test): tastytrade charges the
$1.00 commission on a SELL-TO-OPEN leg only -- i.e. the SHORT leg of a
credit spread being opened. The LONG (protective wing) leg's buy-to-open is
commission-free, and BOTH legs are commission-free to close. Clearing, ORF
and the index-option exchange fee apply to every contract, on every
transaction, opening or closing, buy or sell (footnote on the same
schedule: "Applicable exchange, clearing, and regulatory fees still apply
to all opening and closing trades").

This is config (nothing hardcoded, CLAUDE.md rule 6): the composition root
constructs one `FeeModel` from `config.fee_model` and hands it to every
service that fills a leg. `domain/fees.py` is the ONE place that actually
applies the table to a fill.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


# Single-Listed Exchange Proprietary Index Options Fee, per contract
# (tastytrade "Commissions & Fees" schedule, page 3 table; last updated
# 2026-07-01). Keyed by the INDEX symbol (e.g. "SPX"), not the OCC option
# root ("SPXW") -- the exchange fee is assessed on the index product, not
# the specific expiration series.
DEFAULT_INDEX_OPTION_FEES: dict[str, Decimal] = {
    "SPX": Decimal("0.60"),
    "RUT": Decimal("0.18"),
    "VIX": Decimal("0.35"),
    "OEX": Decimal("0.40"),
    "XEO": Decimal("0.40"),
    "DJX": Decimal("0.18"),
    "XSP": Decimal("0.00"),   # $0.00 below 10 contracts/leg; $0.07 at 10+ (not modeled -- this bot never runs XSP)
    "CBTX": Decimal("0.50"),
    "MBTX": Decimal("0.25"),
    "NDX": Decimal("0.25"),
}

DEFAULT_EXCHANGE_FEE = DEFAULT_INDEX_OPTION_FEES["SPX"]  # doc 06 `underlying` default is SPX


class FeeModelRejected(ValueError):
    def __init__(self, field_name: str, reason: str) -> None:
        self.field, self.reason = field_name, reason
        super().__init__(f"fee_model {field_name!r} rejected: {reason}")


@dataclass(frozen=True)
class FeeModel:
    """PNL-01 per-contract fee table -- `config.fee_model`.

    `commission_open` is charged ONCE per contract, ONLY on a sell-to-open
    leg (a short being opened). `clearing_fee` and `regulatory_fee` apply to
    every contract on every transaction (open or close, buy or sell).
    `index_option_fees` is the per-underlying exchange-fee table; an
    underlying not in the table falls back to `default_exchange_fee`.

    All-Decimal, exact -- no float ever touches a fee (mirrors every other
    money field in this codebase, domain/events.py's own convention).
    """

    commission_open: Decimal = Decimal("1.00")
    clearing_fee: Decimal = Decimal("0.10")
    regulatory_fee: Decimal = Decimal("0.02")
    underlying: str = "SPX"  # doc 06 `underlying` default -- the index this fee table prices
    index_option_fees: dict[str, Decimal] = field(
        default_factory=lambda: dict(DEFAULT_INDEX_OPTION_FEES))
    default_exchange_fee: Decimal = DEFAULT_EXCHANGE_FEE

    def exchange_fee(self, underlying: str | None = None) -> Decimal:
        key = (underlying or self.underlying).upper()
        return self.index_option_fees.get(key, self.default_exchange_fee)

    def per_contract_fee(self, *, role: str, opening: bool, underlying: str | None = None) -> Decimal:
        """The total fee for ONE contract of ONE leg.

        `role` is "short" | "long" (FilledLeg.role); `opening` is True for an
        opening trade (an entry fill), False for a closing trade (stop
        buyback, LEX sale, decay buyback, manual close). Commission applies
        only when `opening and role == "short"` (sell-to-open) -- see the
        module docstring for why. Every other component applies
        unconditionally, matching the schedule's "applies to all opening
        and closing trades."
        """
        total = self.clearing_fee + self.regulatory_fee + self.exchange_fee(underlying)
        if opening and role == "short":
            total += self.commission_open
        return total

    def per_share_fee(self, *, role: str, opening: bool, underlying: str | None = None) -> Decimal:
        """The SAME fee, rescaled to the "per-share" unit every other money
        field on these events uses (`CondorFilled.net_credit`,
        `ShortStopped.fill`, `LongSold.recovery` are all per-share -- REAL
        dollars need `* 100 * contracts`, applied ONCE at the reporting
        layer; see `domain/projection.py`'s and `reporting/folds.py`'s own
        docstrings). A real per-contract dollar fee is, by construction,
        already linear in contracts (each contract is charged its own
        commission/clearing/exchange fee) -- exactly the same linearity
        `entry_dollars`'s `* 100 * contracts` assumes for `net_credit` et al.
        So dividing by 100 here (and NEVER multiplying by contracts again)
        is what makes `entry_dollars_fees` recover the real per-contract
        dollar total, contracts-count-independent: for N contracts,
        `(fee_per_contract / 100) * 100 * N == fee_per_contract * N`.

        Mixing scales here (recording real dollars directly, or worse,
        real-dollars-times-contracts) is exactly the "silently double- or
        under-scale" trap `EntryProjection.settlements`'s own docstring
        warns about for the settlement fields -- this is that same trap on
        the fee side, caught by the PNL-01 build-time verification gate
        (a day's total pnl going wildly negative from ordinary-sized fees is
        the symptom of getting this multiplication backwards).
        """
        return self.per_contract_fee(role=role, opening=opening, underlying=underlying) / Decimal(100)


def validate_fee_model(patch: dict) -> None:
    """UI-03: server-side validation of a `fee_model` config patch (a nested
    dict, the "per-contract fee table" doc 06 describes -- not a scalar, so
    it does not fit `config/validation.py`'s single-key checks, but follows
    the identical reject-never-clamp convention: PNL-01 money going into a
    fee table must never silently become negative or non-numeric)."""
    numeric_keys = ("commission_open", "clearing_fee", "regulatory_fee", "default_exchange_fee")
    for key in numeric_keys:
        if key not in patch:
            continue
        try:
            value = Decimal(str(patch[key]))
        except Exception as exc:  # noqa: BLE001 - any bad literal is a reject, not a crash
            raise FeeModelRejected(key, "not_a_number") from exc
        if value < 0:
            raise FeeModelRejected(key, "negative")
    fees = patch.get("index_option_fees")
    if fees is not None:
        if not isinstance(fees, dict):
            raise FeeModelRejected("index_option_fees", "not_a_table")
        for symbol, raw in fees.items():
            try:
                value = Decimal(str(raw))
            except Exception as exc:  # noqa: BLE001
                raise FeeModelRejected(f"index_option_fees.{symbol}", "not_a_number") from exc
            if value < 0:
                raise FeeModelRejected(f"index_option_fees.{symbol}", "negative")


def fee_model_from_config(cfg: dict | None) -> FeeModel:
    """Build a `FeeModel` from a config dict (the shape `validate_fee_model`
    checks), falling back to the verified tastytrade defaults for any field
    the dict omits. `cfg` is normally `config.get("fee_model")` -- None
    (no override saved yet) yields the pure default table."""
    if not cfg:
        return FeeModel()
    kwargs: dict = {}
    for key in ("commission_open", "clearing_fee", "regulatory_fee", "default_exchange_fee"):
        if key in cfg:
            kwargs[key] = Decimal(str(cfg[key]))
    if "underlying" in cfg:
        kwargs["underlying"] = str(cfg["underlying"])
    if "index_option_fees" in cfg:
        kwargs["index_option_fees"] = {
            str(sym).upper(): Decimal(str(v)) for sym, v in cfg["index_option_fees"].items()
        }
    return FeeModel(**kwargs)
