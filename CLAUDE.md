# CLAUDE.md — On-Chain Commodities Swing-Trade Intelligence Console

## What this is
Personal, analysis-only macro intelligence console. Ingests global events, maps
them to commodity theses, and evaluates on-chain trade expression (Hyperliquid
perps, Ostium, tokenized gold) for MANUAL swing trades held days-to-weeks.
Full spec: `onchain-commodities-mvp-blueprint.md` in this repo — treat it as
canonical unless I tell you it's changed.

## Hard rules (never violate)
- No execution capability, ever: no order placement, no wallet connection or
  signing, no key custody, no auto-routing. If a task implies any of this,
  stop and flag it instead of building it.
- All funding/tracking/scoring math is deterministic Python, never LLM-computed.
  Don't estimate or eyeball a number that should come from code — write the
  function, test it, call it.
- Tagging is rules-first (regex/keyword). LLM enrichment only covers what the
  rules can't resolve.
- Every thesis and trade candidate carries `data_confidence` and
  `signal_confidence` (low/medium/high). Never imply institutional-grade
  certainty from free data sources.
- Use the exact field names, table schemas, and formulas from the blueprint
  (§5–§8) — don't invent alternatives or "improve" them without asking first.
- Hyperliquid, Ostium, and BydFi are ALL modeled as tradeable venues
  (tradeable = true), alongside PAXG/XAUT tokenized spot. Which venue Caleb
  actually uses for any manual trade is his discretion and outside this
  app's concern — the app never executes anything on any venue, ever.
  Candidate generation (M7) still filters instruments WHERE tradeable =
  true; the column remains meaningful metadata even with all venues on.

## Stack
- PostgreSQL (jsonb for raw payloads/tags, UUID PKs; TimescaleDB added later
  for `market_snapshots`)
- Python for ingestion + the scoring engine
- Node or Python for the web backend

## Verification habit — do this before calling anything done
- Any calculation function (premium_pct, funding cost, tracking error,
  liquidity, carry, instrument-fit, composite score): write pytest tests using
  known inputs/expected outputs derived from blueprint §7, run them, and only
  report done once they're green. Show me the test output, not just a claim.
- Any ingestion/tagging code: run it against a small real or fixture payload
  and show me actual output, not a description of expected behavior.

## Design decisions (binding on future build steps)

- 2026-07-12 — **Energy reference prices measure physical spot, not the
  futures the perps track.** EIA daily spot (WTI `RWTC`, Brent `RBRTE`,
  Henry Hub `RNGWHHD`) is a physical/FOB price with a structural basis gap
  to the front-month futures that perp oracles follow, AND it publishes
  T-2..T-6 late. Commodities with this known basis gap: `wti_crude_oil`,
  `brent_crude_oil`, `natural_gas`. Metals (`gold`, `silver`, `platinum`,
  `palladium`, `copper_spot`) have NO such gap — Metals.Dev spot is
  near-real-time (≤60s) and prices the same thing the metal perps track.
  Decision (Caleb, 2026-07-12): keep EIA as the energy reference for now
  and handle the gap with an explicit confidence downgrade plus
  staleness-awareness — never treat an energy spot-vs-mark gap as a clean
  tracking-error signal. RULE for build step 3: the §7.4
  tracking_error_bps / data_confidence logic MUST branch energy vs. metals
  on this categorization. Caleb is separately researching a real
  futures-price feed — do not build toward one until he decides.
  IMPLEMENTED 2026-07-24 (build step 3): `calcs.basis_category()` does the
  categorization (energy / metal / unknown, where unknown is treated like
  energy — conservatively downgraded), `calcs.downgrade_tier()` applies the
  one-step confidence downgrade, and `scoring.compute_tracking_error()`
  branches on it — `has_structural_basis_gap` and both pre-/post-downgrade
  tiers travel with every result. §7.7 substitutes a neutral tracking
  sub-score for energy rather than feeding basis bps into fidelity
  (`scoring._tracking_subscore`). Proven live 2026-07-24: metals tracked
  ~1-2 bps while Brent/WTI showed ~550-590 bps of basis, correctly
  confidence-downgraded to None. Still no futures feed — unchanged.
- 2026-07-14 — **liquidity_profiles (0013) holds all liquidity/risk-tier
  signals in one table, labeled by provenance.** Three kinds of signal
  feed the future §7.5 liquidity score: real resting orders from actual
  matching engines (Hyperliquid l2Book; someday BydFi WS depth), venue
  risk configuration (Hyperliquid marginTables, BydFi risk_limit tiers),
  and synthetic simulations (Ostium getSimOrderbook/getSimSlippage —
  oracle+vault model, no matching engine exists). RULE for build step 3:
  real and synthetic signals must NEVER be presented as equivalent
  numbers — provenance travels with the value wherever it surfaces, and
  any §7.5 input with provenance='synthetic_simulation' caps that
  instrument's data_confidence at 'medium'. Two legs are DELIBERATELY
  deferred, not missing: (1) BydFi order-book depth — WebSocket-only, no
  WS client exists in this project; (2) Ostium synthetic-sim capture —
  waits for step 8's full Ostium activation. Don't "fix" either in
  passing.
  IMPLEMENTED 2026-07-24 (build step 3): `calcs.cap_tier()` clamps a tier
  to an absolute ceiling and `scoring.compute_liquidity_score()` applies
  the 'medium' ceiling when any of the instrument's latest
  liquidity_profiles rows carries provenance='synthetic_simulation',
  reporting `synthetic_cap_applied` and `data_confidence_pre_cap` so the
  adjustment is auditable. The cap is DORMANT today (no writer emits a
  synthetic row yet — leg 2 above) but wired and tested so it fires the
  instant one appears; don't treat the absence of live synthetic rows as
  license to remove it.
- 2026-07-24 — **Build step 3 (§7.3-7.7 scoring engine) is COMPLETE:
  history-sufficiency confidence, hold-period funding, tracking error,
  liquidity, carry, and instrument-fit.** Layering is deliberate and must
  be kept: `history.py` reads the DB and decides how much real history
  exists; `calcs.py` is pure deterministic math with NO DB access (adding
  a `conn` param to it is a layering violation); `scoring.py` composes the
  two and attaches confidence. Every scoring result carries a confidence
  tier, and thin/absent history returns an explicit insufficient-data
  result (reason named — e.g. `no_history`, `zero_span`,
  `no_market_snapshot`, `no_funding_interval`) rather than a fabricated
  number.
  - **History-sufficiency mechanism (why it exists).** Production history
    is only weeks old and BydFi has none, so every window formula (7-day
    funding, 30-day tracking) could either crash on thin data or compute a
    precise-looking number from two days of it. `history.py` is the single
    place that measures how much real history backs a window — coverage is
    the SPAN of the data, never the row count (two rows a week apart is 7
    days of coverage, not "2 days"; a single point spans 0 days and cannot
    support a window aggregate). `calcs.window_confidence()` turns that
    span into a 0-1 ratio and `calcs.confidence_tier()` into a tier, so no
    §7 formula re-derives sufficiency and none can silently skip asking.
  - **Underlying-level spot fallback (an architectural choice, not an
    implementation detail).** §7.4 pairs each mark with a reference spot.
    An instrument's own `market_snapshots.reference_spot_price` column and
    the shared `spot_prices` series for its underlying price the SAME thing
    (0008 populates the former from the latter), so `scoring.compute_
    tracking_error()` prefers whichever covers MORE of the window. This is
    what lets a newly-listed contract (a new BydFi gold perp) inherit
    gold's existing shared spot history immediately instead of waiting out
    its own bootstrap period before §7.4 can say anything — deliberately
    chosen so thin per-instrument history is rescued by commodity-level
    history. Pairing is strictly as-of (a mark takes the newest spot at or
    before it, never a later one — no lookahead); see
    `scoring.pair_as_of()`.
  - **First-pass definitions — all flagged in-code for Caleb's review, one
    line each (reasoning lives in the cited docstring, not here):**
    - Confidence-tier thresholds (None<0.25, low<0.5, medium<0.8, high) —
      `calcs.confidence_tier()` and the `WINDOW_CONFIDENCE_*_FLOOR` consts.
    - §7.5 liquidity sub-weights (vol .35 / OI .25 / spread .25 / impact
      slippage .15) — `calcs.LIQUIDITY_WEIGHTS`.
    - §7.6 carry stability + direction penalties (pstdev-vs-reference and
      sign-flip fraction, shrinking the score toward neutral) —
      `calcs.carry_score()`, `funding_stability_penalty()`,
      `funding_direction_penalty()`, and `CARRY_REFERENCE_RATE`.
    - §7.7 `underlying_match` (exact 100 / crude grades 75 / precious
      complex 50 / else 0) — `calcs.underlying_match()`.
    - §7.7 `venue_quality` (tradeable gate × depth-data-availability +
      venue architecture) — `calcs.venue_quality()`.
  - **§7.8 (thesis_score) and §7.9 (composite score) are DEFERRED to build
    step 6, NOT built here.** They depend on the `theses` table, which is
    empty until event ingestion (step 4) and thesis generation populate it;
    building them now would be scoring against nothing. `scoring.py` stops
    at instrument-fit deliberately — do not add thesis/composite scoring
    until step 6.
  - Also landed alongside: migration 0014 added the §7.3 `funding_cost_*`
    columns plus candidate-level `data_confidence`/`signal_confidence` to
    `trade_candidates` (nullable — NULL is a real "insufficient" result,
    not missing data; don't add a DEFAULT). `scripts/run_scoring_report.py`
    runs the whole chain against live data.

## Corrections log
(Add an entry here every time I correct something, so it isn't repeated.)

- 2026-07-10 — Wrongly concluded "no Brent market exists on Hyperliquid" and
  skipped seeding it. Root cause: the universe check printed only the first 40
  of 100 xyz-dex symbols, so `xyz:BRENTOIL` (~$109M day vol, ~$161M OI) was
  never seen. Fixed in 0004_add_brent_instrument.sql. Lesson: when verifying
  whether something exists in an API response, inspect the FULL list —
  never a truncated/`head`-ed view.
- 2026-07-12 — Seeded a generic 'crude_oil' underlying (and mixed
  'wti_crude'/'brent' codes) instead of per-grade codes. Two independent
  sources (ostium_commodities_schema.csv from Ostium's own docs, and
  hyperliquid_commodities_schema.csv) agree the real vocabulary is
  `wti_crude_oil` / `brent_crude_oil` — canonical everywhere
  now (0006_standardize_crude_vocabulary.sql). One commodity vocabulary
  across theses, rules, and instruments, or the step-6 joins silently miss.
- 2026-07-12 — hyperliquid_commodities_schema.csv listed symbols (HG-PERP,
  NG-PERP, XPT-PERP, XPD-PERP) that don't exist on Hyperliquid; the live
  xyz-dex symbols are xyz:COPPER / xyz:NATGAS / xyz:PLATINUM / xyz:PALLADIUM.
  Lesson: a third-party list identifies WHAT to check, never a symbol to
  seed directly — always confirm the exact string against the live API.
- 2026-07-12 — Ostium rows were seeded with quote_asset='USDC', but Ostium's
  own docs quote every market against USD (USDC is the collateral token, not
  the quote currency); and Hyperliquid copper was seeded as 'copper' where
  Ostium's docs use 'copper_spot'. Both fixed in 0009. Lesson: label
  vocabulary comes from the venue's own docs, and collateral ≠ quote.
- 2026-07-13 — Replaced the 2026-07-12 hard rule "Hyperliquid is
  REFERENCE-ONLY / Ostium the only tradeable venue." This is a decision
  change, not a contradiction: the earlier rule reflected Caleb's US
  trading-access situation at the time, which he says has since evolved.
  All three venues (Hyperliquid, Ostium, BydFi) are now modeled tradeable
  (0010). The app's own boundary is unchanged either way — analysis-only,
  no execution capability on any venue.
