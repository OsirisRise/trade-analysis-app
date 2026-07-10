"""Tests for deterministic calcs (blueprint §7), using known inputs and
hand-derived expected outputs."""

from decimal import Decimal

import pytest

from onchain_console import calcs


class TestPremiumPct:
    def test_positive_premium(self):
        # §7.1: (101 - 100) / 100 = 0.01 → longs likely pay funding
        assert calcs.premium_pct(Decimal("101"), Decimal("100")) == Decimal("0.01")

    def test_negative_premium(self):
        # (99 - 100) / 100 = -0.01 → shorts likely pay
        assert calcs.premium_pct(Decimal("99"), Decimal("100")) == Decimal("-0.01")

    def test_live_shaped_values(self):
        # Values observed on xyz:GOLD 2026-07-10: (4095.1-4092.5)/4092.5
        result = calcs.premium_pct(Decimal("4095.1"), Decimal("4092.5"))
        assert result == Decimal("2.6") / Decimal("4092.5")
        assert result.quantize(Decimal("0.0000001")) == Decimal("0.0006353")

    def test_zero_oracle_raises(self):
        with pytest.raises(ValueError):
            calcs.premium_pct(Decimal("100"), Decimal("0"))


class TestFundingPayment:
    def test_uses_oracle_notional(self):
        # §7.2: size 10 × oracle 100 × rate 0.0000125 = 0.0125
        assert calcs.funding_payment(
            Decimal("10"), Decimal("100"), Decimal("0.0000125")
        ) == Decimal("0.0125")

    def test_per_1k_notional(self):
        # $1k notional at 0.01%/interval costs $0.10 per interval
        assert calcs.funding_payment(
            Decimal("1"), Decimal("1000"), Decimal("0.0001")
        ) == Decimal("0.1")

    def test_negative_rate_means_receipt_for_longs(self):
        assert calcs.funding_payment(
            Decimal("10"), Decimal("100"), Decimal("-0.0000125")
        ) == Decimal("-0.0125")


class TestFundingNormalization:
    def test_8h_equiv_from_hourly(self):
        # Hyperliquid hourly: 0.0000125/hr × 8 = 0.0001
        assert calcs.funding_rate_8h_equiv(Decimal("0.0000125"), 60) == Decimal(
            "0.0001"
        )

    def test_8h_equiv_identity_for_8h_interval(self):
        assert calcs.funding_rate_8h_equiv(Decimal("0.0001"), 480) == Decimal("0.0001")

    def test_apr_est_from_hourly(self):
        # 0.0000125/hr × 8760 hr/yr = 0.1095 (10.95% APR, context only)
        assert calcs.funding_apr_est(Decimal("0.0000125"), 60) == Decimal("0.1095")

    def test_bad_interval_raises(self):
        with pytest.raises(ValueError):
            calcs.funding_rate_8h_equiv(Decimal("0.0001"), 0)
        with pytest.raises(ValueError):
            calcs.funding_apr_est(Decimal("0.0001"), -60)


class TestOpenInterestUsd:
    def test_base_units_times_mark(self):
        assert calcs.open_interest_usd(Decimal("2"), Decimal("4000")) == Decimal(
            "8000"
        )


class TestSpreadBpsEst:
    def test_symmetric_spread(self):
        # (100.1 - 99.9) / 100 = 0.002 → 20 bps
        assert calcs.spread_bps_est(
            Decimal("99.9"), Decimal("100.1"), Decimal("100")
        ) == Decimal("20")

    def test_zero_mid_raises(self):
        with pytest.raises(ValueError):
            calcs.spread_bps_est(Decimal("1"), Decimal("2"), Decimal("0"))
