from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.config import PerpGridConfig, SpotGridConfig
from src.strategy.types import GridBias, GridType


def test_validation_upper_less_than_lower():
    with pytest.raises(ValidationError) as exc:
        SpotGridConfig(
            symbol="BTC/USDC",
            upper_price=Decimal("1000.0"),
            lower_price=Decimal("2000.0"),
            grid_type=GridType.ARITHMETIC,
            grid_count=10,
            total_investment=Decimal("1000.0"),
        )
    assert "Upper price 1000.0 must be greater than lower price 2000.0" in str(
        exc.value
    )


def test_validation_trigger_out_of_bounds():
    with pytest.raises(ValidationError) as exc:
        SpotGridConfig(
            symbol="BTC/USDC",
            upper_price=Decimal("2000.0"),
            lower_price=Decimal("1000.0"),
            grid_type=GridType.ARITHMETIC,
            grid_count=10,
            total_investment=Decimal("1000.0"),
            trigger_price=Decimal("3000.0"),
        )
    assert "Trigger price 3000.0 is outside the grid range" in str(exc.value)


def test_validation_grid_count_too_low():
    with pytest.raises(ValidationError) as exc:
        SpotGridConfig(
            symbol="BTC/USDC",
            upper_price=Decimal("2000.0"),
            lower_price=Decimal("1000.0"),
            grid_type=GridType.ARITHMETIC,
            grid_count=2,
            total_investment=Decimal("1000.0"),
        )
    assert "Grid count 2 must be greater than 2" in str(exc.value)


def test_validation_invalid_symbol_format():
    with pytest.raises(ValidationError):
        SpotGridConfig(
            symbol="BTCUSDC",
            upper_price=Decimal("2000.0"),
            lower_price=Decimal("1000.0"),
            grid_type=GridType.ARITHMETIC,
            grid_count=5,
            total_investment=Decimal("1000.0"),
        )


def test_validation_negative_investment():
    with pytest.raises(ValidationError):
        SpotGridConfig(
            symbol="BTC/USDC",
            upper_price=Decimal("2000.0"),
            lower_price=Decimal("1000.0"),
            grid_type=GridType.ARITHMETIC,
            grid_count=5,
            total_investment=Decimal("-100.0"),
        )


def test_validation_invalid_leverage():
    # Zero leverage
    with pytest.raises(ValidationError):
        PerpGridConfig(
            symbol="BTC",
            leverage=0,
            upper_price=Decimal("2000.0"),
            lower_price=Decimal("1000.0"),
            grid_type=GridType.ARITHMETIC,
            grid_count=5,
            total_investment=Decimal("1000.0"),
            grid_bias=GridBias.LONG,
        )

    # Too high leverage
    with pytest.raises(ValidationError):
        PerpGridConfig(
            symbol="BTC",
            leverage=51,
            upper_price=Decimal("2000.0"),
            lower_price=Decimal("1000.0"),
            grid_type=GridType.ARITHMETIC,
            grid_count=5,
            total_investment=Decimal("1000.0"),
            grid_bias=GridBias.LONG,
        )


def test_validation_valid_configs():
    # Spot
    spot = SpotGridConfig(
        symbol="BTC/USDC",
        upper_price=Decimal("2000.0"),
        lower_price=Decimal("1000.0"),
        grid_type=GridType.ARITHMETIC,
        grid_count=10,
        total_investment=Decimal("1000.0"),
    )
    assert spot.symbol == "BTC/USDC"

    # Perp
    perp = PerpGridConfig(
        symbol="BTC",
        leverage=10,
        is_isolated=True,
        upper_price=Decimal("2000.0"),
        lower_price=Decimal("1000.0"),
        grid_type=GridType.ARITHMETIC,
        grid_count=10,
        total_investment=Decimal("1000.0"),
        grid_bias=GridBias.LONG,
    )
    assert perp.symbol == "BTC"
