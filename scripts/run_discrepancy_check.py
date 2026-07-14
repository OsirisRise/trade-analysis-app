"""Daily cross-venue discrepancy check (diagnostic only — not §7.4).

For each commodity with a reference spot: every active perp across
Hyperliquid (latest stored snapshot), BydFi (quoted live at check time),
and Ostium (no data source until step 8), with the gap vs. spot, funding
normalized to 8h-equivalent, tradeability, and spot staleness.

Usage:
    python scripts/run_discrepancy_check.py
    python scripts/run_discrepancy_check.py --metals-threshold 0.005 --energy-threshold 0.08

Reads public data only; writes nothing. Schedule daily AFTER the snapshot
and spot-refresh jobs so both sides are fresh:
    30 13 * * * cd /path/to/trade-analysis-app && .venv/bin/python scripts/run_discrepancy_check.py
"""

import argparse
import logging
from decimal import Decimal

from onchain_console import bydfi
from onchain_console.db import get_connection
from onchain_console.discrepancy import (
    ENERGY_FLAG_THRESHOLD,
    METALS_FLAG_THRESHOLD,
    build_cross_venue_reports,
    load_active_instruments,
    load_latest_spots,
)

log = logging.getLogger(__name__)

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
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    try:
        live_quotes = bydfi.parse_contracts(bydfi.fetch_symbols())
    except Exception as exc:  # diagnostic must still run without BydFi
        log.warning("BydFi live quotes unavailable (%s)", exc)
        live_quotes = {}

    with get_connection() as conn:
        reports = build_cross_venue_reports(
            load_latest_spots(conn),
            load_active_instruments(conn),
            live_quotes=live_quotes,
            metals_threshold=args.metals_threshold,
            energy_threshold=args.energy_threshold,
        )

    if not reports:
        print("no commodities have both a spot price and active instruments yet")
        return 0

    flagged_total = 0
    for r in sorted(reports, key=lambda x: x.commodity_code):
        age_days = r.spot_age_hours / 24
        print(f"\n{r.commodity_code}  [{r.category.upper()}] "
              f"{CATEGORY_NOTES[r.category]}")
        print(f"  spot     {r.spot_price} {r.spot_unit} ({r.spot_source}), "
              f"as_of {r.spot_as_of:%Y-%m-%d %H:%M%z} "
              f"— {r.spot_age_hours:.1f}h old ({age_days:.1f}d)")
        print(f"  futures  {r.futures_price}")
        for g in r.instruments:
            trade = "tradeable" if g.tradeable else "reference"
            gap_txt = f"gap={g.gap:+.4%}" if g.gap is not None else "gap=n/a"
            f8h = (f"funding_8h={g.funding_rate_8h:+.6%}"
                   if g.funding_rate_8h is not None else "funding_8h=n/a")
            mark_txt = (f"mark={g.mark_price} ({g.price_basis})"
                        if g.mark_price is not None
                        else "no data source yet (step 8)")
            marker = "  ** FLAG **" if g.flagged else ""
            print(f"  {g.venue:12s} {g.symbol:14s} [{g.venue_type or '-':3s}"
                  f"|{trade:9s}] {mark_txt}  {gap_txt}  {f8h}{marker}")
            flagged_total += g.flagged

    print(f"\n{len(reports)} commodities checked, {flagged_total} flagged "
          f"(thresholds: metals {args.metals_threshold:.2%}, "
          f"energy {args.energy_threshold:.2%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
