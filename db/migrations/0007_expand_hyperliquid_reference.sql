-- 0007_expand_hyperliquid_reference.sql
-- Expand Hyperliquid reference-only coverage: copper, natural gas, platinum,
-- palladium.
--
-- Symbol provenance (2026-07-12): a third-party pairs list suggested these
-- four commodities under symbols HG-PERP / NG-PERP / XPT-PERP / XPD-PERP.
-- Those strings do NOT exist on Hyperliquid. Re-verified against the FULL
-- live xyz-dex universe (100 markets, no truncation — see CLAUDE.md
-- corrections log for the Brent lesson); the real symbols and live stats:
--   xyz:COPPER     day vol $0.35M, OI $9.28M
--   xyz:NATGAS     day vol $2.73M, OI $14.76M
--   xyz:PLATINUM   day vol $0.14M, OI $2.30M   (thin)
--   xyz:PALLADIUM  day vol $0.03M, OI $0.95M   (very thin)
-- All four have live marks/oracle/funding (unlike the dead km dex), so all
-- are seeded; the thin ones are fine as reference rows since tradeable=false
-- keeps every Hyperliquid market out of trade-candidate generation (0005).

BEGIN;

INSERT INTO instruments
    (venue, instrument_type, symbol, underlying, chain, quote_asset,
     oracle_source, funding_interval_minutes,
     supports_open_interest, supports_funding, supports_oracle_price,
     status, tradeable)
VALUES
    ('Hyperliquid', 'perp', 'xyz:COPPER',    'copper',      'Hyperliquid L1', 'USDC',
     'Hyperliquid validator median', 60, true, true, true, 'active', false),
    ('Hyperliquid', 'perp', 'xyz:NATGAS',    'natural_gas', 'Hyperliquid L1', 'USDC',
     'Hyperliquid validator median', 60, true, true, true, 'active', false),
    ('Hyperliquid', 'perp', 'xyz:PLATINUM',  'platinum',    'Hyperliquid L1', 'USDC',
     'Hyperliquid validator median', 60, true, true, true, 'active', false),
    ('Hyperliquid', 'perp', 'xyz:PALLADIUM', 'palladium',   'Hyperliquid L1', 'USDC',
     'Hyperliquid validator median', 60, true, true, true, 'active', false);

COMMIT;
