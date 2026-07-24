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


class TestFundingIntervals:
    def test_hyperliquid_hourly(self):
        # §7.3's own example: n = hold_days × 24 at a 60-minute cadence
        assert calcs.funding_intervals(60, 14) == Decimal(336)

    def test_bydfi_four_hourly(self):
        # 1440 / 240 = 6 charges per day × 10 days
        assert calcs.funding_intervals(240, 10) == Decimal(60)

    def test_generalization_is_the_whole_point(self):
        # Same hold, same venue-agnostic rate: BydFi's 4h cadence takes
        # exactly a quarter of Hyperliquid's funding charges.
        assert calcs.funding_intervals(60, 10) == 4 * calcs.funding_intervals(240, 10)

    def test_non_divisor_interval(self):
        # 90-minute cadence = 16 charges/day; nothing assumes a divisor of 24
        assert calcs.funding_intervals(90, 3) == Decimal(48)

    def test_bad_inputs_raise(self):
        with pytest.raises(ValueError):
            calcs.funding_intervals(0, 7)
        with pytest.raises(ValueError):
            calcs.funding_intervals(60, 0)
        with pytest.raises(ValueError):
            calcs.funding_intervals(60, -7)


class TestHoldPeriodFundingEstimate:
    # Worked Hyperliquid example, hand-derived in blueprint §7.3 style.
    # interval 60min, hold 14 days -> n = 336.
    HL_HISTORY = [
        Decimal(v)
        for v in ("0.000005", "0.0000075", "0.00001", "0.0000125", "0.000015")
    ]

    def test_hyperliquid_worked_example(self):
        result = calcs.hold_period_funding_estimate(
            current_funding_rate=Decimal("0.0000125"),
            funding_rate_history=self.HL_HISTORY,
            funding_interval_minutes=60,
            hold_days=14,
        )
        # base: 0.0000125 × 336 = 0.0042 (0.42% of notional over the hold)
        assert result["base"] == Decimal("0.0042")
        # optimistic: mean 0.00005/5 = 0.00001 × 336 = 0.00336
        assert result["optimistic"] == Decimal("0.00336")
        # stress: P90 (inclusive) of 5 sorted points sits at index 3.6 ->
        # 0.0000125 + 0.6 × 0.0000025 = 0.000014; × 336 = 0.004704
        assert result["stress"] == Decimal("0.004704")

    def test_bydfi_four_hour_interval_worked_example(self):
        # interval 240min, hold 10 days -> n = 60
        result = calcs.hold_period_funding_estimate(
            current_funding_rate=Decimal("0.0001"),
            funding_rate_history=[Decimal("0.0001"), Decimal("0.0002")],
            funding_interval_minutes=240,
            hold_days=10,
        )
        assert result["base"] == Decimal("0.0001") * 60 == Decimal("0.006")
        # mean of two = 0.00015 × 60 = 0.009
        assert result["optimistic"] == Decimal("0.009")
        # P90 (inclusive) of 2 points = 0.0001 + 0.9 × 0.0001 = 0.00019
        assert result["stress"] == Decimal("0.00019") * 60 == Decimal("0.0114")

    def test_interval_generalization_changes_the_cost_fourfold(self):
        """The same funding rate held the same number of days costs 4× more
        at Hyperliquid's hourly cadence than at BydFi's 4-hourly one."""
        args = dict(
            current_funding_rate=Decimal("0.0001"),
            funding_rate_history=[Decimal("0.0001")],
            hold_days=10,
        )
        hl = calcs.hold_period_funding_estimate(funding_interval_minutes=60, **args)
        bydfi = calcs.hold_period_funding_estimate(
            funding_interval_minutes=240, **args
        )
        assert hl["base"] == Decimal("0.024")
        assert bydfi["base"] == Decimal("0.006")
        assert hl["base"] == 4 * bydfi["base"]

    def test_empty_history_returns_none_not_a_crash(self):
        # BydFi today: base needs no history and still computes; the two
        # window-derived figures are None rather than fabricated.
        result = calcs.hold_period_funding_estimate(
            current_funding_rate=Decimal("0.0000125"),
            funding_rate_history=[],
            funding_interval_minutes=60,
            hold_days=14,
        )
        assert result["base"] == Decimal("0.0042")
        assert result["optimistic"] is None
        assert result["stress"] is None

    def test_single_observation_is_its_own_percentile(self):
        # statistics.quantiles needs two points; the P90 of one point is it
        result = calcs.hold_period_funding_estimate(
            current_funding_rate=Decimal("0.00002"),
            funding_rate_history=[Decimal("0.00002")],
            funding_interval_minutes=60,
            hold_days=1,
        )
        assert result["base"] == Decimal("0.00048")  # 0.00002 × 24
        assert result["optimistic"] == Decimal("0.00048")
        assert result["stress"] == Decimal("0.00048")

    def test_negative_funding_is_a_receipt_for_longs(self):
        # Sign convention (§7.2): negative rate = longs are paid
        result = calcs.hold_period_funding_estimate(
            current_funding_rate=Decimal("-0.00001"),
            funding_rate_history=[Decimal("-0.00001"), Decimal("-0.00003")],
            funding_interval_minutes=60,
            hold_days=7,
        )
        assert result["base"] == Decimal("-0.00168")  # -0.00001 × 168
        assert result["optimistic"] < 0
        # P90 of [-0.00003, -0.00001] = -0.000012 -> least favourable tail
        assert result["stress"] == Decimal("-0.000012") * 168

    def test_stress_never_exceeds_observed_range(self):
        """The chosen inclusive method must not invent a rate worse than
        anything actually observed — the stdlib default would."""
        history = [Decimal("0.00001"), Decimal("0.00003")]
        result = calcs.hold_period_funding_estimate(
            current_funding_rate=Decimal("0.00001"),
            funding_rate_history=history,
            funding_interval_minutes=60,
            hold_days=1,
        )
        assert result["stress"] <= max(history) * 24
        assert result["stress"] >= min(history) * 24

    def test_history_order_does_not_matter(self):
        shuffled = list(reversed(self.HL_HISTORY))
        ordered = calcs.hold_period_funding_estimate(
            Decimal("0.0000125"), self.HL_HISTORY, 60, 14
        )
        reordered = calcs.hold_period_funding_estimate(
            Decimal("0.0000125"), shuffled, 60, 14
        )
        assert ordered == reordered

    def test_bad_inputs_raise(self):
        with pytest.raises(ValueError):
            calcs.hold_period_funding_estimate(Decimal("0.00001"), [], 0, 7)
        with pytest.raises(ValueError):
            calcs.hold_period_funding_estimate(Decimal("0.00001"), [], 60, 0)


class TestTrackingErrorBps:
    def test_mark_above_spot_is_positive(self):
        # §7.4: 10000 × (4100 - 4000) / 4000 = 250 bps
        assert calcs.tracking_error_bps(
            Decimal("4100"), Decimal("4000")
        ) == Decimal("250")

    def test_mark_below_spot_is_negative(self):
        assert calcs.tracking_error_bps(
            Decimal("3900"), Decimal("4000")
        ) == Decimal("-250")

    def test_perfect_tracking_is_zero(self):
        assert calcs.tracking_error_bps(
            Decimal("4000"), Decimal("4000")
        ) == Decimal("0")

    def test_energy_shaped_values(self):
        # WTI mark 70.50 vs EIA physical spot 69.00 -> 217.39 bps of basis.
        # The number is arithmetic; whether it MEANS tracking failure is the
        # energy-branch question, handled in scoring.py.
        result = calcs.tracking_error_bps(Decimal("70.50"), Decimal("69.00"))
        assert result.quantize(Decimal("0.01")) == Decimal("217.39")

    def test_zero_spot_raises(self):
        with pytest.raises(ValueError):
            calcs.tracking_error_bps(Decimal("4000"), Decimal("0"))


class TestTrackingErrorStats:
    def test_opposite_directions_do_not_cancel(self):
        """The abs() must be applied per pair BEFORE averaging. A +250bps
        day and a -250bps day are two bad days, not a clean average."""
        pairs = [
            (Decimal("4100"), Decimal("4000")),  # +250 bps
            (Decimal("3900"), Decimal("4000")),  # -250 bps
        ]
        stats = calcs.tracking_error_stats(pairs)

        assert stats["avg_abs_7d"] == Decimal("250")  # NOT 0
        assert stats["max_abs_30d"] == Decimal("250")

    def test_avg_and_max_are_distinct_over_the_same_data(self):
        # errors: 50, 100, 300 -> avg 150, max 300
        pairs = [
            (Decimal("4020"), Decimal("4000")),
            (Decimal("4040"), Decimal("4000")),
            (Decimal("4120"), Decimal("4000")),
        ]
        stats = calcs.tracking_error_stats(pairs)

        assert stats["avg_abs_7d"] == Decimal("150")
        assert stats["max_abs_30d"] == Decimal("300")

    def test_seven_day_avg_and_thirty_day_max_come_from_different_slices(self):
        """How scoring.py uses it: the narrow window feeds the average, the
        wide window feeds the maximum, and the answers differ."""
        recent_7d = [
            (Decimal("4020"), Decimal("4000")),  # 50
            (Decimal("4040"), Decimal("4000")),  # 100
        ]
        full_30d = recent_7d + [
            (Decimal("4400"), Decimal("4000")),  # 1000 — an old blowout
        ]

        avg = calcs.tracking_error_stats(recent_7d)["avg_abs_7d"]
        maximum = calcs.tracking_error_stats(full_30d)["max_abs_30d"]

        assert avg == Decimal("75")
        assert maximum == Decimal("1000")
        # the old spike must not drag the recent average
        assert calcs.tracking_error_stats(full_30d)["avg_abs_7d"] != avg

    def test_single_pair(self):
        stats = calcs.tracking_error_stats([(Decimal("4100"), Decimal("4000"))])
        assert stats["avg_abs_7d"] == Decimal("250")
        assert stats["max_abs_30d"] == Decimal("250")

    def test_empty_series_returns_none_not_a_crash(self):
        assert calcs.tracking_error_stats([]) == {
            "avg_abs_7d": None,
            "max_abs_30d": None,
        }

    def test_all_negative_errors_report_positive_magnitudes(self):
        pairs = [
            (Decimal("3900"), Decimal("4000")),  # -250
            (Decimal("3800"), Decimal("4000")),  # -500
        ]
        stats = calcs.tracking_error_stats(pairs)
        assert stats["avg_abs_7d"] == Decimal("375")
        assert stats["max_abs_30d"] == Decimal("500")


class TestBasisCategory:
    """CLAUDE.md 2026-07-12 — the categorization is binding, so it is
    asserted member by member rather than spot-checked."""

    def test_energy_commodities(self):
        for code in ("wti_crude_oil", "brent_crude_oil", "natural_gas"):
            assert calcs.basis_category(code) == "energy", code

    def test_metals_commodities(self):
        for code in ("gold", "silver", "platinum", "palladium", "copper_spot"):
            assert calcs.basis_category(code) == "metal", code

    def test_the_two_sets_are_disjoint_and_complete(self):
        assert not (
            calcs.ENERGY_BASIS_GAP_COMMODITIES & calcs.METALS_NO_BASIS_GAP_COMMODITIES
        )
        assert len(calcs.ENERGY_BASIS_GAP_COMMODITIES) == 3
        assert len(calcs.METALS_NO_BASIS_GAP_COMMODITIES) == 5

    def test_uncategorized_commodity_is_unknown(self):
        assert calcs.basis_category("wheat") == "unknown"
        assert calcs.basis_category("") == "unknown"

    def test_old_pre_0006_vocabulary_is_not_silently_accepted(self):
        # 'crude_oil' / 'brent' were the wrong codes fixed in 0006/0009;
        # they must land in 'unknown' (downgraded), never in 'energy'
        assert calcs.basis_category("crude_oil") == "unknown"
        assert calcs.basis_category("brent") == "unknown"
        assert calcs.basis_category("copper") == "unknown"


class TestDowngradeTier:
    def test_full_ladder(self):
        assert calcs.downgrade_tier("high") == "medium"
        assert calcs.downgrade_tier("medium") == "low"
        assert calcs.downgrade_tier("low") is None

    def test_none_stays_none(self):
        assert calcs.downgrade_tier(None) is None

    def test_downgrade_never_escapes_the_vocabulary(self):
        for tier in calcs.TIER_LADDER:
            assert calcs.downgrade_tier(tier) in calcs.TIER_LADDER


class TestImpactSlippageBps:
    def test_symmetric_book_is_half_the_spread(self):
        # impact bid/ask symmetric about mid: slippage = spread / 2
        spread = calcs.spread_bps_est(
            Decimal("99.9"), Decimal("100.1"), Decimal("100")
        )  # 20 bps
        slippage = calcs.impact_slippage_bps(
            Decimal("99.9"), Decimal("100.1"), Decimal("100")
        )
        assert slippage == Decimal("10")
        assert slippage == spread / 2

    def test_asymmetric_book_diverges_from_half_spread(self):
        # mid below the impact midpoint: mean of |ask-mid| and |mid-bid|
        # ask-mid = 0.30, mid-bid = 0.10 -> mean 0.20 -> 20 bps on mid 100
        slippage = calcs.impact_slippage_bps(
            Decimal("99.9"), Decimal("100.3"), Decimal("100")
        )
        assert slippage == Decimal("20")

    def test_zero_mid_raises(self):
        with pytest.raises(ValueError):
            calcs.impact_slippage_bps(Decimal("1"), Decimal("2"), Decimal("0"))


class TestLiquidityScore:
    # A four-instrument universe, hand-computable. Values chosen so each
    # field has a clean min and max.
    UNIVERSE = {
        "day_volume_usd": {"min": Decimal("0"), "max": Decimal("100")},
        "open_interest_usd": {"min": Decimal("0"), "max": Decimal("200")},
        "spread_bps_est": {"min": Decimal("2"), "max": Decimal("10")},
        "impact_slippage_bps": {"min": Decimal("1"), "max": Decimal("5")},
    }

    def test_best_on_every_axis_scores_100(self):
        # max volume, max OI, min spread, min slippage -> every normalized
        # term is 100, weights sum to 1 -> 100
        score = calcs.liquidity_score(
            Decimal("100"), Decimal("200"), Decimal("2"), Decimal("1"),
            self.UNIVERSE,
        )
        assert score == Decimal("100")

    def test_worst_on_every_axis_scores_0(self):
        score = calcs.liquidity_score(
            Decimal("0"), Decimal("0"), Decimal("10"), Decimal("5"),
            self.UNIVERSE,
        )
        assert score == Decimal("0")

    def test_hand_derived_midrange(self):
        # volume 50/100 = 0.5 -> 50 ; OI 100/200 = 0.5 -> 50
        # spread 6: (6-2)/(10-2)=0.5, inverted -> 50
        # slippage 3: (3-1)/(5-1)=0.5, inverted -> 50
        # all 50, weights sum to 1 -> 50
        score = calcs.liquidity_score(
            Decimal("50"), Decimal("100"), Decimal("6"), Decimal("3"),
            self.UNIVERSE,
        )
        assert score == Decimal("50")

    def test_weights_are_applied_per_field(self):
        # volume best (100), everything else worst (0). Only the 0.35
        # volume weight contributes -> 35.
        score = calcs.liquidity_score(
            Decimal("100"), Decimal("0"), Decimal("10"), Decimal("5"),
            self.UNIVERSE,
        )
        assert score == Decimal("35")

    def test_inverted_fields_reward_lower_cost(self):
        # tightest spread + lowest slippage, worst size -> only the cost
        # family (0.25 + 0.15 = 0.40) contributes at 100 -> 40
        score = calcs.liquidity_score(
            Decimal("0"), Decimal("0"), Decimal("2"), Decimal("1"),
            self.UNIVERSE,
        )
        assert score == Decimal("40")

    def test_min_equals_max_field_is_neutral_not_divide_by_zero(self):
        # every instrument ties on volume: that field scores 50 regardless
        universe = dict(self.UNIVERSE)
        universe["day_volume_usd"] = {"min": Decimal("7"), "max": Decimal("7")}
        # volume neutral (0.35 * 50 = 17.5); make the rest score 100
        # (0.65 * 100 = 65) -> 82.5
        score = calcs.liquidity_score(
            Decimal("7"), Decimal("200"), Decimal("2"), Decimal("1"), universe
        )
        assert score == Decimal("82.5")

    def test_all_fields_tie_scores_exactly_50(self):
        flat = {
            f: {"min": Decimal("5"), "max": Decimal("5")}
            for f in calcs.LIQUIDITY_WEIGHTS
        }
        score = calcs.liquidity_score(
            Decimal("5"), Decimal("5"), Decimal("5"), Decimal("5"), flat
        )
        assert score == Decimal("50")

    def test_missing_value_is_neutral(self):
        # a None input contributes its neutral 50, not a crash
        # volume None -> 50 (0.35*50=17.5); rest best -> 0.65*100=65 -> 82.5
        score = calcs.liquidity_score(
            None, Decimal("200"), Decimal("2"), Decimal("1"), self.UNIVERSE
        )
        assert score == Decimal("82.5")

    def test_value_outside_universe_range_is_clamped(self):
        # a target richer than the universe max must not exceed 100 overall
        score = calcs.liquidity_score(
            Decimal("500"), Decimal("200"), Decimal("2"), Decimal("1"),
            self.UNIVERSE,
        )
        assert score == Decimal("100")

    def test_weights_sum_to_one(self):
        assert sum(calcs.LIQUIDITY_WEIGHTS.values()) == Decimal("1")


class TestCapTier:
    def test_high_caps_to_the_ceiling(self):
        assert calcs.cap_tier("high", "medium") == "medium"

    def test_at_ceiling_is_unchanged(self):
        assert calcs.cap_tier("medium", "medium") == "medium"

    def test_below_ceiling_is_unchanged(self):
        assert calcs.cap_tier("low", "medium") == "low"

    def test_none_stays_none(self):
        assert calcs.cap_tier(None, "medium") is None

    def test_ceiling_higher_than_tier_is_a_noop(self):
        assert calcs.cap_tier("low", "high") == "low"

    def test_result_stays_in_the_ladder(self):
        for tier in calcs.TIER_LADDER:
            assert calcs.cap_tier(tier, "medium") in calcs.TIER_LADDER


class TestFundingDirectionPenalty:
    def test_all_same_sign_is_zero(self):
        assert calcs.funding_direction_penalty(
            [Decimal("0.0001"), Decimal("0.00008"), Decimal("0.00005")]
        ) == Decimal("0")

    def test_every_step_flips_is_one(self):
        # 4 observations, 3 transitions, all flips -> 3/3 = 1
        assert calcs.funding_direction_penalty(
            [Decimal("0.0001"), Decimal("-0.0001"), Decimal("0.0001"),
             Decimal("-0.0001")]
        ) == Decimal("1")

    def test_one_flip_of_four_transitions(self):
        # + + - - -  : one sign change across 4 transitions -> 0.25
        history = [Decimal("0.0001"), Decimal("0.0002"), Decimal("-0.0001"),
                   Decimal("-0.0002"), Decimal("-0.0003")]
        assert calcs.funding_direction_penalty(history) == Decimal("0.25")

    def test_exact_zero_has_no_sign_and_is_not_a_flip(self):
        # 0 between two positives: 0*pos = 0, not < 0, so no flip counted
        history = [Decimal("0.0001"), Decimal("0"), Decimal("0.0001")]
        assert calcs.funding_direction_penalty(history) == Decimal("0")

    def test_single_and_empty_are_zero(self):
        assert calcs.funding_direction_penalty([Decimal("0.0001")]) == Decimal("0")
        assert calcs.funding_direction_penalty([]) == Decimal("0")


class TestFundingStabilityPenalty:
    def test_constant_funding_is_perfectly_stable(self):
        assert calcs.funding_stability_penalty(
            [Decimal("0.00003"), Decimal("0.00003"), Decimal("0.00003")]
        ) == Decimal("0")

    def test_swing_at_reference_saturates_to_one(self):
        # pstdev of [0.0001, 0, 0.0001, 0] = 0.00005 == CARRY_STABILITY_REFERENCE
        assert calcs.funding_stability_penalty(
            [Decimal("0.0001"), Decimal("0"), Decimal("0.0001"), Decimal("0")]
        ) == Decimal("1")

    def test_swing_beyond_reference_clamps_to_one(self):
        assert calcs.funding_stability_penalty(
            [Decimal("0.001"), Decimal("-0.001")]
        ) == Decimal("1")

    def test_half_reference_swing_is_half_penalty(self):
        # pstdev of [0.000025, -0.000025] = 0.000025 = reference/2 -> 0.5
        assert calcs.funding_stability_penalty(
            [Decimal("0.000025"), Decimal("-0.000025")]
        ) == Decimal("0.5")

    def test_single_and_empty_are_zero(self):
        assert calcs.funding_stability_penalty([Decimal("0.0001")]) == Decimal("0")
        assert calcs.funding_stability_penalty([]) == Decimal("0")


class TestCarryScore:
    def test_stable_max_funding_scores_0_for_long_100_for_short(self):
        # constant funding at the reference: long pays max, short receives max
        history = [calcs.CARRY_REFERENCE_RATE] * 4
        assert calcs.carry_score(history, "long") == Decimal("0")
        assert calcs.carry_score(history, "short") == Decimal("100")

    def test_zero_funding_is_neutral_both_directions(self):
        history = [Decimal("0"), Decimal("0"), Decimal("0")]
        assert calcs.carry_score(history, "long") == Decimal("50")
        assert calcs.carry_score(history, "short") == Decimal("50")

    def test_stable_negative_funding_favors_long(self):
        # long RECEIVES when funding is negative -> above neutral
        history = [Decimal("-0.000025")] * 3  # -reference/2
        # benefit for long = +0.000025 -> signal +0.5 -> base 75, stable -> 75
        assert calcs.carry_score(history, "long") == Decimal("75")
        # mirror: short pays -> 25
        assert calcs.carry_score(history, "short") == Decimal("25")

    def test_beyond_reference_clamps_not_overshoots(self):
        # funding far above reference must not push a short past 100
        history = [Decimal("0.01")] * 3
        assert calcs.carry_score(history, "short") == Decimal("100")
        assert calcs.carry_score(history, "long") == Decimal("0")

    def test_volatility_shrinks_a_strong_carry_toward_neutral(self):
        # short, mean favorable but maximally volatile: base 100 but the
        # stability penalty saturates -> reliability 0 -> shrinks to 50
        history = [Decimal("0.0001"), Decimal("0"), Decimal("0.0001"), Decimal("0")]
        # mean 0.00005 = reference -> short base 100; pstdev 0.00005 -> stab 1
        assert calcs.carry_score(history, "short") == Decimal("50")

    def test_stable_beats_volatile_for_the_same_favorable_mean(self):
        stable = [Decimal("0.00003")] * 6  # short-favorable, steady
        # same mean 0.00003 but with swings
        volatile = [Decimal("0.00006"), Decimal("0")] * 3
        assert sum(stable) / 6 == sum(volatile) / 6  # identical mean
        stable_score = calcs.carry_score(stable, "short")
        volatile_score = calcs.carry_score(volatile, "short")
        assert stable_score > volatile_score
        # both favorable-for-short, so both should sit at or above neutral
        assert volatile_score >= Decimal("50")

    def test_direction_flips_shrink_toward_neutral(self):
        # a favorable-mean short whose funding keeps flipping sign
        history = [Decimal("0.00006"), Decimal("-0.00002")] * 3
        # mean = 0.00002 (short-favorable) -> base above 50, but flips + swings
        # pull it back toward neutral
        score = calcs.carry_score(history, "short")
        base_only = Decimal("50") + Decimal("50") * (
            (sum(history) / len(history)) / calcs.CARRY_REFERENCE_RATE
        )
        assert Decimal("50") <= score < base_only

    def test_single_value_has_no_penalty(self):
        # base level from one point, no fabricated instability
        assert calcs.carry_score([calcs.CARRY_REFERENCE_RATE], "short") == Decimal(
            "100"
        )

    def test_empty_history_is_neutral(self):
        assert calcs.carry_score([], "long") == Decimal("50")
        assert calcs.carry_score([], "short") == Decimal("50")

    def test_long_and_short_are_mirror_images_about_50(self):
        history = [Decimal("0.00004"), Decimal("0.00002"), Decimal("0.00003")]
        long_score = calcs.carry_score(history, "long")
        short_score = calcs.carry_score(history, "short")
        assert long_score + short_score == Decimal("100")

    def test_score_stays_in_0_100(self):
        for history in (
            [Decimal("0.01"), Decimal("-0.01")],
            [Decimal("0.00005")] * 3,
            [Decimal("-0.00005")] * 3,
            [Decimal("0"), Decimal("0.0001"), Decimal("-0.0001")],
        ):
            for direction in ("long", "short"):
                score = calcs.carry_score(history, direction)
                assert Decimal("0") <= score <= Decimal("100")

    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError):
            calcs.carry_score([Decimal("0.0001")], "avoid")
        with pytest.raises(ValueError):
            calcs.carry_score([Decimal("0.0001")], "")


class TestTrackingFidelityScore:
    def test_perfect_tracking_is_100(self):
        assert calcs.tracking_fidelity_score(Decimal("0")) == Decimal("100")

    def test_half_reference_is_75(self):
        # 50 bps against a 200 bps reference -> 100*(1-0.25)
        assert calcs.tracking_fidelity_score(Decimal("50")) == Decimal("75")

    def test_reference_drift_is_0(self):
        assert calcs.tracking_fidelity_score(Decimal("200")) == Decimal("0")

    def test_beyond_reference_clamps_to_0(self):
        assert calcs.tracking_fidelity_score(Decimal("5000")) == Decimal("0")

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            calcs.tracking_fidelity_score(Decimal("-1"))


class TestUnderlyingMatch:
    def test_exact_match_is_100(self):
        assert calcs.underlying_match("gold", "gold") == Decimal("100")
        assert calcs.underlying_match("copper_spot", "copper_spot") == Decimal("100")

    def test_crude_grades_are_close_substitutes(self):
        assert calcs.underlying_match(
            "wti_crude_oil", "brent_crude_oil"
        ) == Decimal("75")
        assert calcs.underlying_match(
            "brent_crude_oil", "wti_crude_oil"
        ) == Decimal("75")

    def test_precious_metals_complex_is_partial(self):
        assert calcs.underlying_match("gold", "silver") == Decimal("50")
        assert calcs.underlying_match("platinum", "palladium") == Decimal("50")
        assert calcs.underlying_match("silver", "gold") == Decimal("50")

    def test_unrelated_is_0(self):
        assert calcs.underlying_match("gold", "copper_spot") == Decimal("0")
        assert calcs.underlying_match("natural_gas", "wti_crude_oil") == Decimal("0")
        assert calcs.underlying_match("copper_spot", "silver") == Decimal("0")

    def test_copper_is_not_in_the_precious_complex(self):
        # copper_spot is metal by basis_category but industrial — not a
        # substitute for a precious-metal thesis
        assert calcs.underlying_match("copper_spot", "gold") == Decimal("0")


class TestVenueQuality:
    def test_dex_with_real_depth_is_top(self):
        # data 100, arch 100
        assert calcs.venue_quality(
            "DEX", "perp", True, "real_resting_orders"
        ) == Decimal("100")

    def test_cex_with_risk_config(self):
        # data 65*0.55 + arch 70*0.45 = 35.75 + 31.5
        assert calcs.venue_quality(
            "CEX", "perp", True, "venue_risk_config"
        ) == Decimal("67.25")

    def test_dex_with_no_liquidity_data(self):
        # Ostium today: DEX, synthetic deferred -> no provenance
        # data 25*0.55 + arch 100*0.45 = 13.75 + 45
        assert calcs.venue_quality("DEX", "perp", True, None) == Decimal("58.75")

    def test_dex_with_synthetic_only(self):
        # data 40*0.55 + arch 100*0.45 = 22 + 45
        assert calcs.venue_quality(
            "DEX", "perp", True, "synthetic_simulation"
        ) == Decimal("67")

    def test_tokenized_spot_token(self):
        # PAXG/XAUT: venue_type NULL, custody-free token, no depth data
        # data 25*0.55 + arch 85*0.45 = 13.75 + 38.25
        assert calcs.venue_quality(
            None, "tokenized_spot", True, None
        ) == Decimal("52")

    def test_not_tradeable_is_gated_to_0(self):
        # the gate overrides everything, however good the other signals
        assert calcs.venue_quality(
            "DEX", "perp", False, "real_resting_orders"
        ) == Decimal("0")

    def test_unknown_architecture_is_neutral(self):
        # data 100*0.55 + arch 60*0.45 = 55 + 27
        assert calcs.venue_quality(
            None, "perp", True, "real_resting_orders"
        ) == Decimal("82")


class TestInstrumentFit:
    def test_all_100_is_100(self):
        assert calcs.instrument_fit(
            Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"),
            Decimal("100"),
        ) == Decimal("100")

    def test_weighted_sum_hand_derived(self):
        # 0.30*100 + 0.20*40 + 0.20*60 + 0.20*80 + 0.10*50
        # = 30 + 8 + 12 + 16 + 5 = 71
        assert calcs.instrument_fit(
            Decimal("100"), Decimal("40"), Decimal("60"), Decimal("80"),
            Decimal("50"),
        ) == Decimal("71")

    def test_underlying_match_carries_the_most_weight(self):
        # perfect underlying, everything else 0 -> exactly its 0.30 weight
        assert calcs.instrument_fit(
            Decimal("100"), Decimal("0"), Decimal("0"), Decimal("0"),
            Decimal("0"),
        ) == Decimal("30")

    def test_venue_quality_carries_the_least(self):
        assert calcs.instrument_fit(
            Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"),
            Decimal("100"),
        ) == Decimal("10")

    def test_weights_sum_to_one(self):
        assert sum(calcs.INSTRUMENT_FIT_WEIGHTS.values()) == Decimal("1")


class TestWeightedFitSubset:
    def test_all_five_equals_instrument_fit(self):
        scores = {
            "underlying_match": Decimal("100"),
            "liquidity": Decimal("40"),
            "carry": Decimal("60"),
            "tracking": Decimal("80"),
            "venue_quality": Decimal("50"),
        }
        assert calcs.weighted_fit_subset(scores) == calcs.instrument_fit(
            Decimal("100"), Decimal("40"), Decimal("60"), Decimal("80"),
            Decimal("50"),
        )

    def test_renormalizes_over_present_keys(self):
        # only metadata inputs present: (0.30*100 + 0.10*50) / 0.40
        scores = {
            "underlying_match": Decimal("100"),
            "venue_quality": Decimal("50"),
        }
        assert calcs.weighted_fit_subset(scores) == Decimal("87.5")

    def test_dropping_an_input_reweights_the_rest(self):
        # tracking dropped: remaining weights 0.30/0.20/0.20/0.10 sum 0.80
        # (0.30*100 + 0.20*50 + 0.20*50 + 0.10*50) / 0.80
        # = (30 + 10 + 10 + 5) / 0.80 = 55 / 0.80 = 68.75
        scores = {
            "underlying_match": Decimal("100"),
            "liquidity": Decimal("50"),
            "carry": Decimal("50"),
            "venue_quality": Decimal("50"),
        }
        assert calcs.weighted_fit_subset(scores) == Decimal("68.75")

    def test_single_input_returns_that_value(self):
        assert calcs.weighted_fit_subset(
            {"underlying_match": Decimal("42")}
        ) == Decimal("42")

    def test_empty_is_none(self):
        assert calcs.weighted_fit_subset({}) is None

    def test_ignores_unknown_keys(self):
        assert calcs.weighted_fit_subset(
            {"underlying_match": Decimal("100"), "bogus": Decimal("999")}
        ) == Decimal("100")


class TestWindowConfidence:
    def test_partial_window(self):
        # 3.5 days of history against the §7.3 7-day funding window
        assert calcs.window_confidence(Decimal("3.5"), 7) == Decimal("0.5")

    def test_full_window(self):
        assert calcs.window_confidence(Decimal("30"), 30) == Decimal("1")

    def test_history_longer_than_window_caps_at_one(self):
        # 45 days of data queried over a 30-day window: 1, never 1.5
        assert calcs.window_confidence(Decimal("45"), 30) == Decimal("1")

    def test_zero_coverage(self):
        # BydFi today: no history at all
        assert calcs.window_confidence(Decimal("0"), 7) == Decimal("0")

    def test_thin_real_history(self):
        # 2 days against the §7.4 30-day tracking window
        assert calcs.window_confidence(Decimal("2"), 30) == Decimal("2") / Decimal("30")

    def test_bad_inputs_raise(self):
        with pytest.raises(ValueError):
            calcs.window_confidence(Decimal("3"), 0)
        with pytest.raises(ValueError):
            calcs.window_confidence(Decimal("3"), -7)
        with pytest.raises(ValueError):
            calcs.window_confidence(Decimal("-1"), 7)


class TestConfidenceTier:
    def test_below_floor_is_none(self):
        # 1.5 days of a 7-day window = 0.214 -> nothing honest to say
        assert calcs.confidence_tier(Decimal("0")) is None
        assert calcs.confidence_tier(Decimal("0.2142857")) is None

    def test_low_boundary_is_inclusive(self):
        assert calcs.confidence_tier(Decimal("0.25")) == "low"

    def test_just_below_low_boundary(self):
        assert calcs.confidence_tier(Decimal("0.2499999")) is None

    def test_medium_boundary_is_inclusive(self):
        assert calcs.confidence_tier(Decimal("0.5")) == "medium"

    def test_just_below_medium_boundary(self):
        assert calcs.confidence_tier(Decimal("0.4999999")) == "low"

    def test_high_boundary_is_inclusive(self):
        assert calcs.confidence_tier(Decimal("0.8")) == "high"

    def test_just_below_high_boundary(self):
        assert calcs.confidence_tier(Decimal("0.7999999")) == "medium"

    def test_full_coverage_is_high(self):
        assert calcs.confidence_tier(Decimal("1")) == "high"

    def test_tiers_use_the_data_confidence_vocabulary(self):
        # must stay in the confidence_tier enum's vocabulary (0001_schema)
        tiers = {
            calcs.confidence_tier(Decimal(c) / Decimal(100)) for c in range(0, 101)
        }
        assert tiers == {None, "low", "medium", "high"}

    def test_composed_with_window_confidence(self):
        # 5.6 of 7 days -> 0.8 -> 'high'; 5.5 days -> 0.7857 -> 'medium'
        assert calcs.confidence_tier(
            calcs.window_confidence(Decimal("5.6"), 7)
        ) == "high"
        assert calcs.confidence_tier(
            calcs.window_confidence(Decimal("5.5"), 7)
        ) == "medium"
