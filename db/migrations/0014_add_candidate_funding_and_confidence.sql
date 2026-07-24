-- 0014_add_candidate_funding_and_confidence.sql
-- APPROVED SCHEMA ADDITION (not in blueprint §5.6): asked Caleb 2026-07-23
-- per the ask-first rule, same precedent as the header of
-- 0008_add_spot_prices_table.sql. Both additions below were his call.
--
-- This migration closes two gaps where the blueprint contradicts itself.
--
-- GAP 1 — the §7.3 funding costs had nowhere to live.
--   §7.3 computes funding_cost_base / _optimistic / _stress and says
--   "Store all three on the candidate", but §5.6's own field table never
--   lists them, so 0001_schema created trade_candidates without them.
--   src/onchain_console/scoring.py::compute_hold_period_funding (build
--   step 3) returns all three under exactly those names today.
--
--   All three are NULLABLE on purpose. The scoring engine deliberately
--   returns None rather than a fabricated number when the 7-day funding
--   window has insufficient coverage — NULL is a real, meaningful result
--   here ("we do not know"), not missing data. A DEFAULT or NOT NULL on
--   these columns would destroy the distinction this build step exists to
--   protect. Do not add one.
--
-- GAP 2 — trade_candidates carried no confidence at all.
--   CLAUDE.md's hard rule and blueprint Rule 5 (§9) both state that every
--   thesis AND trade candidate carries data_confidence and
--   signal_confidence, but §5.6's field table omits them, so only theses
--   (0001_schema) ever got the columns. Added here so step 6's candidate
--   generator (M7) can satisfy the rule without a blocking migration.
--   Nullable for now — nothing writes trade_candidates yet; M7 can tighten
--   to NOT NULL once it populates them on every insert.
--
-- On funding_confidence specifically, and why this does NOT contradict
-- 0013's "no data_confidence column on purpose":
--   0013 declined a stored confidence because its cap was a pure function
--   of `provenance`, which IS stored — so the value was always exactly
--   recomputable, and storing it would have duplicated a scoring rule.
--   The §7.3 tier is different in kind: it is derived from how much of the
--   7-day window had real coverage AT GENERATION TIME. That window slides.
--   Recomputing it when reviewing a candidate days later answers a
--   different question than the one the stored costs were built on, so the
--   tier is point-in-time evidence about a stored number, not a derivable
--   property of it. Caleb chose to pin it (2026-07-23).
--   The 0013 rule still stands unchanged for liquidity/provenance.
--
-- NULL semantics for funding_confidence: NULL means the window was too
-- thin to support any tier at all (calcs.confidence_tier returns None
-- below 0.25 coverage), which is the same case in which the three cost
-- columns are NULL. It does NOT mean "not yet computed".

BEGIN;

ALTER TABLE trade_candidates
    ADD COLUMN funding_cost_base       numeric,
    ADD COLUMN funding_cost_optimistic numeric,
    ADD COLUMN funding_cost_stress     numeric,
    ADD COLUMN funding_confidence      confidence_tier,
    ADD COLUMN data_confidence         confidence_tier,
    ADD COLUMN signal_confidence       confidence_tier;

COMMENT ON COLUMN trade_candidates.funding_cost_base IS
    '§7.3 current funding × n, per unit of notional. NULL = insufficient window coverage.';
COMMENT ON COLUMN trade_candidates.funding_cost_optimistic IS
    '§7.3 mean funding over the 7-day window × n, per unit of notional.';
COMMENT ON COLUMN trade_candidates.funding_cost_stress IS
    '§7.3 90th-percentile adverse funding × n, per unit of notional. Long perspective.';
COMMENT ON COLUMN trade_candidates.funding_confidence IS
    'Window coverage tier behind the funding_cost_* figures at generation time. NULL = window too thin for any tier.';

COMMIT;
