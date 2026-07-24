"""Integration test: lookback-window history reads against the real local
database (blueprint §7.3–§7.7 support layer, src/onchain_console/history.py).

Same pattern as test_liquidity_profiles_integration.py — real Postgres inside
a transaction that is ALWAYS rolled back, skipped (not failed) when Postgres
is unavailable, since the rest of the suite is DB-free.

Fixtures insert their OWN instrument inside the rolled-back transaction rather
than reusing a seeded one: the seeded Hyperliquid rows carry real production
snapshots, which would mix into the windows under test and make coverage
assertions non-deterministic.

Timestamps are written as `now() - interval`. Postgres freezes now() to the
transaction start time, so every offset below is exact and every span is a
hand-checkable constant, not an approximation.
"""

import json
from decimal import Decimal
from uuid import uuid4

import pytest

from onchain_console.history import (
    HistorySeries,
    instrument_history_series,
    underlying_spot_series,
)

HOURS_PER_DAY = Decimal(24)


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
def instrument_id(conn):
    """A throwaway instrument with zero snapshots — the BydFi shape."""
    return conn.execute(
        """
        INSERT INTO instruments
            (venue, instrument_type, symbol, underlying, quote_asset,
             funding_interval_minutes, supports_open_interest,
             supports_funding, supports_oracle_price, status)
        VALUES ('TestVenue', 'perp', %s, 'gold', 'USD', 60, true, true, true,
                'active')
        RETURNING id
        """,
        (f"TEST:HISTORY:{uuid4()}",),
    ).fetchone()[0]


def _insert_snapshot(conn, instrument_id, hours_ago, mark_price, funding=None):
    conn.execute(
        """
        INSERT INTO market_snapshots
            (instrument_id, captured_at, mark_price, funding_rate_interval,
             raw_payload)
        VALUES (%s, now() - make_interval(hours => %s), %s, %s, %s)
        """,
        (instrument_id, hours_ago, mark_price, funding, json.dumps({"test": True})),
    )


def _insert_spot(conn, commodity_code, hours_ago, price):
    conn.execute(
        """
        INSERT INTO spot_prices
            (commodity_code, price, unit, source, as_of, raw_payload)
        VALUES (%s, %s, 'usd_per_toz', 'test-source',
                now() - make_interval(hours => %s), %s)
        """,
        (commodity_code, price, hours_ago, json.dumps({"test": True})),
    )


class TestInstrumentHistorySeries:
    def test_full_window(self, conn, instrument_id):
        # Five snapshots spanning 166h -> 1h ago, all inside a 7-day window.
        for hours_ago, price in [
            (166, "4000"), (120, "4010"), (72, "4020"), (24, "4030"), (1, "4040"),
        ]:
            _insert_snapshot(conn, instrument_id, hours_ago, price)

        series = instrument_history_series(
            conn, instrument_id, "mark_price", window_days=7
        )

        assert series.values == [
            Decimal(v) for v in ("4000", "4010", "4020", "4030", "4040")
        ]
        assert series.earliest_captured_at < series.latest_captured_at
        # span = 166h - 1h = 165h = 6.875 days (exact: now() is frozen)
        assert series.days_covered == Decimal(165) / HOURS_PER_DAY
        assert series.days_covered == Decimal("6.875")

    def test_partial_window(self, conn, instrument_id):
        # Two days of history against a 7-day window — the real shape of
        # this project's production history today.
        _insert_snapshot(conn, instrument_id, 48, "4000")
        _insert_snapshot(conn, instrument_id, 1, "4100")

        series = instrument_history_series(
            conn, instrument_id, "mark_price", window_days=7
        )

        assert series.values == [Decimal("4000"), Decimal("4100")]
        assert series.days_covered == Decimal(47) / HOURS_PER_DAY  # 1.958...
        assert series.days_covered < Decimal(2)  # NOT "2 days" — span, not count

    def test_zero_rows_returns_empty_series(self, conn, instrument_id):
        # BydFi-shaped: an instrument with literally no market_snapshots rows.
        series = instrument_history_series(
            conn, instrument_id, "funding_rate_interval", window_days=7
        )

        assert isinstance(series, HistorySeries)
        assert series.values == []
        assert series.is_empty
        assert len(series) == 0
        assert series.earliest_captured_at is None
        assert series.latest_captured_at is None
        assert series.days_covered == Decimal(0)

    def test_zero_rows_for_the_real_bydfi_instruments(self, conn):
        """The live BydFi case, not a synthetic stand-in: every seeded BydFi
        instrument must read cleanly, and the series must agree with an
        independent COUNT (so this keeps passing once BydFi has history)."""
        rows = conn.execute(
            "SELECT id FROM instruments WHERE venue = 'BydFi' ORDER BY symbol"
        ).fetchall()
        if not rows:
            pytest.skip("BydFi seed instruments not present in local DB")

        for (bydfi_id,) in rows:
            series = instrument_history_series(
                conn, bydfi_id, "mark_price", window_days=30
            )
            actual = conn.execute(
                """
                SELECT count(*) FROM market_snapshots
                WHERE instrument_id = %s
                  AND captured_at >= now() - make_interval(days => 30)
                  AND mark_price IS NOT NULL
                """,
                (bydfi_id,),
            ).fetchone()[0]
            assert len(series) == actual
            if actual == 0:
                assert series.days_covered == Decimal(0)
                assert series.earliest_captured_at is None

    def test_window_smaller_than_available_history_truncates(
        self, conn, instrument_id
    ):
        # 10 days of history; a 3-day window must see only the last 3 days.
        for hours_ago, price in [
            (240, "3900"), (168, "3950"), (96, "3980"),
            (48, "4000"), (24, "4010"), (1, "4020"),
        ]:
            _insert_snapshot(conn, instrument_id, hours_ago, price)

        series = instrument_history_series(
            conn, instrument_id, "mark_price", window_days=3
        )

        # only the 48h, 24h and 1h rows are inside 72h
        assert series.values == [Decimal("4000"), Decimal("4010"), Decimal("4020")]
        assert Decimal("3980") not in series.values  # the 96h row stayed out
        assert series.days_covered == Decimal(47) / HOURS_PER_DAY
        assert series.days_covered <= Decimal(3)

        # and the same data over a 30-day window keeps everything
        wide = instrument_history_series(
            conn, instrument_id, "mark_price", window_days=30
        )
        assert len(wide) == 6
        assert wide.days_covered == Decimal(239) / HOURS_PER_DAY

    def test_null_field_rows_are_not_observations(self, conn, instrument_id):
        # mark_price is present on all three; funding only on the outer two.
        _insert_snapshot(conn, instrument_id, 48, "4000", funding="0.0000125")
        _insert_snapshot(conn, instrument_id, 24, "4010", funding=None)
        _insert_snapshot(conn, instrument_id, 1, "4020", funding="0.0000130")

        marks = instrument_history_series(
            conn, instrument_id, "mark_price", window_days=7
        )
        funding = instrument_history_series(
            conn, instrument_id, "funding_rate_interval", window_days=7
        )

        assert len(marks) == 3
        assert funding.values == [Decimal("0.0000125"), Decimal("0.0000130")]
        assert funding.days_covered == Decimal(47) / HOURS_PER_DAY

    def test_only_the_requested_instrument_is_read(self, conn, instrument_id):
        other = conn.execute(
            """
            INSERT INTO instruments
                (venue, instrument_type, symbol, underlying, supports_open_interest,
                 supports_funding, supports_oracle_price)
            VALUES ('TestVenue', 'perp', %s, 'silver', true, true, true)
            RETURNING id
            """,
            (f"TEST:HISTORY:{uuid4()}",),
        ).fetchone()[0]
        _insert_snapshot(conn, instrument_id, 24, "4000")
        _insert_snapshot(conn, other, 24, "50")

        series = instrument_history_series(
            conn, instrument_id, "mark_price", window_days=7
        )
        assert series.values == [Decimal("4000")]

    def test_unknown_field_is_rejected(self, conn, instrument_id):
        with pytest.raises(ValueError, match="unknown market_snapshots series"):
            instrument_history_series(
                conn, instrument_id, "mark_price; DROP TABLE instruments", 7
            )

    def test_non_positive_window_rejected(self, conn, instrument_id):
        with pytest.raises(ValueError, match="window_days must be positive"):
            instrument_history_series(conn, instrument_id, "mark_price", 0)


class TestUnderlyingSpotSeries:
    def test_full_and_truncated_windows(self, conn):
        code = f"test_commodity_{uuid4().hex[:8]}"
        for hours_ago, price in [
            (166, "4000"), (96, "4020"), (48, "4040"), (1, "4060"),
        ]:
            _insert_spot(conn, code, hours_ago, price)

        week = underlying_spot_series(conn, code, window_days=7)
        assert week.field == "price"
        assert week.values == [
            Decimal(v) for v in ("4000", "4020", "4040", "4060")
        ]
        assert week.days_covered == Decimal(165) / HOURS_PER_DAY

        # a 3-day window drops the 166h and 96h observations
        short = underlying_spot_series(conn, code, window_days=3)
        assert short.values == [Decimal("4040"), Decimal("4060")]
        assert short.days_covered == Decimal(47) / HOURS_PER_DAY

    def test_unknown_commodity_returns_empty_series(self, conn):
        series = underlying_spot_series(conn, "no_such_commodity", window_days=30)
        assert series.values == []
        assert series.days_covered == Decimal(0)
        assert series.earliest_captured_at is None

    def test_reads_real_seeded_spot_history(self, conn):
        """Against real spot_prices rows: the series must match an
        independent COUNT and never exceed the requested window."""
        row = conn.execute(
            """
            SELECT commodity_code, count(*)
            FROM spot_prices
            WHERE as_of >= now() - make_interval(days => 30)
            GROUP BY commodity_code
            ORDER BY count(*) DESC, commodity_code
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            pytest.skip("no spot_prices rows in the last 30 days")
        code, expected = row

        series = underlying_spot_series(conn, code, window_days=30)
        assert len(series) == expected
        assert series.days_covered <= Decimal(30)

    def test_non_positive_window_rejected(self, conn):
        with pytest.raises(ValueError, match="window_days must be positive"):
            underlying_spot_series(conn, "gold", -1)
