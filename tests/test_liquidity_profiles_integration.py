"""Integration test: Hyperliquid- and BydFi-sourced liquidity_profiles rows
composed in one table, distinguishable by provenance when queried together —
the composition the eventual liquidity score (build step 3) will depend on.

Uses the real local database inside a transaction that is ALWAYS rolled
back (no committed residue), with real seeded instrument ids to satisfy the
FK and the same real fixtures the unit tests use. Skips (not fails) when
Postgres or the seeds are unavailable, since every other test in the suite
is DB-free.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from onchain_console.bydfi import build_risk_tier_profile_rows, parse_risk_limits
from onchain_console.snapshot_service import build_liquidity_profile_rows

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURED_AT = datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    psycopg = pytest.importorskip("psycopg")
    from onchain_console.db import get_connection

    try:
        connection = get_connection()
    except psycopg.OperationalError as exc:
        pytest.skip(f"local Postgres unavailable: {exc}")
    try:
        yield connection
    finally:
        connection.rollback()  # never commit test rows
        connection.close()


@pytest.fixture
def instrument_ids(conn):
    rows = dict(
        conn.execute(
            """
            SELECT symbol, id FROM instruments
            WHERE (venue, symbol) IN (('Hyperliquid', 'xyz:GOLD'),
                                      ('BydFi', 'XAU-USDT'))
            """
        ).fetchall()
    )
    if set(rows) != {"xyz:GOLD", "XAU-USDT"}:
        pytest.skip("seed instruments not present in local DB")
    return rows


def _insert(conn, row):
    conn.execute(
        """
        INSERT INTO liquidity_profiles
            (instrument_id, captured_at, profile_type, provenance, payload)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (row["instrument_id"], row["captured_at"], row["profile_type"],
         row["provenance"], json.dumps(row["payload"])),
    )


def test_cross_venue_rows_compose_and_split_by_provenance(conn, instrument_ids):
    l2_book = json.loads((FIXTURES / "hl_l2book_gold_sample.json").read_text())
    limits = parse_risk_limits(
        json.loads((FIXTURES / "bydfi_risk_limits_sample.json").read_text())
    )

    # prompt-2 constructor (Hyperliquid, real resting orders)
    (hl_row,) = build_liquidity_profile_rows(
        instrument_ids["xyz:GOLD"], CAPTURED_AT, l2_book, None
    )
    # prompt-3 constructor (BydFi, venue risk configuration)
    (bydfi_row,) = build_risk_tier_profile_rows(
        {"XAU-USDT": instrument_ids["XAU-USDT"]}, limits, CAPTURED_AT
    )

    _insert(conn, hl_row)
    _insert(conn, bydfi_row)

    fetched = conn.execute(
        """
        SELECT i.venue, lp.profile_type, lp.provenance, lp.payload
        FROM liquidity_profiles lp
        JOIN instruments i ON i.id = lp.instrument_id
        WHERE lp.captured_at = %s
        ORDER BY lp.provenance
        """,
        (CAPTURED_AT,),
    ).fetchall()

    assert len(fetched) == 2
    (hl_venue, hl_type, hl_prov, hl_payload), \
        (by_venue, by_type, by_prov, by_payload) = fetched

    # the two sources stay distinguishable when read together
    assert (hl_venue, hl_type, hl_prov) == \
        ("Hyperliquid", "order_book", "real_resting_orders")
    assert (by_venue, by_type, by_prov) == \
        ("BydFi", "risk_tiers", "venue_risk_config")
    assert hl_prov != by_prov

    # payloads round-trip through jsonb intact
    assert hl_payload["coin"] == "xyz:GOLD"
    assert len(hl_payload["levels"][0]) == 20
    assert len(by_payload) == 20
    assert all(t["s"] == "XAU-USDT" for t in by_payload)

    # and a provenance-filtered read (how the liquidity score will select
    # real depth) returns exactly the real-resting-orders row
    real_only = conn.execute(
        """
        SELECT count(*) FROM liquidity_profiles
        WHERE captured_at = %s AND provenance = 'real_resting_orders'
        """,
        (CAPTURED_AT,),
    ).fetchone()[0]
    assert real_only == 1
