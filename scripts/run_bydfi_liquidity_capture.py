"""Daily BydFi risk-tier capture -> liquidity_profiles (0013).

One public risk_limits call (no key needed or allowed — see bydfi.py's hard
boundary), then one risk_tiers/venue_risk_config row per seeded BydFi
instrument. Deliberately NOT part of snapshot_service's hourly run: BydFi
has no snapshot service to piggyback on (nothing writes BydFi rows to
market_snapshots today) and risk tiers change rarely — daily is plenty.

Usage:
    python scripts/run_bydfi_liquidity_capture.py            # fetch + insert
    python scripts/run_bydfi_liquidity_capture.py --dry-run  # fetch + print

Suggested cron (daily, after the spot refresh):
    45 13 * * * cd /path/to/trade-analysis-app && .venv/bin/python scripts/run_bydfi_liquidity_capture.py
"""

import argparse
import json
import logging
from datetime import datetime, timezone

from onchain_console.bydfi import (
    build_risk_tier_profile_rows,
    fetch_risk_limits,
    parse_risk_limits,
)
from onchain_console.db import get_connection

log = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="do not write to DB")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    limits = parse_risk_limits(fetch_risk_limits())
    captured_at = datetime.now(timezone.utc)

    with get_connection() as conn:
        instrument_ids = dict(
            conn.execute(
                """
                SELECT symbol, id FROM instruments
                WHERE venue = 'BydFi' AND status = 'active'
                """
            ).fetchall()
        )
        rows = build_risk_tier_profile_rows(instrument_ids, limits, captured_at)

        # seeded symbols the API response didn't cover
        missing = sorted(set(instrument_ids) - set(limits))
        for symbol in missing:
            log.warning("seeded symbol %s missing from risk_limits response",
                        symbol)

        if args.dry_run:
            log.info("dry run — %d rows built, nothing written", len(rows))
        else:
            with conn.cursor() as cur:
                for lp in rows:
                    cur.execute(
                        """
                        INSERT INTO liquidity_profiles
                            (instrument_id, captured_at, profile_type,
                             provenance, payload)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            lp["instrument_id"], lp["captured_at"],
                            lp["profile_type"], lp["provenance"],
                            json.dumps(lp["payload"]),
                        ),
                    )
            conn.commit()
            log.info("inserted %d liquidity_profiles rows", len(rows))

    id_to_symbol = {v: k for k, v in instrument_ids.items()}
    for lp in rows:
        print(f"{id_to_symbol[lp['instrument_id']]:14s} "
              f"{lp['profile_type']}/{lp['provenance']} "
              f"tiers={len(lp['payload'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
