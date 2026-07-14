-- 0013_add_liquidity_profiles.sql
-- Liquidity/risk-tier capture table (Task 11 proposal, approved by Caleb;
-- prompt 1 of 5 in the liquidity_profiles series).
--
-- Design notes:
--   * All three profile_type / provenance values exist NOW even though
--     'synthetic_sim' / 'synthetic_simulation' has no writer yet — that is
--     Ostium's getSimOrderbook/getSimSlippage data, explicitly deferred to
--     build step 8. Declaring the full value set today avoids a future
--     migration whose only job is adding an enum value.
--   * BydFi order-book depth (profile_type='order_book' on BydFi rows) is
--     NOT populated by this series: BydFi depth is WebSocket-only and no
--     WebSocket client exists in this project. Deliberate gap, not an
--     oversight. BydFi rows in this table will be risk_tiers only for now.
--   * provenance travels with every payload so real resting orders
--     (Hyperliquid l2Book, future BydFi WS depth), venue risk configuration
--     (Hyperliquid marginTables, BydFi risk_limit tiers), and synthetic
--     simulations (Ostium — no matching engine exists; oracle+vault model)
--     are never presented as the same kind of number.
--   * No data_confidence column on purpose: the confidence cap for
--     synthetic provenance is a build-step-3 SCORING RULE, not a stored
--     value (documented in prompt 5 of this series).

BEGIN;

CREATE TABLE liquidity_profiles (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_id uuid NOT NULL REFERENCES instruments (id),
    captured_at   timestamptz NOT NULL,
    profile_type  text NOT NULL CHECK (profile_type IN
                      ('order_book', 'risk_tiers', 'synthetic_sim')),
    provenance    text NOT NULL CHECK (provenance IN
                      ('real_resting_orders', 'venue_risk_config',
                       'synthetic_simulation')),
    payload       jsonb NOT NULL
);

-- Read pattern matches market_snapshots:
-- LEFT JOIN LATERAL ... ORDER BY captured_at DESC LIMIT 1
CREATE INDEX liquidity_profiles_instrument_time_idx
    ON liquidity_profiles (instrument_id, captured_at DESC);

COMMIT;
