"""Deterministic market calculations (blueprint §7).

Hard rule: every number that feeds a decision is computed here, in code,
from venue data — never estimated by an LLM.

This module currently covers the snapshot-level metrics (§7.1, §7.2, plus
funding normalization and the liquidity raw inputs). The hold-period funding
projections, tracking-error aggregates, and 0-100 scores (§7.3–§7.9) belong
to the scoring engine (build step 3) and will live alongside these.

All math uses Decimal: venue APIs return prices as strings, and Decimal
round-trips cleanly into PostgreSQL numeric columns.
"""

from decimal import Decimal

MINUTES_PER_YEAR = Decimal(365 * 24 * 60)


def premium_pct(mark_price: Decimal, oracle_price: Decimal) -> Decimal:
    """§7.1: (mark - oracle) / oracle.

    Positive → longs likely pay funding; negative → shorts likely pay.
    """
    if oracle_price == 0:
        raise ValueError("oracle_price must be non-zero")
    return (mark_price - oracle_price) / oracle_price


def funding_payment(
    position_size: Decimal, oracle_price: Decimal, funding_rate_interval: Decimal
) -> Decimal:
    """§7.2: single-interval funding payment.

    Hyperliquid converts size to notional using the ORACLE price, not mark.
    """
    return position_size * oracle_price * funding_rate_interval


def funding_rate_8h_equiv(
    funding_rate_interval: Decimal, funding_interval_minutes: int
) -> Decimal:
    """Normalize a per-interval funding rate to its 8-hour equivalent
    (simple scaling; Hyperliquid hourly rate × 8)."""
    if funding_interval_minutes <= 0:
        raise ValueError("funding_interval_minutes must be positive")
    return funding_rate_interval * Decimal(480) / Decimal(funding_interval_minutes)


def funding_apr_est(
    funding_rate_interval: Decimal, funding_interval_minutes: int
) -> Decimal:
    """Annualized funding rate — context only, never a decision number (§7.2)."""
    if funding_interval_minutes <= 0:
        raise ValueError("funding_interval_minutes must be positive")
    return (
        funding_rate_interval * MINUTES_PER_YEAR / Decimal(funding_interval_minutes)
    )


def open_interest_usd(open_interest: Decimal, mark_price: Decimal) -> Decimal:
    """§5.5: openInterest (base units) × price. Mark price is used for the
    USD notional of standing OI; funding math uses oracle (§7.2)."""
    return open_interest * mark_price


def spread_bps_est(
    impact_bid_price: Decimal, impact_ask_price: Decimal, mid_price: Decimal
) -> Decimal:
    """Estimated spread in bps from Hyperliquid impact prices."""
    if mid_price == 0:
        raise ValueError("mid_price must be non-zero")
    return Decimal(10000) * (impact_ask_price - impact_bid_price) / mid_price
