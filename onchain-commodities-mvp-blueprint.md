# On-Chain Commodities Swing-Trade Intelligence App — MVP Blueprint

**Version:** 1.0
**Owner:** Caleb Bartlett
**Purpose:** A personal, analysis-only macro intelligence console that ingests global events, maps them to commodity theses, and evaluates on-chain trade expression (perps + tokenized commodities) for manually executed swing trades held over days to weeks.
**Scope guardrail:** This app does **not** place trades, connect wallets, or route orders. It is an analyst, not a broker. All execution is manual by Caleb on the venue of his choice.
**Build environment:** Claude Code, Python/Node ingestion, PostgreSQL, lightweight web UI.

---

## 1. Product Thesis

For a swing trader, being "right on direction" is not the same as making a good trade. The instrument you use to express a view carries costs — funding on perps, tracking error versus the real commodity, thin liquidity, and crowding risk — that can quietly erode or even invert returns over a multi-day hold.

This app answers, on one screen:

- **What changed?** (event ingestion + classification)
- **Why does it matter?** (thesis mapping)
- **Which commodities are affected, and in which direction?** (sensitivity rules)
- **What is the likely time horizon?** (event + thesis metadata)
- **Which on-chain instrument expresses the view best?** (instrument-fit scoring)
- **What are the main frictions or reasons not to trade?** (funding drag, tracking error, liquidity, crowding)

Design bias: **a curated decision-support console, not a trading terminal.** Start narrow (gold + crude oil), be honest about confidence, and keep all quantitative logic deterministic in code — the LLM classifies and summarizes; it never computes the numbers that drive decisions.

---

## 2. System Architecture (3-Layer)

```
┌─────────────────────────────────────────────────────────────┐
│ LAYER 1 — EVENT INTELLIGENCE                                 │
│  Ingest: GDELT, RSS (Reuters/central banks/EIA/USDA/OPEC),   │
│  FRED releases, sanctions/tariff bulletins, manual notes     │
│  → Normalize → Dedupe → Tag (rules first, LLM enrichment)    │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│ LAYER 2 — THESIS ENGINE                                      │
│  Map tagged events → commodity implications via              │
│  commodity_sensitivity_rules → generate/update theses        │
│  with direction, horizon, conviction, novelty, crowding      │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│ LAYER 3 — MARKET VALIDATION & TRADE EXPRESSION               │
│  Snapshot on-chain instruments (Hyperliquid, Ostium, tokens) │
│  → Compute premium, funding cost, tracking error, liquidity, │
│  carry, instrument-fit, composite score → trade_candidates   │
│  → Manual review + journal feedback loop                     │
└──────────────────────────────────────────────────────────────┘
```

**What was deliberately removed** (because this is analysis-only): auto-routing to exchanges, API order placement, execution/failover/retry logic, wallet actions, and smart-contract transaction orchestration.

---

## 3. Modules

Eight modules, buildable in sequence. Each is independently testable.

### M1 — Event Ingestion Service
- Pulls from GDELT DOC 2.0 API, RSS feeds, FRED release calendar, and a manual-entry endpoint.
- Normalizes into the `events` table; deduplicates via `dedupe_hash`.
- Runs hourly for news feeds; daily for slow macro releases.
- **Key rule:** always store `raw_payload` (jsonb) so parsers can improve later without re-fetching.

### M2 — Tagging & Classification Engine
- **Stage 1 (deterministic):** keyword/regex rules assign obvious tags (commodity names, country codes, policy keywords). `assigned_by = rules`.
- **Stage 2 (LLM enrichment):** Claude classifies severity, actor class, sentiment, conflict type, and expected horizon for events the rules can't fully resolve. `assigned_by = llm`.
- Writes to `event_tags` with a `confidence` value per tag.

### M3 — Thesis Engine
- Consumes tagged events + `commodity_sensitivity_rules` to create or update `theses`.
- Applies recency decay so stale headlines fade unless reinforced.
- Computes `conviction_score`, `novelty_score`, `crowding_risk_score`, `funding_risk_score`.

### M4 — Instrument Registry
- Static-ish reference table (`instruments`) of on-chain vehicles and their capabilities (supports funding? oracle price? OI?).
- Seed data below in §6.

### M5 — Market Snapshot Service
- Polls venue APIs and writes time-series rows to `market_snapshots`.
- Hyperliquid: one `metaAndAssetCtxs` call returns mark, mid, oracle, premium, funding, OI, volume, and impact prices for the whole universe — cheap and complete.
- Snapshot cadence: hourly for perps (matches Hyperliquid's hourly funding), daily for tokenized assets and macro series.

### M6 — Scoring & Analytics Engine
- Deterministic Python. Computes all metrics in §7 (premium, funding cost projections, tracking error, liquidity, carry, instrument-fit, thesis, composite).
- Materializes `candidate_scores` and `instrument_health` views.

### M7 — Trade Candidate Generator
- Joins active theses to suitable instruments, applies the §8 logic rules (including "good thesis, bad vehicle" suppression), and writes `trade_candidates` for review.

### M8 — Console UI + Journal
- Web dashboard: event feed, thesis cards, commodity watchlists, and a "trade expression" panel per candidate.
- Journal module (`journal_entries`) captures thesis, catalyst, invalidation, horizon, chosen vehicle, and post-trade review — including the crucial `result_structurally_good` field.
- Alerts are "review this setup," never "execute now."

---

## 4. Data Sources (Free / Low-Cost)

| Category | Source | Endpoint / Access | Auth | Notes & Limits |
|---|---|---|---|---|
| On-chain perps (primary) | **Hyperliquid** | `POST https://api.hyperliquid.xyz/info` with `{"type":"metaAndAssetCtxs"}` for full universe; `{"type":"fundingHistory","coin":...}` for history; `{"type":"predictedFundings"}` for cross-venue | None | Free public API. Info requests weighted (most weight 20; `fundingHistory` adds weight per 20 items). Funding paid **hourly**, capped 4%/hr. ([Hyperliquid API docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals), [Funding docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)) |
| On-chain perps (RWA/commodities) | **Ostium** (Arbitrum) | No documented public REST API. Read data via the **Ostium subgraph** on Arbitrum; funding/rollover ("Net Rate L/S") shown per pair in-app; oracle pricing from **Stork Network** | None (subgraph) | 71 markets incl. **7 commodities**, forex, indices, stocks. Treat as secondary source; confirm live rate on-page before trading. ([Ostium FAQ](https://www.ostium.com/blog/funding-rates-low-slippage-best-perps-dexs-2026)) |
| DeFi ecosystem / protocol context | **DefiLlama** | Free API base `https://api.llama.fi` (full list at `/llms-free.txt`) | None | No auth on free endpoints. Good for protocol TVL, perp-DEX volume context, token/protocol metrics. ([DefiLlama API docs](https://api-docs.defillama.com)) |
| Token prices / metadata | **CoinGecko** | Demo (free) plan | Free key (Demo) | Demo: **100 calls/min, 10,000 calls/month**. Keyless is ~10–30/min shared and unsuitable for scheduled polling — get a Demo key. ([CoinGecko pricing](https://www.coingecko.com/en/api/pricing), [rate limits](https://docs.coingecko.com/docs/errors-and-rate-limits)) |
| Global events / news | **GDELT DOC 2.0** | `https://api.gdeltproject.org/api/v2/doc/doc` (query, JSONFeed output) | None | Free realtime global event/article feed with fulltext search; English search recommended. ([GDELT DOC 2.0](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/), [GDELT data](https://www.gdeltproject.org/data.html)) |
| Macro series | **FRED** (St. Louis Fed) | API `https://fred.stlouisfed.org/docs/api/fred/`; also **no-auth CSV fallback** per series | Free key (optional) | ~800k series: rates, inflation, DXY proxies, spreads, recession indicators. CSV fallback needs no key. ([FRED API](https://fred.stlouisfed.org/docs/api/fred/)) |
| Commodity reference prices | Public/gov releases + delayed feeds (EIA, USDA) + Hyperliquid/Ostium oracle prices | RSS + agency APIs | Varies | Truly-free real-time CME/ICE futures data is limited/delayed; use oracle + delayed references for research, confirm live before trading. |

**Reliability note to keep in the app:** free news + delayed commodity data are good enough for research, not for institutional-grade real-time execution. Every thesis and candidate must carry an explicit confidence tier (§8, Rule 5).

---

## 5. Database Schema

PostgreSQL. Use `jsonb` for raw payloads and flexible tags. Add the **TimescaleDB** extension later if `market_snapshots` grows large. UUID primary keys throughout.

### 5.1 `events`
Raw + normalized event inputs.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `source_type` | enum | `gdelt`, `rss`, `fred_release`, `manual` |
| `source_name` | text | |
| `source_url` | text | |
| `headline` | text | |
| `summary` | text | |
| `published_at` | timestamptz | |
| `event_time_start` | timestamptz null | |
| `event_time_end` | timestamptz null | |
| `country_codes` | text[] | |
| `region_codes` | text[] | |
| `actors` | text[] | |
| `raw_payload` | jsonb | preserve source fidelity |
| `ingested_at` | timestamptz | default now() |
| `dedupe_hash` | text UNIQUE | normalized headline + source + date window |

### 5.2 `event_tags`
Machine-usable factors extracted from events.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `event_id` | UUID FK → events | |
| `tag_type` | enum | `commodity`, `macro_factor`, `policy_type`, `conflict_type`, `geography`, `actor_class`, `sentiment`, `severity` |
| `tag_value` | text | e.g. `gold`, `crude_oil`, `real_yields_down`, `sanction`, `shipping_disruption`, `high` |
| `confidence` | numeric(4,3) | 0–1 |
| `assigned_by` | enum | `rules`, `llm`, `manual` |

### 5.3 `theses`
Interpreted market implications.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `title` | text | |
| `description` | text | |
| `status` | enum | `active`, `invalidated`, `resolved`, `archived` |
| `direction` | enum | `bullish`, `bearish`, `mixed`, `watch` |
| `time_horizon` | enum | `1_3_days`, `1_2_weeks`, `2_6_weeks` |
| `conviction_score` | numeric | 0–100 |
| `novelty_score` | numeric | 0–100 |
| `crowding_risk_score` | numeric | 0–100 |
| `funding_risk_score` | numeric | 0–100 |
| `data_confidence` | enum | `low`, `medium`, `high` |
| `signal_confidence` | enum | `low`, `medium`, `high` |
| `created_at` / `updated_at` | timestamptz | |

Join tables:
- `thesis_events (thesis_id, event_id)`
- `thesis_commodities (thesis_id, commodity_code)`

### 5.4 `instruments`
On-chain vehicles.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `venue` | text | `Hyperliquid`, `Ostium`, `Ethereum` (for tokens) |
| `instrument_type` | enum | `perp`, `tokenized_spot` |
| `symbol` | text | venue symbol |
| `underlying` | text | `gold`, `wti_crude`, `brent`, `silver`, etc. |
| `chain` | text null | |
| `quote_asset` | text null | `USDC`, `USDT` |
| `oracle_source` | text null | e.g. Hyperliquid validator median, Stork |
| `funding_interval_minutes` | int null | Hyperliquid = 60 |
| `supports_open_interest` | bool | |
| `supports_funding` | bool | |
| `supports_oracle_price` | bool | |
| `status` | enum | `active`, `inactive`, `deprecated` |

### 5.5 `market_snapshots`
Time-series state per instrument.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `instrument_id` | UUID FK | |
| `captured_at` | timestamptz | |
| `mark_price` | numeric | `markPx` |
| `mid_price` | numeric null | `midPx` |
| `oracle_price` | numeric null | `oraclePx` |
| `reference_spot_price` | numeric null | external/delayed reference |
| `premium_pct` | numeric null | see §7.1 |
| `funding_rate_interval` | numeric null | raw interval rate (`funding`) |
| `funding_rate_8h_equiv` | numeric null | normalized |
| `funding_apr_est` | numeric null | context only |
| `open_interest_usd` | numeric null | `openInterest` × price |
| `day_volume_usd` | numeric null | `dayNtlVlm` |
| `impact_bid_price` | numeric null | from `impactPxs` |
| `impact_ask_price` | numeric null | from `impactPxs` |
| `spread_bps_est` | numeric null | |
| `tracking_error_bps` | numeric null | see §7.4 |
| `liquidity_score` | numeric null | see §7.5 |
| `raw_payload` | jsonb | |

### 5.6 `trade_candidates`
Analyst-ready setups for manual review.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `thesis_id` | UUID FK | |
| `instrument_id` | UUID FK | |
| `generated_at` | timestamptz | |
| `setup_type` | enum | `momentum`, `mean_reversion`, `event_followthrough`, `hedge`, `safe_haven` |
| `direction` | enum | `long`, `short`, `avoid` |
| `entry_zone_text` | text | |
| `invalidation_text` | text | |
| `hold_period_days_base` | int | |
| `expected_catalysts` | text[] | |
| `risk_notes` | text[] | |
| `thesis_score` | numeric | 0–100 |
| `instrument_fit_score` | numeric | 0–100 |
| `carry_score` | numeric | 0–100 |
| `execution_quality_score` | numeric | 0–100 |
| `composite_score` | numeric | 0–100 |
| `review_status` | enum | `new`, `shortlisted`, `rejected`, `archived` |
| `explanation` | text | human-readable rationale |

### 5.7 `journal_entries`
Feedback loop.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `trade_candidate_id` | UUID FK null | |
| `thesis_id` | UUID FK null | |
| `entry_date` | date | |
| `exit_date` | date null | |
| `manual_action` | enum | `watched`, `entered_elsewhere`, `passed`, `exited` |
| `instrument_used` | text | |
| `reason_for_entry` | text | |
| `reason_for_pass` | text | |
| `invalidation_condition` | text | |
| `post_mortem` | text | |
| `result_directionally_correct` | bool null | |
| `result_structurally_good` | bool null | **the vehicle-quality verdict** |
| `notes` | text | |

### 5.8 `commodity_sensitivity_rules` (config)
Drives event → commodity mapping.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `trigger_tag` | text | e.g. `sanction`, `shipping_disruption`, `real_yields_down` |
| `commodity_code` | text | `gold`, `crude_oil`, ... |
| `expected_direction` | enum | `bullish`, `bearish`, `mixed` |
| `strength_weight` | numeric | 0–1 |
| `half_life_hours` | numeric | recency decay input |

### 5.9 `watchlists`
| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `name` | text | e.g. "gold macro", "energy crisis", "ag squeeze" |
| `commodity_codes` | text[] | |
| `notes` | text | |

---

## 6. Seed Data (Instruments)

Populate `instruments` with a focused MVP set (gold + crude first). Confirm exact venue symbols at build time.

| venue | type | underlying | funding? | oracle? | OI? | notes |
|---|---|---|---|---|---|---|
| Hyperliquid | perp | gold | yes (hourly) | yes | yes | full metaAndAssetCtxs coverage |
| Hyperliquid | perp | silver | yes | yes | yes | |
| Hyperliquid | perp | wti_crude | yes | yes | yes | e.g. CL / USOIL |
| Hyperliquid | perp | brent | yes | yes | yes | BRENTOIL |
| Ostium | perp | gold | yes (rollover) | yes (Stork) | via subgraph | RWA rollover model |
| Ostium | perp | crude_oil | yes | yes (Stork) | via subgraph | one of 7 commodities |
| Ethereum | tokenized_spot | gold (PAXG) | n/a | via CoinGecko | n/a | spot-like, no funding drag |
| Ethereum | tokenized_spot | gold (XAUT) | n/a | via CoinGecko | n/a | spot-like alternative |

---

## 7. Calculations (Deterministic — in code, never LLM)

### 7.1 Premium to oracle
\[
\text{premium\_pct} = \frac{\text{mark\_price} - \text{oracle\_price}}{\text{oracle\_price}}
\]
Positive → longs likely pay funding. Negative → shorts likely pay. (Hyperliquid defines `premium` directly in `metaAndAssetCtxs`.)

### 7.2 Single-interval funding payment
\[
\text{funding\_payment} = \text{position\_size} \times \text{oracle\_price} \times \text{funding\_rate\_interval}
\]
Note: Hyperliquid uses **oracle price** (not mark) to convert size to notional. Expose as: cost per \$1k notional, cost per planned hold, and an annualized figure **as context only**.

### 7.3 Hold-period funding estimate (the swing-trade metric that matters most)
\[
\text{expected\_funding\_cost} = \sum_{t=1}^{n} (\text{notional}_t \times \text{funding\_rate}_t)
\]
For the MVP, approximate over `n` intervals (Hyperliquid: `n = hold_days × 24`):
- `funding_cost_base` = current funding × n
- `funding_cost_optimistic` = 7-day avg funding × n
- `funding_cost_stress` = 90th-percentile adverse funding × n

Store all three on the candidate.

### 7.4 Tracking error
\[
\text{tracking\_error\_bps} = 10000 \times \frac{\text{mark\_price} - \text{reference\_spot\_price}}{\text{reference\_spot\_price}}
\]
Compute current, 7-day average absolute, and 30-day max absolute.

### 7.5 Liquidity proxy score (0–100)
Composite of normalized inputs: day volume (↑ better), open interest (↑ better), estimated spread (↓ better), impact-price slippage from `impactPxs` (↓ better). Min-max normalize each input across the instrument universe, then weight-average.

### 7.6 Carry score (0–100)
- Longs: lower/negative expected hold-period funding cost → higher score.
- Shorts: positive funding receipts → higher score.
- Penalize unstable funding (high 7-day funding volatility) and inconsistent funding direction.

### 7.7 Instrument-fit score (0–100)
\[
\text{instrument\_fit} = 0.30(\text{underlying\_match}) + 0.20(\text{liquidity}) + 0.20(\text{carry}) + 0.20(\text{tracking}) + 0.10(\text{venue\_quality})
\]
Example outcomes: a gold safe-haven thesis may rank **PAXG above a gold perp** when funding is punitive and leverage is unnecessary; an oil short may rank the **perp higher** because tokenized spot-oil options are weak/absent.

### 7.8 Thesis score (0–100)
Inputs: number/quality of supporting events, recency decay, source credibility, cross-source confirmation, commodity-sensitivity match, regime alignment. Weight official/primary sources above social reposts; confirmed event + price reaction + rising volume beats an unconfirmed rumor.

### 7.9 Composite opportunity score (UI ranking field)
\[
\text{composite} = 0.35(\text{thesis}) + 0.25(\text{instrument\_fit}) + 0.15(\text{carry}) + 0.15(\text{liquidity}) + 0.10(100 - \text{crowding\_risk})
\]
This prevents the strongest narrative from winning when the instrument expression is poor.

---

## 8. Logic Rules

**Rule 1 — Event-to-commodity mapping.** Configurable via `commodity_sensitivity_rules`. Examples:
- central-bank easing → gold bullish bias
- Middle East conflict escalation → crude + gold bullish
- sanctions on an oil exporter → crude bullish
- drought / crop disease / export ban → agriculture bullish
- recession shock → industrial commodities bearish, gold mixed (rates/liquidity dependent)

**Rule 2 — Recency decay.**
\[
\text{event\_weight\_now} = \text{base\_weight} \times e^{-\lambda t}
\]
`λ` derived from the rule's `half_life_hours`. Old headlines fade unless reinforced by new evidence.

**Rule 3 — Crowding warning.**
- high positive funding + rising OI → crowded-long risk
- high negative funding + rising OI → crowded-short risk
Surface as a warning flag on the thesis and candidate.

**Rule 4 — Avoid weak trade expression ("good thesis, bad vehicle").** Set `direction = avoid` and explain when any of:
- liquidity score below threshold
- tracking error too high
- expected hold-period funding cost too punitive
- no suitable instrument exists for that commodity (common for agriculture)

**Rule 5 — Confidence tiers.** Every thesis and candidate carries `data_confidence` and `signal_confidence` (low/medium/high). A gold thesis on multiple official sources + clean market data can be high/high; a corn thesis from noisy headlines with poor on-chain expression may be medium/low. Never imply institutional-grade certainty from free data.

---

## 9. Implementation Notes

- **PostgreSQL** with `jsonb` for raw payloads and tags; add **TimescaleDB** for `market_snapshots` when needed.
- **Rules-first, LLM-second.** Deterministic rules assign obvious tags; the LLM only enriches and summarizes.
- **The LLM is never the source of truth for numbers.** Funding math, tracking math, scoring, and rule evaluation stay in code.
- **Snapshot jobs:** hourly for perps (aligns with Hyperliquid hourly funding), daily for macro/tokenized series.
- **Materialized views:** `candidate_scores` and `instrument_health` for fast UI reads.
- **Backfill option:** Dune Hyperliquid tables or `fundingHistory` for historical funding when building percentile/stress inputs.
- **Respect rate limits:** CoinGecko Demo 100/min & 10k/month → get a Demo key, don't run keyless polling. Hyperliquid info weights → batch via one `metaAndAssetCtxs` call rather than per-coin polling.

---

## 10. Build Sequence (MVP → v1)

1. **Schema + migrations** (all tables §5) and seed `instruments` (§6) + `commodity_sensitivity_rules` (§8 R1).
2. **M5 Market Snapshot Service** for Hyperliquid `metaAndAssetCtxs` — get real data flowing first.
3. **M6 Scoring Engine** — implement §7 calcs against live snapshots (start with premium, funding projections, tracking error, liquidity).
4. **M1 Event Ingestion** — GDELT + FRED + a few RSS feeds + manual entry.
5. **M2 Tagging** — deterministic rules first, then LLM enrichment.
6. **M3 Thesis Engine** + **M7 Candidate Generator** with logic rules §8.
7. **M8 Console UI** — event feed, thesis cards, trade-expression panel, journal.
8. Add Ostium (subgraph) + tokenized gold (CoinGecko) as second-wave instruments.

**Definition of MVP done:** for a live event (e.g., an OPEC headline), the app ingests it, tags it, generates a gold and/or crude thesis, ranks the best on-chain instrument with funding/tracking/liquidity context, and shows a candidate card with a clear "trade this / good thesis, bad vehicle / avoid" verdict and confidence tiers — all without any execution capability.

---

## 11. Claude Code Handoff Prompt

Paste the following into Claude Code to start the build.

```
PROJECT: On-Chain Commodities Swing-Trade Intelligence Console (personal, analysis-only)

GOAL
Build a local macro-intelligence app that ingests global events, maps them to
commodity theses (start: gold + WTI/Brent crude), and evaluates on-chain trade
expression (perps + tokenized gold) for MANUAL swing trades held days-to-weeks.
This app NEVER places trades, connects wallets, or routes orders. It is an
analyst dashboard only.

STACK
- PostgreSQL (jsonb for raw payloads/tags; design so TimescaleDB can be added later)
- Python for ingestion + a deterministic scoring engine
- Node or Python web backend + a lightweight web UI (macro console style, not a
  trading terminal)
- LLM (me/Claude) used ONLY for event classification + summaries, NEVER for
  numeric calculations

HARD RULES
1. All funding/tracking/scoring math is deterministic in code. The LLM never
   computes decision numbers.
2. Rules-first tagging; LLM enrichment second.
3. Every thesis and trade candidate carries data_confidence and signal_confidence
   (low/medium/high). Never imply institutional-grade certainty from free data.
4. The app must be able to output "good thesis, bad vehicle / avoid" — a strong
   narrative must not win if instrument expression is poor.

DATA SOURCES (all free/low-cost)
- Hyperliquid info API: POST https://api.hyperliquid.xyz/info
    {"type":"metaAndAssetCtxs"}  -> mark, mid, oracle, premium, funding, OI,
                                    dayNtlVlm, impactPxs for full perp universe
    {"type":"fundingHistory","coin":"...","startTime":ms} -> historical funding
    {"type":"predictedFundings"} -> cross-venue predicted funding
    Funding is hourly, capped 4%/hr; funding payment uses ORACLE price for notional.
- Ostium (Arbitrum): no public REST; read via Ostium subgraph; oracle = Stork.
    7 commodities among 71 markets. Secondary source.
- DefiLlama free API: https://api.llama.fi (no auth) for protocol/perp-DEX context.
- CoinGecko Demo API (free key, 100/min, 10k/month) for PAXG/XAUT token prices.
- GDELT DOC 2.0: https://api.gdeltproject.org/api/v2/doc/doc (JSONFeed) for events.
- FRED: https://fred.stlouisfed.org/docs/api/fred/ (free optional key; CSV
    fallback needs no key) for macro series (rates, DXY proxies, inflation).

BUILD THE SCHEMA
Implement these tables exactly (UUID PKs, timestamptz):
events, event_tags, theses (+ thesis_events, thesis_commodities), instruments,
market_snapshots, trade_candidates, journal_entries, commodity_sensitivity_rules,
watchlists.
[Paste sections 5 and 6 of the blueprint here for exact field lists + seed data.]

IMPLEMENT CALCULATIONS (see blueprint §7 for formulas)
premium_pct; single-interval funding payment (oracle-notional); hold-period
funding (base / optimistic-7d-avg / stress-90th-pctile over hold_days*24 intervals);
tracking_error_bps (current, 7d avg abs, 30d max abs); liquidity_score (0-100 from
volume/OI/spread/impact); carry_score (0-100); instrument_fit (0.30 underlying +
0.20 liquidity + 0.20 carry + 0.20 tracking + 0.10 venue); thesis_score;
composite (0.35 thesis + 0.25 fit + 0.15 carry + 0.15 liquidity + 0.10*(100-crowding)).

IMPLEMENT LOGIC RULES (blueprint §8)
event->commodity mapping via commodity_sensitivity_rules; recency decay
(exp(-lambda*t) from half_life_hours); crowding warning (funding sign + rising OI);
avoid-weak-expression; confidence tiers.

BUILD ORDER
1) schema + migrations + seed instruments & sensitivity rules
2) Hyperliquid snapshot service (hourly) -> market_snapshots
3) scoring engine over live snapshots
4) GDELT + FRED + RSS + manual event ingestion (dedupe via dedupe_hash)
5) rules tagging -> LLM enrichment
6) thesis engine + candidate generator with logic rules
7) console UI (event feed, thesis cards, trade-expression panel) + journal
8) add Ostium subgraph + CoinGecko tokenized gold

MVP ACCEPTANCE TEST
Given a live OPEC/crude or gold-macro headline, the app must: ingest -> tag ->
generate a thesis -> rank the best on-chain instrument with funding/tracking/
liquidity context -> show a candidate card with a clear verdict
(trade / good-thesis-bad-vehicle / avoid) and confidence tiers, with NO execution
capability anywhere in the system.

Start by scaffolding the repo, the Postgres schema, and the Hyperliquid snapshot
service. Show me the migration files and the snapshot service before moving on.
```

---

## 12. Risks & Reminders

- **Instrument mismatch:** you're trading synthetic perps, not CME contracts — tracking error and funding distort the thesis.
- **Funding drag:** you can be directionally right and still lose edge to sustained adverse funding; treat funding as part of the thesis, not a footnote.
- **Liquidity:** on-chain commodity markets are thin outside gold and oil; agriculture may have no usable vehicle.
- **Oracle/latency:** news can move real markets before on-chain venues update cleanly, and vice-versa during shocks.
- **Data licensing:** free sources are fine for research, not for institutional-grade real-time execution — always confirm live pricing on the venue page before a manual trade.
- **Regulatory/tax:** even for personal use, on-chain commodity activity can carry reporting and tax complexity.

---

*Sources: [Hyperliquid perpetuals info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals), [Hyperliquid funding mechanics](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding), [Hyperliquid rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits), [Ostium FAQ / market coverage](https://www.ostium.com/blog/funding-rates-low-slippage-best-perps-dexs-2026), [DefiLlama free API](https://api-docs.defillama.com), [CoinGecko API pricing](https://www.coingecko.com/en/api/pricing) and [rate limits](https://docs.coingecko.com/docs/errors-and-rate-limits), [GDELT DOC 2.0 API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) and [GDELT data hub](https://www.gdeltproject.org/data.html), [FRED API](https://fred.stlouisfed.org/docs/api/fred/).*
