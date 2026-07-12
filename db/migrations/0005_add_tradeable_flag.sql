-- 0005_add_tradeable_flag.sql
-- Venue-legality split (decision 2026-07-12): Caleb cannot legally trade
-- Hyperliquid from the US — Ostium is the ONLY tradeable venue.
--
-- Hyperliquid rows stay status='active': they are still polled by the
-- snapshot service and still drive reference pricing/funding context.
-- tradeable=false only removes them from trade-candidate generation:
-- build step 6's candidate generator (M7) MUST filter WHERE tradeable = true
-- before writing to trade_candidates. M5 (snapshots) and M6 (scoring) need
-- no code changes.
--
-- PAXG/XAUT (Ethereum tokenized spot gold) remain tradeable=true — confirmed
-- with Caleb 2026-07-12: plain ERC-20 tokens, not venue-restricted
-- derivatives, and the §7.7 funding-drag-free gold expression should stay
-- eligible as a candidate.

BEGIN;

ALTER TABLE instruments ADD COLUMN tradeable boolean NOT NULL DEFAULT true;

UPDATE instruments SET tradeable = false WHERE venue = 'Hyperliquid';

COMMIT;
