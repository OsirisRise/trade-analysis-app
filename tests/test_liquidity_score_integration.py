"""Integration test: §7.5 liquidity proxy over real DB history
(scoring.compute_liquidity_score).

Same pattern as the other DB tests — real local Postgres inside a
transaction that is ALWAYS rolled back, throwaway instruments created inside
it, real fixtures where useful.

The centrepiece is the CLAUDE.md 2026-07-14 provenance cap: a
liquidity_profiles row with provenance='synthetic_simulation' must cap
data_confidence at 'medium', even though NOTHING writes such a row in real
data yet (Ostium sim capture is deferred to step 8). The cap is wired now;
this test proves it fires.
"""

import json
from decimal import Decimal
from uuid import uuid4

import pytest

from onchain_console.scoring import compute_liquidity_score
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
def universe(conn):
    """Build an isolated active-perp universe with a realistic spread of
    liquidity values, so min-max normalization has something to bite on.

    Real seeded active perps are deactivated inside the rolled-back
    transaction so the universe under test is exactly the four instruments
    created here — otherwise the real Hyperliquid snapshots would widen the
    min/max unpredictably and make hand-derived assertions impossible.
    """
    conn.execute(
        "UPDATE instruments SET status = 'inactive' "
        "WHERE status = 'active' AND instrument_type = 'perp'"
    )

    def _make(symbol, underlying, volume, oi, spread, impact_half_width):
        instrument_id = conn.execute(
            """
            INSERT INTO instruments
                (venue, instrument_type, symbol, underlying, quote_asset,
                 funding_interval_minutes, supports_open_interest,
                 supports_funding, supports_oracle_price, status)
            VALUES ('TestVenue', 'perp', %s, %s, 'USD', 60, true, true, true,
                    'active')
            RETURNING id
            """,
            (f"{symbol}:{uuid4()}", underlying),
        ).fetchone()[0]
        # impact prices symmetric about mid=100 so slippage is derivable:
        # slippage_bps = impact_half_width, spread_bps = 2 * half_width
        if volume is not None:
            conn.execute(
                """
                INSERT INTO market_snapshots
                    (instrument_id, captured_at, mark_price, mid_price,
                     day_volume_usd, open_interest_usd, spread_bps_est,
                     impact_bid_price, impact_ask_price, raw_payload)
                VALUES (%s, now(), 100, 100, %s, %s, %s, %s, %s, %s)
                """,
                (
                    instrument_id, volume, oi, spread,
                    Decimal("100") - impact_half_width / Decimal("100"),
                    Decimal("100") + impact_half_width / Decimal("100"),
                    json.dumps({"test": True}),
                ),
            )
        return Instrument(instrument_id, "TestVenue", symbol, underlying, 60)

    # a clean spread: one best-in-class, one worst, two middling
    instruments = {
        "rich": _make("RICH", "gold", Decimal("1000"), Decimal("2000"),
                      Decimal("2"), Decimal("1")),
        "poor": _make("POOR", "silver", Decimal("0"), Decimal("0"),
                      Decimal("10"), Decimal("5")),
        "mid1": _make("MID1", "platinum", Decimal("500"), Decimal("1000"),
                      Decimal("6"), Decimal("3")),
        "mid2": _make("MID2", "copper_spot", Decimal("250"), Decimal("500"),
                      Decimal("4"), Decimal("2")),
    }
    return instruments


def _add_profile(conn, instrument, profile_type, provenance):
    conn.execute(
        """
        INSERT INTO liquidity_profiles
            (instrument_id, captured_at, profile_type, provenance, payload)
        VALUES (%s, now(), %s, %s, %s)
        """,
        (instrument.id, profile_type, provenance, json.dumps({"test": True})),
    )


class TestUniverseNormalization:
    def test_richest_instrument_scores_top(self, conn, universe):
        result = compute_liquidity_score(conn, universe["rich"])

        assert result["universe_size"] == 4
        # best volume, best OI, tightest spread, lowest slippage -> 100
        assert result["liquidity_score"] == Decimal("100")
        assert result["data_confidence"] == "high"
        assert result["reason"] is None

    def test_poorest_instrument_scores_bottom(self, conn, universe):
        result = compute_liquidity_score(conn, universe["poor"])
        assert result["liquidity_score"] == Decimal("0")

    def test_middle_instrument_between_the_extremes(self, conn, universe):
        result = compute_liquidity_score(conn, universe["mid2"])
        assert Decimal("0") < result["liquidity_score"] < Decimal("100")

    def test_impact_slippage_is_derived_from_the_snapshot(self, conn, universe):
        # the score used a slippage input even though no column stores it
        result = compute_liquidity_score(conn, universe["rich"])
        assert result["raw_inputs"]["impact_slippage_bps"] == Decimal("1")

    def test_all_four_inputs_present_gives_high_base_confidence(
        self, conn, universe
    ):
        result = compute_liquidity_score(conn, universe["mid1"])
        assert result["data_confidence"] == "high"
        assert result["synthetic_cap_applied"] is False


class TestProvenanceCap:
    """CLAUDE.md 2026-07-14 — the whole point of this prompt."""

    def test_synthetic_simulation_caps_confidence_at_medium(self, conn, universe):
        instrument = universe["rich"]  # would otherwise be high/high

        before = compute_liquidity_score(conn, instrument)
        assert before["data_confidence"] == "high"

        # a synthetic-sim profile row appears for this instrument
        _add_profile(conn, instrument, "synthetic_sim", "synthetic_simulation")

        after = compute_liquidity_score(conn, instrument)

        assert after["data_confidence_pre_cap"] == "high"
        assert after["data_confidence"] == "medium"  # capped
        assert after["synthetic_cap_applied"] is True
        assert "synthetic_simulation" in after["provenances"]
        # the score itself is unchanged — only the confidence is capped
        assert after["liquidity_score"] == before["liquidity_score"]

    def test_real_provenance_does_not_cap(self, conn, universe):
        instrument = universe["rich"]
        _add_profile(conn, instrument, "order_book", "real_resting_orders")
        _add_profile(conn, instrument, "risk_tiers", "venue_risk_config")

        result = compute_liquidity_score(conn, instrument)

        assert result["data_confidence"] == "high"  # not capped
        assert result["synthetic_cap_applied"] is False
        assert set(result["provenances"]) == {
            "real_resting_orders", "venue_risk_config"
        }

    def test_cap_fires_even_mixed_with_real_provenance(self, conn, universe):
        # one synthetic row among real ones still caps
        instrument = universe["rich"]
        _add_profile(conn, instrument, "order_book", "real_resting_orders")
        _add_profile(conn, instrument, "synthetic_sim", "synthetic_simulation")

        result = compute_liquidity_score(conn, instrument)

        assert result["data_confidence"] == "medium"
        assert result["synthetic_cap_applied"] is True

    def test_only_the_latest_profile_row_per_type_counts(self, conn, universe):
        # an OLD synthetic row superseded by a newer real row of the SAME
        # type must not keep capping — DISTINCT ON takes the newest per type
        instrument = universe["rich"]
        conn.execute(
            """
            INSERT INTO liquidity_profiles
                (instrument_id, captured_at, profile_type, provenance, payload)
            VALUES (%s, now() - interval '2 days', 'order_book',
                    'synthetic_simulation', %s)
            """,
            (instrument.id, json.dumps({"old": True})),
        )
        conn.execute(
            """
            INSERT INTO liquidity_profiles
                (instrument_id, captured_at, profile_type, provenance, payload)
            VALUES (%s, now(), 'order_book', 'real_resting_orders', %s)
            """,
            (instrument.id, json.dumps({"new": True})),
        )

        result = compute_liquidity_score(conn, instrument)

        assert result["provenances"] == ["real_resting_orders"]
        assert result["synthetic_cap_applied"] is False
        assert result["data_confidence"] == "high"


class TestBydfiShaped:
    """An instrument with a risk-tier profile but no market_snapshots."""

    def test_no_snapshot_is_insufficient_but_reports_its_profile(
        self, conn, universe
    ):
        bydfi = conn.execute(
            """
            INSERT INTO instruments
                (venue, instrument_type, symbol, underlying, quote_asset,
                 funding_interval_minutes, supports_open_interest,
                 supports_funding, supports_oracle_price, status)
            VALUES ('BydFi', 'perp', %s, 'gold', 'USDT', 240, true, true,
                    true, 'active')
            RETURNING id
            """,
            (f"XAU-USDT:{uuid4()}",),
        ).fetchone()[0]
        instrument = Instrument(bydfi, "BydFi", "XAU-USDT", "gold", 240)
        # it HAS a risk-tier profile (BydFi risk_limit tiers, venue config)
        _add_profile(conn, instrument, "risk_tiers", "venue_risk_config")

        result = compute_liquidity_score(conn, instrument)

        assert result["reason"] == "no_market_snapshot"
        assert result["liquidity_score"] is None
        assert result["data_confidence"] is None
        # but its supplementary profile is still surfaced
        assert len(result["liquidity_profiles"]) == 1
        assert result["liquidity_profiles"][0]["provenance"] == "venue_risk_config"

    def test_bydfi_with_no_snapshot_still_reports_synthetic_signal(
        self, conn, universe
    ):
        # even insufficient, a synthetic profile is flagged (cap would apply
        # once a snapshot exists) — base confidence None so nothing to cap yet
        bydfi = conn.execute(
            """
            INSERT INTO instruments
                (venue, instrument_type, symbol, underlying,
                 funding_interval_minutes, supports_open_interest,
                 supports_funding, supports_oracle_price, status)
            VALUES ('BydFi', 'perp', %s, 'gold', 240, true, true, true, 'active')
            RETURNING id
            """,
            (f"XAU-USDT:{uuid4()}",),
        ).fetchone()[0]
        instrument = Instrument(bydfi, "BydFi", "XAU-USDT", "gold", 240)
        _add_profile(conn, instrument, "synthetic_sim", "synthetic_simulation")

        result = compute_liquidity_score(conn, instrument)

        assert result["reason"] == "no_market_snapshot"
        assert result["data_confidence"] is None
        assert result["synthetic_cap_applied"] is True  # flagged for when data lands

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
            result = compute_liquidity_score(conn, Instrument(*row))
            # BydFi has no snapshots today -> insufficient, cleanly
            if result["liquidity_score"] is None:
                assert result["reason"] == "no_market_snapshot"
                assert result["data_confidence"] is None


class TestPartialInputs:
    def test_missing_spread_lowers_base_confidence(self, conn, universe):
        # an instrument whose snapshot lacks the impact prices -> spread and
        # slippage both None -> only 2 of 4 inputs -> 'medium'
        partial = conn.execute(
            """
            INSERT INTO instruments
                (venue, instrument_type, symbol, underlying,
                 funding_interval_minutes, supports_open_interest,
                 supports_funding, supports_oracle_price, status)
            VALUES ('TestVenue', 'perp', %s, 'gold', 60, true, true, true,
                    'active')
            RETURNING id
            """,
            (f"PART:{uuid4()}",),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO market_snapshots
                (instrument_id, captured_at, mark_price, day_volume_usd,
                 open_interest_usd, raw_payload)
            VALUES (%s, now(), 100, 300, 600, %s)
            """,
            (partial, json.dumps({"test": True})),
        )
        instrument = Instrument(partial, "TestVenue", "PART", "gold", 60)

        result = compute_liquidity_score(conn, instrument)

        assert result["raw_inputs"]["spread_bps_est"] is None
        assert result["raw_inputs"]["impact_slippage_bps"] is None
        assert result["data_confidence"] == "medium"
        # score is still produced, with the missing fields neutral-filled
        assert result["liquidity_score"] is not None
