"""Tests for the Hyperliquid snapshot pipeline against a real fixture payload
(captured from the live xyz dex on 2026-07-10). Expected values are
hand-derived from the fixture numbers via the §7 formulas."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from onchain_console.hyperliquid import dex_of_symbol, parse_asset_ctxs
from onchain_console.snapshot_service import (
    Instrument,
    build_snapshot_row,
    collect_snapshots,
)

FIXTURE = Path(__file__).parent / "fixtures" / "meta_and_asset_ctxs_xyz_sample.json"


@pytest.fixture
def payload():
    meta, ctxs = json.loads(FIXTURE.read_text())
    return meta, ctxs


def make_instrument(symbol: str) -> Instrument:
    return Instrument(
        id=uuid4(),
        venue="Hyperliquid",
        symbol=symbol,
        underlying="gold",
        funding_interval_minutes=60,
    )


class TestDexOfSymbol:
    def test_builder_dex_symbol(self):
        assert dex_of_symbol("xyz:GOLD") == "xyz"

    def test_main_universe_symbol(self):
        assert dex_of_symbol("PAXG") == ""


class TestParseAssetCtxs:
    def test_parses_all_symbols(self, payload):
        parsed = parse_asset_ctxs(*payload)
        assert set(parsed) == {"xyz:GOLD", "xyz:SILVER", "xyz:CL"}

    def test_gold_fields_are_decimal_exact(self, payload):
        gold = parse_asset_ctxs(*payload)["xyz:GOLD"]
        assert gold.mark_price == Decimal("4108.3")
        assert gold.oracle_price == Decimal("4105.5")
        assert gold.mid_price == Decimal("4108.25")
        assert gold.funding_rate_interval == Decimal("0.0000222526")
        assert gold.open_interest == Decimal("33637.8658")
        assert gold.impact_bid_price == Decimal("4108.17")
        assert gold.impact_ask_price == Decimal("4108.3")
        assert gold.raw["dayNtlVlm"] == "22668692.6404199935"


class TestBuildSnapshotRow:
    def test_gold_row_metrics(self, payload):
        gold_ctx = parse_asset_ctxs(*payload)["xyz:GOLD"]
        instrument = make_instrument("xyz:GOLD")
        captured_at = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)

        row = build_snapshot_row(instrument, gold_ctx, captured_at)

        # §7.1 premium: (4108.3 - 4105.5) / 4105.5
        assert row.premium_pct == Decimal("2.8") / Decimal("4105.5")
        # hourly funding × 8 and × 8760
        assert row.funding_rate_8h_equiv == Decimal("0.0001780208")
        assert row.funding_apr_est == Decimal("0.194932776")
        # OI in USD: 33637.8658 × 4108.3
        assert row.open_interest_usd == Decimal("33637.8658") * Decimal("4108.3")
        # spread: 10000 × (4108.3 - 4108.17) / 4108.25
        assert row.spread_bps_est == Decimal("10000") * Decimal("0.13") / Decimal(
            "4108.25"
        )
        # scoring-engine fields are not set at snapshot time
        assert row.raw_payload["premium"] == "0.0006661795"
        assert row.instrument_id == instrument.id
        assert row.captured_at == captured_at


class TestCollectSnapshots:
    def test_builds_rows_and_skips_unknown_symbols(self, payload):
        instruments = [
            make_instrument("xyz:GOLD"),
            make_instrument("xyz:CL"),
            make_instrument("xyz:DOES_NOT_EXIST"),
        ]
        calls = []

        def fake_fetch(dex):
            calls.append(dex)
            return payload

        rows = collect_snapshots(instruments, fetch=fake_fetch)

        # one API call per distinct dex, not per instrument
        assert calls == ["xyz"]
        assert [r.symbol for r in rows] == ["xyz:GOLD", "xyz:CL"]
        assert all(r.captured_at == rows[0].captured_at for r in rows)
