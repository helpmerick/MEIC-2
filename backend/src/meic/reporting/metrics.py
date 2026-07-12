"""RPT-04 return & risk metrics — pure Decimal math over a plain sequence of
per-period net-dollar values (the caller decides whether that sequence is
per-day or per-entry; both readings are legitimate inputs to these functions,
and the pinned vector (TC-RPT-02) exercises the same five numbers as both).

Pinned vector (TC-RPT-02): capital base 10,000; five values +400, +20, −360,
+400, +20 => ROC 4.80%, annualized Sharpe 4.79 (mean daily return 0.96%,
sample stdev ddof=1 ~3.1793%, 0.30195 x sqrt(252) -> 4.79 @ 2dp), max
drawdown $360 (3.60% of base), profit factor 2.33 (840/360), expectancy
+$96/entry, day win rate 80%. Sharpe/Sortino gate "insufficient data" below
`report_min_sample_days` (default 20, D2); ROC always renders regardless of
sample size.

Everything here returns EXACT Decimals (or None for "insufficient data" / an
undefined ratio) — rounding to 2dp happens only at the presentation edge
(RPT-04), never inside these functions, so a caller needing a different
display precision is never fighting a pre-rounded value.
"""
from __future__ import annotations

import math
from decimal import Decimal

TRADING_DAYS_PER_YEAR = 252


def _decimal_sqrt(x: Decimal) -> Decimal:
    """No exact Decimal sqrt exists; converting through float and back is the
    standard approach and is exact enough at this precision (matches the
    pinned vector's stated 3.1793% / 4.79 to 2dp)."""
    return Decimal(math.sqrt(float(x)))


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / len(values)


def _sample_stdev(values: list[Decimal]) -> Decimal:
    """Sample standard deviation, ddof=1 (RPT-04 names it explicitly)."""
    n = len(values)
    if n < 2:
        return Decimal("0")
    m = _mean(values)
    variance = sum(((v - m) ** 2 for v in values), Decimal("0")) / (n - 1)
    return _decimal_sqrt(variance)


def roc(values: list[Decimal], base: Decimal) -> Decimal:
    """Return on capital = total net / capital base (RPT-04, D1)."""
    return sum(values, Decimal("0")) / base


def sharpe(values: list[Decimal], base: Decimal, *, rf_pct: Decimal = Decimal("0"),
           min_sample_days: int = 20) -> Decimal | None:
    """Annualized Sharpe = mean(daily_net/base - rf_daily) / stdev(ddof=1) * sqrt(252).
    `rf_pct` is the ANNUAL risk-free rate as a percentage (config.sharpe_risk_free_pct,
    e.g. Decimal("2") for 2%; default 0, D3). Gated below `min_sample_days`
    (config.report_min_sample_days, D2) -> None ("insufficient data")."""
    if len(values) < min_sample_days:
        return None
    rf_daily = (rf_pct / Decimal(100)) / Decimal(TRADING_DAYS_PER_YEAR)
    daily_returns = [v / base - rf_daily for v in values]
    sd = _sample_stdev(daily_returns)
    if sd == 0:
        return None  # ill-defined (e.g. every value identical) -- never divide by zero
    mean_r = _mean(daily_returns)
    return (mean_r / sd) * _decimal_sqrt(Decimal(TRADING_DAYS_PER_YEAR))


def sortino(values: list[Decimal], base: Decimal, *, rf_pct: Decimal = Decimal("0"),
            min_sample_days: int = 20) -> Decimal | None:
    """Annualized Sortino: same numerator as Sharpe, denominator is downside
    deviation (only below-target returns contribute, all `n` in the
    denominator — the standard Sortino convention). Gated identically to
    Sharpe (doc 10: "Sharpe/Sortino gate below report_min_sample_days")."""
    if len(values) < min_sample_days:
        return None
    n = len(values)
    rf_daily = (rf_pct / Decimal(100)) / Decimal(TRADING_DAYS_PER_YEAR)
    daily_returns = [v / base - rf_daily for v in values]
    downside_sq = sum((min(Decimal("0"), r) ** 2 for r in daily_returns), Decimal("0"))
    downside_dev = _decimal_sqrt(downside_sq / n)
    if downside_dev == 0:
        return None  # no downside at all (or ill-defined) -- never divide by zero
    mean_r = _mean(daily_returns)
    return (mean_r / downside_dev) * _decimal_sqrt(Decimal(TRADING_DAYS_PER_YEAR))


def max_drawdown(values: list[Decimal], base: Decimal) -> tuple[Decimal, Decimal]:
    """Peak-to-trough drawdown of the CUMULATIVE net curve (RPT-04): ($, % of
    base). Both are exact and non-negative; ($0, 0%) for an all-nondecreasing
    curve."""
    cumulative = Decimal("0")
    peak = Decimal("0")
    worst = Decimal("0")
    for v in values:
        cumulative += v
        if cumulative > peak:
            peak = cumulative
        drawdown = peak - cumulative
        if drawdown > worst:
            worst = drawdown
    return worst, (worst / base)


def profit_factor(values: list[Decimal]) -> Decimal | None:
    """Gross wins / gross losses (absolute). None when there are no losing
    periods (undefined ratio — never a fabricated infinity)."""
    wins = sum((v for v in values if v > 0), Decimal("0"))
    losses = -sum((v for v in values if v < 0), Decimal("0"))
    if losses == 0:
        return None
    return wins / losses


def expectancy(values: list[Decimal]) -> Decimal | None:
    """Mean net per period (RPT-04's "expectancy per filled entry" when the
    caller passes per-entry pnls). None for an empty sequence."""
    if not values:
        return None
    return _mean(values)


def day_win_rate(values: list[Decimal]) -> Decimal | None:
    """Exact fraction of periods with a strictly positive net."""
    if not values:
        return None
    return Decimal(sum(1 for v in values if v > 0)) / len(values)


def avg_win(values: list[Decimal]) -> Decimal | None:
    wins = [v for v in values if v > 0]
    return _mean(wins) if wins else None


def avg_loss(values: list[Decimal]) -> Decimal | None:
    losses = [v for v in values if v < 0]
    return _mean(losses) if losses else None


def longest_losing_streak(values: list[Decimal]) -> int:
    """Longest run of consecutive strictly-negative periods, IN ORDER."""
    longest = current = 0
    for v in values:
        if v < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest
