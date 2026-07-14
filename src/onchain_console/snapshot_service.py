"""M5 — Market Snapshot Service (Hyperliquid).

Reads active Hyperliquid perp instruments from the DB, fetches one
metaAndAssetCtxs payload per dex, computes the deterministic snapshot
metrics (calcs.py), and writes rows to market_snapshots.

tracking_error_bps / liquidity_score / reference_spot_price stay NULL here:
they are scoring-engine outputs (build step 3) that need history and an
external reference series.

Analysis-only: this module reads public market data. It never places
orders, signs, or touches keys.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import psycopg

from onchain_console import calcs
from onchain_console.hyperliquid import (
    AssetCtx,
    dex_of_symbol,
    fetch_l2_book,
    fetch_meta_and_asset_ctxs,
    margin_table_id_by_symbol,
    margin_tables_by_id,
    parse_asset_ctxs,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Instrument:
    id: UUID
    venue: str
    symbol: str
    underlying: str
    funding_interval_minutes: int | None


@dataclass(frozen=True)
class SnapshotRow:
    instrument_id: UUID
    symbol: str  # not stored; for logging/dry-run display
    captured_at: datetime
    mark_price: Decimal
    mid_price: Decimal | None
    oracle_price: Decimal | None
    reference_spot_price: Decimal | None
    premium_pct: Decimal | None
    funding_rate_interval: Decimal | None
    funding_rate_8h_equiv: Decimal | None
    funding_apr_est: Decimal | None
    open_interest_usd: Decimal | None
    day_volume_usd: Decimal | None
    impact_bid_price: Decimal | None
    impact_ask_price: Decimal | None
    spread_bps_est: Decimal | None
    raw_payload: dict


def load_active_hyperliquid_perps(conn: psycopg.Connection) -> list[Instrument]:
    # PAXG guard: the instrument_type = 'perp' filter must stay. The
    # Ethereum/tokenized_spot PAXG and XAUT rows are priced via CoinGecko
    # (build step 8) because their §7.7 role is the funding-drag-free gold
    # expression — never source them from Hyperliquid's PAXG-USDC perp,
    # which is a different, funded instrument.
    rows = conn.execute(
        """
        SELECT id, venue, symbol, underlying, funding_interval_minutes
        FROM instruments
        WHERE venue = 'Hyperliquid'
          AND instrument_type = 'perp'
          AND status = 'active'
        ORDER BY symbol
        """
    ).fetchall()
    return [Instrument(*row) for row in rows]


def build_snapshot_row(
    instrument: Instrument,
    ctx: AssetCtx,
    captured_at: datetime,
    reference_spot_price: Decimal | None = None,
    l2_book: dict | None = None,
    margin_table: dict | None = None,
) -> SnapshotRow:
    prem = (
        calcs.premium_pct(ctx.mark_price, ctx.oracle_price)
        if ctx.oracle_price
        else None
    )
    interval_min = instrument.funding_interval_minutes
    f8h = apr = None
    if ctx.funding_rate_interval is not None and interval_min:
        f8h = calcs.funding_rate_8h_equiv(ctx.funding_rate_interval, interval_min)
        apr = calcs.funding_apr_est(ctx.funding_rate_interval, interval_min)
    oi_usd = (
        calcs.open_interest_usd(ctx.open_interest, ctx.mark_price)
        if ctx.open_interest is not None
        else None
    )
    spread = None
    if ctx.impact_bid_price and ctx.impact_ask_price and ctx.mid_price:
        spread = calcs.spread_bps_est(
            ctx.impact_bid_price, ctx.impact_ask_price, ctx.mid_price
        )
    # Task 10 (2026-07-13): l2Book (real resting-order depth) and the
    # asset's margin table ride along inside raw_payload as namespaced keys
    # — raw capture only, nothing downstream reads them yet. Plain ctx keys
    # stay at the top level so existing readers are unaffected.
    raw_payload = dict(ctx.raw)
    if l2_book is not None:
        raw_payload["_l2_book"] = l2_book
    if margin_table is not None:
        raw_payload["_margin_table"] = margin_table

    return SnapshotRow(
        instrument_id=instrument.id,
        symbol=instrument.symbol,
        captured_at=captured_at,
        mark_price=ctx.mark_price,
        mid_price=ctx.mid_price,
        oracle_price=ctx.oracle_price,
        reference_spot_price=reference_spot_price,
        premium_pct=prem,
        funding_rate_interval=ctx.funding_rate_interval,
        funding_rate_8h_equiv=f8h,
        funding_apr_est=apr,
        open_interest_usd=oi_usd,
        day_volume_usd=ctx.day_volume_usd,
        impact_bid_price=ctx.impact_bid_price,
        impact_ask_price=ctx.impact_ask_price,
        spread_bps_est=spread,
        raw_payload=raw_payload,
    )


def collect_snapshots(
    instruments: list[Instrument],
    captured_at: datetime | None = None,
    fetch=fetch_meta_and_asset_ctxs,
    spot_by_underlying: dict | None = None,
    fetch_l2=None,
) -> list[SnapshotRow]:
    """One metaAndAssetCtxs call per distinct dex (rate-limit friendly),
    then map each instrument symbol to its asset context.

    spot_by_underlying carries the latest known reference spot per commodity
    (spot_prices ledger, 0008); rows copy it so §7.4 tracking math has a
    same-row reference. fetch_l2 (e.g. fetch_l2_book) adds one l2Book call
    per instrument; a failed depth fetch degrades to None rather than
    sinking the whole snapshot run."""
    captured_at = captured_at or datetime.now(timezone.utc)
    spot_by_underlying = spot_by_underlying or {}
    dexes = sorted({dex_of_symbol(i.symbol) for i in instruments})
    ctx_by_symbol: dict[str, AssetCtx] = {}
    margin_tables: dict[int, dict] = {}
    table_id_by_symbol: dict[str, int | None] = {}
    for dex in dexes:
        meta, asset_ctxs = fetch(dex)
        ctx_by_symbol.update(parse_asset_ctxs(meta, asset_ctxs))
        margin_tables.update(margin_tables_by_id(meta))
        table_id_by_symbol.update(margin_table_id_by_symbol(meta))

    rows: list[SnapshotRow] = []
    for instrument in instruments:
        ctx = ctx_by_symbol.get(instrument.symbol)
        if ctx is None:
            log.warning(
                "symbol %s not found in Hyperliquid universe — skipped",
                instrument.symbol,
            )
            continue
        l2_book = None
        if fetch_l2 is not None:
            try:
                l2_book = fetch_l2(instrument.symbol)
            except Exception:
                log.warning("l2Book fetch failed for %s", instrument.symbol)
        table_id = table_id_by_symbol.get(instrument.symbol)
        margin_table = None
        if table_id is not None:
            # ids without an explicit marginTables entry are simple
            # single-tier tables; store the reference either way.
            margin_table = {"id": table_id, "table": margin_tables.get(table_id)}
        rows.append(
            build_snapshot_row(
                instrument,
                ctx,
                captured_at,
                reference_spot_price=spot_by_underlying.get(instrument.underlying),
                l2_book=l2_book,
                margin_table=margin_table,
            )
        )
    return rows


def insert_snapshots(conn: psycopg.Connection, rows: list[SnapshotRow]) -> int:
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO market_snapshots (
                    instrument_id, captured_at, mark_price, mid_price,
                    oracle_price, reference_spot_price, premium_pct,
                    funding_rate_interval, funding_rate_8h_equiv,
                    funding_apr_est, open_interest_usd, day_volume_usd,
                    impact_bid_price, impact_ask_price, spread_bps_est,
                    raw_payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s)
                """,
                (
                    r.instrument_id, r.captured_at, r.mark_price, r.mid_price,
                    r.oracle_price, r.reference_spot_price, r.premium_pct,
                    r.funding_rate_interval,
                    r.funding_rate_8h_equiv, r.funding_apr_est,
                    r.open_interest_usd, r.day_volume_usd, r.impact_bid_price,
                    r.impact_ask_price, r.spread_bps_est,
                    json.dumps(r.raw_payload),
                ),
            )
    conn.commit()
    return len(rows)


def run(conn: psycopg.Connection, dry_run: bool = False) -> list[SnapshotRow]:
    # Local import: spot_service also imports latest-spot reading; keep the
    # modules import-cycle-free.
    from onchain_console.spot_service import latest_spot_by_commodity

    instruments = load_active_hyperliquid_perps(conn)
    if not instruments:
        log.warning("no active Hyperliquid perp instruments seeded")
        return []
    rows = collect_snapshots(
        instruments,
        spot_by_underlying=latest_spot_by_commodity(conn),
        fetch_l2=fetch_l2_book,
    )
    if dry_run:
        log.info("dry run — %d rows built, nothing written", len(rows))
    else:
        n = insert_snapshots(conn, rows)
        log.info("inserted %d market_snapshots rows", n)
    return rows
