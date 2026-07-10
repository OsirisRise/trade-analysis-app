"""Run one Hyperliquid market snapshot cycle (M5).

Usage:
    python scripts/run_snapshot.py            # fetch + write to market_snapshots
    python scripts/run_snapshot.py --dry-run  # fetch + print, no DB write

Schedule hourly (matches Hyperliquid's hourly funding), e.g. cron:
    5 * * * * cd /path/to/trade-analysis-app && .venv/bin/python scripts/run_snapshot.py
"""

import argparse
import logging

from onchain_console.db import get_connection
from onchain_console.snapshot_service import run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="do not write to DB")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    with get_connection() as conn:
        rows = run(conn, dry_run=args.dry_run)

    for r in rows:
        print(
            f"{r.symbol:12s} mark={r.mark_price} oracle={r.oracle_price} "
            f"premium={r.premium_pct:.6%} funding_1h={r.funding_rate_interval} "
            f"8h_equiv={r.funding_rate_8h_equiv} apr_est={r.funding_apr_est:.4%} "
            f"OI_usd={r.open_interest_usd:.0f} dayVlm={r.day_volume_usd:.0f} "
            f"spread_bps={r.spread_bps_est:.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
