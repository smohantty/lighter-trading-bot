import pytest
import math
from src.strategy.perp_grid import PerpGridStrategy, GridZone, StrategyState
from src.config import PerpGridConfig
from src.strategy.types import GridType, GridBias, ZoneMode
from src.engine.context import StrategyContext, MarketInfo, Balance
from src.model import OrderSide, LimitOrderRequest, OrderFill

def create_test_context(symbol: str = "HYPE") -> StrategyContext:
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
            min_base_amount=0.1,
            min_quote_amount=10.0
        )
    }
    ctx = StrategyContext(markets)
    # Give some balance
    ctx.update_perp_balance("USDC", 10000.0, 10000.0)
    return ctx

def test_perp_grid_init_long_bias():
    symbol = "HYPE"
    ctx = create_test_context(symbol)
    
    config = PerpGridConfig(
        symbol=symbol,
        leverage=1,
        upper_price=110.0,
        lower_price=90.0,
        grid_type=GridType.ARITHMETIC,
        grid_count=3,
        total_investment=100.0,
        grid_bias=GridBias.LONG,
        trigger_price=None
    )
    
    strategy = PerpGridStrategy(config)
    strategy.initialize_zones(100.0, ctx)
    
    # Grid: 90, 100, 110. (2 zones)
    # Zone 0: [90, 100]. Market 100.
    # Zone 1: [100, 110]. Market 100.
    
    assert len(strategy.zones) == 2
    
    # Zone 0: Lower 90, Upper 100. LastPrice 100.
    # Long Bias. Zone is below or at price.
    # Rust logic: if lower > initial (100) -> Sell (Close). else -> Buy (Open).
    # 90 is NOT > 100. So Buy.
    z0 = strategy.zones[0]
    assert z0.pending_side == OrderSide.BUY
    assert z0.mode == ZoneMode.LONG
    
    # Zone 1: Lower 100, Upper 110.
    # 100 is NOT > 100. So Buy.
    # Wait, check logic again.
    # if lower > initial_price ...
    # Z0: 90 > 100 False -> Buy
    # Z1: 100 > 100 False -> Buy
    
    z1 = strategy.zones[1]
    assert z1.pending_side == OrderSide.BUY
    assert z1.mode == ZoneMode.LONG
    
    # Check Active Orders
    # Should have placed 2 orders?
    # refresh_orders called.
    assert len(ctx.order_queue) == 2
    assert len(strategy.active_order_map) == 2

def test_perp_grid_execution_flow():
    symbol = "HYPE"
    ctx = create_test_context(symbol)
    
    # Setup simple 2-zone grid: 90-100-110
    config = PerpGridConfig(
        symbol=symbol,
        leverage=1,
        upper_price=110.0,
        lower_price=90.0,
        grid_type=GridType.ARITHMETIC,
        grid_count=3,
        total_investment=200.0, # 100 per zone. Mid ~100. Size ~1.
        grid_bias=GridBias.LONG,
        trigger_price=None
    )
    strategy = PerpGridStrategy(config)
    strategy.initialize_zones(100.0, ctx)
    
    # Expect 2 Buy Orders
    assert len(ctx.order_queue) == 2
    o1 = ctx.order_queue[0]
    o2 = ctx.order_queue[1]
    
    assert isinstance(o1, LimitOrderRequest)
    assert o1.side == OrderSide.BUY
    assert o1.price == 90.0
    
    assert isinstance(o2, LimitOrderRequest)
    assert o2.side == OrderSide.BUY
    assert o2.price == 100.0
    
    # Simulate Fill on Zone 1 (100.0)
    # o2 is associated with zone 1 (range 100-110)
    ctx.order_queue.clear()
    
    # Find cloid for zone 1
    zone1_cloid = strategy.zones[1].order_id
    assert zone1_cloid is not None
    
    fill = OrderFill(
        side=OrderSide.BUY,
        size=1.0, 
        price=100.0,
        fee=0.1,
        cloid=zone1_cloid
    )
    
    strategy.on_order_filled(fill, ctx)
    
    # Should place Counter Order: SELL at 110
    assert len(ctx.order_queue) == 1
    counter_order = ctx.order_queue[0]
    assert isinstance(counter_order, LimitOrderRequest)
    assert counter_order.side == OrderSide.SELL
    assert counter_order.price == 110.0
    assert counter_order.reduce_only is True # Closing long
    
    # Position should be +1
    assert strategy.position_size == 1.0

def test_perp_grid_short_bias():
    symbol = "HYPE"
    ctx = create_test_context(symbol)
    
    config = PerpGridConfig(
        symbol=symbol,
        leverage=1,
        upper_price=110.0,
        lower_price=90.0,
        grid_type=GridType.ARITHMETIC,
        grid_count=3,
        total_investment=200.0,
        grid_bias=GridBias.SHORT,
        trigger_price=None
    )
    strategy = PerpGridStrategy(config)
    strategy.initialize_zones(100.0, ctx)
    
    # Short Bias Logic:
    # If upper < initial (100) -> Buy (Close). Else -> Sell (Open).
    # Z0: 90-100. Upper 100 < 100? No. Sell.
    # Z1: 100-110. Upper 110 < 100? No. Sell.
    
    z0 = strategy.zones[0]
    assert z0.pending_side == OrderSide.SELL
    assert z0.mode == ZoneMode.SHORT
    
    z1 = strategy.zones[1]
    assert z1.pending_side == OrderSide.SELL
    assert z1.mode == ZoneMode.SHORT
    
    # Expect 2 Sell orders
    assert len(ctx.order_queue) == 2
    
    o1 = ctx.order_queue[0]
    assert isinstance(o1, LimitOrderRequest)
    assert o1.side == OrderSide.SELL
    assert o1.price == 100.0
