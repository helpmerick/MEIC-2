// SPX options carry a $100-per-point contract multiplier, and ENT-04 lets an
// entry trade more than one contract — a Decimal premium string like "5.20"
// is $520 of real cash for a single contract, "0.40" is $40. The backend's
// per-entry/day-report Decimal fields (net_credit, pnl, per_entry_pnl,
// premium_received, DayReport.total_credit/day_pnl) are PER-SHARE, unscaled
// by the contract multiplier OR by contracts (see backend/src/meic/domain/
// projection.py and reporting/folds.py's module docstring: "`.net_credit`/
// `.pnl` are PER-SHARE amounts... real dollars need `* 100 * contracts`").
// This module is the frontend's ONE place that performs that same
// conversion for DISPLAY (operator request 2026-07-11) — it mirrors
// reporting/folds.py's `entry_dollars`/`entry_credit_dollars` exactly
// (CONTRACT_MULTIPLIER = 100), and matches the convention server.py's
// `_live_pnl_enricher` already uses for `live_pnl` (already real dollars).
//
// The decimal point is shifted with exact BigInt digit arithmetic, never
// `Number(x) * 100` — floating point cannot represent most 2dp decimals
// exactly (5.2 * 100 === 520.00000000000006 in IEEE754 double), so a naive
// float multiply can silently corrupt a cash figure derived from a broker
// Decimal string. Sub-cent precision in the source string (rare, but never
// forbidden by the domain) survives the shift unrounded.

const DECIMAL_RE = /^(-?)(\d+)(?:\.(\d+))?$/;

/** Exact `value * factor` for a Decimal STRING and a positive integer
 * `factor`, computed with BigInt digit arithmetic — no float ever touches
 * the value. An unparsable `value` contributes "0" (honest, never NaN in
 * the UI, matching the codebase's existing null-safety conventions). */
function multiplyDecimalString(value: string, factor: number): string {
  const m = DECIMAL_RE.exec(value.trim());
  if (!m) return "0";
  const [, sign, intPart, fracPart = ""] = m;
  const digits = intPart + fracPart;
  const big = BigInt(digits) * BigInt(factor);
  const negative = sign === "-" && big !== 0n;
  if (fracPart.length === 0) {
    return (negative ? "-" : "") + big.toString();
  }
  const padded = big.toString().padStart(fracPart.length + 1, "0");
  const head = padded.slice(0, padded.length - fracPart.length);
  const tail = padded.slice(padded.length - fracPart.length).replace(/0+$/, "");
  const magnitude = tail.length > 0 ? `${head}.${tail}` : head;
  return (negative ? "-" : "") + magnitude;
}

/** Real cash value of a per-share premium Decimal STRING for `contracts`
 * contracts: `premium * 100 * contracts`, exact. */
function scale(premium: string, contracts: number): string {
  return multiplyDecimalString(premium, 100 * contracts);
}

/** "+$520" / "-$105" — a signed contract-dollar amount, matching the
 * codebase's existing `money()` sign convention (>= 0 gets "+"). Use for any
 * P&L-shaped figure (entry P&L, per-side premium, per-entry table rows). */
export function contractDollars(premium: string, contracts = 1): string {
  const scaled = scale(premium, contracts);
  const negative = scaled.startsWith("-");
  return (negative ? "-$" : "+$") + (negative ? scaled.slice(1) : scaled);
}

/** Unsigned magnitude only (no "$", no sign) — for splicing into an existing
 * literal `$` template (e.g. `credit $${contractDollarsPlain(...)}`) where a
 * forced +/- sign would be new and unwanted, such as a credit that is always
 * collected (never negative in practice). */
export function contractDollarsPlain(premium: string, contracts = 1): string {
  const scaled = scale(premium, contracts);
  return scaled.startsWith("-") ? scaled.slice(1) : scaled;
}

/** The exact contract-dollar amount as a number, for aggregating several
 * entries into one display total (see `formatDollars` below to render the
 * aggregate). Safe past this point because the risky operation — the
 * decimal-point shift — has already happened as exact string arithmetic;
 * summing the resulting (generally whole-cent-of-dollar) numbers in
 * floating point introduces no meaningful error for realistic option
 * premiums. */
export function contractDollarsValue(premium: string, contracts = 1): number {
  return Number(scale(premium, contracts));
}

/** "+$520" / "-$40.50" from a plain number (e.g. a sum of several
 * `contractDollarsValue` results). Rounds to the nearest cent-of-a-dollar to
 * clean any float summation noise, then trims a trailing ".00"/".0" so a
 * whole-dollar total reads as "$520", not "$520.00" — while still showing
 * genuine sub-dollar precision (e.g. "$520.5") when it's really there. */
export function formatDollars(n: number): string {
  const cleaned = Math.round(n * 100) / 100;
  const abs = Math.abs(cleaned);
  let s = abs.toFixed(2);
  if (s.endsWith(".00")) s = s.slice(0, -3);
  else if (s.endsWith("0")) s = s.slice(0, -1);
  return (cleaned < 0 ? "-$" : "+$") + s;
}
