"""Integration test: §7.4 tracking error over real DB history
(scoring.compute_tracking_error).

Same pattern as the other DB tests — real local Postgres inside a
transaction that is ALWAYS rolled back, throwaway instruments created
inside it, now() frozen at transaction start so every span is exact.

Three things this file exists to prove:
  1. the underlying-level spot fallback genuinely rescues an instrument
     whose own reference_spot_price coverage is thin (the BydFi-gold case);
  2. the CLAUDE.md 2026-07-12 energy-vs-metals branch fires correctly;
  3. zero coverage returns an explicit None result, never a number.
"""

import json
from decimal import Decimal
from uuid import uuid4

import pytest

from onchain_console.scoring import compute_tracking_error
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
    def _make(underlying="gold", venue="TestVenue"):
        instrument_id = conn.execute(
            """
            INSERT INTO instruments
                (venue, instrument_type, symbol, underlying, quote_asset,
                 funding_interval_minutes, supports_open_interest,
                 supports_funding, supports_oracle_price, status)
            VALUES (%s, 'perp', %s, %s, 'USD', 60, true, true, true, 'active')
            RETURNING id
            """,
            (venue, f"TEST:TRACK:{uuid4()}", underlying),
        ).fetchone()[0]
        return Instrument(instrument_id, venue, "TEST", underlying, 60)

    return _make


def _insert_mark(conn, instrument, hours_ago, mark_price, reference_spot=None):
    conn.execute(
        """
        INSERT INTO market_snapshots
            (instrument_id, captured_at, mark_price, reference_spot_price,
             raw_payload)
        VALUES (%s, now() - make_interval(hours => %s), %s, %s, %s)
        """,
        (instrument.id, hours_ago, mark_price, reference_spot,
         json.dumps({"test": True})),
    )


@pytest.fixture
def spot(conn):
    """Insert a spot_prices row, isolating that commodity's series first.

    The local DB holds REAL captured spot history for the production
    commodity codes (gold, wti_crude_oil, ...). A test asserting an exact
    pairing or an exact bps value must not silently pair against those
    rows — so the first write for a given code clears that commodity's
    existing rows. Rolled back with everything else, so the real history
    is untouched outside this transaction.
    """
    isolated: set[str] = set()

    def _insert(commodity_code, hours_ago, price):
        if commodity_code not in isolated:
            conn.execute(
                "DELETE FROM spot_prices WHERE commodity_code = %s",
                (commodity_code,),
            )
            isolated.add(commodity_code)
        conn.execute(
            """
            INSERT INTO spot_prices
                (commodity_code, price, unit, source, as_of, raw_payload)
            VALUES (%s, %s, 'usd_per_toz', %s,
                    now() - make_interval(hours => %s), %s)
            """,
            (commodity_code, price, f"test-{uuid4().hex[:8]}", hours_ago,
             json.dumps({"test": True})),
        )

    return _insert


class TestUnderlyingFallback:
    """(a) The shared underlying spot series must rescue an instrument whose
    own reference_spot_price coverage is thin — a newly listed contract
    inherits its commodity's history instead of bootstrapping its own."""

    def test_new_contract_with_no_own_reference_uses_underlying_history(
        self, conn, make_instrument, spot
    ):
        # a brand-new gold contract: 25 days of marks, reference_spot_price
        # never populated (exactly what the snapshot service writes today)
        instrument = make_instrument(underlying="gold")
        for hours_ago in (600, 480, 360, 240, 120, 1):
            _insert_mark(conn, instrument, hours_ago, "4000")
        # gold's shared spot series already goes back 25 days
        for hours_ago in (601, 300, 2):
            spot("gold", hours_ago, "4000")

        result = compute_tracking_error(conn, instrument, hold_days=14)

        assert result["spot_source"] == "underlying_spot"
        assert result["observations"] == 6
        # span 599h = 24.958d of a 30d window -> 0.832 -> 'high'
        assert result["days_covered"] == Decimal(599) / HOURS_PER_DAY
        assert result["confidence_tier"] == "high"
        assert result["reason"] is None

    def test_without_the_fallback_the_same_contract_says_nothing(
        self, conn, make_instrument
    ):
        """Control for the test above: identical mark history, but an
        underlying with no shared spot series. This is the confidence the
        fallback is buying."""
        orphan_code = f"test_no_spot_{uuid4().hex[:8]}"
        instrument = make_instrument(underlying=orphan_code)
        for hours_ago in (600, 480, 360, 240, 120, 1):
            _insert_mark(conn, instrument, hours_ago, "4000")

        result = compute_tracking_error(conn, instrument, hold_days=14)

        assert result["reason"] == "no_spot_history"
        assert result["tracking_error_bps"] is None
        assert result["avg_abs_7d"] is None
        assert result["max_abs_30d"] is None
        assert result["confidence_tier"] is None

    def test_instrument_own_reference_wins_when_it_covers_more(
        self, conn, make_instrument, spot
    ):
        """When the instrument HAS its own reference_spot_price stamped on
        each row, that is preferred: same-row alignment means zero lag."""
        instrument = make_instrument(underlying="gold")
        for hours_ago in (600, 300, 1):
            _insert_mark(conn, instrument, hours_ago, "4040", reference_spot="4000")
        # a thinner shared series must not displace it
        spot("gold", 2, "3000")

        result = compute_tracking_error(conn, instrument, hold_days=14)

        assert result["spot_source"] == "instrument_reference"
        assert result["max_spot_lag_days"] == Decimal(0)  # same-row alignment
        # 10000 x (4040 - 4000) / 4000 = 100 bps, not the 3000-based number
        assert result["tracking_error_bps"] == Decimal("100")
        assert result["avg_abs_7d"] == Decimal("100")
        assert result["max_abs_30d"] == Decimal("100")

    def test_fallback_pairs_are_as_of_not_nearest(self, conn, make_instrument, spot):
        """The fallback still obeys the no-lookahead rule: a mark keeps the
        older spot even when a newer one lands minutes later."""
        instrument = make_instrument(underlying="gold")
        _insert_mark(conn, instrument, 100, "4000")
        _insert_mark(conn, instrument, 50, "4000")
        spot("gold", 101, "4000")
        spot("gold", 49, "8000")  # just AFTER the last mark

        result = compute_tracking_error(conn, instrument, hold_days=14)

        # both marks priced off the 4000 spot -> 0 bps, never the 8000 one
        assert result["tracking_error_bps"] == Decimal("0")
        assert result["max_abs_30d"] == Decimal("0")


class TestEnergyVsMetalsBranch:
    """(b) CLAUDE.md 2026-07-12 — the §7.4 confidence logic MUST branch on
    the energy/metals categorization."""

    def _build(self, conn, make_instrument, spot, underlying):
        instrument = make_instrument(underlying=underlying)
        for hours_ago in (600, 480, 360, 240, 120, 1):
            _insert_mark(conn, instrument, hours_ago, "4000")
        for hours_ago in (601, 300, 2):
            spot(underlying, hours_ago, "4000")
        return compute_tracking_error(conn, instrument, hold_days=14)

    def test_gold_keeps_its_coverage_tier(self, conn, make_instrument, spot):
        result = self._build(conn, make_instrument, spot, "gold")

        assert result["basis_category"] == "metal"
        assert result["has_structural_basis_gap"] is False
        assert result["confidence_tier_pre_basis"] == "high"
        assert result["confidence_tier"] == "high"  # no downgrade

    @pytest.mark.parametrize(
        "underlying", ["wti_crude_oil", "brent_crude_oil", "natural_gas"]
    )
    def test_energy_is_downgraded_one_step(self, conn, make_instrument, spot, underlying):
        result = self._build(conn, make_instrument, spot, underlying)

        assert result["basis_category"] == "energy"
        assert result["has_structural_basis_gap"] is True
        # identical history to the gold case, one tier lower
        assert result["confidence_tier_pre_basis"] == "high"
        assert result["confidence_tier"] == "medium"

    def test_energy_and_metal_differ_only_by_the_branch(self, conn, make_instrument, spot):
        """Same shaped history, same coverage — the ONLY difference in the
        verdict comes from the commodity categorization."""
        gold = self._build(conn, make_instrument, spot, "gold")
        wti = self._build(conn, make_instrument, spot, "wti_crude_oil")

        assert gold["days_covered"] == wti["days_covered"]
        assert gold["confidence_tier_pre_basis"] == wti["confidence_tier_pre_basis"]
        assert gold["confidence_tier"] != wti["confidence_tier"]
        assert gold["has_structural_basis_gap"] is False
        assert wti["has_structural_basis_gap"] is True

    def test_uncategorized_underlying_is_treated_conservatively(
        self, conn, make_instrument, spot
    ):
        code = f"test_unknown_{uuid4().hex[:8]}"
        instrument = make_instrument(underlying=code)
        for hours_ago in (600, 300, 1):
            _insert_mark(conn, instrument, hours_ago, "4000")
        for hours_ago in (601, 2):
            spot(code, hours_ago, "4000")

        result = compute_tracking_error(conn, instrument, hold_days=14)

        assert result["basis_category"] == "unknown"
        assert result["has_structural_basis_gap"] is True
        assert result["confidence_tier"] == "medium"  # downgraded from high

    def test_energy_staleness_stays_visible(self, conn, make_instrument, spot):
        """EIA publishes T-2..T-6. The pairing is correct but stale, and the
        staleness must be reported rather than absorbed."""
        instrument = make_instrument(underlying="wti_crude_oil")
        for hours_ago in (600, 300, 1):
            _insert_mark(conn, instrument, hours_ago, "70.5")
        # spot last published 5 days before the newest mark
        spot("wti_crude_oil", 601, "69.0")
        spot("wti_crude_oil", 121, "69.0")

        result = compute_tracking_error(conn, instrument, hold_days=14)

        assert result["max_spot_lag_days"] == Decimal(601 - 300) / HOURS_PER_DAY
        assert result["max_spot_lag_days"] > Decimal(5)
        # and the gap it produces is flagged, not presented as clean
        assert result["has_structural_basis_gap"] is True
        assert result["tracking_error_bps"] is not None


class TestZeroCoverage:
    """(c) BydFi-shaped: nothing to compute, and it says so."""

    def test_no_marks_at_all(self, conn, make_instrument):
        instrument = make_instrument(underlying="gold")

        result = compute_tracking_error(conn, instrument, hold_days=14)

        assert result["reason"] == "no_mark_history"
        assert result["tracking_error_bps"] is None
        assert result["avg_abs_7d"] is None
        assert result["max_abs_30d"] is None
        assert result["confidence_tier"] is None
        assert result["observations"] == 0
        assert result["days_covered"] == Decimal(0)
        # the categorization is still reported — it does not depend on data
        assert result["basis_category"] == "metal"

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
            result = compute_tracking_error(conn, Instrument(*row), hold_days=14)
            if result["observations"] == 0:
                assert result["tracking_error_bps"] is None
                assert result["confidence_tier"] is None
                assert result["reason"] is not None

    def test_marks_entirely_before_the_first_spot_report_no_overlap(
        self, conn, make_instrument, spot
    ):
        code = f"test_gap_{uuid4().hex[:8]}"
        instrument = make_instrument(underlying=code)
        for hours_ago in (600, 500):
            _insert_mark(conn, instrument, hours_ago, "4000")
        spot(code, 100, "4000")  # spot starts long after

        result = compute_tracking_error(conn, instrument, hold_days=14)

        assert result["reason"] == "no_overlap"
        assert result["tracking_error_bps"] is None
        assert result["observations"] == 0

    def test_single_pair_reports_current_but_no_window_aggregates(
        self, conn, make_instrument, spot
    ):
        """A point-in-time comparison is honestly computable from one pair;
        the two window aggregates are not."""
        instrument = make_instrument(underlying="gold")
        _insert_mark(conn, instrument, 5, "4100")
        spot("gold", 6, "4000")

        result = compute_tracking_error(conn, instrument, hold_days=14)

        assert result["observations"] == 1
        assert result["days_covered"] == Decimal(0)
        assert result["reason"] == "zero_span"
        assert result["tracking_error_bps"] == Decimal("250")  # measured
        assert result["avg_abs_7d"] is None  # not supportable
        assert result["max_abs_30d"] is None


class TestWindowSlicing:
    def test_old_spike_lifts_the_30d_max_but_not_the_7d_avg(
        self, conn, make_instrument, spot
    ):
        instrument = make_instrument(underlying="gold")
        spot("gold", 700, "4000")
        _insert_mark(conn, instrument, 600, "4400")  # +1000 bps, 25 days ago
        _insert_mark(conn, instrument, 100, "4020")  # +50 bps, ~4 days ago
        _insert_mark(conn, instrument, 2, "4040")  # +100 bps, recent

        result = compute_tracking_error(conn, instrument, hold_days=14)

        assert result["observations"] == 3
        assert result["observations_7d"] == 2
        assert result["max_abs_30d"] == Decimal("1000")
        assert result["avg_abs_7d"] == Decimal("75")  # (50 + 100) / 2

    def test_hold_days_does_not_alter_the_fixed_windows(self, conn, make_instrument, spot):
        instrument = make_instrument(underlying="gold")
        spot("gold", 700, "4000")
        for hours_ago in (600, 300, 1):
            _insert_mark(conn, instrument, hours_ago, "4040")

        short = compute_tracking_error(conn, instrument, hold_days=3)
        long = compute_tracking_error(conn, instrument, hold_days=45)

        assert short["window_days"] == long["window_days"] == 30
        assert short["max_abs_30d"] == long["max_abs_30d"]
        assert short["hold_days"] == 3 and long["hold_days"] == 45

    def test_non_positive_hold_days_rejected(self, conn, make_instrument):
        with pytest.raises(ValueError, match="hold_days must be positive"):
            compute_tracking_error(conn, make_instrument(), hold_days=0)
