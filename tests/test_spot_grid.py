import pytest
from src.strategy.spot_grid import SpotGridStrategy, GridZone, StrategyState
from src.config import SpotGridConfig
from src.strategy.types import GridType
from src.engine.context import StrategyContext, MarketInfo
from src.model import OrderSide, OrderFill

def create_test_context(symbol: str = "HYPE/USDC") -> StrategyContext:
    markets = {
        symbol: MarketInfo(
            symbol=symbol,
            coin="HYPE",
            market_id=0,
            sz_decimals=2,
            price_decimals=2,
            market_type="spot",
            base_asset_id=1,
            quote_asset_id=2,
            min_base_amount=0.1,
            min_quote_amount=10.0
        )
    }
    ctx = StrategyContext(markets)
    # Give some balance
    ctx.update_spot_balance("HYPE", 10.0, 10.0)
    ctx.update_spot_balance("USDC", 10000.0, 10000.0)
    return ctx

def test_spot_grid_init():
    symbol = "HYPE/USDC"
    ctx = create_test_context(symbol)
    
    config = SpotGridConfig(
        symbol=symbol,
        upper_price=110.0,
        lower_price=90.0,
        grid_type=GridType.ARITHMETIC,
        grid_count=3,
        total_investment=200.0,
        trigger_price=None
    )
    
    strategy = SpotGridStrategy(config)
    strategy.initialize_zones(100.0, ctx)
    
    # Grid: 90, 100, 110. (2 zones)
    assert len(strategy.zones) == 2
    
    # Init Price = 100.
    # Zone 0: [90, 100]. Lower 90 <= 100. So we have Quote, waiting to BUY at 90.
    z0 = strategy.zones[0]
    assert z0.pending_side == OrderSide.BUY
    assert z0.entry_price == 0.0
    
    # Zone 1: [100, 110]. Lower 100 <= 100. So we have Quote, waiting to BUY at 100.
    # Wait, Rust logic: if lower > initial_price -> Sell. Else Buy.
    # 100 > 100 is False. So Buy.
    z1 = strategy.zones[1]
    assert z1.pending_side == OrderSide.BUY

    # Expect 2 BUY orders
    assert len(ctx.order_queue) == 2
    assert ctx.order_queue[0].side == OrderSide.BUY
    assert ctx.order_queue[1].side == OrderSide.BUY

def test_spot_grid_rebalance():
    # Scene: Price 100. Grid 90-110.
    # But User has NO Quote (USDC), only Base (HYPE).
    # Strategy should generate SELL orders to acquire USDC for the Buy zones.
    symbol = "HYPE/USDC"
    ctx = create_test_context(symbol)
    ctx.update_spot_balance("USDC", 0.0, 0.0) # Deficit
    ctx.update_spot_balance("HYPE", 100.0, 100.0)
    
    config = SpotGridConfig(
        symbol=symbol,
        upper_price=110.0,
        lower_price=90.0,
        grid_type=GridType.ARITHMETIC,
        grid_count=3,
        total_investment=200.0,
        trigger_price=None
    )
    
    strategy = SpotGridStrategy(config)
    strategy.initialize_zones(100.0, ctx)
    
    # Zones want to BUY (pending_side BUY).
    # Total Quote Required: Zone0(100/95 * 90) + Zone1(100/105 * 100) approx 200 USDC.
    # We have 0 USDC.
    # Logic: Quote Deficit > 0. Need to Sell Base.
    # Sells at nearest level > market? 110.
    
    assert strategy.state == StrategyState.AcquiringAssets
    assert len(ctx.order_queue) == 1
    rebalance = ctx.order_queue[0]
    assert rebalance.side == OrderSide.SELL
    # Should sell enough HYPE to get ~200 USDC
