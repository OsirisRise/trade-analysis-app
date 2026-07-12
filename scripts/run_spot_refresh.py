"""Run one daily reference-spot refresh (Metals.Dev + EIA).

Usage:
    python scripts/run_spot_refresh.py            # fetch + write + stamp
    python scripts/run_spot_refresh.py --dry-run  # fetch + print only

Schedule daily (blueprint cadence for macro/tokenized series), e.g. cron:
    15 13 * * * cd /path/to/trade-analysis-app && .venv/bin/python scripts/run_spot_refresh.py
"""

import argparse
import logging

from onchain_console.db import get_connection
from onchain_console.spot_service import run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="do not write to DB")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    with get_connection() as conn:
        spots = run(conn, dry_run=args.dry_run)

    for s in spots:
        print(
            f"{s.commodity_code:16s} {s.price} {s.unit:14s} "
            f"source={s.source:10s} as_of={s.as_of:%Y-%m-%d %H:%M%z}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
