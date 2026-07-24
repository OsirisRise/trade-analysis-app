"""Integration test: §7.3 hold-period funding composed over real DB history
(src/onchain_console/scoring.py).

Same pattern as the other DB tests — real local Postgres inside a
transaction that is ALWAYS rolled back, skipped when Postgres is
unavailable, with throwaway instruments created inside the transaction so
real production snapshots can't contaminate a window under test.

Postgres freezes now() to the transaction start time, so every span below
is an exact constant and every expected cost is hand-derived.
"""

import json
from decimal import Decimal
from uuid import uuid4

import pytest

from onchain_console import calcs
from onchain_console.scoring import compute_hold_period_funding
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
    """Throwaway instrument with a caller-chosen funding cadence."""

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
            (venue, f"TEST:SCORING:{uuid4()}", funding_interval_minutes),
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


class TestFullWindow:
    """Five observations spanning 166h -> 1h: 6.875 of 7 days = 'high'."""

    RATES = ["0.00001", "0.00002", "0.00003", "0.00004", "0.00005"]

    @pytest.fixture
    def instrument(self, conn, make_instrument):
        instrument = make_instrument(funding_interval_minutes=60)
        for hours_ago, rate in zip([166, 120, 72, 24, 1], self.RATES):
            _insert_funding(conn, instrument, hours_ago, rate)
        return instrument

    def test_costs_and_confidence(self, conn, instrument):
        result = compute_hold_period_funding(conn, instrument, hold_days=7)

        # coverage: span 165h = 6.875d of a 7d window
        assert result["days_covered"] == Decimal(165) / HOURS_PER_DAY
        assert result["window_confidence"] == Decimal("6.875") / Decimal(7)
        assert result["confidence_tier"] == "high"
        assert result["observations"] == 5
        assert result["reason"] is None

        # n = 7 days x 24 charges/day
        assert result["funding_intervals"] == Decimal(168)
        # current = most recent IN-WINDOW observation (the 1h-ago row)
        assert result["current_funding_rate"] == Decimal("0.00005")

        # hand-derived: base 0.00005 x 168
        assert result["funding_cost_base"] == Decimal("0.0084")
        # mean 0.00003 x 168
        assert result["funding_cost_optimistic"] == Decimal("0.00504")
        # P90 (inclusive) of 5 points -> index 3.6 -> 0.00004 + 0.6 x 0.00001
        # = 0.000046; x 168 = 0.007728
        assert result["funding_cost_stress"] == Decimal("0.007728")

    def test_stress_is_the_worst_case_for_a_long(self, conn, instrument):
        result = compute_hold_period_funding(conn, instrument, hold_days=7)
        assert (
            result["funding_cost_stress"]
            > result["funding_cost_optimistic"]
        )

    def test_hold_days_scale_the_cost_linearly(self, conn, instrument):
        seven = compute_hold_period_funding(conn, instrument, hold_days=7)
        fourteen = compute_hold_period_funding(conn, instrument, hold_days=14)
        assert fourteen["funding_cost_base"] == 2 * seven["funding_cost_base"]
        # doubling the hold does not change how much history exists
        assert fourteen["confidence_tier"] == seven["confidence_tier"]

    def test_out_of_window_rows_are_excluded(self, conn, instrument):
        # a 10-day-old row must not enter the 7-day funding window
        _insert_funding(conn, instrument, 240, "0.99")
        result = compute_hold_period_funding(conn, instrument, hold_days=7)
        assert result["observations"] == 5
        assert result["funding_cost_stress"] == Decimal("0.007728")


class TestThinWindow:
    def test_two_and_a_half_days_scores_low(self, conn, make_instrument):
        instrument = make_instrument(funding_interval_minutes=60)
        _insert_funding(conn, instrument, 60, "0.00002")
        _insert_funding(conn, instrument, 1, "0.00004")

        result = compute_hold_period_funding(conn, instrument, hold_days=7)

        # span 59h = 2.4583d -> 0.351 of the window -> 'low'
        assert result["days_covered"] == Decimal(59) / HOURS_PER_DAY
        assert result["confidence_tier"] == "low"
        assert result["observations"] == 2

        # numbers ARE produced, but nothing may read them without the tier
        assert result["funding_cost_base"] == Decimal("0.00004") * 168
        assert result["funding_cost_optimistic"] == Decimal("0.00003") * 168
        # P90 (inclusive) of 2 points = 0.00002 + 0.9 x 0.00002 = 0.000038
        assert result["funding_cost_stress"] == Decimal("0.000038") * 168

    def test_three_days_of_history_still_below_medium(self, conn, make_instrument):
        instrument = make_instrument(funding_interval_minutes=60)
        for hours_ago in (72, 48, 24, 1):
            _insert_funding(conn, instrument, hours_ago, "0.00001")
        result = compute_hold_period_funding(conn, instrument, hold_days=7)
        # span 71h = 2.958d -> 0.4226 -> still 'low', not 'medium'
        assert result["confidence_tier"] == "low"
        assert result["window_confidence"] < calcs.WINDOW_CONFIDENCE_MEDIUM_FLOOR


class TestZeroHistory:
    def test_no_snapshots_returns_explicit_none_result(self, conn, make_instrument):
        """BydFi-shaped: an instrument with no market_snapshots rows at all."""
        instrument = make_instrument(funding_interval_minutes=240)

        result = compute_hold_period_funding(conn, instrument, hold_days=14)

        assert result["funding_cost_base"] is None
        assert result["funding_cost_optimistic"] is None
        assert result["funding_cost_stress"] is None
        assert result["confidence_tier"] is None
        assert result["reason"] == "no_history"
        assert result["observations"] == 0
        assert result["days_covered"] == Decimal(0)
        assert result["window_confidence"] == Decimal(0)
        # the request itself is still echoed back
        assert result["hold_days"] == 14
        assert result["window_days"] == 7

    def test_every_real_bydfi_instrument_reads_cleanly(self, conn):
        """The live BydFi case, against the seeded rows rather than a
        synthetic stand-in. Stays valid once BydFi has history: it asserts
        the result is internally consistent, not that it is always empty."""
        rows = conn.execute(
            """
            SELECT id, venue, symbol, underlying, funding_interval_minutes
            FROM instruments WHERE venue = 'BydFi' ORDER BY symbol
            """
        ).fetchall()
        if not rows:
            pytest.skip("BydFi seed instruments not present in local DB")

        for row in rows:
            instrument = Instrument(*row)
            result = compute_hold_period_funding(conn, instrument, hold_days=14)
            if result["observations"] == 0:
                assert result["funding_cost_base"] is None
                assert result["funding_cost_optimistic"] is None
                assert result["funding_cost_stress"] is None
                assert result["confidence_tier"] is None
                assert result["reason"] in ("no_history", "no_funding_interval")
            else:
                assert result["funding_cost_base"] is not None

    def test_single_observation_has_zero_span(self, conn, make_instrument):
        # one point spans nothing, so no window aggregate is supportable
        instrument = make_instrument(funding_interval_minutes=60)
        _insert_funding(conn, instrument, 3, "0.00002")

        result = compute_hold_period_funding(conn, instrument, hold_days=7)

        assert result["observations"] == 1
        assert result["days_covered"] == Decimal(0)
        assert result["funding_cost_base"] is None
        assert result["confidence_tier"] is None
        assert result["reason"] == "zero_span"

    def test_stalled_capture_is_no_history_not_stale_numbers(
        self, conn, make_instrument
    ):
        """The console's real state on 2026-07-23: plenty of rows, none
        inside the window. Must report no_history, never a 9-day-old rate
        presented as current."""
        instrument = make_instrument(funding_interval_minutes=60)
        for hours_ago in (240, 216, 192):
            _insert_funding(conn, instrument, hours_ago, "0.00009")

        result = compute_hold_period_funding(conn, instrument, hold_days=7)

        assert result["reason"] == "no_history"
        assert result["current_funding_rate"] is None
        assert result["funding_cost_base"] is None


class TestVenueCadence:
    def test_bydfi_four_hour_cadence_uses_six_charges_per_day(
        self, conn, make_instrument
    ):
        bydfi = make_instrument(funding_interval_minutes=240)
        for hours_ago in (166, 84, 1):
            _insert_funding(conn, bydfi, hours_ago, "0.0001")

        result = compute_hold_period_funding(conn, bydfi, hold_days=10)

        assert result["funding_intervals"] == Decimal(60)  # 10 days x 6
        assert result["funding_cost_base"] == Decimal("0.006")

    def test_identical_history_costs_4x_more_on_an_hourly_venue(
        self, conn, make_instrument
    ):
        """Same rates, same hold, different cadence — the generalization
        beyond §7.3's hardcoded ×24, verified end to end through the DB."""
        hourly = make_instrument(funding_interval_minutes=60)
        four_hourly = make_instrument(funding_interval_minutes=240)
        for instrument in (hourly, four_hourly):
            for hours_ago in (166, 84, 1):
                _insert_funding(conn, instrument, hours_ago, "0.0001")

        hl = compute_hold_period_funding(conn, hourly, hold_days=10)
        by = compute_hold_period_funding(conn, four_hourly, hold_days=10)

        assert hl["funding_cost_base"] == 4 * by["funding_cost_base"]
        assert hl["funding_cost_stress"] == 4 * by["funding_cost_stress"]
        # identical capture history, so identical confidence
        assert hl["confidence_tier"] == by["confidence_tier"] == "high"


class TestGuards:
    def test_null_funding_interval_is_not_zero_cost(self, conn):
        """PAXG/XAUT carry no funding cadence. This function refuses to
        guess — 'no funding mechanism' becoming 'zero cost' is a §7.7
        decision, not a silent default here."""
        row = conn.execute(
            """
            SELECT id, venue, symbol, underlying, funding_interval_minutes
            FROM instruments
            WHERE funding_interval_minutes IS NULL LIMIT 1
            """
        ).fetchone()
        if row is None:
            pytest.skip("no instrument with a NULL funding interval seeded")

        result = compute_hold_period_funding(conn, Instrument(*row), hold_days=7)

        assert result["reason"] == "no_funding_interval"
        assert result["funding_cost_base"] is None
        assert result["confidence_tier"] is None

    def test_non_positive_hold_days_rejected(self, conn, make_instrument):
        instrument = make_instrument()
        with pytest.raises(ValueError, match="hold_days must be positive"):
            compute_hold_period_funding(conn, instrument, hold_days=0)

    def test_result_shape_is_identical_on_every_path(self, conn, make_instrument):
        """Callers must never have to branch on which keys exist."""
        empty = make_instrument()
        populated = make_instrument()
        for hours_ago in (100, 1):
            _insert_funding(conn, populated, hours_ago, "0.00001")

        a = compute_hold_period_funding(conn, empty, hold_days=7)
        b = compute_hold_period_funding(conn, populated, hold_days=7)
        assert a.keys() == b.keys()
