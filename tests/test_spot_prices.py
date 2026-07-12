"""Tests for the reference spot price clients (Metals.Dev + EIA).

Fixtures: the two EIA files are real payloads captured live on 2026-07-12
(DEMO_KEY); the Metals.Dev file is the verbatim sample response from their
docs. Expected values are hand-derived from the fixture contents."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from onchain_console.spot_prices import (
    LBS_TO_TOZ_PRICE_FACTOR,
    parse_eia,
    parse_metals_dev,
    price_per_lb_from_toz,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def metals_payload():
    return json.loads((FIXTURES / "metals_dev_latest_sample.json").read_text())


@pytest.fixture
def eia_petroleum_payload():
    return json.loads((FIXTURES / "eia_petroleum_spot_sample.json").read_text())


@pytest.fixture
def eia_natgas_payload():
    return json.loads((FIXTURES / "eia_natgas_spot_sample.json").read_text())


class TestUnitConversion:
    def test_factor_is_exact_nist_ratio(self):
        # 453.59237 g/lb ÷ 31.1034768 g/toz
        assert LBS_TO_TOZ_PRICE_FACTOR == Decimal("453.59237") / Decimal(
            "31.1034768"
        )

    def test_copper_2023_sanity(self):
        # Docs sample: copper 0.2584 USD/toz → ~3.77 USD/lb, the real
        # mid-2023 copper price — validates the conversion direction.
        per_lb = price_per_lb_from_toz(Decimal("0.2584"))
        assert per_lb == Decimal("0.2584") * LBS_TO_TOZ_PRICE_FACTOR
        assert Decimal("3.7") < per_lb < Decimal("3.8")


class TestParseMetalsDev:
    def test_five_commodities_extracted(self, metals_payload):
        spots = {s.commodity_code: s for s in parse_metals_dev(metals_payload)}
        assert set(spots) == {"gold", "silver", "platinum", "palladium", "copper"}

    def test_precious_metals_pass_through_toz(self, metals_payload):
        spots = {s.commodity_code: s for s in parse_metals_dev(metals_payload)}
        assert spots["gold"].price == Decimal("1923.86")
        assert spots["gold"].unit == "usd_per_toz"
        assert spots["silver"].price == Decimal("22.905")
        assert spots["platinum"].price == Decimal("916.569")
        assert spots["palladium"].price == Decimal("1229.684")

    def test_copper_converted_to_lb(self, metals_payload):
        spots = {s.commodity_code: s for s in parse_metals_dev(metals_payload)}
        assert spots["copper"].price == Decimal("0.2584") * LBS_TO_TOZ_PRICE_FACTOR
        assert spots["copper"].unit == "usd_per_lb"
        # quoted toz value preserved for auditability
        assert spots["copper"].raw["quoted"] == "0.2584"

    def test_as_of_from_metal_timestamp(self, metals_payload):
        gold = parse_metals_dev(metals_payload)[0]
        assert gold.as_of == datetime(
            2023, 7, 5, 6, 16, 2, 829000, tzinfo=timezone.utc
        )
        assert gold.source == "metals.dev"

    def test_wrong_unit_rejected(self, metals_payload):
        metals_payload["unit"] = "g"
        with pytest.raises(ValueError):
            parse_metals_dev(metals_payload)


class TestParseEia:
    def test_wti_and_brent_latest_period_wins(self, eia_petroleum_payload):
        # Fixture has Brent rows for 07-06/07-03/07-02/07-01 and WTI rows for
        # 07-06/07-02 — parser must pick the max period per series.
        spots = {s.commodity_code: s for s in parse_eia(eia_petroleum_payload)}
        assert set(spots) == {"wti_crude_oil", "brent_crude_oil"}
        assert spots["wti_crude_oil"].price == Decimal("69.6")
        assert spots["brent_crude_oil"].price == Decimal("69.56")
        assert spots["wti_crude_oil"].unit == "usd_per_bbl"
        assert spots["wti_crude_oil"].as_of == datetime(
            2026, 7, 6, tzinfo=timezone.utc
        )
        assert spots["wti_crude_oil"].source == "eia"

    def test_henry_hub(self, eia_natgas_payload):
        (spot,) = parse_eia(eia_natgas_payload)
        assert spot.commodity_code == "natural_gas"
        assert spot.price == Decimal("3.29")
        assert spot.unit == "usd_per_mmbtu"
        assert spot.as_of == datetime(2026, 7, 6, tzinfo=timezone.utc)

    def test_unexpected_units_rejected(self, eia_natgas_payload):
        eia_natgas_payload["response"]["data"][0]["units"] = "$/GAL"
        with pytest.raises(ValueError):
            parse_eia(eia_natgas_payload)

    def test_unknown_series_ignored(self, eia_petroleum_payload):
        eia_petroleum_payload["response"]["data"].append(
            {"series": "SOMETHING_ELSE", "period": "2026-07-06", "value": "1",
             "units": "$/BBL"}
        )
        spots = parse_eia(eia_petroleum_payload)
        assert {s.commodity_code for s in spots} == {
            "wti_crude_oil", "brent_crude_oil"
        }
