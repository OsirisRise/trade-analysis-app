"""Scoring engine composition layer (blueprint §7.3–§7.7, build step 3).

Why this is its own module rather than more of calcs.py: the project keeps
a deliberate three-layer split, and these functions are the only ones that
span it.

    history.py  reads the DB, decides how much real history exists
    calcs.py    pure deterministic math, NO DB access (its own docstring
                and the §7.1/§7.2 functions commit it to that discipline)
    scoring.py  composes the two — takes a connection, fetches windows,
                calls the pure functions, and attaches the confidence that
                travels with every number

Putting a `conn` parameter into calcs.py would break the property that
makes its math trivially testable, so the composition lives here instead.

Every result returned from this module carries its coverage confidence.
A number without a tier attached is not a result this module produces.

Read-only: SELECTs and arithmetic. No execution capability of any kind.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import psycopg

from onchain_console import calcs
from onchain_console.discrepancy import ACTIVE_PERP_PREDICATE
from onchain_console.history import (
    HistorySeries,
    instrument_history_series,
    span_days,
    timedelta_days,
    underlying_spot_series,
)
from onchain_console.snapshot_service import Instrument

# §7.3 says "7-day avg funding" for the optimistic leg; the stress
# percentile is drawn from the same window.
FUNDING_WINDOW_DAYS = 7

# §7.4 asks for a 7-day average absolute and a 30-day maximum absolute, so
# the wider window bounds the query and the narrower one slices it.
TRACKING_WINDOW_DAYS = 30
TRACKING_AVG_WINDOW_DAYS = 7


def _funding_result(
    *,
    hold_days: int,
    base: Decimal | None = None,
    optimistic: Decimal | None = None,
    stress: Decimal | None = None,
    current_funding_rate: Decimal | None = None,
    intervals: Decimal | None = None,
    days_covered: Decimal = Decimal(0),
    window_confidence: Decimal = Decimal(0),
    confidence_tier: str | None = None,
    observations: int = 0,
    reason: str | None = None,
) -> dict:
    """One result shape for every path, so callers never branch on which
    keys exist — only on whether the values are None."""
    return {
        # §7.3 field names, as they will be stored on the candidate
        "funding_cost_base": base,
        "funding_cost_optimistic": optimistic,
        "funding_cost_stress": stress,
        # inputs, so a surprising figure can be traced without a re-query
        "current_funding_rate": current_funding_rate,
        "funding_intervals": intervals,
        "hold_days": hold_days,
        # how much of the window is real (prompt 1)
        "window_days": FUNDING_WINDOW_DAYS,
        "days_covered": days_covered,
        "window_confidence": window_confidence,
        "confidence_tier": confidence_tier,
        "observations": observations,
        # set only when the three costs are None, naming which case it was
        "reason": reason,
    }


def compute_hold_period_funding(
    conn: psycopg.Connection, instrument: Instrument, hold_days: int
) -> dict:
    """§7.3 hold-period funding for one instrument, with coverage confidence.

    Reads the instrument's own 7-day funding history, measures how much of
    that window is genuinely covered, and returns the three §7.3 costs with
    the resulting tier attached. The instrument's own
    `funding_interval_minutes` sets `n`, so Hyperliquid and BydFi are
    handled by the same code path.

    Returns an all-None result (with `reason` naming the case) rather than
    a fabricated number when:

    * `funding_interval_minutes` is NULL — 'no_funding_interval'. Today
      that is the PAXG/XAUT tokenized-spot rows. NOTE for §7.7: their real
      funding cost is arguably ZERO, not unknown — being funding-drag-free
      is the whole reason those instruments earn their place in a gold
      thesis. This function will not assert that on its own; treating
      "no funding mechanism" as "zero cost" is a scoring decision for the
      instrument-fit work, not a silent default here.
    * zero funding observations in the window — 'no_history'. BydFi today.
    * one observation, or several inside a single instant — 'zero_span'.
      days_covered is 0 (a single point spans nothing, per history.py), so
      no window aggregate is supportable.

    `current_funding_rate` is the most recent observation INSIDE the
    window, not the latest row in the table. If capture has been stalled
    longer than the window, there is no current rate and the result is
    'no_history' — which is the honest answer, not a stale one.
    """
    if hold_days <= 0:
        raise ValueError("hold_days must be positive")

    if instrument.funding_interval_minutes is None:
        return _funding_result(hold_days=hold_days, reason="no_funding_interval")

    series = instrument_history_series(
        conn, instrument.id, "funding_rate_interval", FUNDING_WINDOW_DAYS
    )
    confidence = calcs.window_confidence(series.days_covered, FUNDING_WINDOW_DAYS)
    tier = calcs.confidence_tier(confidence)

    if series.days_covered == 0:
        return _funding_result(
            hold_days=hold_days,
            days_covered=series.days_covered,
            window_confidence=confidence,
            confidence_tier=tier,
            observations=len(series),
            reason="no_history" if series.is_empty else "zero_span",
        )

    current_funding_rate = series.values[-1]
    costs = calcs.hold_period_funding_estimate(
        current_funding_rate=current_funding_rate,
        funding_rate_history=series.values,
        funding_interval_minutes=instrument.funding_interval_minutes,
        hold_days=hold_days,
    )

    return _funding_result(
        hold_days=hold_days,
        base=costs["base"],
        optimistic=costs["optimistic"],
        stress=costs["stress"],
        current_funding_rate=current_funding_rate,
        intervals=calcs.funding_intervals(
            instrument.funding_interval_minutes, hold_days
        ),
        days_covered=series.days_covered,
        window_confidence=confidence,
        confidence_tier=tier,
        observations=len(series),
    )


# ---------------------------------------------------------------------------
# §7.4 Tracking error
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairedObservation:
    """One mark price matched to the spot price in force when it was taken."""

    mark_price: Decimal
    spot_price: Decimal
    captured_at: datetime  # when the mark was observed
    spot_as_of: datetime  # when that spot price was observed
    lag: timedelta  # captured_at - spot_as_of, always >= 0


def pair_as_of(
    marks: list[tuple[Decimal, datetime]],
    spots: list[tuple[Decimal, datetime]],
) -> list[PairedObservation]:
    """Match each mark to the most recent spot observed AT OR BEFORE it.

    Both inputs must be ordered oldest-first, which is what history.py
    guarantees (both queries ORDER BY ... ASC).

    THE RULE: a mark taken at time t is paired with the latest spot whose
    observation time is <= t. Never a later one.

    Why never a later one — this is the whole point of the function. Spot
    and mark are two independent series sampled on unrelated schedules:
    Hyperliquid marks arrive whenever the capture job runs, Metals.Dev
    publishes continuously, and EIA publishes days late. Pairing a mark
    with the *nearest* spot in either direction would routinely reach
    forward in time and compare a mark against a price that did not exist
    when that mark was set. That is lookahead bias: it would make tracking
    error look artificially small precisely when spot was moving fast,
    which is exactly when a real tracking failure matters most. Reaching
    only backwards is also what a trader actually experienced — at time t
    the newest spot they could possibly have known was the last published
    one.

    Marks older than the first available spot observation are DROPPED, not
    paired. There is no known spot for them and inventing one (by reaching
    forward to the first spot, or by extrapolating) would fabricate the
    very number this build step refuses to fabricate. This is why a long
    mark history can still yield few pairs, and why `days_covered` in the
    result is measured over the PAIRED observations rather than over the
    raw mark series.

    Spot observations are carried forward indefinitely: the most recent one
    stays in force until a newer one appears, exactly as a stale price does
    in the real world. That staleness is not silently absorbed — every pair
    records its `lag`, and the caller surfaces the worst one so an energy
    instrument's T-2..T-6 EIA lag stays visible (CLAUDE.md 2026-07-12).

    Single pass, O(n + m), because both series are already sorted.
    """
    paired: list[PairedObservation] = []
    spot_index = 0
    current_spot: tuple[Decimal, datetime] | None = None

    for mark_price, captured_at in marks:
        # advance through every spot published at or before this mark,
        # keeping the last one — that is the price in force at captured_at
        while spot_index < len(spots) and spots[spot_index][1] <= captured_at:
            current_spot = spots[spot_index]
            spot_index += 1

        if current_spot is None:
            continue  # no spot known yet at this mark's timestamp; drop it

        spot_price, spot_as_of = current_spot
        paired.append(
            PairedObservation(
                mark_price=mark_price,
                spot_price=spot_price,
                captured_at=captured_at,
                spot_as_of=spot_as_of,
                lag=captured_at - spot_as_of,
            )
        )

    return paired


def _select_spot_source(
    instrument_reference: HistorySeries, underlying_spot: HistorySeries
) -> tuple[str, list[tuple[Decimal, datetime]]]:
    """Choose between the instrument's own reference_spot_price column and
    the shared underlying-level spot_prices series.

    Both price the same thing by construction: 0008 established that
    market_snapshots.reference_spot_price is populated FROM spot_prices for
    the instrument's underlying. Preferring whichever has better coverage
    is therefore not mixing two different measurements.

    The instrument's own column wins ties because it is stamped on the same
    row as the mark (zero pairing lag). The underlying series takes over
    whenever it covers strictly more of the window — which is the mechanism
    Caleb asked for: a newly added BydFi gold contract inherits gold's
    existing shared spot history immediately instead of waiting out its own
    bootstrap period before §7.4 can say anything.

    Emptiness is checked BEFORE coverage, because days_covered cannot tell
    the two apart at the bottom: a series with a single observation spans
    zero days, exactly like a series with none at all. Comparing spans
    alone would let an EMPTY instrument-reference series beat a usable
    one-observation underlying series (0 > 0 is false), and §7.4 would
    report 'no_spot_history' while holding a perfectly good spot price.
    """
    if instrument_reference.is_empty:
        return "underlying_spot", underlying_spot.observations
    if underlying_spot.is_empty:
        return "instrument_reference", instrument_reference.observations
    if underlying_spot.days_covered > instrument_reference.days_covered:
        return "underlying_spot", underlying_spot.observations
    return "instrument_reference", instrument_reference.observations


def _tracking_result(
    *,
    hold_days: int,
    current_bps: Decimal | None = None,
    avg_abs_7d: Decimal | None = None,
    max_abs_30d: Decimal | None = None,
    underlying: str | None = None,
    basis_category: str | None = None,
    spot_source: str | None = None,
    max_spot_lag_days: Decimal | None = None,
    days_covered: Decimal = Decimal(0),
    window_confidence: Decimal = Decimal(0),
    confidence_tier: str | None = None,
    confidence_tier_pre_basis: str | None = None,
    observations: int = 0,
    observations_7d: int = 0,
    reason: str | None = None,
) -> dict:
    """One result shape on every path (same discipline as _funding_result)."""
    return {
        # §7.4 values
        "tracking_error_bps": current_bps,
        "avg_abs_7d": avg_abs_7d,
        "max_abs_30d": max_abs_30d,
        # which reference series produced them, and how stale it was
        "underlying": underlying,
        "basis_category": basis_category,
        "has_structural_basis_gap": basis_category in ("energy", "unknown")
        if basis_category is not None
        else None,
        "spot_source": spot_source,
        "max_spot_lag_days": max_spot_lag_days,
        # coverage, measured over PAIRED observations
        "window_days": TRACKING_WINDOW_DAYS,
        "days_covered": days_covered,
        "window_confidence": window_confidence,
        "confidence_tier": confidence_tier,
        "confidence_tier_pre_basis": confidence_tier_pre_basis,
        "observations": observations,
        "observations_7d": observations_7d,
        "hold_days": hold_days,
        "reason": reason,
    }


def compute_tracking_error(
    conn: psycopg.Connection, instrument: Instrument, hold_days: int
) -> dict:
    """§7.4 tracking error for one instrument, with the energy/metals branch.

    Pulls 30 days of the instrument's own marks, pairs each one as-of
    against a reference spot series (see pair_as_of for the pairing rule
    and _select_spot_source for which series), and reduces to the three
    §7.4 figures: current, 7-day average absolute, 30-day maximum absolute.

    THE ENERGY/METALS BRANCH (CLAUDE.md 2026-07-12, binding):
    EIA energy spot is a physical/FOB price with a structural basis gap to
    the front-month futures perp oracles follow, and publishes T-2..T-6
    late. Metals.Dev spot has no such gap. So for `wti_crude_oil`,
    `brent_crude_oil` and `natural_gas` — and for any uncategorized
    underlying — the coverage-derived tier is downgraded one step, and
    `has_structural_basis_gap` is set so nothing downstream can read the
    number as a clean tracking-error signal. Both the pre- and
    post-downgrade tiers are returned so the adjustment is auditable.
    A gap this large is a property of the DATA SOURCE, not of how much
    history exists, so more history never removes the downgrade.
    Per the same entry: do NOT build toward a futures feed until Caleb
    decides on one.

    `hold_days` is echoed for interface symmetry with
    compute_hold_period_funding but does not enter the math — §7.4 fixes
    its own 7- and 30-day windows. Say so rather than pretending it is used.

    Returns an all-None result (with `reason`) when no pairing is possible:
    'no_mark_history', 'no_spot_history', or 'no_overlap' (marks exist but
    every one predates the first spot observation).

    NOTE — one deliberate difference from compute_hold_period_funding: the
    CURRENT tracking error is a point-in-time value, not a window
    statistic, so a single pair is enough to report it honestly. When only
    one pair exists (days_covered == 0) this returns that current value
    while leaving avg_abs_7d and max_abs_30d as None, with reason
    'zero_span'. Nulling a directly-observed comparison would discard a
    real measurement; nulling the two window aggregates is required.
    """
    if hold_days <= 0:
        raise ValueError("hold_days must be positive")

    marks = instrument_history_series(
        conn, instrument.id, "mark_price", TRACKING_WINDOW_DAYS
    )
    category = calcs.basis_category(instrument.underlying)

    if marks.is_empty:
        return _tracking_result(
            hold_days=hold_days,
            underlying=instrument.underlying,
            basis_category=category,
            reason="no_mark_history",
        )

    instrument_reference = instrument_history_series(
        conn, instrument.id, "reference_spot_price", TRACKING_WINDOW_DAYS
    )
    underlying_spot = underlying_spot_series(
        conn, instrument.underlying, TRACKING_WINDOW_DAYS
    )
    spot_source, spots = _select_spot_source(instrument_reference, underlying_spot)

    if not spots:
        return _tracking_result(
            hold_days=hold_days,
            underlying=instrument.underlying,
            basis_category=category,
            reason="no_spot_history",
        )

    pairs = pair_as_of(marks.observations, spots)
    if not pairs:
        return _tracking_result(
            hold_days=hold_days,
            underlying=instrument.underlying,
            basis_category=category,
            spot_source=spot_source,
            reason="no_overlap",
        )

    # coverage is the span of what could actually be PAIRED, not of the
    # raw mark series — unpaired leading marks contribute no comparison
    days_covered = span_days(pairs[0].captured_at, pairs[-1].captured_at)
    confidence = calcs.window_confidence(days_covered, TRACKING_WINDOW_DAYS)
    tier_pre_basis = calcs.confidence_tier(confidence)
    tier = (
        calcs.downgrade_tier(tier_pre_basis)
        if category in ("energy", "unknown")
        else tier_pre_basis
    )

    now = conn.execute("SELECT now()").fetchone()[0]
    cutoff_7d = now - timedelta(days=TRACKING_AVG_WINDOW_DAYS)
    pairs_7d = [pair for pair in pairs if pair.captured_at >= cutoff_7d]

    current_bps = calcs.tracking_error_bps(
        pairs[-1].mark_price, pairs[-1].spot_price
    )
    max_lag_days = timedelta_days(max(pair.lag for pair in pairs))

    if days_covered == 0:
        return _tracking_result(
            hold_days=hold_days,
            current_bps=current_bps,
            underlying=instrument.underlying,
            basis_category=category,
            spot_source=spot_source,
            max_spot_lag_days=max_lag_days,
            days_covered=days_covered,
            window_confidence=confidence,
            confidence_tier=tier,
            confidence_tier_pre_basis=tier_pre_basis,
            observations=len(pairs),
            observations_7d=len(pairs_7d),
            reason="zero_span",
        )

    # §7.4 wants the average over 7 days and the maximum over 30, so
    # tracking_error_stats is called once per window and each call's
    # correspondingly-named key is the one that is read.
    avg_abs_7d = calcs.tracking_error_stats(
        [(pair.mark_price, pair.spot_price) for pair in pairs_7d]
    )["avg_abs_7d"]
    max_abs_30d = calcs.tracking_error_stats(
        [(pair.mark_price, pair.spot_price) for pair in pairs]
    )["max_abs_30d"]

    return _tracking_result(
        hold_days=hold_days,
        current_bps=current_bps,
        avg_abs_7d=avg_abs_7d,
        max_abs_30d=max_abs_30d,
        underlying=instrument.underlying,
        basis_category=category,
        spot_source=spot_source,
        max_spot_lag_days=max_lag_days,
        days_covered=days_covered,
        window_confidence=confidence,
        confidence_tier=tier,
        confidence_tier_pre_basis=tier_pre_basis,
        observations=len(pairs),
        observations_7d=len(pairs_7d),
    )


# ---------------------------------------------------------------------------
# §7.5 Liquidity proxy score
# ---------------------------------------------------------------------------

# The synthetic-simulation confidence ceiling (CLAUDE.md 2026-07-14): any
# §7.5 input drawn from a liquidity_profiles row with this provenance caps
# the instrument's data_confidence here, no matter how complete the rest of
# its inputs are.
SYNTHETIC_PROVENANCE = "synthetic_simulation"
SYNTHETIC_CONFIDENCE_CEILING = "medium"

# The four §7.5 raw inputs, in the order liquidity_score() expects them.
LIQUIDITY_INPUT_FIELDS = (
    "day_volume_usd",
    "open_interest_usd",
    "spread_bps_est",
    "impact_slippage_bps",
)


def _latest_liquidity_inputs(conn: psycopg.Connection) -> dict:
    """Latest market_snapshots row per active perp, reduced to the four §7.5
    inputs. impact_slippage_bps is derived here (not a stored column) from
    the same impact prices spread uses.

    Keyed by instrument_id. Only instruments that HAVE a snapshot appear;
    an instrument absent from this dict has no market-snapshot liquidity
    signal at all (BydFi today).
    """
    rows = conn.execute(
        f"""
        SELECT i.id, snap.day_volume_usd, snap.open_interest_usd,
               snap.spread_bps_est, snap.impact_bid_price,
               snap.impact_ask_price, snap.mid_price
        FROM instruments i
        LEFT JOIN LATERAL (
            SELECT s.day_volume_usd, s.open_interest_usd, s.spread_bps_est,
                   s.impact_bid_price, s.impact_ask_price, s.mid_price
            FROM market_snapshots s
            WHERE s.instrument_id = i.id
            ORDER BY s.captured_at DESC
            LIMIT 1
        ) snap ON true
        WHERE {ACTIVE_PERP_PREDICATE}
        """
    ).fetchall()

    inputs: dict = {}
    for (
        instrument_id,
        day_volume_usd,
        open_interest_usd,
        spread_bps_est,
        impact_bid_price,
        impact_ask_price,
        mid_price,
    ) in rows:
        # LEFT JOIN LATERAL yields all-NULL when no snapshot exists; skip
        # those so they never enter the universe min/max as phantom zeros.
        if (
            day_volume_usd is None
            and open_interest_usd is None
            and spread_bps_est is None
            and mid_price is None
        ):
            continue
        slippage = None
        if impact_bid_price is not None and impact_ask_price is not None and mid_price:
            slippage = calcs.impact_slippage_bps(
                impact_bid_price, impact_ask_price, mid_price
            )
        inputs[instrument_id] = {
            "day_volume_usd": day_volume_usd,
            "open_interest_usd": open_interest_usd,
            "spread_bps_est": spread_bps_est,
            "impact_slippage_bps": slippage,
        }
    return inputs


def _universe_stats(latest_inputs: dict) -> dict:
    """Per-field {min, max} over every active perp that has a snapshot.

    NULLs are excluded per field: an instrument missing spread still
    contributes its volume to the volume min/max. A field with no non-NULL
    value anywhere yields an empty {} and normalizes to neutral downstream.
    """
    stats: dict = {}
    for field in LIQUIDITY_INPUT_FIELDS:
        present = [
            row[field] for row in latest_inputs.values() if row[field] is not None
        ]
        stats[field] = (
            {"min": min(present), "max": max(present)} if present else {}
        )
    return stats


def _latest_liquidity_profiles(conn: psycopg.Connection, instrument_id) -> list[dict]:
    """The latest liquidity_profiles row of EACH profile_type for one
    instrument (0013). DISTINCT ON collapses the time series to the newest
    per type, matching the table's documented read pattern.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT ON (profile_type)
               profile_type, provenance, captured_at
        FROM liquidity_profiles
        WHERE instrument_id = %s
        ORDER BY profile_type, captured_at DESC
        """,
        (instrument_id,),
    ).fetchall()
    return [
        {"profile_type": pt, "provenance": prov, "captured_at": ts}
        for pt, prov, ts in rows
    ]


def _base_confidence(inputs: dict) -> str | None:
    """Data confidence from input COMPLETENESS, before the provenance cap.

    FIRST PASS, flagged for review — §7.5 does not define liquidity
    confidence tiers, and unlike §7.3/§7.4 this score reads a single latest
    snapshot rather than a window, so window-coverage does not apply. The
    stand-in is simply how many of the four inputs are actually present:

        4 present -> 'high'      2-3 -> 'medium'      1 -> 'low'   0 -> None

    Rationale: a liquidity score averaged over neutral-filled missing inputs
    is real but weakly-founded, and completeness is the honest axis for that.
    This is deliberately conservative and easy to replace once Caleb decides
    how liquidity confidence should be calibrated.
    """
    present = sum(1 for f in LIQUIDITY_INPUT_FIELDS if inputs[f] is not None)
    if present == 0:
        return None
    if present >= 4:
        return "high"
    if present >= 2:
        return "medium"
    return "low"


def _liquidity_result(
    *,
    score: Decimal | None = None,
    data_confidence: str | None = None,
    data_confidence_pre_cap: str | None = None,
    synthetic_cap_applied: bool = False,
    raw_inputs: dict | None = None,
    universe_size: int = 0,
    profiles: list[dict] | None = None,
    provenances: list[str] | None = None,
    reason: str | None = None,
) -> dict:
    """One result shape on every path (same discipline as the §7.3/§7.4
    results): callers branch on values, never on which keys exist."""
    return {
        "liquidity_score": score,
        "data_confidence": data_confidence,
        "data_confidence_pre_cap": data_confidence_pre_cap,
        "synthetic_cap_applied": synthetic_cap_applied,
        "raw_inputs": raw_inputs
        or {field: None for field in LIQUIDITY_INPUT_FIELDS},
        "universe_size": universe_size,
        # supplementary liquidity_profiles signal (0013), reported even when
        # the market-snapshot score is insufficient
        "liquidity_profiles": profiles or [],
        "provenances": provenances or [],
        "reason": reason,
    }


def compute_liquidity_score(
    conn: psycopg.Connection, instrument: Instrument
) -> dict:
    """§7.5 liquidity proxy for one instrument, with the provenance cap.

    The instrument's four raw inputs (from its latest market_snapshots row)
    are min-max normalized against the active-perp universe — status='active'
    AND instrument_type='perp', the exact predicate discrepancy.py uses
    (ACTIVE_PERP_PREDICATE) — and weight-averaged by calcs.liquidity_score.

    THE PROVENANCE CAP (CLAUDE.md 2026-07-14, binding): the instrument's
    latest liquidity_profiles rows travel with the result as a supplementary
    signal. If ANY of them carries provenance='synthetic_simulation' — real
    resting orders and synthetic oracle+vault simulations must never be
    presented as equivalent — data_confidence is capped at 'medium' here,
    however complete the market-snapshot inputs are. Nothing produces a
    synthetic row in real data yet (Ostium sim capture is deferred to step
    8), so today the cap is dormant; it is wired now so it fires the moment
    such a row appears rather than being retrofitted later.

    Returns an explicit insufficient-data result (reason='no_market_snapshot',
    score None, data_confidence None) when the instrument has no
    market_snapshots row at all — BydFi today. Its liquidity_profiles
    risk-tier row, if present, is still reported: the market-snapshot score
    is what is insufficient, not necessarily the whole liquidity picture.
    """
    latest_inputs = _latest_liquidity_inputs(conn)
    universe_stats = _universe_stats(latest_inputs)
    profiles = _latest_liquidity_profiles(conn, instrument.id)
    provenances = sorted({p["provenance"] for p in profiles})
    has_synthetic = SYNTHETIC_PROVENANCE in provenances

    own_inputs = latest_inputs.get(instrument.id)
    if own_inputs is None:
        # no market snapshot; the §7.5 score is unsupportable even if a
        # risk-tier profile exists. Still surface the profiles + the cap
        # signal so the caller sees the full picture.
        return _liquidity_result(
            data_confidence=None,
            synthetic_cap_applied=has_synthetic,  # would cap, but base is None
            universe_size=len(latest_inputs),
            profiles=profiles,
            provenances=provenances,
            reason="no_market_snapshot",
        )

    score = calcs.liquidity_score(
        own_inputs["day_volume_usd"],
        own_inputs["open_interest_usd"],
        own_inputs["spread_bps_est"],
        own_inputs["impact_slippage_bps"],
        universe_stats,
    )

    base_confidence = _base_confidence(own_inputs)
    data_confidence = (
        calcs.cap_tier(base_confidence, SYNTHETIC_CONFIDENCE_CEILING)
        if has_synthetic
        else base_confidence
    )

    return _liquidity_result(
        score=score,
        data_confidence=data_confidence,
        data_confidence_pre_cap=base_confidence,
        synthetic_cap_applied=has_synthetic and data_confidence != base_confidence,
        raw_inputs=own_inputs,
        universe_size=len(latest_inputs),
        profiles=profiles,
        provenances=provenances,
    )


# ---------------------------------------------------------------------------
# §7.6 Carry score
# ---------------------------------------------------------------------------


def _carry_result(
    *,
    direction: str,
    score: Decimal | None = None,
    mean_funding_rate: Decimal | None = None,
    stability_penalty: Decimal | None = None,
    direction_penalty: Decimal | None = None,
    days_covered: Decimal = Decimal(0),
    window_confidence: Decimal = Decimal(0),
    confidence_tier: str | None = None,
    observations: int = 0,
    reason: str | None = None,
) -> dict:
    """One result shape on every path (same discipline as the §7.3/§7.4/§7.5
    results): callers branch on values, never on which keys exist."""
    return {
        "carry_score": score,
        "direction": direction,
        # the components behind the score, so a surprising value is auditable
        # without a re-query
        "mean_funding_rate": mean_funding_rate,
        "stability_penalty": stability_penalty,
        "direction_penalty": direction_penalty,
        # coverage (prompt 1), identical window and semantics to §7.3
        "window_days": FUNDING_WINDOW_DAYS,
        "days_covered": days_covered,
        "window_confidence": window_confidence,
        "confidence_tier": confidence_tier,
        "observations": observations,
        "reason": reason,
    }


def compute_carry_score(
    conn: psycopg.Connection, instrument: Instrument, direction: str
) -> dict:
    """§7.6 carry score for one instrument in one direction, with confidence.

    Same shape as compute_hold_period_funding: reads the instrument's own
    7-day funding_rate_interval window, measures coverage, and attaches the
    resulting tier. `direction` is 'long' or 'short' — the score is
    direction-specific (a market whose funding pays shorts is good carry for
    a short and bad carry for a long), so the caller asks per direction.

    Returns an all-None result (with `reason`) rather than a fabricated
    number when:
      * funding_interval_minutes is NULL — 'no_funding_interval' (PAXG/XAUT).
        As in §7.3, whether "no funding mechanism" should read as neutral
        carry (funding-drag-free, arguably a POINT in a gold thesis) rather
        than unknown is a §7.7 decision, not a silent default here.
      * zero funding observations in the window — 'no_history' (BydFi today).
      * a single observation, spanning zero days — 'zero_span'. Carry
        stability cannot be assessed from one point (calcs.carry_score would
        return a penalty-free base level), so the honest report is
        insufficient-with-confidence, not that base level dressed as a
        settled score.
    """
    if direction not in ("long", "short"):
        raise ValueError("direction must be 'long' or 'short'")

    if instrument.funding_interval_minutes is None:
        return _carry_result(direction=direction, reason="no_funding_interval")

    series = instrument_history_series(
        conn, instrument.id, "funding_rate_interval", FUNDING_WINDOW_DAYS
    )
    confidence = calcs.window_confidence(series.days_covered, FUNDING_WINDOW_DAYS)
    tier = calcs.confidence_tier(confidence)

    if series.days_covered == 0:
        return _carry_result(
            direction=direction,
            days_covered=series.days_covered,
            window_confidence=confidence,
            confidence_tier=tier,
            observations=len(series),
            reason="no_history" if series.is_empty else "zero_span",
        )

    mean_rate = sum(series.values) / Decimal(len(series.values))
    return _carry_result(
        direction=direction,
        score=calcs.carry_score(series.values, direction),
        mean_funding_rate=mean_rate,
        stability_penalty=calcs.funding_stability_penalty(series.values),
        direction_penalty=calcs.funding_direction_penalty(series.values),
        days_covered=series.days_covered,
        window_confidence=confidence,
        confidence_tier=tier,
        observations=len(series),
    )


# ---------------------------------------------------------------------------
# §7.7 Instrument-fit composite
# ---------------------------------------------------------------------------

# tracking sub-scores are unusable (excluded from the composite) only when no
# tracking comparison could be formed at all. 'zero_span' still yields a
# usable current tracking_error_bps, so it is NOT in this set.
_TRACKING_NO_SIGNAL = frozenset(
    {"no_mark_history", "no_spot_history", "no_overlap"}
)

# tracking sub-score handed to §7.7 for an energy/uncategorized instrument,
# whose raw bps is a structural basis gap, not a clean tracking signal
# (CLAUDE.md 2026-07-12). Neutral: neither reward nor penalize on a number
# that does not measure tracking fidelity.
TRACKING_BASIS_GAP_NEUTRAL = Decimal(50)

# ranking of liquidity_profiles provenance, best first, for venue_quality.
_PROVENANCE_RANK = (
    "real_resting_orders",
    "venue_risk_config",
    "synthetic_simulation",
)

# nominal hold used only to satisfy compute_tracking_error's signature; §7.4
# fixes its own 7/30-day windows and does not use hold_days in the math.
_TRACKING_NOMINAL_HOLD_DAYS = 14


def _best_provenance(provenances: list[str]) -> str | None:
    for provenance in _PROVENANCE_RANK:
        if provenance in provenances:
            return provenance
    return None


def _tracking_subscore(tracking_result: dict) -> Decimal | None:
    """Turn a §7.4 result into a 0–100 tracking sub-score, or None if no
    tracking comparison exists.

    Energy / uncategorized instruments (structural basis gap) get a neutral
    50 — their raw bps is basis, not tracking fidelity. Clean (metals)
    instruments map their average absolute drift (falling back to the
    current value when the 7-day average is unsupported) through
    tracking_fidelity_score.
    """
    if tracking_result["reason"] in _TRACKING_NO_SIGNAL:
        return None
    if tracking_result["has_structural_basis_gap"]:
        return TRACKING_BASIS_GAP_NEUTRAL
    bps = tracking_result["avg_abs_7d"]
    if bps is None:
        bps = tracking_result["tracking_error_bps"]  # zero_span: current only
    return calcs.tracking_fidelity_score(abs(bps))


def _instrument_metadata(conn: psycopg.Connection, instrument_id) -> dict:
    venue_type, instrument_type, tradeable = conn.execute(
        "SELECT venue_type, instrument_type, tradeable FROM instruments "
        "WHERE id = %s",
        (instrument_id,),
    ).fetchone()
    return {
        "venue_type": venue_type,
        "instrument_type": instrument_type,
        "tradeable": tradeable,
    }


def compute_instrument_fit(
    conn: psycopg.Connection,
    instrument: Instrument,
    thesis_commodity_code: str,
    direction: str,
) -> dict:
    """§7.7 instrument-fit composite for one (thesis-commodity, instrument,
    direction) triple.

    underlying_match and venue_quality are computed from instrument metadata
    (always available). liquidity, carry and tracking are pulled from the
    §7.5/§7.6/§7.4 orchestration functions and may be insufficient.

    INSUFFICIENT SUB-SCORES — decision, documented:
    instrument_fit is a COMPARATIVE RANKING score whose job is to keep an
    instrument rankable for manual review, not a single measured quantity
    where None cleanly means "unknown". So rather than collapsing the whole
    composite to None the moment one market input is thin, this DROPS each
    insufficient market sub-score and renormalizes the §7.7 weights over
    what remains (weighted_fit_subset) — preserving the relative importance
    of what is actually known. underlying_match is always present, so a
    score is always returned.

    What protects against over-trusting a thin composite is CONFIDENCE, not
    absence: `confidence` is the weakest tier among the INCLUDED market
    inputs, downgraded one further step if any market input had to be
    dropped. With NO market inputs at all (BydFi today: no snapshot, so
    liquidity/carry/tracking are all insufficient) the score is still
    returned — it is a metadata-only prior from underlying_match +
    venue_quality — but `confidence` is None and `reason` is 'metadata_only'
    to mark it untrustworthy. This is a deliberate, documented departure
    from the single-formula convention (where None score ↔ None confidence):
    for a ranking composite, a flagged weak signal beats dropping the
    instrument from consideration entirely.
    """
    if direction not in ("long", "short"):
        raise ValueError("direction must be 'long' or 'short'")

    metadata = _instrument_metadata(conn, instrument.id)

    liquidity_result = compute_liquidity_score(conn, instrument)
    carry_result = compute_carry_score(conn, instrument, direction)
    tracking_result = compute_tracking_error(
        conn, instrument, _TRACKING_NOMINAL_HOLD_DAYS
    )

    um = calcs.underlying_match(instrument.underlying, thesis_commodity_code)
    vq = calcs.venue_quality(
        metadata["venue_type"],
        metadata["instrument_type"],
        metadata["tradeable"],
        _best_provenance(liquidity_result["provenances"]),
    )

    # metadata inputs are always present
    scores: dict = {"underlying_match": um, "venue_quality": vq}
    components: dict = {
        "underlying_match": um,
        "venue_quality": vq,
        "liquidity": None,
        "carry": None,
        "tracking": None,
    }
    sub_confidences: dict = {}

    if liquidity_result["liquidity_score"] is not None:
        scores["liquidity"] = liquidity_result["liquidity_score"]
        components["liquidity"] = liquidity_result["liquidity_score"]
        sub_confidences["liquidity"] = liquidity_result["data_confidence"]

    if carry_result["carry_score"] is not None:
        scores["carry"] = carry_result["carry_score"]
        components["carry"] = carry_result["carry_score"]
        sub_confidences["carry"] = carry_result["confidence_tier"]

    tracking_sub = _tracking_subscore(tracking_result)
    if tracking_sub is not None:
        scores["tracking"] = tracking_sub
        components["tracking"] = tracking_sub
        sub_confidences["tracking"] = tracking_result["confidence_tier"]

    market_inputs = ("liquidity", "carry", "tracking")
    included_market = [k for k in market_inputs if k in scores]
    dropped_market = [k for k in market_inputs if k not in scores]

    # confidence = weakest INCLUDED market tier, downgraded once if any
    # market input was dropped; None when no market input contributed.
    if included_market:
        confidence = min(
            (sub_confidences[k] for k in included_market),
            key=calcs.TIER_LADDER.index,
        )
        if dropped_market:
            confidence = calcs.downgrade_tier(confidence)
        reason = "partial_market_data" if dropped_market else None
    else:
        confidence = None
        reason = "metadata_only"

    return {
        "instrument_fit": calcs.weighted_fit_subset(scores),
        "confidence": confidence,
        "direction": direction,
        "thesis_commodity_code": thesis_commodity_code,
        "underlying": instrument.underlying,
        "components": components,
        "included_inputs": sorted(scores),
        "dropped_inputs": dropped_market,
        "sub_confidences": sub_confidences,
        "reason": reason,
    }
