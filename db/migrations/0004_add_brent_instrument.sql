-- 0004_add_brent_instrument.sql
-- Correction: Brent DOES trade on Hyperliquid's xyz dex as xyz:BRENTOIL.
-- The 0002 seed omitted it because the build-time universe check truncated
-- the symbol list to the first 40 names (of 100). Confirmed live 2026-07-10:
--   xyz:BRENTOIL  day vol ~$109M, OI ~$161M, hourly funding, 20x max leverage
-- (0002 has already been applied, so this row lands via a new migration.)

BEGIN;

INSERT INTO instruments
    (venue, instrument_type, symbol, underlying, chain, quote_asset,
     oracle_source, funding_interval_minutes,
     supports_open_interest, supports_funding, supports_oracle_price, status)
VALUES
    ('Hyperliquid', 'perp', 'xyz:BRENTOIL', 'brent', 'Hyperliquid L1', 'USDC',
     'Hyperliquid validator median', 60, true, true, true, 'active');

COMMIT;
