"""Daily spot-vs-perp discrepancy diagnostic (deterministic math).

Standalone monitoring, NOT the §7.4 tracking_error_bps calculation (build
step 3) — writes nothing to trade_candidates or theses; it only reads the
spot_prices ledger and latest market_snapshots and reports gaps.

Categorization (CLAUDE.md "Design decisions", 2026-07-12): energy references
(EIA) are physical spot with a structural basis gap to the futures the perps
track, plus a T-2..T-6 publication lag — large gaps are EXPECTED there.
Metals references (Metals.Dev) are near-real-time spot pricing the same
thing the metal perps track — a large gap on a metal is genuinely
suspicious.

Default flag thresholds (approved by Caleb 2026-07-12, adjustable via CLI):
  metals 1%  — beyond bad-data / real-dislocation territory for a
               near-real-time reference that should track within bps.
  energy 10% — routine staleness+basis runs 5-8%; 10% stays quiet on the
               expected gap but still catches rally-driven blowouts
               (e.g. Brent +13.4% observed 2026-07-12).

No futures feed exists yet: report rows carry an explicit
"futures: not yet available" placeholder so a real feed can slot in later
without restructuring — never a fabricated number.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import psycopg

ENERGY_COMMODITIES = frozenset({"wti_crude_oil", "brent_crude_oil", "natural_gas"})

METALS_FLAG_THRESHOLD = Decimal("0.01")   # 1%
ENERGY_FLAG_THRESHOLD = Decimal("0.10")   # 10%

FUTURES_NOT_AVAILABLE = "not yet available"


def categorize(commodity_code: str) -> str:
    """'energy' (known spot-vs-futures basis gap, gaps expected) or
    'metal' (reference tracks the perp's pricing; gaps are suspicious)."""
    return "energy" if commodity_code in ENERGY_COMMODITIES else "metal"


def pct_gap(mark_price: Decimal, spot_price: Decimal) -> Decimal:
    """(mark - spot) / spot. Positive → perp above reference."""
    if spot_price <= 0:
        raise ValueError("spot_price must be positive")
    return (mark_price - spot_price) / spot_price


def spot_age_hours(as_of: datetime, now: datetime) -> Decimal:
    """Age of the spot observation in hours (Decimal, deterministic)."""
    if now < as_of:
        raise ValueError("now must not precede as_of")
    return Decimal(int((now - as_of).total_seconds())) / Decimal(3600)


def should_flag(
    gap: Decimal,
    category: str,
    metals_threshold: Decimal = METALS_FLAG_THRESHOLD,
    energy_threshold: Decimal = ENERGY_FLAG_THRESHOLD,
) -> bool:
    threshold = energy_threshold if category == "energy" else metals_threshold
    return abs(gap) > threshold


@dataclass(frozen=True)
class InstrumentGap:
    venue: str
    symbol: str
    mark_price: Decimal
    gap: Decimal
    flagged: bool


@dataclass(frozen=True)
class DiscrepancyReport:
    commodity_code: str
    category: str  # 'energy' | 'metal'
    spot_price: Decimal
    spot_unit: str
    spot_source: str
    spot_as_of: datetime
    spot_age_hours: Decimal
    futures_price: str = FUTURES_NOT_AVAILABLE  # placeholder until a feed exists
    instruments: list[InstrumentGap] = field(default_factory=list)


def load_latest_pairs(conn: psycopg.Connection) -> list[tuple]:
    """(commodity, spot price/unit/source/as_of, venue, symbol, mark) for
    every commodity that has both a spot entry and an active instrument
    with at least one snapshot."""
    return conn.execute(
        """
        WITH latest_spot AS (
            SELECT DISTINCT ON (commodity_code)
                   commodity_code, price, unit, source, as_of
            FROM spot_prices
            ORDER BY commodity_code, as_of DESC
        ),
        latest_snap AS (
            SELECT DISTINCT ON (s.instrument_id)
                   i.underlying, i.venue, i.symbol, s.mark_price
            FROM market_snapshots s
            JOIN instruments i ON i.id = s.instrument_id
            WHERE i.status = 'active'
            ORDER BY s.instrument_id, s.captured_at DESC
        )
        SELECT ls.commodity_code, ls.price, ls.unit, ls.source, ls.as_of,
               sn.venue, sn.symbol, sn.mark_price
        FROM latest_spot ls
        JOIN latest_snap sn ON sn.underlying = ls.commodity_code
        ORDER BY ls.commodity_code, sn.venue, sn.symbol
        """
    ).fetchall()


def build_reports(
    pairs: list[tuple],
    now: datetime | None = None,
    metals_threshold: Decimal = METALS_FLAG_THRESHOLD,
    energy_threshold: Decimal = ENERGY_FLAG_THRESHOLD,
) -> list[DiscrepancyReport]:
    now = now or datetime.now(timezone.utc)
    by_commodity: dict[str, DiscrepancyReport] = {}
    for commodity, price, unit, source, as_of, venue, symbol, mark in pairs:
        if commodity not in by_commodity:
            by_commodity[commodity] = DiscrepancyReport(
                commodity_code=commodity,
                category=categorize(commodity),
                spot_price=price,
                spot_unit=unit,
                spot_source=source,
                spot_as_of=as_of,
                spot_age_hours=spot_age_hours(as_of, now),
            )
        report = by_commodity[commodity]
        gap = pct_gap(mark, price)
        report.instruments.append(
            InstrumentGap(
                venue=venue,
                symbol=symbol,
                mark_price=mark,
                gap=gap,
                flagged=should_flag(gap, report.category,
                                    metals_threshold, energy_threshold),
            )
        )
    return list(by_commodity.values())
