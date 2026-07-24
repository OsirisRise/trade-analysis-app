"""Live scoring report — runs the whole §7.3-7.7 chain against real data.

For every active perp (status='active' AND instrument_type='perp'), computes
the four scoring-engine outputs and prints each score next to its confidence
tier, so a well-backed Hyperliquid instrument and an insufficient-data BydFi
instrument appear side by side. This is the proof the chain degrades
gracefully on real data, not just in fixtures.

Reads public/stored data only; writes nothing.

Report parameter choices (a report needs concrete values; these do NOT
change any formula):
  * hold_days = 14. The blueprint frames this as a days-to-weeks swing hold;
    two weeks is a representative mid-point, and §7.4's windows are fixed
    regardless so only §7.3 funding cost scales with it.
  * direction = 'short'. Carry (§7.6) is direction-specific; short is an
    arbitrary but fixed reference direction for a uniform table. The long
    carry score is simply 100 - short (they mirror about 50), so one column
    conveys both. Override either with --hold-days / --direction.

Usage:
    python scripts/run_scoring_report.py
    python scripts/run_scoring_report.py --hold-days 7 --direction long
"""

import argparse

from onchain_console.db import get_connection
from onchain_console.discrepancy import ACTIVE_PERP_PREDICATE
from onchain_console.scoring import (
    compute_carry_score,
    compute_hold_period_funding,
    compute_liquidity_score,
    compute_tracking_error,
)
from onchain_console.snapshot_service import Instrument


def load_active_perps(conn) -> list[Instrument]:
    """Every active perp on every venue (the exact §7.5 universe predicate)."""
    rows = conn.execute(
        f"""
        SELECT i.id, i.venue, i.symbol, i.underlying, i.funding_interval_minutes
        FROM instruments i
        WHERE {ACTIVE_PERP_PREDICATE}
        ORDER BY i.venue, i.symbol
        """
    ).fetchall()
    return [Instrument(*row) for row in rows]


def _cell(value, tier: str | None, reason: str | None, fmt: str) -> str:
    """A score cell: value+tier when present, else the honest reason.

    tier is shown in brackets; None tier reads as 'none' so an insufficient
    result never masquerades as a confident one.
    """
    if value is None:
        return f"{'--':>9} [{(reason or 'n/a'):^11}]"
    return f"{format(value, fmt):>9} [{str(tier):^11}]"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hold-days", type=int, default=14)
    parser.add_argument(
        "--direction", choices=("long", "short"), default="short"
    )
    args = parser.parse_args()

    with get_connection() as conn:
        instruments = load_active_perps(conn)

        header = (
            f"{'venue':<12}{'symbol':<14}{'underlying':<16}"
            f"{'funding_base(14d)':>25}{'tracking_avg_bps':>25}"
            f"{'liquidity(0-100)':>25}{'carry_'+args.direction+'(0-100)':>25}"
        )
        print(
            f"\nLIVE SCORING REPORT  —  hold_days={args.hold_days}, "
            f"carry direction={args.direction}\n"
        )
        print(header)
        print("-" * len(header))

        for inst in instruments:
            fund = compute_hold_period_funding(conn, inst, args.hold_days)
            track = compute_tracking_error(conn, inst, args.hold_days)
            liq = compute_liquidity_score(conn, inst)
            carry = compute_carry_score(conn, inst, args.direction)

            row = (
                f"{inst.venue:<12}{inst.symbol:<14}{inst.underlying:<16}"
                f"{_cell(fund['funding_cost_base'], fund['confidence_tier'], fund['reason'], '+.5f'):>25}"
                f"{_cell(track['avg_abs_7d'], track['confidence_tier'], track['reason'], '.1f'):>25}"
                f"{_cell(liq['liquidity_score'], liq['data_confidence'], liq['reason'], '.1f'):>25}"
                f"{_cell(carry['carry_score'], carry['confidence_tier'], carry['reason'], '.1f'):>25}"
            )
            print(row)

    print(
        "\nEach cell: score [confidence_tier].  '--' with a reason means an "
        "explicit insufficient-data result — no fabricated number.\n"
        "funding_base is per-unit-of-notional over the hold; tracking_avg_bps "
        "is the 7d mean |mark-spot|; energy tracking is basis-downgraded.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
