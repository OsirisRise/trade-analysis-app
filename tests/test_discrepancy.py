"""Tests for the spot-vs-perp discrepancy diagnostic — known inputs and
hand-derived expected outputs, same standard as calcs.py."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from types import SimpleNamespace

from onchain_console.discrepancy import (
    ENERGY_FLAG_THRESHOLD,
    FUTURES_NOT_AVAILABLE,
    METALS_FLAG_THRESHOLD,
    build_cross_venue_reports,
    categorize,
    pct_gap,
    should_flag,
    spot_age_hours,
)


class TestCategorize:
    def test_energy(self):
        for code in ("wti_crude_oil", "brent_crude_oil", "natural_gas"):
            assert categorize(code) == "energy"

    def test_metals(self):
        for code in ("gold", "silver", "platinum", "palladium", "copper_spot"):
            assert categorize(code) == "metal"


class TestPctGap:
    def test_perp_above_spot(self):
        # (73.99 - 69.6) / 69.6 — the observed WTI gap on 2026-07-12
        assert pct_gap(Decimal("73.99"), Decimal("69.6")) == Decimal(
            "4.39"
        ) / Decimal("69.6")

    def test_perp_below_spot_negative(self):
        assert pct_gap(Decimal("99"), Decimal("100")) == Decimal("-0.01")

    def test_zero_spot_raises(self):
        with pytest.raises(ValueError):
            pct_gap(Decimal("1"), Decimal("0"))


class TestSpotAge:
    def test_six_days(self):
        as_of = datetime(2026, 7, 6, tzinfo=timezone.utc)
        now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
        assert spot_age_hours(as_of, now) == Decimal("156")  # 6.5 days

    def test_future_as_of_raises(self):
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        as_of = datetime(2026, 7, 12, tzinfo=timezone.utc)
        with pytest.raises(ValueError):
            spot_age_hours(as_of, now)


class TestShouldFlag:
    def test_metal_default_1pct(self):
        assert not should_flag(Decimal("0.01"), "metal")     # at threshold: no
        assert should_flag(Decimal("0.0101"), "metal")       # just over: yes
        assert should_flag(Decimal("-0.02"), "metal")        # abs value

    def test_energy_default_10pct(self):
        assert not should_flag(Decimal("0.08"), "energy")    # routine basis
        assert should_flag(Decimal("0.134"), "energy")       # Brent blowout
        assert not should_flag(Decimal("-0.10"), "energy")   # at threshold: no

    def test_custom_thresholds(self):
        assert should_flag(Decimal("0.006"), "metal",
                           metals_threshold=Decimal("0.005"))
        assert not should_flag(Decimal("0.09"), "energy",
                               energy_threshold=Decimal("0.15"))


class TestBuildCrossVenueReports:
    NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    AS_OF_ENERGY = datetime(2026, 7, 6, tzinfo=timezone.utc)
    AS_OF_METAL = datetime(2026, 7, 13, 11, 0, tzinfo=timezone.utc)

    def make_spots(self):
        return [
            ("wti_crude_oil", Decimal("69.6"), "usd_per_bbl", "eia",
             self.AS_OF_ENERGY),
            ("gold", Decimal("4100"), "usd_per_toz", "metals.dev",
             self.AS_OF_METAL),
        ]

    def make_instruments(self):
        # (underlying, venue, symbol, venue_type, tradeable,
        #  funding_interval_minutes, snap_mark, snap_funding)
        return [
            ("gold", "BydFi", "XAU-USDT", "CEX", True, 240, None, None),
            ("gold", "Hyperliquid", "xyz:GOLD", "DEX", True, 60,
             Decimal("4182"), Decimal("0.0000125")),
            ("gold", "Ostium", "XAU/USD", "DEX", True, None, None, None),
            ("wti_crude_oil", "Hyperliquid", "xyz:CL", "DEX", True, 60,
             Decimal("73.99"), Decimal("0.0000422766")),
            # commodity with no spot entry -> whole instrument skipped
            ("uranium", "Hyperliquid", "xyz:URANIUM", "DEX", True, 60,
             Decimal("1"), Decimal("0")),
        ]

    def make_live_quotes(self):
        return {
            "XAU-USDT": SimpleNamespace(
                mark_price=Decimal("4104"),
                funding_rate=Decimal("0.00005"),
                funding_interval_minutes=240,
            )
        }

    def build(self):
        return {
            r.commodity_code: r
            for r in build_cross_venue_reports(
                self.make_spots(), self.make_instruments(),
                live_quotes=self.make_live_quotes(), now=self.NOW,
            )
        }

    def test_snapshot_venue_hyperliquid(self):
        gold = self.build()["gold"]
        hl = next(g for g in gold.instruments if g.venue == "Hyperliquid")
        assert hl.price_basis == "snapshot"
        # gap 82/4100 = 2% -> over the 1% metal threshold
        assert hl.gap == Decimal("82") / Decimal("4100")
        assert hl.flagged
        # hourly funding normalized: 0.0000125 × 8
        assert hl.funding_rate_8h == Decimal("0.0001")
        assert hl.venue_type == "DEX" and hl.tradeable

    def test_live_venue_bydfi(self):
        gold = self.build()["gold"]
        by = next(g for g in gold.instruments if g.venue == "BydFi")
        assert by.price_basis == "live"
        assert by.mark_price == Decimal("4104")
        # 4/4100 ≈ 0.098% -> under the metal threshold
        assert by.gap == Decimal("4") / Decimal("4100")
        assert not by.flagged
        # 4h funding normalized: 0.00005 × 2 — comparable with Hyperliquid
        assert by.funding_rate_8h == Decimal("0.0001")
        assert by.venue_type == "CEX"

    def test_no_source_venue_ostium(self):
        gold = self.build()["gold"]
        ost = next(g for g in gold.instruments if g.venue == "Ostium")
        assert ost.price_basis == "none"
        assert ost.mark_price is None and ost.gap is None
        assert ost.funding_rate_8h is None
        assert not ost.flagged  # never flag what has no data

    def test_energy_threshold_and_staleness(self):
        wti = self.build()["wti_crude_oil"]
        assert wti.category == "energy"
        assert wti.spot_age_hours == Decimal("180")  # 7.5 days
        (hl,) = wti.instruments
        assert not hl.flagged  # +6.3% is a routine energy basis gap

    def test_commodity_without_spot_is_skipped(self):
        assert "uranium" not in self.build()

    def test_futures_placeholder_never_a_number(self):
        for r in self.build().values():
            assert r.futures_price == FUTURES_NOT_AVAILABLE

    def test_default_thresholds_documented_values(self):
        assert METALS_FLAG_THRESHOLD == Decimal("0.01")
        assert ENERGY_FLAG_THRESHOLD == Decimal("0.10")
