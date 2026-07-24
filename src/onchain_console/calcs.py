"""Deterministic market calculations (blueprint §7).

Hard rule: every number that feeds a decision is computed here, in code,
from venue data — never estimated by an LLM.

This module currently covers the snapshot-level metrics (§7.1, §7.2, plus
funding normalization and the liquidity raw inputs). The hold-period funding
projections, tracking-error aggregates, and 0-100 scores (§7.3–§7.9) belong
to the scoring engine (build step 3) and will live alongside these.

All math uses Decimal: venue APIs return prices as strings, and Decimal
round-trips cleanly into PostgreSQL numeric columns.
"""

import statistics
from decimal import Decimal

MINUTES_PER_YEAR = Decimal(365 * 24 * 60)
MINUTES_PER_DAY = Decimal(1440)


def premium_pct(mark_price: Decimal, oracle_price: Decimal) -> Decimal:
    """§7.1: (mark - oracle) / oracle.

    Positive → longs likely pay funding; negative → shorts likely pay.
    """
    if oracle_price == 0:
        raise ValueError("oracle_price must be non-zero")
    return (mark_price - oracle_price) / oracle_price


def funding_payment(
    position_size: Decimal, oracle_price: Decimal, funding_rate_interval: Decimal
) -> Decimal:
    """§7.2: single-interval funding payment.

    Hyperliquid converts size to notional using the ORACLE price, not mark.
    """
    return position_size * oracle_price * funding_rate_interval


def funding_rate_8h_equiv(
    funding_rate_interval: Decimal, funding_interval_minutes: int
) -> Decimal:
    """Normalize a per-interval funding rate to its 8-hour equivalent
    (simple scaling; Hyperliquid hourly rate × 8)."""
    if funding_interval_minutes <= 0:
        raise ValueError("funding_interval_minutes must be positive")
    return funding_rate_interval * Decimal(480) / Decimal(funding_interval_minutes)


def funding_apr_est(
    funding_rate_interval: Decimal, funding_interval_minutes: int
) -> Decimal:
    """Annualized funding rate — context only, never a decision number (§7.2)."""
    if funding_interval_minutes <= 0:
        raise ValueError("funding_interval_minutes must be positive")
    return (
        funding_rate_interval * MINUTES_PER_YEAR / Decimal(funding_interval_minutes)
    )


def open_interest_usd(open_interest: Decimal, mark_price: Decimal) -> Decimal:
    """§5.5: openInterest (base units) × price. Mark price is used for the
    USD notional of standing OI; funding math uses oracle (§7.2)."""
    return open_interest * mark_price


def spread_bps_est(
    impact_bid_price: Decimal, impact_ask_price: Decimal, mid_price: Decimal
) -> Decimal:
    """Estimated spread in bps from Hyperliquid impact prices."""
    if mid_price == 0:
        raise ValueError("mid_price must be non-zero")
    return Decimal(10000) * (impact_ask_price - impact_bid_price) / mid_price


def impact_slippage_bps(
    impact_bid_price: Decimal, impact_ask_price: Decimal, mid_price: Decimal
) -> Decimal:
    """One-way execution slippage in bps from Hyperliquid impact prices.

    §7.5 lists BOTH "estimated spread" and "impact-price slippage from
    impactPxs" as liquidity inputs. They are computed from the same two
    impact prices, so they are related but not identical:

        spread_bps_est   = full round-trip width, (ask - bid) / mid
        impact_slippage  = one-way cost, the mean of each side's distance
                           from mid: ((ask - mid) + (mid - bid)) / 2 / mid

    For a book symmetric about mid the second is exactly half the first, so
    on symmetric depth the two inputs ARE collinear — a fact the §7.5
    weighting must account for (see LIQUIDITY_WEIGHTS) rather than
    double-counting execution cost. When impact prices sit asymmetrically
    around mid (one-sided depth) they diverge, and the slippage figure is
    the one that reflects what a taker actually pays on the worse side of
    that asymmetry. Lower is better.
    """
    if mid_price == 0:
        raise ValueError("mid_price must be non-zero")
    ask_side = abs(impact_ask_price - mid_price)
    bid_side = abs(mid_price - impact_bid_price)
    return Decimal(10000) * (ask_side + bid_side) / Decimal(2) / mid_price


# ---------------------------------------------------------------------------
# §7.3 Hold-period funding — the swing-trade metric that matters most.
# ---------------------------------------------------------------------------

# statistics.quantiles method for the §7.3 stress percentile. FIRST PASS,
# flagged for Caleb's review — the blueprint says "90th-percentile adverse
# funding" without naming a method, and the two stdlib methods disagree
# sharply on the thin history this project actually has:
#
#   observations [0.00001, 0.00003]  (max ever seen: 0.00003)
#     method='exclusive' (stdlib default) -> 0.000044   ABOVE anything observed
#     method='inclusive'                  -> 0.000028
#
# 'exclusive' estimates a population percentile and extrapolates past the
# sample range; on a 2-point window it reports a stress funding rate that
# has never occurred. 'inclusive' treats the window as the population it
# literally is — the observed window — and is bounded by real observations.
# Chosen deliberately: a stress number invented above every rate ever seen
# is the precise-looking-figure-from-thin-data failure this build step
# exists to prevent. The trade-off is that on thin history 'inclusive'
# understates the tail, which is why the coverage tier travels with it.
STRESS_QUANTILE_METHOD = "inclusive"


def funding_intervals(funding_interval_minutes: int, hold_days: int) -> Decimal:
    """§7.3 `n`: how many funding charges a position takes over `hold_days`.

    The blueprint writes n = hold_days × 24 because it assumes Hyperliquid's
    hourly cadence. Generalized here so each instrument uses its OWN
    interval — Hyperliquid 60min -> 24/day, BydFi 240min -> 6/day. Holding
    the same position for the same days on BydFi therefore takes a quarter
    as many funding charges as on Hyperliquid, which is a real difference
    in carry, not a rounding detail.
    """
    if funding_interval_minutes <= 0:
        raise ValueError("funding_interval_minutes must be positive")
    if hold_days <= 0:
        raise ValueError("hold_days must be positive")
    return Decimal(hold_days) * MINUTES_PER_DAY / Decimal(funding_interval_minutes)


def hold_period_funding_estimate(
    current_funding_rate: Decimal,
    funding_rate_history: list[Decimal],
    funding_interval_minutes: int,
    hold_days: int,
) -> dict:
    """§7.3: base / optimistic / stress funding cost over a hold period.

    Returns cost PER UNIT OF NOTIONAL (a dimensionless total funding rate
    for the whole hold). Multiply by position notional for dollars — see
    funding_payment() for the single-interval notional form.

    * base       = current funding × n
    * optimistic = mean of `funding_rate_history` × n
    * stress     = 90th-percentile adverse funding × n

    `funding_rate_history` is whatever window the caller actually has; the
    mean is taken over exactly what is passed in. An empty list yields
    None for optimistic and stress (never a divide-by-zero, never a
    fabricated number) while base — which needs no history — still
    computes. A single observation yields that observation as the stress
    percentile, since statistics.quantiles requires two points and the
    P90 of a one-point sample is that point.

    SIGN CONVENTION — this signature carries no direction, so every figure
    is from the LONG's perspective, matching §7.2 (positive rate = longs
    pay). "Adverse" is therefore the upper tail. A short reads the same
    numbers with the sign flipped, but its adverse tail is the 10th
    percentile, which is NOT -P90 of the same sample. §7.6's carry score
    scores both directions, so it will need that decision made explicitly
    rather than inheriting this one.
    """
    n = funding_intervals(funding_interval_minutes, hold_days)
    base = current_funding_rate * n

    if not funding_rate_history:
        return {"base": base, "optimistic": None, "stress": None}

    mean_rate = sum(funding_rate_history) / Decimal(len(funding_rate_history))

    if len(funding_rate_history) == 1:
        stress_rate = funding_rate_history[0]
    else:
        stress_rate = statistics.quantiles(
            funding_rate_history, n=10, method=STRESS_QUANTILE_METHOD
        )[8]  # 9 cut points; index 8 is the 90th percentile

    return {"base": base, "optimistic": mean_rate * n, "stress": stress_rate * n}


# ---------------------------------------------------------------------------
# §7.4 Tracking error — how far the venue's mark drifts from real-world spot.
# ---------------------------------------------------------------------------


def tracking_error_bps(mark_price: Decimal, reference_spot_price: Decimal) -> Decimal:
    """§7.4: 10000 × (mark - reference_spot) / reference_spot.

    Signed: positive means the venue marks ABOVE real-world spot. Callers
    that want magnitude take abs() — tracking_error_stats() does.

    This is the value that finally populates market_snapshots.
    tracking_error_bps, NULL since 0001 because no reference series
    existed until spot_prices (0008).

    Whether a nonzero result means anything depends on the commodity: for
    metals the reference prices the same thing the perp tracks, but for
    energy there is a structural basis gap (CLAUDE.md 2026-07-12). That
    branch is applied by the caller in scoring.py — this function is
    arithmetic and knows nothing about it.
    """
    if reference_spot_price == 0:
        raise ValueError("reference_spot_price must be non-zero")
    return Decimal(10000) * (mark_price - reference_spot_price) / reference_spot_price


def tracking_error_stats(paired_series: list[tuple[Decimal, Decimal]]) -> dict:
    """§7.4 aggregates over (mark_price, matching_spot_price) pairs.

    The caller aligns the pairs and controls the window — this function
    just reduces whatever it is given. Returns both aggregate shapes the
    blueprint asks for:

        avg_abs_7d  mean of |tracking_error_bps| over the pairs given
        max_abs_30d max  of |tracking_error_bps| over the pairs given

    The key names carry the blueprint's window vocabulary, but nothing
    here enforces a window: pass a 7-day slice and read avg_abs_7d, pass
    a 30-day slice and read max_abs_30d. scoring.compute_tracking_error
    calls it twice, once per window, for exactly that reason.

    Absolute values are taken per pair BEFORE averaging, so a +40bps day
    and a -40bps day average to 40, not 0. Drift in either direction is
    equally bad for an instrument meant to track spot.

    An empty list yields None for both, never a divide-by-zero.
    """
    if not paired_series:
        return {"avg_abs_7d": None, "max_abs_30d": None}

    abs_errors = [
        abs(tracking_error_bps(mark, spot)) for mark, spot in paired_series
    ]
    return {
        "avg_abs_7d": sum(abs_errors) / Decimal(len(abs_errors)),
        "max_abs_30d": max(abs_errors),
    }


# ---------------------------------------------------------------------------
# §7.5 Liquidity proxy score (0–100).
# ---------------------------------------------------------------------------

# §7.5 names four inputs — day volume (↑ better), open interest (↑ better),
# estimated spread (↓ better), impact-price slippage (↓ better) — but does
# NOT give sub-weights. This is a reasoned FIRST PASS, flagged for Caleb's
# review, not a settled calibration.
#
# Reasoning:
#   * The four inputs split into two families: how much size the market can
#     absorb (volume, OI) and what it costs to get in and out (spread,
#     slippage). I give the size family 0.60 and the cost family 0.40.
#     For a swing trade held days-to-weeks, being able to enter and exit in
#     size without moving the market matters more than shaving a few bps off
#     a one-time entry cost — the position is not being churned.
#   * Within size, day_volume 0.35 > open_interest 0.25. Turnover is the
#     harder-to-game, more direct measure of how much can actually change
#     hands over a hold; OI is standing conviction and can be large in a
#     market that barely trades, so it supports volume rather than leads.
#   * Within cost, spread 0.25 > impact_slippage 0.15. Both derive from the
#     same impact prices and are collinear on symmetric depth (see
#     impact_slippage_bps), so weighting them equally would double-count
#     execution cost. Spread leads as the round-trip figure; slippage gets
#     the smaller weight and earns it only when depth is asymmetric, where
#     it carries information spread alone does not.
#
# Weights sum to 1.0. Directions live alongside so normalization can invert
# the "lower is better" fields.
LIQUIDITY_WEIGHTS = {
    "day_volume_usd": Decimal("0.35"),
    "open_interest_usd": Decimal("0.25"),
    "spread_bps_est": Decimal("0.25"),
    "impact_slippage_bps": Decimal("0.15"),
}

# True = higher raw value is better (normalize directly); False = lower is
# better (invert during normalization).
LIQUIDITY_HIGHER_IS_BETTER = {
    "day_volume_usd": True,
    "open_interest_usd": True,
    "spread_bps_est": False,
    "impact_slippage_bps": False,
}

# Score given to a field when the universe min == max (every instrument
# ties, so the field cannot discriminate) or the instrument's own value is
# missing. Neutral midpoint — neither rewards nor penalizes.
NEUTRAL_FIELD_SCORE = Decimal("50")


def _normalize_field(
    value: Decimal | None,
    field_min: Decimal,
    field_max: Decimal,
    higher_is_better: bool,
) -> Decimal:
    """Min-max a single field onto 0–100, inverting ↓-better fields.

    Degenerate cases collapse to NEUTRAL_FIELD_SCORE: a missing value
    (nothing to place) and min == max (the field ties across the whole
    universe, so it carries no discriminating information — scoring it 0 or
    100 would be an artifact of the tie, not a real ranking).
    """
    if value is None or field_max == field_min:
        return NEUTRAL_FIELD_SCORE
    fraction = (value - field_min) / (field_max - field_min)
    if not higher_is_better:
        fraction = Decimal(1) - fraction
    scaled = fraction * Decimal(100)
    # clamp: a target outside the universe range (e.g. an instrument not in
    # the active-perp universe scored against it) must not exceed 0–100.
    return min(Decimal(100), max(Decimal(0), scaled))


def liquidity_score(
    day_volume_usd: Decimal | None,
    open_interest_usd: Decimal | None,
    spread_bps_est: Decimal | None,
    impact_slippage_bps: Decimal | None,
    universe_stats: dict,
) -> Decimal:
    """§7.5 composite liquidity proxy, 0–100 (higher = more liquid).

    Each input is min-max normalized against the active instrument universe
    — `universe_stats[field] = {"min": Decimal, "max": Decimal}` — then
    weight-averaged per LIQUIDITY_WEIGHTS. The ↓-better fields (spread,
    slippage) are inverted so that lower raw cost yields a higher score.

    Degenerate handling (see _normalize_field): a field whose universe
    min == max, or whose value for this instrument is None, scores the
    neutral midpoint (50) instead of dividing by zero or fabricating a rank.
    Because the weights sum to 1, an all-neutral instrument scores exactly
    50 — a legible "no signal" rather than a spurious extreme.

    This function is pure arithmetic and knows nothing about provenance or
    confidence. The CLAUDE.md 2026-07-14 synthetic-simulation cap is applied
    by the caller (scoring.compute_liquidity_score), because it is a
    property of where the DATA came from, not of the score's value.
    """
    values = {
        "day_volume_usd": day_volume_usd,
        "open_interest_usd": open_interest_usd,
        "spread_bps_est": spread_bps_est,
        "impact_slippage_bps": impact_slippage_bps,
    }
    total = Decimal(0)
    for field, weight in LIQUIDITY_WEIGHTS.items():
        stats = universe_stats.get(field) or {}
        normalized = _normalize_field(
            values[field],
            stats.get("min", values[field]),
            stats.get("max", values[field]),
            LIQUIDITY_HIGHER_IS_BETTER[field],
        )
        total += weight * normalized
    return total


# ---------------------------------------------------------------------------
# §7.6 Carry score (0–100).
#
# §7.6 is qualitative: "Longs: lower/negative expected hold-period funding
# cost -> higher score. Shorts: positive funding receipts -> higher score.
# Penalize unstable funding (high 7-day funding volatility) and inconsistent
# funding direction." No formula. The concrete definition below is a FIRST
# PASS, flagged for Caleb's review.
#
# Shape of the definition:
#   1. A directional LEVEL maps mean funding to a 0–100 base score. Positive
#      funding = longs pay (§7.2), so a long's carry benefit is -mean and a
#      short's is +mean. That benefit is scaled against a reference rate and
#      linearly mapped: 0 funding -> 50, a full favorable reference -> 100,
#      a full adverse reference -> 0.
#   2. Two RELIABILITY penalties then shrink the score toward the neutral
#      midpoint (50), never past it: a stability penalty from the funding
#      standard deviation, and a direction penalty from the sign-flip rate.
#
# Why shrink toward 50 rather than subtract points: instability and
# direction flips make the carry UNRELIABLE, not simply bad. Shrinking to
# neutral says "we can't count on this carry" symmetrically — it discounts a
# great-looking carry that is actually jumpy, and equally stops trusting a
# terrible-looking one that keeps flipping. Subtracting points instead would
# treat volatility as pure badness and could only ever push scores down,
# which misreads a volatile-but-mean-neutral market as bad carry. The
# alternative (volatility-as-cost) is a legitimate other reading — flagged
# for review.
#
# ALL RATES ARE PER FUNDING INTERVAL (the funding_rate_interval column the
# §7.6 orchestration pulls). The pure function never sees the interval
# length, so the reference scale is anchored to Hyperliquid's hourly cadence
# — the only venue with funding history today. A 4h-cadence BydFi rate of
# the same APR is 4x larger per interval and would score more extreme; when
# BydFi funding history exists, normalize to an 8h-equivalent before scoring
# (or make the reference interval-aware). Documented limitation, not a bug.
# ---------------------------------------------------------------------------

# Per-interval favorable-carry magnitude that maps to a maximal base score.
# 0.00005/hr ≈ 43.8% APR — a strong but not absurd hourly carry, chosen so
# the observed Hyperliquid range (gold ~6e-6, copper ~3e-5, brent ~2e-4)
# spreads across the 0–100 band rather than saturating at the extremes.
CARRY_REFERENCE_RATE = Decimal("0.00005")

# Per-interval funding standard deviation at which the stability penalty
# saturates to 1 (funding this jumpy carries no dependable signal). Set
# equal to CARRY_REFERENCE_RATE: swings rivaling a full strong-carry
# magnitude mean the carry cannot be relied on at all.
CARRY_STABILITY_REFERENCE = Decimal("0.00005")

CARRY_NEUTRAL_SCORE = Decimal("50")


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(high, max(low, value))


def funding_direction_penalty(funding_rate_history: list[Decimal]) -> Decimal:
    """0–1 inconsistency of funding DIRECTION over the window.

    Fraction of consecutive observation pairs whose funding sign flips:
    sign_changes / (n - 1). 0 = funding never changed sign (a dependable
    carry direction); 1 = it flipped at every step (no dependable direction
    to price a carry on).

    A pair counts as a flip only when the two rates are strictly opposite in
    sign (a * b < 0); an exact-zero funding reading has no sign and never
    counts as a flip. Fewer than two observations -> 0: nothing to assess,
    and the caller expresses that absence as low confidence, not as a
    fabricated penalty.
    """
    if len(funding_rate_history) < 2:
        return Decimal(0)
    flips = sum(
        1
        for a, b in zip(funding_rate_history, funding_rate_history[1:])
        if a * b < 0
    )
    return Decimal(flips) / Decimal(len(funding_rate_history) - 1)


def funding_stability_penalty(funding_rate_history: list[Decimal]) -> Decimal:
    """0–1 instability of funding LEVEL over the window.

    Population standard deviation of the funding rates divided by
    CARRY_STABILITY_REFERENCE, clamped to [0, 1]. 0 = perfectly steady
    funding; 1 = swings at least as large as a full strong-carry magnitude.

    Population (not sample) standard deviation, treating the window as the
    observed population it literally is — the same stance §7.3 takes with
    its inclusive quantiles. Fewer than two observations -> 0 (no fabricated
    penalty from a single point).
    """
    if len(funding_rate_history) < 2:
        return Decimal(0)
    stdev = statistics.pstdev(funding_rate_history)
    return _clamp(stdev / CARRY_STABILITY_REFERENCE, Decimal(0), Decimal(1))


def carry_score(funding_rate_history: list[Decimal], direction: str) -> Decimal:
    """§7.6 carry score, 0–100 (higher = more favorable, more reliable carry).

    `direction` is 'long' or 'short'. The mean funding over the window is
    turned into a directional carry benefit (long: -mean; short: +mean),
    scaled against CARRY_REFERENCE_RATE and mapped linearly to a base score
    (0 funding -> 50, full favorable -> 100, full adverse -> 0). The
    stability and direction penalties then shrink that base toward 50 via a
    combined reliability factor (1 - stability)(1 - direction), so either
    kind of unreliability pulls the score toward neutral and both together
    pull it further — but never past 50.

    History handling, per the "say it via confidence, not a fabricated
    number" rule:
      * empty      -> 50, an explicit neutral no-information default. The
                      §7.6 orchestration never surfaces this; it returns an
                      insufficient result on zero history. Reachable only by
                      calling this function directly.
      * one value  -> base level from that single rate, with NO penalty:
                      stability and direction are unassessable from one
                      point, so this refuses to invent instability. The
                      orchestration still treats one point as insufficient
                      (zero span), so the caller's confidence, not this
                      score, carries the "can't assess" signal.
    """
    if direction not in ("long", "short"):
        raise ValueError("direction must be 'long' or 'short'")
    if not funding_rate_history:
        return CARRY_NEUTRAL_SCORE

    mean_rate = sum(funding_rate_history) / Decimal(len(funding_rate_history))
    benefit_rate = -mean_rate if direction == "long" else mean_rate
    signal = _clamp(benefit_rate / CARRY_REFERENCE_RATE, Decimal(-1), Decimal(1))
    base_score = CARRY_NEUTRAL_SCORE + Decimal(50) * signal

    reliability = (Decimal(1) - funding_stability_penalty(funding_rate_history)) * (
        Decimal(1) - funding_direction_penalty(funding_rate_history)
    )
    return CARRY_NEUTRAL_SCORE + (base_score - CARRY_NEUTRAL_SCORE) * reliability


# ---------------------------------------------------------------------------
# §7.7 Instrument-fit composite (0–100).
#   instrument_fit = 0.30·underlying_match + 0.20·liquidity + 0.20·carry
#                    + 0.20·tracking + 0.10·venue_quality
#
# liquidity (§7.5), carry (§7.6) and tracking (§7.4, via tracking_fidelity_
# score below) come from the earlier sub-scores. underlying_match and
# venue_quality are NOT defined anywhere in the blueprint — the definitions
# below are concrete FIRST PASSES and the two pieces most in need of review.
# ---------------------------------------------------------------------------

INSTRUMENT_FIT_WEIGHTS = {
    "underlying_match": Decimal("0.30"),
    "liquidity": Decimal("0.20"),
    "carry": Decimal("0.20"),
    "tracking": Decimal("0.20"),
    "venue_quality": Decimal("0.10"),
}


# ---- tracking → 0–100 fidelity -------------------------------------------
# §7.4 produces a tracking error in bps; §7.7 needs it as a 0–100 "higher is
# better" input. Absolute average drift maps linearly to a fidelity score,
# 0 bps -> 100, TRACKING_REFERENCE_BPS or worse -> 0.
#
# TRACKING_REFERENCE_BPS is a FIRST PASS. It applies to METALS only: their
# reference prices the same thing the perp tracks, so tight tracking is
# expected and 200 bps (2%) of persistent average drift is already poor. For
# ENERGY the reference has a structural basis gap (CLAUDE.md 2026-07-12) and
# the raw bps is NOT a clean tracking signal — the orchestration substitutes
# a neutral value there rather than feeding thousands of bps of basis into
# this function.
TRACKING_REFERENCE_BPS = Decimal(200)


def tracking_fidelity_score(abs_tracking_bps: Decimal) -> Decimal:
    """Map absolute tracking error (bps) to a 0–100 fidelity score."""
    if abs_tracking_bps < 0:
        raise ValueError("abs_tracking_bps must be non-negative")
    return Decimal(100) * (
        Decimal(1) - _clamp(abs_tracking_bps / TRACKING_REFERENCE_BPS, Decimal(0), Decimal(1))
    )


# ---- underlying_match (FIRST PASS — needs review) ------------------------
# How well an instrument's underlying expresses a THESIS's commodity. The
# vocabulary is shared across theses/rules/instruments (CLAUDE.md), so an
# exact code match is the primary, unambiguous signal. Beyond exact match,
# some commodities are genuine substitutes for expressing the same macro
# view, with basis risk that grows as the substitution loosens:
#
#   * WTI vs Brent crude — near-perfect substitutes (grade/location basis
#     only); a crude thesis is well expressed by either -> 75.
#   * within the precious-metals complex (gold/silver/platinum/palladium) —
#     they co-move on shared safe-haven / real-rate / USD drivers, but each
#     has its own supply-demand (silver and the PGMs are half-industrial),
#     so expressing a gold thesis via silver carries real basis -> 50.
#   * anything else (industrial copper, natural gas, or across families) —
#     not a defensible substitute for a different commodity's thesis -> 0.
#
# NOTE: deliberately NOT reusing basis_category (energy/metal) here — it
# lumps industrial copper with precious gold, which are poor mutual
# substitutes. These groups are about price co-movement, a different axis.
CRUDE_GRADES = frozenset({"wti_crude_oil", "brent_crude_oil"})
PRECIOUS_METALS = frozenset({"gold", "silver", "platinum", "palladium"})


def underlying_match(instrument_underlying: str, thesis_commodity_code: str) -> Decimal:
    """§7.7 underlying_match sub-score, 0–100 (FIRST PASS — see notes above)."""
    if instrument_underlying == thesis_commodity_code:
        return Decimal(100)
    pair = {instrument_underlying, thesis_commodity_code}
    if pair <= CRUDE_GRADES:
        return Decimal(75)
    if pair <= PRECIOUS_METALS:
        return Decimal(50)
    return Decimal(0)


# ---- venue_quality (FIRST PASS — needs review) ---------------------------
# Trustworthiness of the venue as a place to actually express the trade AND
# assess it. Built from the three signals with real footing, each reasoned
# through individually:
#
#  1. tradeable — a GATE, not a gradient. M7 only generates candidates for
#     tradeable=true instruments; tradeable=false means no trade can be
#     expressed on that venue at all, which is disqualifying rather than a
#     small deduction. So tradeable=false -> 0; tradeable=true adds nothing
#     further. A multiplier is the honest reading of the flag.
#  2. depth-data availability — the clearest venue-assessability signal, and
#     it maps straight onto the 0013 / 2026-07-14 provenance hierarchy: real
#     resting-order depth we can actually see (Hyperliquid l2Book) > venue
#     risk-config tiers (BydFi) > synthetic simulation (Ostium) > nothing.
#     100 / 65 / 40 / 25.
#  3. venue architecture — this is an on-chain-FIRST console, so on-chain
#     venues are the native, custody-and-transparency-first choice. A DEX
#     (on-chain order book / oracle-vault) is fully on-chain; a tokenized-
#     spot ERC-20 (PAXG/XAUT, venue_type NULL) is custody-free but a plain
#     spot token with no venue market structure; a CEX (BydFi) reintroduces
#     custody + counterparty + opaque internal matching. 100 / 85 / 70.
#
# Combine depth-data (0.55, concrete per-instrument evidence) with
# architecture (0.45, a venue-level prior), then multiply by the tradeable
# gate. Oracle transparency was considered and left out of this first pass to
# keep the score anchored on the three factors above.
VENUE_DATA_QUALITY = {
    "real_resting_orders": Decimal(100),
    "venue_risk_config": Decimal(65),
    "synthetic_simulation": Decimal(40),
}
VENUE_DATA_QUALITY_NONE = Decimal(25)
VENUE_DATA_WEIGHT = Decimal("0.55")
VENUE_ARCH_WEIGHT = Decimal("0.45")


def venue_quality(
    venue_type: str | None,
    instrument_type: str,
    tradeable: bool,
    best_provenance: str | None,
) -> Decimal:
    """§7.7 venue_quality sub-score, 0–100 (FIRST PASS — see notes above).

    `best_provenance` is the strongest liquidity_profiles provenance that
    exists for this instrument (or None), which the caller derives from the
    §7.5 result.
    """
    if not tradeable:
        return Decimal(0)  # gate: cannot express a trade here

    data = VENUE_DATA_QUALITY.get(best_provenance, VENUE_DATA_QUALITY_NONE)

    if venue_type == "DEX":
        arch = Decimal(100)
    elif venue_type == "CEX":
        arch = Decimal(70)
    elif instrument_type == "tokenized_spot":
        arch = Decimal(85)  # custody-free token, venue_type deliberately NULL
    else:
        arch = Decimal(60)  # unknown architecture -> neutral prior

    return data * VENUE_DATA_WEIGHT + arch * VENUE_ARCH_WEIGHT


def instrument_fit(
    underlying_match: Decimal,
    liquidity: Decimal,
    carry: Decimal,
    tracking: Decimal,
    venue_quality: Decimal,
) -> Decimal:
    """§7.7 exact weighted sum, all five inputs on a 0–100 scale.

    This is the blueprint formula verbatim, assuming every input is present.
    When some sub-scores are insufficient the orchestration uses
    weighted_fit_subset() instead, which renormalizes over what is known.
    """
    w = INSTRUMENT_FIT_WEIGHTS
    return (
        w["underlying_match"] * underlying_match
        + w["liquidity"] * liquidity
        + w["carry"] * carry
        + w["tracking"] * tracking
        + w["venue_quality"] * venue_quality
    )


def weighted_fit_subset(scores: dict) -> Decimal | None:
    """Renormalized §7.7 weighted average over whichever INSTRUMENT_FIT_WEIGHTS
    keys appear in `scores`.

    Used when one or more sub-scores are insufficient: dropping the missing
    input and renormalizing the remaining weights preserves the RELATIVE
    importance of what is actually known, and keeps the result on 0–100.
    With all five keys present it equals instrument_fit(). Returns None for
    an empty dict (nothing to compose).
    """
    keys = [k for k in scores if k in INSTRUMENT_FIT_WEIGHTS]
    total_weight = sum(INSTRUMENT_FIT_WEIGHTS[k] for k in keys)
    if total_weight == 0:
        return None
    return sum(INSTRUMENT_FIT_WEIGHTS[k] * scores[k] for k in keys) / total_weight


# ---------------------------------------------------------------------------
# Lookback-window coverage — how much of a §7 window is actually backed by
# real history. Pure functions; the DB side lives in history.py.
# ---------------------------------------------------------------------------

# confidence_tier() thresholds — FIRST PASS, flagged for Caleb's review.
WINDOW_CONFIDENCE_LOW_FLOOR = Decimal("0.25")
WINDOW_CONFIDENCE_MEDIUM_FLOOR = Decimal("0.5")
WINDOW_CONFIDENCE_HIGH_FLOOR = Decimal("0.8")


def window_confidence(days_covered: Decimal, window_days: int) -> Decimal:
    """Fraction of a requested lookback window that real history covers.

    min(days_covered / window_days, 1) — capped at 1 so history longer than
    the window (or clock skew at the boundary) can't report >100% coverage.
    """
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    if days_covered < 0:
        raise ValueError("days_covered must be non-negative")
    return min(days_covered / Decimal(window_days), Decimal(1))


def confidence_tier(window_confidence: Decimal) -> str | None:
    """Map a 0–1 coverage ratio to the low/medium/high vocabulary — or None.

    Thresholds (inclusive lower bound):
        < 0.25        -> None      genuinely insufficient; say nothing
        0.25 – <0.5   -> 'low'
        0.5  – <0.8   -> 'medium'
        >= 0.8        -> 'high'

    Reasoning, stated so it can be argued with — this is a FIRST PASS, not
    a settled calibration:

    * None below 0.25 is the point of the whole utility. On the §7.3 7-day
      funding window that floor is under two days; on the §7.4 30-day
      tracking window it is 7.5 days. Neither can honestly be labelled a
      7-day average or a 30-day maximum, and a 'low'-tagged number still
      gets read, plotted, and compared. None means the caller should emit
      NULL, not a hedged figure.
    * 0.8 for 'high' rather than 1.0 because a full window is rarely
      literally achieved: capture jobs miss runs, EIA publishes T-2..T-6,
      and requiring 1.0 would leave everything permanently at 'medium',
      which destroys the tier's information. 0.8 of a 7-day window is 5.6
      days — most of a week including a weekend funding cycle.
    * 0.5 as the low/medium line is the plain "more than half the window
      is real" reading.

    These map onto the data_confidence enum (low/medium/high), so a §7
    caller can hand the result straight through — but coverage is only one
    input to data_confidence. Provenance caps still apply on top: per the
    2026-07-14 decision, a §7.5 input with provenance='synthetic_simulation'
    caps data_confidence at 'medium' no matter how complete its window is.
    """
    if window_confidence < WINDOW_CONFIDENCE_LOW_FLOOR:
        return None
    if window_confidence < WINDOW_CONFIDENCE_MEDIUM_FLOOR:
        return "low"
    if window_confidence < WINDOW_CONFIDENCE_HIGH_FLOOR:
        return "medium"
    return "high"


# ---------------------------------------------------------------------------
# Reference-price basis categorization — CLAUDE.md design decision
# 2026-07-12, binding on §7.4. Transcribed from that entry, not re-derived.
#
# EIA daily spot is a PHYSICAL/FOB price with a structural basis gap to the
# front-month futures that perp oracles follow, and it publishes T-2..T-6
# late. Metals.Dev spot is near-real-time (<=60s) and prices the same thing
# the metal perps track, so it has no such gap.
#
# The decision: keep EIA as the energy reference for now and handle the gap
# with an explicit confidence downgrade plus staleness-awareness — never
# treat an energy spot-vs-mark gap as a clean tracking-error signal.
#
# Caleb is separately researching a real futures-price feed. Do NOT build
# toward one until he decides.
# ---------------------------------------------------------------------------

# Commodities with the known basis gap (verbatim from the 2026-07-12 entry).
ENERGY_BASIS_GAP_COMMODITIES = frozenset(
    {"wti_crude_oil", "brent_crude_oil", "natural_gas"}
)

# Commodities with NO such gap (verbatim from the same entry).
METALS_NO_BASIS_GAP_COMMODITIES = frozenset(
    {"gold", "silver", "platinum", "palladium", "copper_spot"}
)

TIER_LADDER = (None, "low", "medium", "high")


def basis_category(commodity_code: str) -> str:
    """'energy' | 'metal' | 'unknown' for the §7.4 branch.

    'unknown' is returned for any commodity the 2026-07-12 entry did not
    categorize. It is treated like energy downstream (downgraded), because
    an uncategorized reference series has an UNVERIFIED relationship to
    what the perp tracks, and the rule's whole point is not to present an
    unverified gap as a clean signal. Adding a commodity to neither set is
    therefore safe by default but should be resolved deliberately.
    """
    if commodity_code in ENERGY_BASIS_GAP_COMMODITIES:
        return "energy"
    if commodity_code in METALS_NO_BASIS_GAP_COMMODITIES:
        return "metal"
    return "unknown"


def downgrade_tier(tier: str | None) -> str | None:
    """One step down the low/medium/high ladder; 'low' falls off to None.

    'high' -> 'medium' -> 'low' -> None -> None.
    """
    if tier is None:
        return None
    index = TIER_LADDER.index(tier)
    return TIER_LADDER[index - 1]


def cap_tier(tier: str | None, ceiling: str | None) -> str | None:
    """The lower of `tier` and `ceiling` on the None<low<medium<high ladder.

    A ceiling, unlike downgrade_tier's single step, clamps to an absolute
    maximum however far above it the tier sits: cap_tier('high', 'medium')
    is 'medium', and so is cap_tier('medium', 'medium'). This is the shape
    the CLAUDE.md 2026-07-14 rule needs — a synthetic-simulation input caps
    data_confidence at 'medium' outright, it does not merely nudge it down.
    A tier already at or below the ceiling is unchanged; None (insufficient)
    stays None.
    """
    return min(tier, ceiling, key=TIER_LADDER.index)
