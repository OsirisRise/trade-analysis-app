"""Tests for the BydFi public market-data client, against a real payload
captured live on 2026-07-13 (trimmed to the 9 seeded commodities + BTC)."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from onchain_console.bydfi import parse_contracts, parse_risk_limits

FIXTURES = Path(__file__).parent / "fixtures"

SEEDED = {
    "XAU-USDT", "XAUT-USDT", "PAXG-USDT", "XAG-USDT", "COPPER-USDT",
    "XPD-USDT", "CL-USDT", "BZ-USDT", "NATGAS-USDT",
}


@pytest.fixture
def symbols_payload():
    return json.loads((FIXTURES / "bydfi_symbols_sample.json").read_text())


@pytest.fixture
def risk_payload():
    return json.loads((FIXTURES / "bydfi_risk_limits_sample.json").read_text())


class TestParseContracts:
    def test_all_seeded_symbols_present(self, symbols_payload):
        contracts = parse_contracts(symbols_payload)
        assert SEEDED <= set(contracts)

    def test_gold_contract_fields(self, symbols_payload):
        xau = parse_contracts(symbols_payload)["XAU-USDT"]
        assert xau.alias == "XAUUSDT"  # the UI/screenshot form
        assert xau.base_coin == "XAU"
        assert xau.quote_coin == xau.settle_coin == "USDT"
        assert xau.mark_price == Decimal("4006.81")
        assert xau.index_price == Decimal("4004.55")
        assert xau.max_leverage == 100
        assert xau.status == 0  # normal (uniform across live universe)
        # funding interval arrives as 4 (hours) -> normalized to minutes
        assert xau.funding_interval_minutes == 240

    def test_all_seeded_contracts_are_linear(self, symbols_payload):
        contracts = parse_contracts(symbols_payload)
        # §7.2 funding math assumes linear margining; 0012 asserts none of
        # the seeded contracts are inverse — this keeps that assertion true
        assert all(not contracts[s].inverse for s in SEEDED)

    def test_error_envelope_raises(self, symbols_payload):
        symbols_payload["code"] = 500
        symbols_payload["message"] = "boom"
        with pytest.raises(RuntimeError, match="500"):
            parse_contracts(symbols_payload)


class TestParseRiskLimits:
    def test_tiers_by_symbol(self, risk_payload):
        limits = parse_risk_limits(risk_payload)
        assert set(limits) == {"XAU-USDT", "CL-USDT"}
        xau = limits["XAU-USDT"]
        assert len(xau) == 20
        level_1 = next(t for t in xau if t["ml"] == 1)
        # raw tier fields: mv = max notional, mmr = maintenance margin rate
        assert level_1["s"] == "XAU-USDT"
        assert level_1["mmr"] == 0.5
