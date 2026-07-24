"""Integration test: §7.6 carry score over real DB history
(scoring.compute_carry_score).

Same pattern as test_scoring_integration.py (§7.3) — real local Postgres
inside a transaction that is ALWAYS rolled back, throwaway instruments
created inside it, now() frozen at transaction start so every span is exact
and every expected score is hand-derived.
"""

import json
from decimal import Decimal
from uuid import uuid4

import pytest

from onchain_console import calcs
from onchain_console.scoring import compute_carry_score
from onchain_console.snapshot_service import Instrument

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
def make_instrument(conn):
    def _make(funding_interval_minutes=60, venue="TestVenue"):
        instrument_id = conn.execute(
            """
            INSERT INTO instruments
                (venue, instrument_type, symbol, underlying, quote_asset,
                 funding_interval_minutes, supports_open_interest,
                 supports_funding, supports_oracle_price, status)
            VALUES (%s, 'perp', %s, 'gold', 'USD', %s, true, true, true, 'active')
            RETURNING id
            """,
            (venue, f"TEST:CARRY:{uuid4()}", funding_interval_minutes),
        ).fetchone()[0]
        return Instrument(
            id=instrument_id,
            venue=venue,
            symbol="TEST",
            underlying="gold",
            funding_interval_minutes=funding_interval_minutes,
        )

    return _make


def _insert_funding(conn, instrument, hours_ago, funding_rate):
    conn.execute(
        """
        INSERT INTO market_snapshots
            (instrument_id, captured_at, mark_price, funding_rate_interval,
             raw_payload)
        VALUES (%s, now() - make_interval(hours => %s), 4000, %s, %s)
        """,
        (instrument.id, hours_ago, funding_rate, json.dumps({"test": True})),
    )


class TestFullHistory:
    def test_stable_positive_funding_scores_short_high_long_low(
        self, conn, make_instrument
    ):
        # steady funding at the reference rate over ~6.9 days
        instrument = make_instrument()
        for hours_ago in (166, 120, 72, 24, 1):
            _insert_funding(conn, instrument, hours_ago, calcs.CARRY_REFERENCE_RATE)

        short = compute_carry_score(conn, instrument, "short")
        long = compute_carry_score(conn, instrument, "long")

        assert short["carry_score"] == Decimal("100")  # short is paid, steadily
        assert long["carry_score"] == Decimal("0")  # long pays, steadily
        assert short["stability_penalty"] == Decimal("0")
        assert short["direction_penalty"] == Decimal("0")
        assert short["confidence_tier"] == "high"  # 6.875/7 days
        assert short["observations"] == 5
        assert short["reason"] is None

    def test_volatile_funding_is_discounted_toward_neutral(
        self, conn, make_instrument
    ):
        # same short-favorable mean as a steady case, but maximally jumpy
        instrument = make_instrument()
        for hours_ago, rate in zip(
            (166, 120, 72, 24, 1),
            ("0.0001", "0", "0.0001", "0", "0.0001"),
        ):
            _insert_funding(conn, instrument, hours_ago, rate)

        result = compute_carry_score(conn, instrument, "short")

        # mean 0.00006 favors a short, but the swings pull it back
        assert result["mean_funding_rate"] == Decimal("0.00006")
        assert result["stability_penalty"] > Decimal("0")
        assert result["carry_score"] < Decimal("100")
        assert result["carry_score"] >= Decimal("50")  # still short-favorable

    def test_direction_flips_are_penalized(self, conn, make_instrument):
        instrument = make_instrument()
        for hours_ago, rate in zip(
            (166, 120, 72, 24, 1),
            ("0.00006", "-0.00002", "0.00006", "-0.00002", "0.00006"),
        ):
            _insert_funding(conn, instrument, hours_ago, rate)

        result = compute_carry_score(conn, instrument, "short")

        assert result["direction_penalty"] > Decimal("0")
        # mean is short-favorable but the flip + swing discount it
        assert Decimal("50") <= result["carry_score"] < Decimal("100")

    def test_long_and_short_mirror_about_50(self, conn, make_instrument):
        instrument = make_instrument()
        for hours_ago, rate in zip(
            (166, 120, 72, 24, 1),
            ("0.00004", "0.00003", "0.00002", "0.00003", "0.00004"),
        ):
            _insert_funding(conn, instrument, hours_ago, rate)

        long = compute_carry_score(conn, instrument, "long")
        short = compute_carry_score(conn, instrument, "short")
        assert long["carry_score"] + short["carry_score"] == Decimal("100")

    def test_matches_the_pure_function_on_the_same_values(
        self, conn, make_instrument
    ):
        instrument = make_instrument()
        rates = ["0.00004", "0.00002", "0.00005", "0.00001", "0.00003"]
        for hours_ago, rate in zip((166, 120, 72, 24, 1), rates):
            _insert_funding(conn, instrument, hours_ago, rate)

        result = compute_carry_score(conn, instrument, "short")
        expected = calcs.carry_score([Decimal(r) for r in rates], "short")
        assert result["carry_score"] == expected


class TestThinHistory:
    def test_two_days_still_scores_but_low_confidence(self, conn, make_instrument):
        instrument = make_instrument()
        _insert_funding(conn, instrument, 48, "0.00003")
        _insert_funding(conn, instrument, 1, "0.00003")

        result = compute_carry_score(conn, instrument, "short")

        # span 47h = 1.958d -> 0.28 of the window -> 'low'
        assert result["confidence_tier"] == "low"
        assert result["carry_score"] is not None
        assert result["observations"] == 2


class TestZeroHistory:
    def test_no_snapshots_returns_explicit_none(self, conn, make_instrument):
        instrument = make_instrument()

        result = compute_carry_score(conn, instrument, "long")

        assert result["carry_score"] is None
        assert result["confidence_tier"] is None
        assert result["reason"] == "no_history"
        assert result["observations"] == 0
        assert result["mean_funding_rate"] is None
        assert result["direction"] == "long"

    def test_single_observation_is_insufficient(self, conn, make_instrument):
        # one point spans zero days: stability unassessable -> zero_span
        instrument = make_instrument()
        _insert_funding(conn, instrument, 3, "0.00003")

        result = compute_carry_score(conn, instrument, "short")

        assert result["observations"] == 1
        assert result["days_covered"] == Decimal(0)
        assert result["carry_score"] is None
        assert result["reason"] == "zero_span"

    def test_stalled_capture_is_no_history_not_stale(self, conn, make_instrument):
        instrument = make_instrument()
        for hours_ago in (240, 216, 192):  # all older than the 7-day window
            _insert_funding(conn, instrument, hours_ago, "0.00009")

        result = compute_carry_score(conn, instrument, "short")

        assert result["reason"] == "no_history"
        assert result["carry_score"] is None

    def test_null_funding_interval_is_not_scored(self, conn):
        # PAXG/XAUT: no funding mechanism -> explicit, not a fabricated carry
        row = conn.execute(
            """
            SELECT id, venue, symbol, underlying, funding_interval_minutes
            FROM instruments WHERE funding_interval_minutes IS NULL LIMIT 1
            """
        ).fetchone()
        if row is None:
            pytest.skip("no instrument with a NULL funding interval seeded")

        result = compute_carry_score(conn, Instrument(*row), "long")

        assert result["reason"] == "no_funding_interval"
        assert result["carry_score"] is None

    def test_every_real_bydfi_instrument_reads_cleanly(self, conn):
        rows = conn.execute(
            """
            SELECT id, venue, symbol, underlying, funding_interval_minutes
            FROM instruments WHERE venue = 'BydFi' ORDER BY symbol
            """
        ).fetchall()
        if not rows:
            pytest.skip("BydFi seed instruments not present in local DB")

        for row in rows:
            for direction in ("long", "short"):
                result = compute_carry_score(conn, Instrument(*row), direction)
                if result["observations"] == 0:
                    assert result["carry_score"] is None
                    assert result["confidence_tier"] is None
                    assert result["reason"] in ("no_history", "no_funding_interval")


class TestVenueCadence:
    def test_bydfi_cadence_is_accepted(self, conn, make_instrument):
        # a 4h-cadence instrument still scores; the per-interval caveat is
        # documented, but the code path must not crash or special-case
        bydfi = make_instrument(funding_interval_minutes=240)
        for hours_ago in (166, 84, 1):
            _insert_funding(conn, bydfi, hours_ago, "0.00003")

        result = compute_carry_score(conn, bydfi, "short")
        assert result["carry_score"] is not None
        assert result["confidence_tier"] == "high"


class TestGuards:
    def test_invalid_direction_rejected(self, conn, make_instrument):
        with pytest.raises(ValueError, match="direction must be"):
            compute_carry_score(conn, make_instrument(), "avoid")

    def test_result_shape_identical_on_every_path(self, conn, make_instrument):
        empty = make_instrument()
        populated = make_instrument()
        for hours_ago in (100, 1):
            _insert_funding(conn, populated, hours_ago, "0.00003")

        a = compute_carry_score(conn, empty, "long")
        b = compute_carry_score(conn, populated, "long")
        assert a.keys() == b.keys()
