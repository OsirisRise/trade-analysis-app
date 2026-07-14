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

from onchain_console import calcs

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
    venue_type: str | None    # 'CEX' | 'DEX' | None (tokenized spot)
    tradeable: bool
    price_basis: str          # 'snapshot' | 'live' | 'none'
    mark_price: Decimal | None
    gap: Decimal | None       # vs. reference spot; None when no price yet
    funding_rate_8h: Decimal | None  # normalized so venues are comparable
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


def load_latest_spots(conn: psycopg.Connection) -> list[tuple]:
    """(commodity_code, price, unit, source, as_of) — latest per commodity."""
    return conn.execute(
        """
        SELECT DISTINCT ON (commodity_code)
               commodity_code, price, unit, source, as_of
        FROM spot_prices
        ORDER BY commodity_code, as_of DESC
        """
    ).fetchall()


def load_active_instruments(conn: psycopg.Connection) -> list[tuple]:
    """Every active perp with its latest snapshot mark/funding when one
    exists (Hyperliquid has snapshots; BydFi is quoted live at check time;
    Ostium has no data source until step 8 — surfaces as 'none')."""
    return conn.execute(
        """
        SELECT i.underlying, i.venue, i.symbol, i.venue_type, i.tradeable,
               i.funding_interval_minutes,
               snap.mark_price, snap.funding_rate_interval
        FROM instruments i
        LEFT JOIN LATERAL (
            SELECT s.mark_price, s.funding_rate_interval
            FROM market_snapshots s
            WHERE s.instrument_id = i.id
            ORDER BY s.captured_at DESC
            LIMIT 1
        ) snap ON true
        WHERE i.status = 'active' AND i.instrument_type = 'perp'
        ORDER BY i.underlying, i.venue, i.symbol
        """
    ).fetchall()


def build_cross_venue_reports(
    spots: list[tuple],
    instruments: list[tuple],
    live_quotes: dict | None = None,
    now: datetime | None = None,
    metals_threshold: Decimal = METALS_FLAG_THRESHOLD,
    energy_threshold: Decimal = ENERGY_FLAG_THRESHOLD,
) -> list[DiscrepancyReport]:
    """Cross-venue comparison per commodity: every active perp on every
    venue, its gap to the reference spot, and funding normalized to an
    8h-equivalent rate so Hyperliquid (1h) and BydFi (4h) are comparable.

    live_quotes maps venue symbol -> object with mark_price, funding_rate,
    funding_interval_minutes attributes (BydfiContract), used for venues
    quoted live at check time instead of from stored snapshots.
    """
    now = now or datetime.now(timezone.utc)
    live_quotes = live_quotes or {}

    reports: dict[str, DiscrepancyReport] = {}
    for commodity, price, unit, source, as_of in spots:
        reports[commodity] = DiscrepancyReport(
            commodity_code=commodity,
            category=categorize(commodity),
            spot_price=price,
            spot_unit=unit,
            spot_source=source,
            spot_as_of=as_of,
            spot_age_hours=spot_age_hours(as_of, now),
        )

    for (underlying, venue, symbol, venue_type, tradeable,
         interval_minutes, snap_mark, snap_funding) in instruments:
        report = reports.get(underlying)
        if report is None:
            continue  # instrument's commodity has no reference spot yet

        mark = funding = None
        basis = "none"
        if snap_mark is not None:
            mark, funding, basis = snap_mark, snap_funding, "snapshot"
        quote = live_quotes.get(symbol)
        if quote is not None and quote.mark_price is not None:
            mark, funding, basis = quote.mark_price, quote.funding_rate, "live"
            interval_minutes = quote.funding_interval_minutes or interval_minutes

        gap = pct_gap(mark, report.spot_price) if mark is not None else None
        funding_8h = (
            calcs.funding_rate_8h_equiv(funding, interval_minutes)
            if funding is not None and interval_minutes
            else None
        )
        report.instruments.append(
            InstrumentGap(
                venue=venue,
                symbol=symbol,
                venue_type=venue_type,
                tradeable=tradeable,
                price_basis=basis,
                mark_price=mark,
                gap=gap,
                funding_rate_8h=funding_8h,
                flagged=(
                    should_flag(gap, report.category,
                                metals_threshold, energy_threshold)
                    if gap is not None else False
                ),
            )
        )

    return [r for r in reports.values() if r.instruments]
