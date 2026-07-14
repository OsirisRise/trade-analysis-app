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
- [x] 2b. Reference spot prices (Metals.Dev + EIA) → `spot_prices` ledger
      + `reference_spot_price` stamping (daily cadence)
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
.venv/bin/python scripts/run_spot_refresh.py        # daily reference spots
.venv/bin/python -m pytest                          # test suite
```

Both scripts accept `--dry-run` (fetch + print, no DB write). API keys for the
spot service go in `.env` (see `.env.example`): `METALS_DEV_API_KEY` (free
tier, 100 req/month) and `EIA_API_KEY` (free registration; `DEMO_KEY` works
for light testing).

Schedule snapshots hourly (matches Hyperliquid's hourly funding) and spots
daily (blueprint's macro cadence; ~30 Metals.Dev calls/month):

```cron
5 * * * *  cd /Users/calebbartlett/trade-analysis-app && .venv/bin/python scripts/run_snapshot.py
15 13 * * * cd /Users/calebbartlett/trade-analysis-app && .venv/bin/python scripts/run_spot_refresh.py
30 13 * * * cd /Users/calebbartlett/trade-analysis-app && .venv/bin/python scripts/run_discrepancy_check.py
```

The discrepancy check is a read-only cross-venue diagnostic (not §7.4
tracking error): for each commodity it prints every active perp across
Hyperliquid (stored snapshot), BydFi (quoted live at check time), and
Ostium (no data source until step 8) — gap vs. reference spot, funding
normalized to 8h-equivalent (Hyperliquid pays hourly, BydFi every 4h),
tradeability, and spot staleness, using the energy-vs-metals basis
categorization from CLAUDE.md's design decisions. Default flag thresholds:
metals 1%, energy 10% (adjust with `--metals-threshold` /
`--energy-threshold`).

## Venues

| venue | type | data path | notes |
|---|---|---|---|
| Hyperliquid | DEX | `metaAndAssetCtxs` + `l2Book` (snapshot service) | commodity perps on the xyz builder dex; hourly funding; real order-book depth + margin tiers captured raw in `raw_payload` (`_l2_book`, `_margin_table`) |
| BydFi | CEX | `www.bydfi.com/swap/public/{symbols,risk_limits}` — no key needed or allowed | 4h funding; linear USDT contracts; NO open-interest endpoint; depth is WebSocket-only; the documented `/v1/fapi/market/*` REST base URL is not published |
| Ostium | DEX | `@ostium/builder-sdk` via `OstiumClient.createReadOnly()` ONLY (`ostium/read_client.js`, Node >= 18 — nvm has v24) | write methods throw INVALID_CONFIG in this mode; full trading surface is forbidden (see boundary comment in `ostium/read_client.js`) |

All three venues are modeled tradeable (see CLAUDE.md); the app itself
never executes anything anywhere.

## Layout

```
db/migrations/           numbered SQL migrations (schema §5, seeds §6 + §8 R1)
src/onchain_console/
  calcs.py               deterministic §7 math (Decimal; never LLM-computed)
  hyperliquid.py         info-API client + metaAndAssetCtxs parsing
  snapshot_service.py    M5: instruments → fetch per dex → market_snapshots
  spot_prices.py         Metals.Dev + EIA clients (per-commodity spot)
  spot_service.py        daily refresh → spot_prices ledger + stamping
  discrepancy.py         cross-venue gap/funding diagnostic (energy/metal aware)
  bydfi.py               BydFi public market data (CEX; read-only, no key)
ostium/                  read-only Ostium SDK reader (Node >= 18; see below)
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
