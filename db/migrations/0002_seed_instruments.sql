-- 0002_seed_instruments.sql
-- Seed instruments per blueprint §6, with venue symbols confirmed against the
-- live Hyperliquid API on 2026-07-10.
--
-- Symbol confirmation notes (2026-07-10):
--   * Commodity perps on Hyperliquid live on builder-deployed (HIP-3) dexes,
--     not the main perp universe. The "xyz" dex carries the liquid markets:
--       xyz:GOLD   (day vol ~$22M,  OI ~$138M)
--       xyz:SILVER (day vol ~$99M,  OI ~$75M)
--       xyz:CL     (day vol ~$179M, OI ~$209M)   <- WTI crude
--     The "km"/"mkts" (Kinetiq) dex also lists GOLD/SILVER/USOIL but showed
--     zero volume/OI and stale marks, so it is not seeded.
--   * BRENT: no Brent market exists anywhere on Hyperliquid as of 2026-07-10
--     (blueprint §6 said "BRENTOIL" pending confirmation — confirmation FAILED,
--     so no row is seeded; add it if/when a venue lists one).
--   * Ostium rows are seeded 'inactive' until the subgraph integration
--     (build step 8) confirms live pair symbols.
--   * PAXG/XAUT tokenized spot rows are active; they are priced via CoinGecko
--     (build step 8), not the Hyperliquid snapshot service.

BEGIN;

INSERT INTO instruments
    (venue, instrument_type, symbol, underlying, chain, quote_asset,
     oracle_source, funding_interval_minutes,
     supports_open_interest, supports_funding, supports_oracle_price, status)
VALUES
    -- Hyperliquid perps (xyz builder dex; hourly funding)
    ('Hyperliquid', 'perp', 'xyz:GOLD',   'gold',      'Hyperliquid L1', 'USDC',
     'Hyperliquid validator median', 60, true, true, true, 'active'),
    ('Hyperliquid', 'perp', 'xyz:SILVER', 'silver',    'Hyperliquid L1', 'USDC',
     'Hyperliquid validator median', 60, true, true, true, 'active'),
    ('Hyperliquid', 'perp', 'xyz:CL',     'wti_crude', 'Hyperliquid L1', 'USDC',
     'Hyperliquid validator median', 60, true, true, true, 'active'),

    -- Ostium perps (Arbitrum; rollover funding model; oracle = Stork).
    -- Inactive until symbols are confirmed via the Ostium subgraph (step 8).
    ('Ostium', 'perp', 'XAU/USD', 'gold',      'Arbitrum', 'USDC',
     'Stork', NULL, true, true, true, 'inactive'),
    ('Ostium', 'perp', 'CL/USD',  'crude_oil', 'Arbitrum', 'USDC',
     'Stork', NULL, true, true, true, 'inactive'),

    -- Tokenized spot gold (no funding drag; priced via CoinGecko in step 8)
    ('Ethereum', 'tokenized_spot', 'PAXG', 'gold', 'Ethereum', NULL,
     'CoinGecko', NULL, false, false, false, 'active'),
    ('Ethereum', 'tokenized_spot', 'XAUT', 'gold', 'Ethereum', NULL,
     'CoinGecko', NULL, false, false, false, 'active');

COMMIT;
