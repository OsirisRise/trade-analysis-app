-- 0012_seed_bydfi_instruments.sql
-- BydFi (CEX) commodity perps, every symbol confirmed against the live
-- public API on 2026-07-13 (GET https://www.bydfi.com/swap/public/symbols,
-- 405 contracts, no auth). Screenshots were treated as leads only, per the
-- corrections-log rule.
--
-- Live-verification notes (2026-07-13):
--   * All nine contracts: status=0 (uniform across all 405 live contracts =
--     normal/trading), settle/quote = USDT, funding interval 4 HOURS
--     (funding_interval_minutes = 240 — not Hyperliquid's 60).
--   * reverse/inverse flag = false on ALL nine → linear USDT-margined, so
--     the §7.2 funding formula applies unchanged. Nothing inverse-margined
--     was seeded.
--   * NATGAS-USDT was NOT in Caleb's screenshots but IS live (mark ~2.86 —
--     real natural gas). Do not confuse with GAS-USDT, which is the NEO
--     "Gas" crypto token, not a commodity.
--   * PLATINUM: no XPT/platinum contract exists on BydFi (full-universe
--     check) — deliberately not seeded.
--   * No open-interest endpoint exists (documented or probed live) →
--     supports_open_interest = false; §8 Rule 3 crowding logic cannot use
--     OI on this venue.
--
-- GUARD — three distinct gold instruments on one venue (same spirit as the
-- Hyperliquid-PAXG-vs-CoinGecko-PAXG guard in 0002):
--   XAU-USDT  = synthetic gold-price perp (index from XAU reference)
--   XAUT-USDT = perp on Tether Gold token
--   PAXG-USDT = perp on Paxos Gold token
-- These are THREE SEPARATE ROWS tracking the same underlying ('gold').
-- Never merge or dedupe them: their funding, leverage caps, and index
-- construction differ. And PAXG-USDT here is a LEVERAGED PERPETUAL — it is
-- NOT the existing Ethereum/tokenized_spot PAXG row, which must keep
-- pricing from CoinGecko spot with no funding drag (§7.7's worked example
-- depends on that row staying funding-free).

BEGIN;

INSERT INTO instruments
    (venue, instrument_type, symbol, underlying, chain, quote_asset,
     oracle_source, funding_interval_minutes,
     supports_open_interest, supports_funding, supports_oracle_price,
     status, tradeable, venue_type)
VALUES
    ('BydFi', 'perp', 'XAU-USDT',    'gold',            NULL, 'USDT',
     'BydFi index price', 240, false, true, true, 'active', true, 'CEX'),
    ('BydFi', 'perp', 'XAUT-USDT',   'gold',            NULL, 'USDT',
     'BydFi index price', 240, false, true, true, 'active', true, 'CEX'),
    ('BydFi', 'perp', 'PAXG-USDT',   'gold',            NULL, 'USDT',
     'BydFi index price', 240, false, true, true, 'active', true, 'CEX'),
    ('BydFi', 'perp', 'XAG-USDT',    'silver',          NULL, 'USDT',
     'BydFi index price', 240, false, true, true, 'active', true, 'CEX'),
    ('BydFi', 'perp', 'COPPER-USDT', 'copper_spot',     NULL, 'USDT',
     'BydFi index price', 240, false, true, true, 'active', true, 'CEX'),
    ('BydFi', 'perp', 'XPD-USDT',    'palladium',       NULL, 'USDT',
     'BydFi index price', 240, false, true, true, 'active', true, 'CEX'),
    ('BydFi', 'perp', 'CL-USDT',     'wti_crude_oil',   NULL, 'USDT',
     'BydFi index price', 240, false, true, true, 'active', true, 'CEX'),
    ('BydFi', 'perp', 'BZ-USDT',     'brent_crude_oil', NULL, 'USDT',
     'BydFi index price', 240, false, true, true, 'active', true, 'CEX'),
    ('BydFi', 'perp', 'NATGAS-USDT', 'natural_gas',     NULL, 'USDT',
     'BydFi index price', 240, false, true, true, 'active', true, 'CEX');

COMMIT;
