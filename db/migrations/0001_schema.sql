-- 0001_schema.sql
-- Full schema per blueprint §5 (exact field names). UUID PKs, timestamptz.
-- gen_random_uuid() is built into PostgreSQL 13+.

BEGIN;

-- ---------------------------------------------------------------------------
-- Enum types
-- ---------------------------------------------------------------------------
CREATE TYPE source_type AS ENUM ('gdelt', 'rss', 'fred_release', 'manual');
CREATE TYPE tag_type AS ENUM (
    'commodity', 'macro_factor', 'policy_type', 'conflict_type',
    'geography', 'actor_class', 'sentiment', 'severity'
);
CREATE TYPE assigned_by AS ENUM ('rules', 'llm', 'manual');
CREATE TYPE thesis_status AS ENUM ('active', 'invalidated', 'resolved', 'archived');
CREATE TYPE thesis_direction AS ENUM ('bullish', 'bearish', 'mixed', 'watch');
CREATE TYPE time_horizon AS ENUM ('1_3_days', '1_2_weeks', '2_6_weeks');
CREATE TYPE confidence_tier AS ENUM ('low', 'medium', 'high');
CREATE TYPE instrument_type AS ENUM ('perp', 'tokenized_spot');
CREATE TYPE instrument_status AS ENUM ('active', 'inactive', 'deprecated');
CREATE TYPE setup_type AS ENUM (
    'momentum', 'mean_reversion', 'event_followthrough', 'hedge', 'safe_haven'
);
CREATE TYPE candidate_direction AS ENUM ('long', 'short', 'avoid');
CREATE TYPE review_status AS ENUM ('new', 'shortlisted', 'rejected', 'archived');
CREATE TYPE manual_action AS ENUM ('watched', 'entered_elsewhere', 'passed', 'exited');
CREATE TYPE expected_direction AS ENUM ('bullish', 'bearish', 'mixed');

-- ---------------------------------------------------------------------------
-- §5.1 events — raw + normalized event inputs
-- ---------------------------------------------------------------------------
CREATE TABLE events (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type      source_type NOT NULL,
    source_name      text NOT NULL,
    source_url       text,
    headline         text NOT NULL,
    summary          text,
    published_at     timestamptz NOT NULL,
    event_time_start timestamptz,
    event_time_end   timestamptz,
    country_codes    text[] NOT NULL DEFAULT '{}',
    region_codes     text[] NOT NULL DEFAULT '{}',
    actors           text[] NOT NULL DEFAULT '{}',
    raw_payload      jsonb NOT NULL,
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    dedupe_hash      text NOT NULL UNIQUE
);

CREATE INDEX events_published_at_idx ON events (published_at DESC);
CREATE INDEX events_source_type_idx ON events (source_type);

-- ---------------------------------------------------------------------------
-- §5.2 event_tags — machine-usable factors extracted from events
-- ---------------------------------------------------------------------------
CREATE TABLE event_tags (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id    uuid NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    tag_type    tag_type NOT NULL,
    tag_value   text NOT NULL,
    confidence  numeric(4,3) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    assigned_by assigned_by NOT NULL
);

CREATE INDEX event_tags_event_id_idx ON event_tags (event_id);
CREATE INDEX event_tags_type_value_idx ON event_tags (tag_type, tag_value);

-- ---------------------------------------------------------------------------
-- §5.3 theses — interpreted market implications
-- ---------------------------------------------------------------------------
CREATE TABLE theses (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title               text NOT NULL,
    description         text,
    status              thesis_status NOT NULL DEFAULT 'active',
    direction           thesis_direction NOT NULL,
    time_horizon        time_horizon NOT NULL,
    conviction_score    numeric NOT NULL CHECK (conviction_score BETWEEN 0 AND 100),
    novelty_score       numeric NOT NULL CHECK (novelty_score BETWEEN 0 AND 100),
    crowding_risk_score numeric NOT NULL CHECK (crowding_risk_score BETWEEN 0 AND 100),
    funding_risk_score  numeric NOT NULL CHECK (funding_risk_score BETWEEN 0 AND 100),
    data_confidence     confidence_tier NOT NULL,
    signal_confidence   confidence_tier NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE thesis_events (
    thesis_id uuid NOT NULL REFERENCES theses (id) ON DELETE CASCADE,
    event_id  uuid NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    PRIMARY KEY (thesis_id, event_id)
);

CREATE TABLE thesis_commodities (
    thesis_id      uuid NOT NULL REFERENCES theses (id) ON DELETE CASCADE,
    commodity_code text NOT NULL,
    PRIMARY KEY (thesis_id, commodity_code)
);

-- ---------------------------------------------------------------------------
-- §5.4 instruments — on-chain vehicles
-- ---------------------------------------------------------------------------
CREATE TABLE instruments (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    venue                    text NOT NULL,
    instrument_type          instrument_type NOT NULL,
    symbol                   text NOT NULL,
    underlying               text NOT NULL,
    chain                    text,
    quote_asset              text,
    oracle_source            text,
    funding_interval_minutes int,
    supports_open_interest   boolean NOT NULL,
    supports_funding         boolean NOT NULL,
    supports_oracle_price    boolean NOT NULL,
    status                   instrument_status NOT NULL DEFAULT 'active',
    UNIQUE (venue, symbol)
);

-- ---------------------------------------------------------------------------
-- §5.5 market_snapshots — time-series state per instrument
-- (design allows converting to a TimescaleDB hypertable later)
-- ---------------------------------------------------------------------------
CREATE TABLE market_snapshots (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_id         uuid NOT NULL REFERENCES instruments (id),
    captured_at           timestamptz NOT NULL,
    mark_price            numeric NOT NULL,
    mid_price             numeric,
    oracle_price          numeric,
    reference_spot_price  numeric,
    premium_pct           numeric,
    funding_rate_interval numeric,
    funding_rate_8h_equiv numeric,
    funding_apr_est       numeric,
    open_interest_usd     numeric,
    day_volume_usd        numeric,
    impact_bid_price      numeric,
    impact_ask_price      numeric,
    spread_bps_est        numeric,
    tracking_error_bps    numeric,
    liquidity_score       numeric,
    raw_payload           jsonb NOT NULL
);

CREATE INDEX market_snapshots_instrument_time_idx
    ON market_snapshots (instrument_id, captured_at DESC);

-- ---------------------------------------------------------------------------
-- §5.6 trade_candidates — analyst-ready setups for manual review
-- ---------------------------------------------------------------------------
CREATE TABLE trade_candidates (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    thesis_id               uuid NOT NULL REFERENCES theses (id),
    instrument_id           uuid NOT NULL REFERENCES instruments (id),
    generated_at            timestamptz NOT NULL DEFAULT now(),
    setup_type              setup_type NOT NULL,
    direction               candidate_direction NOT NULL,
    entry_zone_text         text,
    invalidation_text       text,
    hold_period_days_base   int,
    expected_catalysts      text[] NOT NULL DEFAULT '{}',
    risk_notes              text[] NOT NULL DEFAULT '{}',
    thesis_score            numeric NOT NULL CHECK (thesis_score BETWEEN 0 AND 100),
    instrument_fit_score    numeric NOT NULL CHECK (instrument_fit_score BETWEEN 0 AND 100),
    carry_score             numeric NOT NULL CHECK (carry_score BETWEEN 0 AND 100),
    execution_quality_score numeric NOT NULL CHECK (execution_quality_score BETWEEN 0 AND 100),
    composite_score         numeric NOT NULL CHECK (composite_score BETWEEN 0 AND 100),
    review_status           review_status NOT NULL DEFAULT 'new',
    explanation             text
);

CREATE INDEX trade_candidates_thesis_idx ON trade_candidates (thesis_id);
CREATE INDEX trade_candidates_review_idx ON trade_candidates (review_status, generated_at DESC);

-- ---------------------------------------------------------------------------
-- §5.7 journal_entries — feedback loop
-- ---------------------------------------------------------------------------
CREATE TABLE journal_entries (
    id                           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_candidate_id           uuid REFERENCES trade_candidates (id),
    thesis_id                    uuid REFERENCES theses (id),
    entry_date                   date NOT NULL,
    exit_date                    date,
    manual_action                manual_action NOT NULL,
    instrument_used              text,
    reason_for_entry             text,
    reason_for_pass              text,
    invalidation_condition       text,
    post_mortem                  text,
    result_directionally_correct boolean,
    result_structurally_good     boolean,
    notes                        text
);

-- ---------------------------------------------------------------------------
-- §5.8 commodity_sensitivity_rules (config) — drives event → commodity mapping
-- ---------------------------------------------------------------------------
CREATE TABLE commodity_sensitivity_rules (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trigger_tag        text NOT NULL,
    commodity_code     text NOT NULL,
    expected_direction expected_direction NOT NULL,
    strength_weight    numeric NOT NULL CHECK (strength_weight >= 0 AND strength_weight <= 1),
    half_life_hours    numeric NOT NULL CHECK (half_life_hours > 0),
    UNIQUE (trigger_tag, commodity_code)
);

-- ---------------------------------------------------------------------------
-- §5.9 watchlists
-- ---------------------------------------------------------------------------
CREATE TABLE watchlists (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL,
    commodity_codes text[] NOT NULL DEFAULT '{}',
    notes           text
);

COMMIT;
