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

## Corrections log
(Add an entry here every time I correct something, so it isn't repeated.)

- 2026-07-10 — Wrongly concluded "no Brent market exists on Hyperliquid" and
  skipped seeding it. Root cause: the universe check printed only the first 40
  of 100 xyz-dex symbols, so `xyz:BRENTOIL` (~$109M day vol, ~$161M OI) was
  never seen. Fixed in 0004_add_brent_instrument.sql. Lesson: when verifying
  whether something exists in an API response, inspect the FULL list —
  never a truncated/`head`-ed view.
