-- 0010_all_venues_tradeable.sql
-- Scope change (Caleb, 2026-07-13): Hyperliquid, Ostium, and BydFi are all
-- modeled as tradeable. The 0005 rule ("Hyperliquid reference-only — US
-- legal access") is superseded: per Caleb, the US trading-access situation
-- evolved. Which venue he actually uses for any manual trade is his
-- discretion and outside this app's concern either way — the app remains
-- analysis-only and never executes anything on any venue.
--
-- The tradeable column stays (still meaningful metadata and the M7
-- candidate-generator filter still reads it); it just no longer gates
-- Hyperliquid out.

BEGIN;

UPDATE instruments SET tradeable = true
WHERE venue IN ('Hyperliquid', 'Ostium');

COMMIT;
