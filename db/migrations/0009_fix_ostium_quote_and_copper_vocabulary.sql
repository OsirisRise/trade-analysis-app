-- 0009_fix_ostium_quote_and_copper_vocabulary.sql
-- Two label corrections surfaced by the CSV audit against Ostium's own docs
-- (ostium_commodities_schema.csv, source: docs.ostium.com).
--
-- 1) Ostium quote_asset is USD, not USDC. Ostium pairs are quoted against
--    USD; USDC is the COLLATERAL token, not the quote currency. Hyperliquid
--    rows keep USDC — that label is correct for them.
--
-- 2) Copper vocabulary: Ostium's docs call their copper market
--    'copper_spot' (XCU/USD). The Hyperliquid reference row (0007) was
--    seeded as 'copper'. Same failure class as the crude_oil fix (0006) —
--    standardized now, while only one venue has copper seeded, so the
--    step-6 thesis→instrument joins can't silently split later.
--    Canonical code everywhere: copper_spot.

BEGIN;

UPDATE instruments SET quote_asset = 'USD'
WHERE venue = 'Ostium' AND quote_asset = 'USDC';

UPDATE instruments SET underlying = 'copper_spot'
WHERE venue = 'Hyperliquid' AND symbol = 'xyz:COPPER' AND underlying = 'copper';

-- Keep the spot ledger on the same vocabulary (no rows expected yet — the
-- metals feed hasn't run with a live key — but harmless if any exist).
UPDATE spot_prices SET commodity_code = 'copper_spot'
WHERE commodity_code = 'copper';

COMMIT;
