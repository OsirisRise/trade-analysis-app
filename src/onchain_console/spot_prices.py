"""Reference spot price clients: Metals.Dev + EIA (read-only market data).

Sources and identifiers were confirmed live on 2026-07-12 (never hardcoded
from third-party lists — CLAUDE.md corrections log):

* Metals.Dev  GET /v1/latest?currency=USD&unit=toz
    metals.gold / silver / platinum / palladium quoted in USD per troy oz —
    matching the perp quote convention. metals.copper arrives in the same
    requested unit (toz) and is converted to USD/lb (CME convention, matching
    xyz:COPPER's quote) with exact deterministic constants.
    Free tier: 100 requests/month; one call covers all five metals, so daily
    polling uses ~30/month.

* EIA v2 (api.eia.gov)
    petroleum/pri/spt/data     series RWTC  = WTI Cushing spot, $/BBL
                               series RBRTE = Brent Europe spot, $/BBL
    natural-gas/pri/fut/data   series RNGWHHD = Henry Hub spot, $/MMBTU
    Series IDs confirmed against the live facet endpoints. Daily frequency;
    publication lags several days — as_of records the real observation date
    so staleness is visible downstream.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import requests

from onchain_console.config import EIA_API_URL, METALS_DEV_API_URL

# Exact unit constants (NIST): never eyeball a conversion.
GRAMS_PER_TROY_OUNCE = Decimal("31.1034768")
GRAMS_PER_POUND = Decimal("453.59237")
LBS_TO_TOZ_PRICE_FACTOR = GRAMS_PER_POUND / GRAMS_PER_TROY_OUNCE  # ≈ 14.5833

# Metals.Dev response key -> (commodity_code, unit after conversion)
METALS_DEV_COMMODITIES = {
    "gold": ("gold", "usd_per_toz"),
    "silver": ("silver", "usd_per_toz"),
    "platinum": ("platinum", "usd_per_toz"),
    "palladium": ("palladium", "usd_per_toz"),
    # 'copper_spot' is the canonical code (0009), matching Ostium's docs
    "copper": ("copper_spot", "usd_per_lb"),  # converted from toz
}

# EIA series -> (route, commodity_code, expected units string, our unit label)
EIA_SERIES = {
    "RWTC": ("petroleum/pri/spt/data", "wti_crude_oil", "$/BBL", "usd_per_bbl"),
    "RBRTE": ("petroleum/pri/spt/data", "brent_crude_oil", "$/BBL", "usd_per_bbl"),
    "RNGWHHD": ("natural-gas/pri/fut/data", "natural_gas", "$/MMBTU", "usd_per_mmbtu"),
}


@dataclass(frozen=True)
class SpotPrice:
    commodity_code: str
    price: Decimal
    unit: str
    source: str
    as_of: datetime
    raw: dict


def price_per_lb_from_toz(price_per_toz: Decimal) -> Decimal:
    """USD/toz -> USD/lb. A pound holds ~14.58 troy oz of mass, so the
    per-pound price is the per-toz price times that factor."""
    return price_per_toz * LBS_TO_TOZ_PRICE_FACTOR


def fetch_metals_dev(api_key: str, session: requests.Session | None = None) -> dict:
    http = session or requests
    resp = http.get(
        METALS_DEV_API_URL,
        params={"api_key": api_key, "currency": "USD", "unit": "toz"},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "success":
        raise RuntimeError(
            f"metals.dev error {payload.get('error_code')}: "
            f"{payload.get('error_message')}"
        )
    return payload


def parse_metals_dev(payload: dict) -> list[SpotPrice]:
    """Extract the five commodities we track from a /v1/latest payload."""
    if payload.get("unit") != "toz" or payload.get("currency") != "USD":
        raise ValueError(
            f"expected USD/toz payload, got {payload.get('currency')}/"
            f"{payload.get('unit')}"
        )
    as_of = datetime.fromisoformat(
        payload["timestamps"]["metal"].replace("Z", "+00:00")
    )
    spots: list[SpotPrice] = []
    for key, (commodity_code, unit) in METALS_DEV_COMMODITIES.items():
        raw_price = Decimal(str(payload["metals"][key]))
        price = price_per_lb_from_toz(raw_price) if unit == "usd_per_lb" else raw_price
        spots.append(
            SpotPrice(
                commodity_code=commodity_code,
                price=price,
                unit=unit,
                source="metals.dev",
                as_of=as_of,
                raw={"key": key, "quoted": str(raw_price), "quoted_unit": "usd_per_toz"},
            )
        )
    return spots


def fetch_eia_route(
    api_key: str,
    route: str,
    series_ids: list[str],
    session: requests.Session | None = None,
) -> dict:
    """One data call per EIA route, latest daily rows for the given series."""
    http = session or requests
    params: list[tuple[str, str]] = [
        ("api_key", api_key),
        ("frequency", "daily"),
        ("data[0]", "value"),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "desc"),
        ("length", str(10 * len(series_ids))),
    ]
    params += [("facets[series][]", s) for s in series_ids]
    resp = http.get(f"{EIA_API_URL}/{route}/", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def parse_eia(payload: dict) -> list[SpotPrice]:
    """Latest observation per known series from an EIA v2 data payload."""
    latest: dict[str, dict] = {}
    for row in payload["response"]["data"]:
        series = row.get("series")
        if series not in EIA_SERIES:
            continue
        if series not in latest or row["period"] > latest[series]["period"]:
            latest[series] = row

    spots: list[SpotPrice] = []
    for series, row in latest.items():
        _, commodity_code, expected_units, unit = EIA_SERIES[series]
        if row["units"] != expected_units:
            raise ValueError(
                f"EIA series {series}: expected units {expected_units}, "
                f"got {row['units']}"
            )
        as_of = datetime.strptime(row["period"], "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        spots.append(
            SpotPrice(
                commodity_code=commodity_code,
                price=Decimal(str(row["value"])),
                unit=unit,
                source="eia",
                as_of=as_of,
                raw=row,
            )
        )
    return spots
