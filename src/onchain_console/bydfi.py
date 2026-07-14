"""BydFi public market-data client (CEX; read-only by construction).

HARD BOUNDARY — analysis-only, same rule as Hyperliquid's Exchange endpoint
and Ostium's write methods: this module must NEVER call BydFi's Trading
Interface or Account Interface (anything under futures/trade or
futures/user, or any private endpoint). No API key exists for this venue
anywhere in this project and none is needed: BydFi's public market data
requires no authentication (developers.bydfi.com/en/started). Do not create,
read, or reference a BydFi key — a key would only ever be useful for the
forbidden private interfaces.

Endpoint reality check (2026-07-13): BydFi's developer docs describe a REST
Market Interface under /v1/fapi/market/* (exchange_info, depth, mark_price,
funding_rate, funding_rate_history, risk_limit), but publish no base URL,
and those paths resolve on no discoverable BydFi host. What IS live and
public is the exchange's own frontend API:

    GET https://www.bydfi.com/swap/public/symbols      (all 405 contracts:
        contract spec + markPrice/indexPrice + funding rate/interval/next)
    GET https://www.bydfi.com/swap/public/risk_limits  (all risk tiers)

symbols is a superset of the documented exchange_info + mark_price +
funding_rate responses, so this client covers those three documented
endpoints in one call. Order-book depth and funding-rate HISTORY are only
available over WebSocket (wss://stream.bydfi.com) / undiscovered REST — they
are deliberately NOT implemented here rather than pointed at unverified
paths (see CLAUDE.md corrections log, 2026-07-10 lesson).

Envelope: every response is {"code": 200, "message": "", "data": ...}.
Funding interval arrives in HOURS (4 for every commodity contract) —
convert to minutes at the parse boundary so downstream code keeps one unit.
"""

from dataclasses import dataclass
from decimal import Decimal

import requests

from onchain_console.config import BYDFI_PUBLIC_URL

# BydFi's CDN rejects default library user agents.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}


@dataclass(frozen=True)
class BydfiContract:
    symbol: str            # canonical API symbol, e.g. 'XAU-USDT'
    alias: str             # UI alias, e.g. 'XAUUSDT' (what screenshots show)
    base_coin: str
    quote_coin: str
    settle_coin: str
    inverse: bool          # False = linear; §7.2 funding math applies as-is
    status: int            # 0 = normal/trading (uniform across live universe)
    max_leverage: int | None
    mark_price: Decimal | None
    index_price: Decimal | None
    funding_rate: Decimal | None
    last_funding_rate: Decimal | None
    funding_interval_minutes: int | None  # converted from API hours
    next_funding_time_ms: int | None
    raw: dict


def _dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _unwrap(payload: dict):
    if payload.get("code") != 200:
        raise RuntimeError(
            f"BydFi error {payload.get('code')}: {payload.get('message')}"
        )
    return payload["data"]


def fetch_symbols(session: requests.Session | None = None) -> dict:
    """GET /swap/public/symbols — full contract universe, no auth."""
    http = session or requests
    resp = http.get(f"{BYDFI_PUBLIC_URL}/symbols", headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_risk_limits(session: requests.Session | None = None) -> dict:
    """GET /swap/public/risk_limits — tiered risk limits for every symbol.

    Tier fields: s=symbol, ml=risk level, mv=max notional value,
    mmr=maintenance margin rate, v/cum=venue internals. Stored raw; no
    scoring logic is built on these yet (Task 11 proposal pending)."""
    http = session or requests
    resp = http.get(f"{BYDFI_PUBLIC_URL}/risk_limits", headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def parse_contracts(payload: dict) -> dict[str, BydfiContract]:
    """Index the symbols response by canonical symbol."""
    contracts: dict[str, BydfiContract] = {}
    for row in _unwrap(payload):
        interval_h = row.get("fundingInterval")
        contracts[row["symbol"]] = BydfiContract(
            symbol=row["symbol"],
            alias=row.get("alias", ""),
            base_coin=row["baseCoin"],
            quote_coin=row["quoteCoin"],
            settle_coin=row["settleCoin"],
            inverse=bool(row.get("inverse")),
            status=row.get("status", -1),
            max_leverage=row.get("maxLeverage"),
            mark_price=_dec(row.get("markPrice")),
            index_price=_dec(row.get("indexPrice")),
            funding_rate=_dec(row.get("fundingRate")),
            last_funding_rate=_dec(row.get("lastFundingRate")),
            funding_interval_minutes=(
                interval_h * 60 if interval_h is not None else None
            ),
            next_funding_time_ms=row.get("nextFundingTime"),
            raw=row,
        )
    return contracts


def parse_risk_limits(payload: dict) -> dict[str, list[dict]]:
    """symbol -> raw tier list (raw storage only, per Task 10/11 scope)."""
    return dict(_unwrap(payload))
