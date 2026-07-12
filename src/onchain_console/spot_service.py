"""Daily reference-spot refresh service.

Writes per-commodity spot prices to the spot_prices ledger (0008), then
stamps market_snapshots.reference_spot_price on the most recent snapshot
row of every active instrument whose underlying matches. New snapshot rows
also copy the latest known spot at insert time (snapshot_service.run).

Cadence: daily — matches the blueprint's tokenized/macro cadence and stays
well inside Metals.Dev's 100 req/month free quota.

Analysis-only: reads public market data; no execution capability.
"""

import json
import logging

import psycopg

from onchain_console.config import EIA_API_KEY, METALS_DEV_API_KEY
from onchain_console.spot_prices import (
    EIA_SERIES,
    SpotPrice,
    fetch_eia_route,
    fetch_metals_dev,
    parse_eia,
    parse_metals_dev,
)

log = logging.getLogger(__name__)


def collect_spot_prices(
    metals_dev_api_key: str = METALS_DEV_API_KEY,
    eia_api_key: str = EIA_API_KEY,
) -> list[SpotPrice]:
    spots: list[SpotPrice] = []

    if metals_dev_api_key:
        spots += parse_metals_dev(fetch_metals_dev(metals_dev_api_key))
    else:
        log.warning("METALS_DEV_API_KEY not set — skipping metals spot prices")

    if eia_api_key:
        routes: dict[str, list[str]] = {}
        for series, (route, *_rest) in EIA_SERIES.items():
            routes.setdefault(route, []).append(series)
        for route, series_ids in routes.items():
            spots += parse_eia(fetch_eia_route(eia_api_key, route, series_ids))
    else:
        log.warning("EIA_API_KEY not set — skipping energy spot prices")

    return spots


def insert_spot_prices(conn: psycopg.Connection, spots: list[SpotPrice]) -> int:
    """Idempotent: reruns on the same observation are no-ops via the
    (commodity_code, source, as_of) unique constraint."""
    inserted = 0
    with conn.cursor() as cur:
        for s in spots:
            cur.execute(
                """
                INSERT INTO spot_prices
                    (commodity_code, price, unit, source, as_of, raw_payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (commodity_code, source, as_of) DO NOTHING
                """,
                (s.commodity_code, s.price, s.unit, s.source, s.as_of,
                 json.dumps(s.raw)),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted


def latest_spot_by_commodity(conn: psycopg.Connection) -> dict:
    """Most recent as_of per commodity -> Decimal price. Used both here for
    stamping and by snapshot_service at insert time."""
    rows = conn.execute(
        """
        SELECT DISTINCT ON (commodity_code) commodity_code, price
        FROM spot_prices
        ORDER BY commodity_code, as_of DESC
        """
    ).fetchall()
    return {code: price for code, price in rows}


def stamp_latest_snapshots(conn: psycopg.Connection) -> int:
    """Set reference_spot_price on the most recent market_snapshots row of
    each active instrument whose underlying has a known spot price."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH latest_spot AS (
                SELECT DISTINCT ON (commodity_code) commodity_code, price
                FROM spot_prices
                ORDER BY commodity_code, as_of DESC
            ),
            latest_snap AS (
                SELECT DISTINCT ON (s.instrument_id) s.id, i.underlying
                FROM market_snapshots s
                JOIN instruments i ON i.id = s.instrument_id
                WHERE i.status = 'active'
                ORDER BY s.instrument_id, s.captured_at DESC
            )
            UPDATE market_snapshots m
            SET reference_spot_price = ls.price
            FROM latest_snap sn
            JOIN latest_spot ls ON ls.commodity_code = sn.underlying
            WHERE m.id = sn.id
            """
        )
        stamped = cur.rowcount
    conn.commit()
    return stamped


def run(conn: psycopg.Connection, dry_run: bool = False) -> list[SpotPrice]:
    spots = collect_spot_prices()
    if dry_run:
        log.info("dry run — %d spot prices fetched, nothing written", len(spots))
        return spots
    inserted = insert_spot_prices(conn, spots)
    stamped = stamp_latest_snapshots(conn)
    log.info(
        "spot refresh: %d fetched, %d new ledger rows, %d snapshot rows stamped",
        len(spots), inserted, stamped,
    )
    return spots
