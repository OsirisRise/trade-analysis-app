"""Tests for the Hyperliquid snapshot pipeline against a real fixture payload
(captured from the live xyz dex on 2026-07-10). Expected values are
hand-derived from the fixture numbers via the §7 formulas."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from onchain_console.hyperliquid import (
    dex_of_symbol,
    margin_table_id_by_symbol,
    margin_tables_by_id,
    parse_asset_ctxs,
)
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
        assert set(parsed) == {"xyz:GOLD", "xyz:SILVER", "xyz:CL", "xyz:BRENTOIL"}

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


class TestBrentSnapshotRow:
    """xyz:BRENTOIL was missed by the original truncated universe check;
    fixture entry captured live 2026-07-10."""

    def test_brent_row_metrics(self, payload):
        brent_ctx = parse_asset_ctxs(*payload)["xyz:BRENTOIL"]
        instrument = Instrument(
            id=uuid4(),
            venue="Hyperliquid",
            symbol="xyz:BRENTOIL",
            underlying="brent",
            funding_interval_minutes=60,
        )
        captured_at = datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)

        row = build_snapshot_row(instrument, brent_ctx, captured_at)

        assert row.mark_price == Decimal("76.059")
        assert row.oracle_price == Decimal("76.054")
        # §7.1 premium: (76.059 - 76.054) / 76.054
        assert row.premium_pct == Decimal("0.005") / Decimal("76.054")
        # hourly funding 0.0000116848 × 8 and × 8760
        assert row.funding_rate_8h_equiv == Decimal("0.0000934784")
        assert row.funding_apr_est == Decimal("0.102358848")
        # OI in USD: 2120733.6600000011 × 76.059 (~$161M, matches UI)
        assert row.open_interest_usd == Decimal("2120733.6600000011") * Decimal(
            "76.059"
        )
        # spread: 10000 × (76.0672 - 76.057) / 76.0595
        assert row.spread_bps_est == Decimal("10000") * Decimal("0.0102") / Decimal(
            "76.0595"
        )


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
        # no spot ledger provided -> reference stays NULL
        assert all(r.reference_spot_price is None for r in rows)

    def test_reference_spot_copied_by_underlying(self, payload):
        instruments = [make_instrument("xyz:GOLD")]  # underlying = 'gold'
        rows = collect_snapshots(
            instruments,
            fetch=lambda dex: payload,
            spot_by_underlying={"gold": Decimal("4105.80"), "silver": Decimal("1")},
        )
        assert rows[0].reference_spot_price == Decimal("4105.80")


class TestRawCaptureExtras:
    """Task 10 (2026-07-13): l2Book + margin tables ride inside raw_payload."""

    @pytest.fixture
    def l2_book(self):
        return json.loads(
            (Path(__file__).parent / "fixtures" / "hl_l2book_gold_sample.json")
            .read_text()
        )

    def test_margin_tables_parsed_from_fixture_meta(self, payload):
        meta, _ = payload
        tables = margin_tables_by_id(meta)
        # real xyz meta carries one explicit table (id 50, single 50x tier)
        assert tables == {
            50: {"description": "",
                 "marginTiers": [{"lowerBound": "0.0", "maxLeverage": 50}]}
        }
        ids = margin_table_id_by_symbol(meta)
        assert ids["xyz:GOLD"] == 25
        assert ids["xyz:CL"] == 20

    def test_l2_book_and_margin_table_embedded_in_raw_payload(
        self, payload, l2_book
    ):
        rows = collect_snapshots(
            [make_instrument("xyz:GOLD")],
            fetch=lambda dex: payload,
            fetch_l2=lambda symbol: l2_book,
        )
        raw = rows[0].raw_payload
        assert raw["_l2_book"]["coin"] == "xyz:GOLD"
        assert len(raw["_l2_book"]["levels"][0]) == 20  # real resting bids
        # id 25 has no explicit marginTables entry (simple single-tier table)
        assert raw["_margin_table"] == {"id": 25, "table": None}
        # plain ctx keys still at top level (existing readers unaffected)
        assert "markPx" in raw

    def test_l2_book_failure_degrades_to_none(self, payload):
        def boom(symbol):
            raise RuntimeError("depth down")

        rows = collect_snapshots(
            [make_instrument("xyz:GOLD")],
            fetch=lambda dex: payload,
            fetch_l2=boom,
        )
        assert "_l2_book" not in rows[0].raw_payload
        assert rows[0].mark_price is not None  # snapshot itself survived
