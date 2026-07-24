"""Integration test: §7.7 instrument-fit composite over real DB
(scoring.compute_instrument_fit), composing the §7.4/§7.5/§7.6 orchestrations.

Same pattern as the other DB tests — real local Postgres inside a
transaction that is ALWAYS rolled back, throwaway instruments created inside
it, now() frozen so spans are exact.
"""

import json
from decimal import Decimal
from uuid import uuid4

import pytest

from onchain_console import calcs
from onchain_console.scoring import compute_instrument_fit
from onchain_console.snapshot_service import Instrument


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
    def _make(underlying="gold", venue="Hyperliquid", venue_type="DEX",
              instrument_type="perp", funding_interval_minutes=60,
              tradeable=True):
        instrument_id = conn.execute(
            """
            INSERT INTO instruments
                (venue, venue_type, instrument_type, symbol, underlying,
                 quote_asset, funding_interval_minutes, supports_open_interest,
                 supports_funding, supports_oracle_price, status, tradeable)
            VALUES (%s, %s, %s, %s, %s, 'USD', %s, true, true, true, 'active', %s)
            RETURNING id
            """,
            (venue, venue_type, instrument_type, f"TEST:FIT:{uuid4()}",
             underlying, funding_interval_minutes, tradeable),
        ).fetchone()[0]
        return Instrument(instrument_id, venue, "TEST", underlying,
                          funding_interval_minutes)

    return _make


def _insert_snapshot(conn, instrument, hours_ago, *, mark, reference_spot=None,
                     funding=None, liquidity=False):
    cols = ["instrument_id", "captured_at", "mark_price", "raw_payload"]
    vals = ["%s", "now() - make_interval(hours => %s)", "%s", "%s"]
    args = [instrument.id, hours_ago, mark, json.dumps({"t": True})]

    def add(col, value):
        cols.insert(-1, col)
        vals.insert(-1, "%s")
        args.insert(-1, value)

    if reference_spot is not None:
        add("reference_spot_price", reference_spot)
    if funding is not None:
        add("funding_rate_interval", funding)
    if liquidity:
        add("mid_price", Decimal("100"))
        add("day_volume_usd", Decimal("1000000"))
        add("open_interest_usd", Decimal("2000000"))
        add("spread_bps_est", Decimal("5"))
        add("impact_bid_price", Decimal("99.95"))
        add("impact_ask_price", Decimal("100.05"))

    conn.execute(
        f"INSERT INTO market_snapshots ({', '.join(cols)}) VALUES ({', '.join(vals)})",
        args,
    )


def _add_profile(conn, instrument, profile_type, provenance):
    conn.execute(
        """
        INSERT INTO liquidity_profiles
            (instrument_id, captured_at, profile_type, provenance, payload)
        VALUES (%s, now(), %s, %s, %s)
        """,
        (instrument.id, profile_type, provenance, json.dumps({"t": True})),
    )


class TestWellBackedHyperliquid:
    """A gold DEX perp with full funding, mark/reference and liquidity
    history, plus real order-book depth — every §7.7 input present."""

    @pytest.fixture
    def instrument(self, conn, make_instrument):
        instrument = make_instrument(underlying="gold")
        # 8 rows spanning 600h -> 1h: >24d for tracking, and 5 inside 7d for
        # carry. Stable funding 0.00003; mark 4010 vs reference 4000 = 25 bps.
        for hours_ago in (600, 480, 300, 166, 120, 72, 24, 1):
            _insert_snapshot(
                conn, instrument, hours_ago,
                mark="4010", reference_spot="4000", funding="0.00003",
                liquidity=(hours_ago == 1),
            )
        _add_profile(conn, instrument, "order_book", "real_resting_orders")
        return instrument

    def test_all_five_inputs_present_and_high_confidence(self, conn, instrument):
        result = compute_instrument_fit(conn, instrument, "gold", "short")

        assert result["included_inputs"] == [
            "carry", "liquidity", "tracking", "underlying_match", "venue_quality"
        ]
        assert result["dropped_inputs"] == []
        assert result["reason"] is None
        assert result["confidence"] == "high"

    def test_component_values_are_hand_derivable(self, conn, instrument):
        result = compute_instrument_fit(conn, instrument, "gold", "short")
        c = result["components"]

        assert c["underlying_match"] == Decimal("100")  # gold == gold
        assert c["venue_quality"] == Decimal("100")  # DEX + real depth
        assert c["carry"] == Decimal("80")  # short, stable 0.00003 -> signal 0.6
        assert c["tracking"] == Decimal("87.5")  # 25 bps -> 100*(1-25/200)
        assert c["liquidity"] is not None  # value depends on universe

    def test_fit_equals_the_pure_weighted_sum_of_its_components(
        self, conn, instrument
    ):
        result = compute_instrument_fit(conn, instrument, "gold", "short")
        c = result["components"]
        assert result["instrument_fit"] == calcs.instrument_fit(
            c["underlying_match"], c["liquidity"], c["carry"],
            c["tracking"], c["venue_quality"],
        )

    def test_direction_flows_through_to_carry(self, conn, instrument):
        short = compute_instrument_fit(conn, instrument, "gold", "short")
        long = compute_instrument_fit(conn, instrument, "gold", "long")
        # carry mirrors about 50; long pays the 0.00003 funding -> 20
        assert short["components"]["carry"] == Decimal("80")
        assert long["components"]["carry"] == Decimal("20")

    def test_thesis_on_a_different_metal_lowers_underlying_match(
        self, conn, instrument
    ):
        # same gold instrument scored against a silver thesis -> 50, not 100
        result = compute_instrument_fit(conn, instrument, "silver", "short")
        assert result["components"]["underlying_match"] == Decimal("50")


class TestEnergyNeutralTracking:
    def test_energy_tracking_is_neutral_not_penalized(self, conn, make_instrument):
        instrument = make_instrument(underlying="wti_crude_oil")
        for hours_ago in (600, 300, 1):
            _insert_snapshot(
                conn, instrument, hours_ago,
                mark="80", reference_spot="70", funding="0.00003",
                liquidity=(hours_ago == 1),
            )

        result = compute_instrument_fit(conn, instrument, "wti_crude_oil", "short")

        # 10/70 would be ~1428 bps of BASIS, but energy tracking is neutralized
        assert result["components"]["tracking"] == Decimal("50")
        assert "tracking" in result["included_inputs"]


class TestBydfiMetadataOnly:
    """BydFi CEX perp with a risk-tier profile but no market_snapshots:
    liquidity/carry/tracking all insufficient -> a metadata-only prior."""

    def test_returns_a_flagged_metadata_only_score(self, conn, make_instrument):
        instrument = make_instrument(
            underlying="gold", venue="BydFi", venue_type="CEX",
            funding_interval_minutes=240,
        )
        _add_profile(conn, instrument, "risk_tiers", "venue_risk_config")

        result = compute_instrument_fit(conn, instrument, "gold", "short")

        assert result["reason"] == "metadata_only"
        assert result["confidence"] is None
        assert result["included_inputs"] == ["underlying_match", "venue_quality"]
        assert sorted(result["dropped_inputs"]) == ["carry", "liquidity", "tracking"]

        # a score is still returned so the instrument stays rankable
        # um 100, vq = CEX(70)*0.45 + risk_config(65)*0.55 = 67.25
        # fit = (0.30*100 + 0.10*67.25) / 0.40 = 91.8125
        assert result["components"]["venue_quality"] == Decimal("67.25")
        assert result["instrument_fit"] == Decimal("91.8125")

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
            instrument = Instrument(*row)
            result = compute_instrument_fit(
                conn, instrument, instrument.underlying, "short"
            )
            # no market data today -> metadata-only, but always a score
            assert result["instrument_fit"] is not None
            if not result["included_inputs"] or result["included_inputs"] == [
                "underlying_match", "venue_quality"
            ]:
                assert result["reason"] == "metadata_only"
                assert result["confidence"] is None


class TestPartialMarketData:
    def test_liquidity_only_downgrades_confidence_one_step(
        self, conn, make_instrument
    ):
        # orphan underlying (no spot series), snapshot with liquidity inputs
        # and a mark but NO funding and NO reference: carry + tracking drop,
        # liquidity survives.
        code = f"orphan_{uuid4().hex[:8]}"
        instrument = make_instrument(underlying=code)
        _insert_snapshot(conn, instrument, 1, mark="100", liquidity=True)

        result = compute_instrument_fit(conn, instrument, code, "short")

        assert result["included_inputs"] == [
            "liquidity", "underlying_match", "venue_quality"
        ]
        assert sorted(result["dropped_inputs"]) == ["carry", "tracking"]
        assert result["reason"] == "partial_market_data"
        # liquidity had all 4 raw inputs -> 'high', downgraded once for the
        # two dropped market inputs -> 'medium'
        assert result["confidence"] == "medium"
        assert result["instrument_fit"] is not None


class TestGuards:
    def test_invalid_direction_rejected(self, conn, make_instrument):
        with pytest.raises(ValueError, match="direction must be"):
            compute_instrument_fit(conn, make_instrument(), "gold", "avoid")

    def test_not_tradeable_gates_venue_quality_to_zero(self, conn, make_instrument):
        instrument = make_instrument(underlying="gold", tradeable=False)
        _add_profile(conn, instrument, "order_book", "real_resting_orders")

        result = compute_instrument_fit(conn, instrument, "gold", "short")
        assert result["components"]["venue_quality"] == Decimal("0")
