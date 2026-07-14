-- 0011_add_venue_type.sql
-- CEX/DEX classification per venue. Hyperliquid (on-chain L1 order book)
-- and Ostium (Arbitrum oracle/vault perps) are DEXes; BydFi (0012) is a
-- centralized exchange. The Ethereum tokenized-spot rows (PAXG/XAUT) are
-- tokens, not venues — venue_type stays NULL for them deliberately.

BEGIN;

ALTER TABLE instruments
    ADD COLUMN venue_type text CHECK (venue_type IN ('CEX', 'DEX'));

UPDATE instruments SET venue_type = 'DEX'
WHERE venue IN ('Hyperliquid', 'Ostium');

COMMIT;
