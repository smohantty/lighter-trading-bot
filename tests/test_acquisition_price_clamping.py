"""Tests for acquisition price clamping to ACQUISITION_SPREAD.

When grid spacing is wide (e.g. 5-10%), the nearest grid level can be far from
market price, causing the acquisition order to never fill. The fix clamps the
acquisition price so it is never further than ACQUISITION_SPREAD (0.1%) from
the current market price.
"""

from decimal import Decimal

from src.config import PerpGridConfig, SpotGridConfig
from src.constants import ACQUISITION_SPREAD
from src.engine.context import MarketInfo, StrategyContext
from src.model import OrderSide
from src.strategy.perp_grid import PerpGridStrategy
from src.strategy.spot_grid import SpotGridStrategy
from src.strategy.types import GridBias, GridType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_perp_context(symbol: str = "HYPE") -> StrategyContext:
    markets = {
        symbol: MarketInfo(
            symbol=symbol,
            coin=symbol,
            market_id=0,
            sz_decimals=2,
            price_decimals=2,
            market_type="perp",
            base_asset_id=0,
            quote_asset_id=0,
            min_base_amount=Decimal("0.1"),
            min_quote_amount=Decimal("10.0"),
        )
    }
    ctx = StrategyContext(markets)
    ctx.update_perp_balance("USDC", 100000.0, 100000.0)
    return ctx


def _create_spot_context() -> StrategyContext:
    markets = {
        "LIT/USDC": MarketInfo(
            symbol="LIT/USDC",
            coin="LIT",
            market_id=0,
            sz_decimals=4,
            price_decimals=4,
            market_type="spot",
            base_asset_id=0,
            quote_asset_id=1,
            min_base_amount=Decimal("1.0"),
            min_quote_amount=Decimal("5.0"),
        )
    }
    ctx = StrategyContext(markets)
    ctx.update_spot_balance("LIT", 100000.0, 100000.0)
    ctx.update_spot_balance("USDC", 100000.0, 100000.0)
    return ctx


# ===========================================================================
# Perp Grid — BUY side
# ===========================================================================


def test_perp_buy_wide_grid_clamps_to_spread():
    """Wide grid (10%): nearest buy level is far below market.
    Clamping should push the price up to within ACQUISITION_SPREAD of market."""
    ctx = _create_perp_context()
    config = PerpGridConfig(
        symbol="HYPE",
        leverage=1,
        grid_range_high=Decimal("110.0"),
        grid_range_low=Decimal("90.0"),
        grid_type=GridType.ARITHMETIC,
        grid_count=3,  # levels: 90, 100, 110 -> ~10% spacing
        total_investment=Decimal("300.0"),
        grid_bias=GridBias.LONG,
        trigger_price=None,
    )
    strategy = PerpGridStrategy(config)
    strategy.initialize_zones(Decimal("105.0"), ctx)

    # Market at 105: nearest buy level below market is 100 (5% away).
    # ACQUISITION_SPREAD.markdown(105) = 105 * 0.999 = 104.895
    # max(100, 104.895) = 104.895 -> rounded ROUND_DOWN to 104.89
    current_price = Decimal("105.0")
    price = strategy._calculate_acquisition_price(OrderSide.BUY, current_price)

    spread_price = ACQUISITION_SPREAD.markdown(current_price)
    rounded_spread = strategy.market.round_price(spread_price)
    # Price must be at least as close as spread_price (clamped up)
    assert price >= rounded_spread, (
        f"BUY price {price} should be >= spread_price {rounded_spread}"
    )
    # And definitely closer than the raw grid level (100)
    assert price > Decimal("100.0")


def test_perp_buy_narrow_grid_uses_grid_level():
    """Narrow grid: nearest buy level is already within ACQUISITION_SPREAD.
    The grid level should be used as-is (no clamping needed)."""
    ctx = _create_perp_context()
    config = PerpGridConfig(
        symbol="HYPE",
        leverage=1,
        grid_range_high=Decimal("100.2"),
        grid_range_low=Decimal("99.8"),
        grid_type=GridType.ARITHMETIC,
        grid_count=5,  # levels: 99.8, 99.9, 100.0, 100.1, 100.2 -> ~0.1% spacing
        total_investment=Decimal("300.0"),
        grid_bias=GridBias.LONG,
        trigger_price=None,
    )
    strategy = PerpGridStrategy(config)
    strategy.initialize_zones(Decimal("100.05"), ctx)

    # Market at 100.05: nearest buy level below is 100.0 (0.05% away, within 0.1%)
    # spread_price = 100.05 * 0.999 = 99.94995 -> max(100.0, 99.94995) = 100.0
    current_price = Decimal("100.05")
    price = strategy._calculate_acquisition_price(OrderSide.BUY, current_price)

    assert price == Decimal("100.0")


# ===========================================================================
# Perp Grid — SELL side
# ===========================================================================


def test_perp_sell_wide_grid_clamps_to_spread():
    """Wide grid (10%): nearest sell level is far above market.
    Clamping should pull the price down to within ACQUISITION_SPREAD of market."""
    ctx = _create_perp_context()
    config = PerpGridConfig(
        symbol="HYPE",
        leverage=1,
        grid_range_high=Decimal("110.0"),
        grid_range_low=Decimal("90.0"),
        grid_type=GridType.ARITHMETIC,
        grid_count=3,  # levels: 90, 100, 110
        total_investment=Decimal("300.0"),
        grid_bias=GridBias.SHORT,
        trigger_price=None,
    )
    strategy = PerpGridStrategy(config)
    strategy.initialize_zones(Decimal("95.0"), ctx)

    # Market at 95: nearest sell level above market is 100 (5.26% away).
    # ACQUISITION_SPREAD.markup(95) = 95 * 1.001 = 95.095
    # min(100, 95.095) = 95.095 -> rounded ROUND_DOWN to 95.09
    current_price = Decimal("95.0")
    price = strategy._calculate_acquisition_price(OrderSide.SELL, current_price)

    spread_price = ACQUISITION_SPREAD.markup(current_price)
    rounded_spread = strategy.market.round_price(spread_price)
    # Price must be at most spread_price (clamped down)
    assert price <= rounded_spread, (
        f"SELL price {price} should be <= spread_price {rounded_spread}"
    )
    # And definitely closer than the raw grid level (100)
    assert price < Decimal("100.0")


def test_perp_sell_narrow_grid_uses_grid_level():
    """Narrow grid: nearest sell level is already within ACQUISITION_SPREAD."""
    ctx = _create_perp_context()
    config = PerpGridConfig(
        symbol="HYPE",
        leverage=1,
        grid_range_high=Decimal("100.2"),
        grid_range_low=Decimal("99.8"),
        grid_type=GridType.ARITHMETIC,
        grid_count=5,
        total_investment=Decimal("300.0"),
        grid_bias=GridBias.SHORT,
        trigger_price=None,
    )
    strategy = PerpGridStrategy(config)
    strategy.initialize_zones(Decimal("99.95"), ctx)

    # Market at 99.95: nearest sell level above is 100.0 (0.05% away, within 0.1%)
    # spread_price = 99.95 * 1.001 = 100.04995 -> min(100.0, 100.04995) = 100.0
    current_price = Decimal("99.95")
    price = strategy._calculate_acquisition_price(OrderSide.SELL, current_price)

    assert price == Decimal("100.0")


# ===========================================================================
# Spot Grid — BUY side
# ===========================================================================


def test_spot_buy_wide_grid_clamps_to_spread():
    """Wide grid: nearest buy level is far below market.
    Clamping should push the price up to within ACQUISITION_SPREAD of market."""
    ctx = _create_spot_context()
    config = SpotGridConfig(
        symbol="LIT/USDC",
        grid_range_high=Decimal("2.0"),
        grid_range_low=Decimal("1.0"),
        grid_type=GridType.ARITHMETIC,
        grid_count=3,  # levels: 1.0, 1.5, 2.0 -> 33% spacing
        total_investment=Decimal("100.0"),
        type="spot_grid",
    )
    strategy = SpotGridStrategy(config)
    strategy.initialize_zones(Decimal("1.6"), ctx)

    # Market at 1.6: nearest buy level below is 1.5 (6.25% away).
    # ACQUISITION_SPREAD.markdown(1.6) = 1.6 * 0.999 = 1.5984
    # max(1.5, 1.5984) = 1.5984 -> clamped up.
    current_price = Decimal("1.6")
    price = strategy._calculate_acquisition_price(OrderSide.BUY, current_price)

    spread_price = ACQUISITION_SPREAD.markdown(current_price)
    rounded_spread = strategy.market.round_price(spread_price)
    assert price >= rounded_spread, (
        f"BUY price {price} should be >= spread_price {rounded_spread}"
    )
    assert price > Decimal("1.5")


def test_spot_buy_narrow_grid_uses_grid_level():
    """Narrow grid: nearest buy level is already within ACQUISITION_SPREAD."""
    ctx = _create_spot_context()
    config = SpotGridConfig(
        symbol="LIT/USDC",
        grid_range_high=Decimal("1.002"),
        grid_range_low=Decimal("0.998"),
        grid_type=GridType.ARITHMETIC,
        grid_count=5,  # ~0.1% spacing
        total_investment=Decimal("100.0"),
        type="spot_grid",
    )
    strategy = SpotGridStrategy(config)
    strategy.initialize_zones(Decimal("1.0005"), ctx)

    current_price = Decimal("1.0005")
    price = strategy._calculate_acquisition_price(OrderSide.BUY, current_price)

    # Nearest buy level (1.0) is within 0.05%, should be used as-is
    assert price == Decimal("1.0")


# ===========================================================================
# Spot Grid — SELL side
# ===========================================================================


def test_spot_sell_wide_grid_clamps_to_spread():
    """Wide grid: nearest sell level is far above market.
    Clamping should pull the price down to within ACQUISITION_SPREAD of market."""
    ctx = _create_spot_context()
    config = SpotGridConfig(
        symbol="LIT/USDC",
        grid_range_high=Decimal("2.0"),
        grid_range_low=Decimal("1.0"),
        grid_type=GridType.ARITHMETIC,
        grid_count=3,  # levels: 1.0, 1.5, 2.0
        total_investment=Decimal("100.0"),
        type="spot_grid",
    )
    strategy = SpotGridStrategy(config)
    strategy.initialize_zones(Decimal("0.5"), ctx)

    # Market at 1.4: nearest sell level above is 1.5 (7.14% away).
    # ACQUISITION_SPREAD.markup(1.4) = 1.4 * 1.001 = 1.4014
    # min(1.5, 1.4014) = 1.4014 -> clamped down.
    current_price = Decimal("1.4")
    price = strategy._calculate_acquisition_price(OrderSide.SELL, current_price)

    spread_price = ACQUISITION_SPREAD.markup(current_price)
    rounded_spread = strategy.market.round_price(spread_price)
    assert price <= rounded_spread, (
        f"SELL price {price} should be <= spread_price {rounded_spread}"
    )
    assert price < Decimal("1.5")


def test_spot_sell_narrow_grid_uses_grid_level():
    """Narrow grid: nearest sell level is already within ACQUISITION_SPREAD."""
    ctx = _create_spot_context()
    config = SpotGridConfig(
        symbol="LIT/USDC",
        grid_range_high=Decimal("1.002"),
        grid_range_low=Decimal("0.998"),
        grid_type=GridType.ARITHMETIC,
        grid_count=5,
        total_investment=Decimal("100.0"),
        type="spot_grid",
    )
    strategy = SpotGridStrategy(config)
    strategy.initialize_zones(Decimal("0.997"), ctx)

    current_price = Decimal("0.9995")
    price = strategy._calculate_acquisition_price(OrderSide.SELL, current_price)

    # Nearest sell level (1.0) is within 0.05%, should be used as-is
    assert price == Decimal("1.0")
