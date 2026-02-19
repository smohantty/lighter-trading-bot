"""Validate that all WebSocket events emitted by the bot conform to the shared JSON schema."""

import json
from decimal import Decimal
from pathlib import Path

import jsonschema
import pytest

from src.broadcast.server import DecimalEncoder
from src.broadcast.types import (
    MarketEvent,
    OrderEvent,
    WSEvent,
    config_event,
    error_event,
    grid_state_event,
    info_event,
    market_update_event,
    order_update_event,
    perp_grid_summary_event,
    spot_grid_summary_event,
)
from src.strategy.types import (
    GridState,
    PerpGridSummary,
    SpotGridSummary,
    ZoneInfo,
)

SCHEMA_PATH = (
    Path(__file__).parent.parent / "schema" / "bot-ws-schema" / "schema" / "events.json"
)


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA_PATH.read_text())


def serialize(event: WSEvent) -> dict:
    """Serialize an event the same way StatusBroadcaster does."""
    raw = json.dumps(event.to_dict(), cls=DecimalEncoder)
    return json.loads(raw)


def validate(event_dict: dict, schema: dict):
    jsonschema.validate(instance=event_dict, schema=schema)


class TestSchemaValidation:
    def test_config_spot_event(self, schema):
        event = config_event(
            {
                "type": "spot_grid",
                "symbol": "ETH/USDC",
                "upper_price": 4000.0,
                "lower_price": 3000.0,
                "grid_type": "arithmetic",
                "grid_count": 10,
                "total_investment": 1000.0,
                "trigger_price": None,
                "sz_decimals": 4,
                "px_decimals": 2,
            }
        )
        validate(serialize(event), schema)

    def test_config_perp_event(self, schema):
        event = config_event(
            {
                "type": "perp_grid",
                "symbol": "HYPE",
                "leverage": 5,
                "is_isolated": False,
                "upper_price": 30.0,
                "lower_price": 20.0,
                "grid_type": "geometric",
                "grid_count": 20,
                "total_investment": 5000.0,
                "grid_bias": "long",
                "trigger_price": None,
            }
        )
        validate(serialize(event), schema)

    def test_info_event(self, schema):
        event = info_event(network="mainnet", exchange="lighter")
        validate(serialize(event), schema)

    def test_spot_grid_summary_event(self, schema):
        summary = SpotGridSummary(
            symbol="ETH/USDC",
            state="Running",
            uptime="2d 14h 30m",
            position_size=Decimal("1.5"),
            matched_profit=Decimal("45.23"),
            total_profit=Decimal("52.10"),
            total_fees=Decimal("3.12"),
            initial_entry_price=Decimal("3500.0"),
            grid_count=10,
            range_low=Decimal("3000.0"),
            range_high=Decimal("4000.0"),
            grid_spacing_pct=(Decimal("1.05"), Decimal("1.05")),
            roundtrips=12,
            base_balance=Decimal("1.5"),
            quote_balance=Decimal("500.0"),
        )
        event = spot_grid_summary_event(summary)
        validate(serialize(event), schema)

    def test_perp_grid_summary_event(self, schema):
        summary = PerpGridSummary(
            symbol="HYPE",
            state="Running",
            uptime="1d 8h 15m",
            position_size=Decimal("100.0"),
            position_side="Long",
            matched_profit=Decimal("120.50"),
            total_profit=Decimal("135.20"),
            total_fees=Decimal("8.30"),
            leverage=5,
            grid_bias="long",
            grid_count=20,
            range_low=Decimal("20.0"),
            range_high=Decimal("30.0"),
            grid_spacing_pct=(Decimal("0.5"), Decimal("0.5")),
            roundtrips=8,
            margin_balance=Decimal("1135.20"),
            initial_entry_price=Decimal("25.0"),
        )
        event = perp_grid_summary_event(summary)
        validate(serialize(event), schema)

    def test_grid_state_spot_event(self, schema):
        state = GridState(
            symbol="ETH/USDC",
            strategy_type="spot_grid",
            grid_bias=None,
            zones=[
                ZoneInfo(
                    index=0,
                    buy_price=Decimal("3000.0"),
                    sell_price=Decimal("3100.0"),
                    size=Decimal("0.15"),
                    order_side="Buy",
                    has_order=True,
                    is_reduce_only=False,
                    entry_price=Decimal("3050.0"),
                    roundtrip_count=3,
                ),
            ],
        )
        event = grid_state_event(state)
        validate(serialize(event), schema)

    def test_grid_state_perp_event(self, schema):
        state = GridState(
            symbol="HYPE",
            strategy_type="perp_grid",
            grid_bias="long",
            zones=[
                ZoneInfo(
                    index=0,
                    buy_price=Decimal("20.0"),
                    sell_price=Decimal("20.5"),
                    size=Decimal("10.0"),
                    order_side="Buy",
                    has_order=True,
                    is_reduce_only=False,
                    entry_price=Decimal("20.25"),
                    roundtrip_count=1,
                ),
                ZoneInfo(
                    index=1,
                    buy_price=Decimal("20.5"),
                    sell_price=Decimal("21.0"),
                    size=Decimal("10.0"),
                    order_side="Sell",
                    has_order=True,
                    is_reduce_only=True,
                    entry_price=Decimal("20.75"),
                    roundtrip_count=0,
                ),
            ],
        )
        event = grid_state_event(state)
        validate(serialize(event), schema)

    def test_order_update_event(self, schema):
        event = order_update_event(
            OrderEvent(
                oid=123456,
                cloid="0xa1b2c3d4",
                side="Buy",
                price=3500.0,
                size=0.1,
                status="filled",
                fee=0.035,
                is_taker=False,
            )
        )
        validate(serialize(event), schema)

    def test_market_update_event(self, schema):
        event = market_update_event(MarketEvent(price=3542.75))
        validate(serialize(event), schema)

    def test_error_event(self, schema):
        event = error_event("Connection lost: timeout after 30s")
        validate(serialize(event), schema)
