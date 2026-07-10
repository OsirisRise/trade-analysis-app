-- 0003_seed_sensitivity_rules.sql
-- Seed commodity_sensitivity_rules per blueprint §8 Rule 1 examples.
-- trigger_tag values match the tag_value vocabulary used by the tagging
-- engine (M2). strength_weight 0-1; half_life_hours drives recency decay
-- (§8 Rule 2: weight_now = base_weight * exp(-lambda * t)).

BEGIN;

INSERT INTO commodity_sensitivity_rules
    (trigger_tag, commodity_code, expected_direction, strength_weight, half_life_hours)
VALUES
    -- central-bank easing → gold bullish bias
    ('central_bank_easing',       'gold',        'bullish', 0.70, 168),
    ('real_yields_down',          'gold',        'bullish', 0.80, 168),

    -- Middle East conflict escalation → crude + gold bullish
    ('mideast_conflict_escalation', 'crude_oil', 'bullish', 0.80, 72),
    ('mideast_conflict_escalation', 'gold',      'bullish', 0.60, 72),

    -- sanctions on an oil exporter → crude bullish
    ('sanction_oil_exporter',     'crude_oil',   'bullish', 0.75, 120),

    -- shipping disruption → crude bullish
    ('shipping_disruption',       'crude_oil',   'bullish', 0.65, 96),

    -- drought / crop disease / export ban → agriculture bullish
    ('drought',                   'agriculture', 'bullish', 0.60, 336),
    ('crop_disease',              'agriculture', 'bullish', 0.60, 336),
    ('export_ban_agriculture',    'agriculture', 'bullish', 0.70, 240),

    -- recession shock → industrial commodities bearish, gold mixed
    ('recession_shock',           'industrial_metals', 'bearish', 0.70, 336),
    ('recession_shock',           'crude_oil',         'bearish', 0.65, 336),
    ('recession_shock',           'gold',              'mixed',   0.50, 336);

COMMIT;
