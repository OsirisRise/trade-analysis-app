# Build Step 3 — Scoring Engine (§7.3–§7.7)

**Status:** Complete · **Date:** 2026-07-24 · **Suite:** 278 tests passing
· **Scope delivered:** hold-period funding, tracking error, liquidity,
carry, and instrument-fit scoring, plus the history-sufficiency confidence
layer that underpins all of them.

---

## 1. Executive summary

The console can now score an on-chain commodity instrument on the four
dimensions the blueprint calls for (§7.3 funding cost, §7.4 tracking error,
§7.5 liquidity, §7.6 carry) and compose them into a §7.7 instrument-fit
verdict. The engine's defining property is honesty about thin data: with
only a couple of weeks of production history — and none at all for BydFi —
every score either reports a real number **with a confidence tier attached**
or returns an explicit "insufficient data" result naming the reason. It
never fabricates a precise-looking figure from two days of history.

This was delivered as an isolated, self-contained build step. Event
ingestion (step 4) and thesis/candidate generation (step 6) are untouched;
the two blueprint scores that depend on them (§7.8 thesis score, §7.9
composite) are deliberately **not** built, to avoid scoring against an empty
`theses` table.

## 2. What was built

| Area | Blueprint | What it does |
|---|---|---|
| History sufficiency | — | Measures how much *real* history backs a lookback window (by span, not row count) and converts it to a confidence tier. The single source of truth every other score consults. |
| Hold-period funding | §7.3 | base / optimistic / stress funding cost over a hold, generalized to each venue's own funding cadence. |
| Tracking error | §7.4 | Current, 7-day-avg, and 30-day-max drift of the venue mark vs. real-world spot, with the energy-vs-metals branch. |
| Liquidity proxy | §7.5 | 0–100 composite of volume / open interest / spread / impact-slippage, min-max normalized across the active universe, with the synthetic-data confidence cap. |
| Carry | §7.6 | 0–100 direction-aware carry quality, penalizing unstable and direction-flipping funding. |
| Instrument-fit | §7.7 | Weighted composite of the above plus `underlying_match` and `venue_quality`. |

**Architecture — a deliberate three-layer split (now recorded as a binding
rule in CLAUDE.md):**

- `history.py` — reads the database, decides how much real history exists.
- `calcs.py` — pure deterministic math, **no database access**. Trivially
  unit-testable; this is where every number a decision depends on is
  computed (never estimated by an LLM, per the project's hard rule).
- `scoring.py` — composes the two, attaches confidence, and is the only
  layer that touches both a connection and the math.

## 3. Proven against live data

On 2026-07-24 the whole chain was run against the live production database
via the new `scripts/run_scoring_report.py`. Two findings worth the PM's
attention:

1. **It degrades gracefully, verifiably.** In a single run, Hyperliquid
   instruments returned real, high-confidence liquidity scores and tight
   metals tracking (gold 1.6 bps, silver 1.7 bps), while every BydFi
   instrument returned honest "insufficient data" across the board — and
   the energy instruments' large tracking gaps (Brent/WTI ~550–590 bps)
   were correctly confidence-downgraded rather than presented as clean
   signals. That side-by-side is the proof the safety behavior works
   outside of tests.

2. **An operational gap surfaced (needs a decision).** The hourly snapshot
   capture and daily spot-refresh jobs had not run since 2026-07-13/14 — a
   ~10-day stall. They were restarted manually to produce the live report,
   but **nothing is currently scheduling them.** Until a scheduler
   (cron/systemd) runs `scripts/run_snapshot.py` hourly and
   `scripts/run_spot_refresh.py` daily, the window-based scores (funding,
   carry) cannot accumulate the multi-day history they need. This is the
   top operational follow-up.

## 4. Decisions that need Product/PM review

These were built as reasoned **first passes** and flagged in-code for
Caleb's review — they are working definitions, not settled calibration.
None blocks progress, but each is a knob that affects candidate ranking.

- **Two genuinely undefined §7.7 inputs.** `underlying_match` (how well an
  instrument's commodity expresses a thesis's commodity) and `venue_quality`
  were absent from the blueprint entirely and had to be defined from
  scratch. These warrant the closest look.
- **Confidence-tier thresholds** (how much window coverage earns
  low/medium/high).
- **Liquidity sub-weights** (volume/OI/spread/slippage split).
- **Carry stability formula** (how volatility and direction-flips discount
  a carry score).
- **A ranking trap to resolve before step 6.** Because instrument-fit
  computes on whatever data exists, a metadata-only instrument (no market
  data) can numerically outscore a fully-data-backed one — precisely
  because it lacks the data that would pull it down. Confidence is the
  guardrail, but step 6's candidate generation must rank by confidence
  first (or exclude metadata-only), not by raw score.

## 5. Two prior rules confirmed implemented

- **Energy-vs-metals basis rule (decided 2026-07-12).** Energy reference
  prices carry a structural basis gap to what the perps track; the rule
  that this must never be treated as a clean tracking signal is now enforced
  in code and proven live (see §3 above).
- **Synthetic-data confidence cap (decided 2026-07-14).** Any liquidity
  input derived from a synthetic simulation caps that instrument's data
  confidence at "medium." Implemented, tested, and auditable — currently
  dormant because no synthetic data source is live yet (that's a step-8
  item), but wired to fire the moment one appears.

## 6. Database change

Migration `0014` adds the three §7.3 funding-cost columns to
`trade_candidates`, plus the candidate-level `data_confidence` /
`signal_confidence` columns that the project's hard rules require but the
original schema omitted. All nullable by design — NULL is a real
"insufficient" result, not missing data. **This migration is written but has
not been applied to any database yet;** apply with
`scripts/apply_migrations.py` when ready.

## 7. Verification

- **278 automated tests pass** from a clean state — pure-function unit tests
  with hand-derived expected values, and integration tests that run against
  a real local Postgres inside always-rolled-back transactions (they skip
  cleanly, never fail, when Postgres is absent).
- Every calculation was checked against hand-derived values or live data
  before being called done, per the project's verification habit.

## 8. Explicitly deferred (do not build early)

- **§7.8 thesis score** and **§7.9 composite score** — depend on the
  `theses` table, empty until steps 4 and 6. Deferred to step 6.
- **Real futures-price feed** for energy — Caleb is researching separately;
  no code should be built toward it until he decides.
- **BydFi order-book depth** (WebSocket-only) and **Ostium synthetic-sim
  capture** — deferred to later steps by prior design decisions.

## 9. Recommended next actions

1. **Schedule the capture jobs** (top priority) so history accumulates.
2. Review the five first-pass definitions in §4, especially the two
   undefined §7.7 inputs.
3. Apply migration `0014` when ready to persist scoring output.
4. Proceed to build step 4 (event ingestion) — **only on Caleb's explicit
   go-ahead**, per the close-out of this step.
