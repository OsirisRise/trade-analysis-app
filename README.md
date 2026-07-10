# On-Chain Commodities Swing-Trade Intelligence Console

Personal, **analysis-only** macro intelligence console. Ingests global events,
maps them to commodity theses (gold + WTI/Brent crude first), and evaluates
on-chain trade expression (Hyperliquid perps, Ostium, tokenized gold) for
manual swing trades held days-to-weeks.

**This app never places trades, connects wallets, or routes orders.**
Full spec: `onchain-commodities-mvp-blueprint.md` (canonical). Working rules:
`CLAUDE.md`.

## Status (build order, blueprint §10)

- [x] 1. Schema + migrations + seed instruments & sensitivity rules
- [x] 2. Hyperliquid snapshot service (M5) → `market_snapshots`
- [ ] 3. Scoring engine (M6) — §7.3–§7.9 over snapshot history
- [ ] 4. Event ingestion (M1) — GDELT + FRED + RSS + manual
- [ ] 5. Rules tagging → LLM enrichment (M2)
- [ ] 6. Thesis engine (M3) + candidate generator (M7)
- [ ] 7. Console UI + journal (M8)
- [ ] 8. Ostium subgraph + CoinGecko tokenized gold

## Setup

```bash
# Postgres 17 (Homebrew, keg-only) runs as a background service: starts at
# login, auto-restarts, and the generated launchd plist pins
# LC_ALL=en_US.UTF-8 (without it the postmaster dies on macOS with
# "became multithreaded during startup" — do not remove it).
brew services start postgresql@17                              # once; persists
/opt/homebrew/opt/postgresql@17/bin/createdb trade_analysis   # first time only

# Python
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env   # adjust DATABASE_URL if needed

# Migrations (idempotent; tracked in schema_migrations)
.venv/bin/python scripts/apply_migrations.py
```

## Run

```bash
.venv/bin/python scripts/run_snapshot.py            # fetch + write snapshots
.venv/bin/python scripts/run_snapshot.py --dry-run  # fetch + print only
.venv/bin/python -m pytest                          # test suite
```

Schedule the snapshot hourly (matches Hyperliquid's hourly funding):

```cron
5 * * * * cd /Users/calebbartlett/trade-analysis-app && .venv/bin/python scripts/run_snapshot.py
```

## Layout

```
db/migrations/           numbered SQL migrations (schema §5, seeds §6 + §8 R1)
src/onchain_console/
  calcs.py               deterministic §7 math (Decimal; never LLM-computed)
  hyperliquid.py         info-API client + metaAndAssetCtxs parsing
  snapshot_service.py    M5: instruments → fetch per dex → market_snapshots
scripts/                 apply_migrations.py, run_snapshot.py
tests/                   pytest suite + real fixture payload
```

## Venue notes (confirmed 2026-07-10)

- Hyperliquid commodity perps live on the **xyz builder dex** (HIP-3), not the
  main universe: `xyz:GOLD`, `xyz:SILVER`, `xyz:CL` (WTI — the UI displays it
  as "WTIOIL-USDC"), `xyz:BRENTOIL` (Brent). Query with
  `{"type":"metaAndAssetCtxs","dex":"xyz"}`. The xyz dex has ~100 markets,
  including other commodities (COPPER, NATGAS, TTF, PLATINUM, PALLADIUM,
  URANIUM, ALUMINIUM, CORN, WHEAT) available for later expansion.
- The `km`/`mkts` (Kinetiq) dex lists GOLD/SILVER/USOIL but had zero volume/OI
  and stale marks — not seeded.
- **PAXG source guard:** Hyperliquid's main dex has a leveraged PAXG-USDC
  perp. The seeded Ethereum/tokenized_spot PAXG row is NOT that instrument —
  it must always be priced via CoinGecko spot data (its §7.7 role is the
  funding-drag-free gold expression).
- Ostium rows are seeded `inactive` until the subgraph integration (step 8)
  confirms pair symbols.
