-- 0008_add_spot_prices_table.sql
-- APPROVED SCHEMA ADDITION (not in blueprint §5): asked Caleb 2026-07-12 per
-- the ask-first rule; he chose a dedicated spot-price ledger over stamping
-- snapshot rows only.
--
-- One row per commodity per source per observation date. This is the source
-- of truth for real-world reference prices; market_snapshots.reference_spot_price
-- (§5.5, unused since 0001) is populated FROM this table:
--   * at snapshot insert time (each new row copies the latest known spot), and
--   * by the daily spot refresh, which re-stamps the most recent snapshot row
--     per matching active instrument.
-- The §7.4 tracking-error aggregates (7d avg abs / 30d max abs) in build
-- step 3 read this table directly for their daily spot series.
--
-- Sources (confirmed live 2026-07-12):
--   * Metals.Dev /v1/latest — gold, silver, platinum, palladium (USD/toz)
--     and copper (converted toz -> lb in code, deterministic constants).
--     Free tier 100 req/month; daily polling ~30/month.
--   * EIA API v2 — WTI (series RWTC) and Brent (RBRTE) on
--     petroleum/pri/spt/data in $/BBL; Henry Hub natural gas (RNGWHHD) on
--     natural-gas/pri/fut/data in $/MMBTU. Series IDs confirmed against the
--     live facet lists, not hardcoded from memory. NOTE: EIA daily spot
--     publishes with a several-day lag (T-2 to T-6).

BEGIN;

CREATE TABLE spot_prices (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    commodity_code text NOT NULL,           -- matches instruments.underlying vocabulary
    price          numeric NOT NULL CHECK (price > 0),
    unit           text NOT NULL,           -- 'usd_per_toz', 'usd_per_lb', 'usd_per_bbl', 'usd_per_mmbtu'
    source         text NOT NULL,           -- 'metals.dev', 'eia'
    as_of          timestamptz NOT NULL,    -- observation time per the source
    captured_at    timestamptz NOT NULL DEFAULT now(),
    raw_payload    jsonb NOT NULL,
    UNIQUE (commodity_code, source, as_of)  -- daily job reruns are no-ops
);

CREATE INDEX spot_prices_commodity_time_idx
    ON spot_prices (commodity_code, as_of DESC);

COMMIT;
