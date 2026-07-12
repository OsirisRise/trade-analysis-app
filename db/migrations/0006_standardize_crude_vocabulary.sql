-- 0006_standardize_crude_vocabulary.sql
-- Correction (2026-07-12): the seeds used three different crude vocabularies
-- ('wti_crude', 'brent', 'crude_oil'), which would break the thesis→
-- instrument join in build step 6. Two independent sources (ostium_symbols
-- from Ostium's docs; a Hyperliquid pairs list) agree on per-grade codes.
-- Canonical underlying codes everywhere from now on:
--   wti_crude_oil, brent_crude_oil
-- Ostium's WTI symbol is WTI/USD (per Ostium docs), not CL/USD.

BEGIN;

-- Hyperliquid perps
UPDATE instruments SET underlying = 'wti_crude_oil'
WHERE venue = 'Hyperliquid' AND symbol = 'xyz:CL' AND underlying = 'wti_crude';

UPDATE instruments SET underlying = 'brent_crude_oil'
WHERE venue = 'Hyperliquid' AND symbol = 'xyz:BRENTOIL' AND underlying = 'brent';

-- Ostium WTI row: fix both symbol and underlying
UPDATE instruments SET symbol = 'WTI/USD', underlying = 'wti_crude_oil'
WHERE venue = 'Ostium' AND symbol = 'CL/USD' AND underlying = 'crude_oil';

-- New Ostium Brent row. status='inactive' only means "not polled yet" —
-- pending step 8 subgraph confirmation. tradeable=true: Ostium is the
-- tradeable venue (0005).
INSERT INTO instruments
    (venue, instrument_type, symbol, underlying, chain, quote_asset,
     oracle_source, funding_interval_minutes,
     supports_open_interest, supports_funding, supports_oracle_price,
     status, tradeable)
VALUES
    ('Ostium', 'perp', 'BRENT/USD', 'brent_crude_oil', 'Arbitrum', 'USDC',
     'Stork', NULL, true, true, true, 'inactive', true);

-- Sensitivity rules: each generic 'crude_oil' rule becomes one WTI rule and
-- one Brent rule with identical weights/half-lives, then the generic row
-- is removed.
INSERT INTO commodity_sensitivity_rules
    (trigger_tag, commodity_code, expected_direction, strength_weight, half_life_hours)
SELECT r.trigger_tag, v.code, r.expected_direction, r.strength_weight, r.half_life_hours
FROM commodity_sensitivity_rules r
CROSS JOIN (VALUES ('wti_crude_oil'), ('brent_crude_oil')) AS v(code)
WHERE r.commodity_code = 'crude_oil';

DELETE FROM commodity_sensitivity_rules WHERE commodity_code = 'crude_oil';

COMMIT;
