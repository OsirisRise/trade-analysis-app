"""Lookback-window history reads for the scoring engine (blueprint §7.3–§7.7).

Every window-based formula in §7 — the 7-day average funding of §7.3, the
7-day-average / 30-day-max absolute tracking error of §7.4, the funding
volatility of §7.6 — assumes a full window of history. In reality this
project's production history is a couple of weeks old, and BydFi has no
`market_snapshots` rows at all. This module is the ONE place that answers
"how much real history is actually behind this number?", so no §7 formula
has to re-derive it (and so none of them can quietly skip asking).

Two rules this module exists to enforce:

* Thin history must never raise. A zero-row instrument returns an empty
  series, not an exception — the caller decides to emit NULL rather than
  crashing the scoring run.
* Coverage is measured as the SPAN of the data, never the row count. Two
  snapshots taken a week apart are 7 days of span, not "2 days"; ten
  snapshots taken in one afternoon are a fraction of a day, not "10 days".
  `days_covered` feeds `calcs.window_confidence()`, which is what a §7
  formula must consult before presenting a precise-looking aggregate.

Read-only: this module issues SELECTs and nothing else.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import psycopg
from psycopg import sql

# market_snapshots numeric columns a lookback window may be built from (§5.5).
# A column name cannot be a bound parameter, so it is whitelisted here and
# then quoted as an identifier — never string-formatted from caller input.
SNAPSHOT_SERIES_FIELDS = frozenset(
    {
        "mark_price",
        "mid_price",
        "oracle_price",
        "reference_spot_price",
        "premium_pct",
        "funding_rate_interval",
        "funding_rate_8h_equiv",
        "funding_apr_est",
        "open_interest_usd",
        "day_volume_usd",
        "impact_bid_price",
        "impact_ask_price",
        "spread_bps_est",
        "tracking_error_bps",
        "liquidity_score",
    }
)

SECONDS_PER_DAY = Decimal(86400)


@dataclass(frozen=True)
class HistorySeries:
    """One field's observed values over a lookback window, oldest first.

    `days_covered` is the span between the first and last observation, in
    days — NOT the row count and NOT the requested window. Consequences
    worth knowing before using it:

    * zero rows  -> Decimal(0), with both timestamps None;
    * one row    -> Decimal(0). A single point spans nothing, so it cannot
      support a window aggregate. This is deliberately conservative.
    * span says nothing about DENSITY. Two rows a week apart report 7 days
      of coverage while being a very thin sample; `len(values)` is the
      density check, and a §7 formula that needs one should apply its own
      minimum-observations rule on top of the coverage ratio.
    """

    field: str
    window_days: int
    values: list[Decimal]
    timestamps: list[datetime]
    earliest_captured_at: datetime | None
    latest_captured_at: datetime | None
    days_covered: Decimal

    def __len__(self) -> int:
        return len(self.values)

    @property
    def is_empty(self) -> bool:
        return not self.values

    @property
    def observations(self) -> list[tuple[Decimal, datetime]]:
        """(value, timestamp) pairs, oldest first.

        Needed by the §7.4 as-of pairing, which has to know WHEN each
        observation happened to avoid pairing a mark with a spot price
        that did not exist yet.
        """
        return list(zip(self.values, self.timestamps))


def timedelta_days(delta: timedelta) -> Decimal:
    """A timedelta expressed in days, as an exact Decimal.

    Built from timedelta's integer components rather than total_seconds()
    so no float ever touches a value that reaches the scoring math.
    """
    seconds = (
        Decimal(delta.days) * SECONDS_PER_DAY
        + Decimal(delta.seconds)
        + Decimal(delta.microseconds) / Decimal(1_000_000)
    )
    return seconds / SECONDS_PER_DAY


def span_days(earliest: datetime | None, latest: datetime | None) -> Decimal:
    """Exact elapsed days between two timestamps; 0 if either is missing."""
    if earliest is None or latest is None:
        return Decimal(0)
    return timedelta_days(latest - earliest)


def _series(
    field: str, window_days: int, rows: list[tuple[Decimal, datetime]]
) -> HistorySeries:
    if not rows:
        return HistorySeries(
            field=field,
            window_days=window_days,
            values=[],
            timestamps=[],
            earliest_captured_at=None,
            latest_captured_at=None,
            days_covered=Decimal(0),
        )
    earliest, latest = rows[0][1], rows[-1][1]  # query orders ASC by time
    return HistorySeries(
        field=field,
        window_days=window_days,
        values=[value for value, _ in rows],
        timestamps=[timestamp for _, timestamp in rows],
        earliest_captured_at=earliest,
        latest_captured_at=latest,
        days_covered=span_days(earliest, latest),
    )


def _validate_window_days(window_days: int) -> None:
    if window_days <= 0:
        raise ValueError("window_days must be positive")


def instrument_history_series(
    conn: psycopg.Connection,
    instrument_id: UUID,
    field: str,
    window_days: int,
) -> HistorySeries:
    """`field` from market_snapshots for one instrument over the last
    `window_days`, oldest observation first.

    Rows whose `field` is NULL are excluded: the snapshot service leaves
    columns NULL when the venue did not supply them (an Ostium row has no
    funding_rate_interval; tracking_error_bps is NULL until step 3 fills
    it), and a missing value is not an observation. Coverage is therefore
    measured over the rows that actually carry the field, which is what a
    §7 aggregate over that field is entitled to claim.

    Instruments with no market_snapshots rows at all — BydFi today — return
    an empty series, never an exception.
    """
    if field not in SNAPSHOT_SERIES_FIELDS:
        raise ValueError(
            f"unknown market_snapshots series field {field!r}; "
            f"allowed: {sorted(SNAPSHOT_SERIES_FIELDS)}"
        )
    _validate_window_days(window_days)

    query = sql.SQL(
        """
        SELECT {field}, captured_at
        FROM market_snapshots
        WHERE instrument_id = %s
          AND captured_at >= now() - make_interval(days => %s)
          AND {field} IS NOT NULL
        ORDER BY captured_at ASC
        """
    ).format(field=sql.Identifier(field))

    rows = conn.execute(query, (instrument_id, window_days)).fetchall()
    return _series(field, window_days, rows)


def underlying_spot_series(
    conn: psycopg.Connection,
    commodity_code: str,
    window_days: int,
) -> HistorySeries:
    """Reference spot `price` for one commodity over the last `window_days`,
    oldest observation first (spot_prices, 0008).

    `as_of` — the source's own observation time, not our capture time — is
    the window axis, because EIA energy spot publishes T-2..T-6 and the
    §7.4 energy branch has to see that staleness rather than have it hidden
    behind an ingest timestamp.

    Ordering is (as_of, source) so a commodity ever served by two sources
    returns deterministically; today each commodity_code has exactly one
    source, so no source filter is offered. Add one before mixing sources —
    interleaved series from different sources are not a single price series.
    """
    _validate_window_days(window_days)

    rows = conn.execute(
        """
        SELECT price, as_of
        FROM spot_prices
        WHERE commodity_code = %s
          AND as_of >= now() - make_interval(days => %s)
        ORDER BY as_of ASC, source ASC
        """,
        (commodity_code, window_days),
    ).fetchall()
    return _series("price", window_days, rows)
