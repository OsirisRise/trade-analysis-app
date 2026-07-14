"""Tests for the BydFi public market-data client, against a real payload
captured live on 2026-07-13 (trimmed to the 9 seeded commodities + BTC)."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from onchain_console.bydfi import (
    build_risk_tier_profile_rows,
    parse_contracts,
    parse_risk_limits,
)

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


class TestBuildRiskTierProfileRows:
    """0013 (prompt 3): per-symbol liquidity_profiles row construction
    against the real captured risk_limits fixture."""

    CAPTURED_AT = datetime(2026, 7, 14, 13, 45, tzinfo=timezone.utc)

    def test_rows_for_symbols_present_on_both_sides(self, risk_payload):
        limits = parse_risk_limits(risk_payload)
        ids = {"XAU-USDT": uuid4(), "CL-USDT": uuid4()}

        rows = build_risk_tier_profile_rows(ids, limits, self.CAPTURED_AT)

        assert len(rows) == 2
        by_id = {r["instrument_id"]: r for r in rows}
        xau = by_id[ids["XAU-USDT"]]
        assert xau["profile_type"] == "risk_tiers"
        assert xau["provenance"] == "venue_risk_config"
        assert xau["captured_at"] == self.CAPTURED_AT
        # payload is that symbol's full tier list from the fixture
        assert xau["payload"] == limits["XAU-USDT"]
        assert len(xau["payload"]) == 20
        assert all(t["s"] == "XAU-USDT" for t in xau["payload"])

    def test_symbols_on_only_one_side_are_skipped(self, risk_payload):
        limits = parse_risk_limits(risk_payload)
        ids = {
            "XAU-USDT": uuid4(),
            "NATGAS-USDT": uuid4(),  # seeded, but not in this fixture
        }
        rows = build_risk_tier_profile_rows(ids, limits, self.CAPTURED_AT)
        assert [r["instrument_id"] for r in rows] == [ids["XAU-USDT"]]
        # and limits-only symbols (CL-USDT here) produce nothing either
        assert build_risk_tier_profile_rows({}, limits, self.CAPTURED_AT) == []
