"""Tests for the spot-vs-perp discrepancy diagnostic — known inputs and
hand-derived expected outputs, same standard as calcs.py."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from onchain_console.discrepancy import (
    ENERGY_FLAG_THRESHOLD,
    FUTURES_NOT_AVAILABLE,
    METALS_FLAG_THRESHOLD,
    build_reports,
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


class TestBuildReports:
    NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)

    def make_pairs(self):
        as_of_energy = datetime(2026, 7, 6, tzinfo=timezone.utc)
        as_of_metal = datetime(2026, 7, 12, 11, 0, tzinfo=timezone.utc)
        return [
            # one commodity, two instruments (both venues)
            ("wti_crude_oil", Decimal("69.6"), "usd_per_bbl", "eia",
             as_of_energy, "Hyperliquid", "xyz:CL", Decimal("73.99")),
            ("wti_crude_oil", Decimal("69.6"), "usd_per_bbl", "eia",
             as_of_energy, "Ostium", "WTI/USD", Decimal("74.10")),
            ("gold", Decimal("4100"), "usd_per_toz", "metals.dev",
             as_of_metal, "Hyperliquid", "xyz:GOLD", Decimal("4182")),
        ]

    def test_groups_by_commodity_with_gaps(self):
        reports = {r.commodity_code: r
                   for r in build_reports(self.make_pairs(), now=self.NOW)}
        wti = reports["wti_crude_oil"]
        assert wti.category == "energy"
        assert wti.spot_age_hours == Decimal("156")
        assert [g.symbol for g in wti.instruments] == ["xyz:CL", "WTI/USD"]
        assert wti.instruments[0].gap == Decimal("4.39") / Decimal("69.6")

    def test_flags_use_category_thresholds(self):
        reports = {r.commodity_code: r
                   for r in build_reports(self.make_pairs(), now=self.NOW)}
        # WTI gaps ~6.3% / ~6.5%: under the 10% energy threshold -> quiet
        assert not any(g.flagged for g in reports["wti_crude_oil"].instruments)
        # gold gap 82/4100 = 2%: over the 1% metal threshold -> flagged
        gold = reports["gold"].instruments[0]
        assert gold.gap == Decimal("82") / Decimal("4100")
        assert gold.flagged

    def test_futures_placeholder_never_a_number(self):
        for r in build_reports(self.make_pairs(), now=self.NOW):
            assert r.futures_price == FUTURES_NOT_AVAILABLE

    def test_default_thresholds_documented_values(self):
        assert METALS_FLAG_THRESHOLD == Decimal("0.01")
        assert ENERGY_FLAG_THRESHOLD == Decimal("0.10")
