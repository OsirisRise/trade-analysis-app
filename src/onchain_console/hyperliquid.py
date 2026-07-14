"""Hyperliquid info-API client (read-only market data; no trading endpoints).

One metaAndAssetCtxs call returns mark, mid, oracle, premium, funding, OI,
day volume, and impact prices for a whole dex universe — so the snapshot
service batches per dex instead of polling per coin (rate-limit note, §9).

Commodity perps live on builder-deployed (HIP-3) dexes and are addressed as
"<dex>:<NAME>" (e.g. "xyz:GOLD"); main-universe perps have bare names.
"""

from dataclasses import dataclass
from decimal import Decimal

import requests

from onchain_console.config import HYPERLIQUID_INFO_URL


@dataclass(frozen=True)
class AssetCtx:
    """Parsed metaAndAssetCtxs entry for one asset."""

    symbol: str
    mark_price: Decimal
    mid_price: Decimal | None
    oracle_price: Decimal | None
    premium: Decimal | None  # venue-reported premium; we recompute our own
    funding_rate_interval: Decimal | None
    open_interest: Decimal | None  # base units
    day_volume_usd: Decimal | None
    impact_bid_price: Decimal | None
    impact_ask_price: Decimal | None
    raw: dict


def dex_of_symbol(symbol: str) -> str:
    """'xyz:GOLD' -> 'xyz'; main-universe symbols ('PAXG') -> ''."""
    return symbol.split(":", 1)[0] if ":" in symbol else ""


def fetch_meta_and_asset_ctxs(
    dex: str = "", session: requests.Session | None = None
) -> tuple[dict, list[dict]]:
    """POST {"type":"metaAndAssetCtxs"} (plus dex for builder dexes)."""
    body: dict = {"type": "metaAndAssetCtxs"}
    if dex:
        body["dex"] = dex
    http = session or requests
    resp = http.post(HYPERLIQUID_INFO_URL, json=body, timeout=30)
    resp.raise_for_status()
    meta, asset_ctxs = resp.json()
    return meta, asset_ctxs


def fetch_l2_book(
    symbol: str, session: requests.Session | None = None
) -> dict:
    """POST {"type":"l2Book","coin":...} — real resting-order depth (up to
    20 levels/side) from the on-chain order book. Raw capture only; no
    scoring logic reads this yet (Task 11 proposal pending). Builder-dex
    assets use their prefixed name ('xyz:GOLD') as the coin."""
    http = session or requests
    resp = http.post(
        HYPERLIQUID_INFO_URL, json={"type": "l2Book", "coin": symbol}, timeout=30
    )
    resp.raise_for_status()
    return resp.json()


def margin_tables_by_id(meta: dict) -> dict[int, dict]:
    """meta.marginTables is [[id, {description, marginTiers}], ...] —
    Hyperliquid's tiered leverage-by-notional data, already present in every
    metaAndAssetCtxs response (no extra endpoint). Assets reference it via
    universe[].marginTableId; ids without an explicit entry are simple
    single-tier tables (the asset's maxLeverage applies at any notional)."""
    return {entry[0]: entry[1] for entry in meta.get("marginTables", [])}


def margin_table_id_by_symbol(meta: dict) -> dict[str, int | None]:
    return {u["name"]: u.get("marginTableId") for u in meta.get("universe", [])}


def _dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def parse_asset_ctxs(meta: dict, asset_ctxs: list[dict]) -> dict[str, AssetCtx]:
    """Zip meta.universe with the index-aligned asset contexts."""
    parsed: dict[str, AssetCtx] = {}
    for asset, ctx in zip(meta["universe"], asset_ctxs, strict=True):
        symbol = asset["name"]
        impact_pxs = ctx.get("impactPxs") or [None, None]
        parsed[symbol] = AssetCtx(
            symbol=symbol,
            mark_price=Decimal(str(ctx["markPx"])),
            mid_price=_dec(ctx.get("midPx")),
            oracle_price=_dec(ctx.get("oraclePx")),
            premium=_dec(ctx.get("premium")),
            funding_rate_interval=_dec(ctx.get("funding")),
            open_interest=_dec(ctx.get("openInterest")),
            day_volume_usd=_dec(ctx.get("dayNtlVlm")),
            impact_bid_price=_dec(impact_pxs[0]),
            impact_ask_price=_dec(impact_pxs[1]),
            raw=ctx,
        )
    return parsed
