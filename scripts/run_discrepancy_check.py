"""Daily spot-vs-perp discrepancy check (diagnostic only — not §7.4).

Usage:
    python scripts/run_discrepancy_check.py
    python scripts/run_discrepancy_check.py --metals-threshold 0.005 --energy-threshold 0.08

Reads spot_prices + latest market_snapshots; writes nothing. Schedule daily
AFTER the snapshot and spot-refresh jobs so both sides are fresh:
    30 13 * * * cd /path/to/trade-analysis-app && .venv/bin/python scripts/run_discrepancy_check.py
"""

import argparse
from decimal import Decimal

from onchain_console.db import get_connection
from onchain_console.discrepancy import (
    ENERGY_FLAG_THRESHOLD,
    METALS_FLAG_THRESHOLD,
    build_reports,
    load_latest_pairs,
)

CATEGORY_NOTES = {
    "energy": "known futures/spot basis — large gaps EXPECTED (see CLAUDE.md design decisions)",
    "metal": "reference tracks perp pricing — gaps are suspicious",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metals-threshold", type=Decimal,
                        default=METALS_FLAG_THRESHOLD,
                        help="flag threshold for metals (default 0.01 = 1%%)")
    parser.add_argument("--energy-threshold", type=Decimal,
                        default=ENERGY_FLAG_THRESHOLD,
                        help="flag threshold for energy (default 0.10 = 10%%)")
    args = parser.parse_args()

    with get_connection() as conn:
        reports = build_reports(
            load_latest_pairs(conn),
            metals_threshold=args.metals_threshold,
            energy_threshold=args.energy_threshold,
        )

    if not reports:
        print("no commodities have both a spot price and an active snapshot yet")
        return 0

    flagged_total = 0
    for r in reports:
        age_days = r.spot_age_hours / 24
        print(f"\n{r.commodity_code}  [{r.category.upper()}] "
              f"{CATEGORY_NOTES[r.category]}")
        print(f"  spot     {r.spot_price} {r.spot_unit} ({r.spot_source}), "
              f"as_of {r.spot_as_of:%Y-%m-%d %H:%M%z} "
              f"— {r.spot_age_hours:.1f}h old ({age_days:.1f}d)")
        print(f"  futures  {r.futures_price}")
        for g in r.instruments:
            marker = "  ** FLAG **" if g.flagged else ""
            print(f"  {g.venue:12s} {g.symbol:14s} mark={g.mark_price} "
                  f"gap={g.gap:+.4%}{marker}")
            flagged_total += g.flagged

    threshold_note = (f"thresholds: metals {args.metals_threshold:.2%}, "
                      f"energy {args.energy_threshold:.2%}")
    print(f"\n{len(reports)} commodities checked, {flagged_total} flagged "
          f"({threshold_note})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
