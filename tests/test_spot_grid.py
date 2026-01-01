import pytest
from unittest.mock import MagicMock
from src.strategy.spot_grid import SpotGridStrategy, GridZone, StrategyState
from src.strategy.types import GridType, GridBias
from src.config import SpotGridConfig
from src.engine.context import StrategyContext, MarketInfo
from src.model import OrderSide, LimitOrderRequest, OrderFill, Cloid

@pytest.fixture
def spot_config():
    return SpotGridConfig(
        symbol="LIT/USDC",
        upper_price=2.0,
        lower_price=1.0,
        grid_type=GridType.ARITHMETIC,
        grid_count=3,
        total_investment=100.0,
        type="spot_grid"
    )

@pytest.fixture
def market_info():
    info = MagicMock(spec=MarketInfo)
    info.min_quote_amount = 5.0
    info.min_base_amount = 1.0
    info.round_price.side_effect = lambda x: round(x, 4)
    info.round_size.side_effect = lambda x: round(x, 4)
    info.clamp_to_min_size.side_effect = lambda x: max(x, 1.0)
    info.clamp_to_min_notional.side_effect = lambda size, px: size if size*px >= 5.0 else 0.0
    info.last_price = 1.6
    return info

@pytest.fixture
def context(market_info):
    ctx = MagicMock(spec=StrategyContext)
    ctx.market_info.return_value = market_info
    ctx.get_spot_available.return_value = 1000.0 
    ctx.generate_cloid.side_effect = lambda: Cloid(123)
    return ctx

def test_initialization_rust_logic(spot_config, context):
    """
    Test initialization with Rust logic:
    Price = 1.6
    Grid: 1.0, 1.5, 2.0
    
    Zone 0: 1.0 - 1.5. Lower(1.0) <= 1.6. Rust: If lower > initial (1.6) -> Sell. Else -> Buy.
    Since 1.0 < 1.6, this should be BUY pending side (waiting to buy dip at 1.0).
    Wait, logic check:
    Rust: 
    if lower > initial_price { Sell } else { Buy }
    
    Zone 0 (1.0-1.5): lower=1.0. 1.0 > 1.6 False. -> Pending BUY.
    Zone 1 (1.5-2.0): lower=1.5. 1.5 > 1.6 False. -> Pending BUY.
    
    Wait, if I am at 1.6, and Zone 1 is 1.5-2.0. 
    If price drops to 1.5, I want to buy. Correct.
    If price rises to 2.0, I want to sell (if I have it).
    
    Rust logic says "Zone AT or BELOW price line: We have quote, waiting to buy at lower".
    My price is 1.6.
    Zone 0 lower is 1.0. 1.0 < 1.6. So we are waiting to buy at 1.0. Correct.
    Zone 1 lower is 1.5. 1.5 < 1.6. So we are waiting to buy at 1.5. Correct.
    
    So both zones should be BUY pending side.
    """
    strategy = SpotGridStrategy(spot_config)
    strategy.initialize_zones(1.6, context)
    
    assert len(strategy.zones) == 2
    
    # Zone 0
    assert strategy.zones[0].lower_price == 1.0
    assert strategy.zones[0].pending_side == OrderSide.BUY
    
    # Zone 1
    assert strategy.zones[1].lower_price == 1.5
    assert strategy.zones[1].pending_side == OrderSide.BUY
    
    # Check Inventory Initialization
    # Available Base = 1000.0 (from mocked context)
    # Required Base for pending BUYs is 0 (we need quote)
    # Actually logic: if lower > initial (1.6) -> Sell (base needed).
    # Here both zones lower (1.0, 1.5) < initial (1.6). So both are BUY.
    # Required Base = 0.
    # Required Quote = sum(size * lower)
    assert strategy.inventory_base == 0.0
    # inventory_quote is initialized to min(avail_quote, required_quote).
    # We didn't mock avail_quote explicitly in context fixture? 
    # context fixture has get_spot_available return 1000.0 for ANY asset.
    # So avail_quote = 1000.0.
    # required_quote > 0. So inventory_quote should be > 0.
    assert strategy.inventory_quote > 0.0

def test_initialization_sell_logic(spot_config, context):
    """
    Test case where we are below the grid, so we should be selling (if we had bag).
    Price = 0.5.
    Grid: 1.0, 1.5, 2.0.
    
    Zone 0 lower=1.0. 1.0 > 0.5. True. -> Pending SELL.
    Zone 1 lower=1.5. 1.5 > 0.5. True. -> Pending SELL.
    """
    strategy = SpotGridStrategy(spot_config)
    strategy.initialize_zones(0.5, context)
    
    assert strategy.zones[0].pending_side == OrderSide.SELL
    assert strategy.zones[1].pending_side == OrderSide.SELL

def test_fill_lifecycle_rust(spot_config, context):
    strategy = SpotGridStrategy(spot_config)
    strategy.state = StrategyState.Running
    
    # Create manual zone: BUY side (waiting to buy at 1.0)
    zone = GridZone(
        index=0,
        lower_price=1.0,
        upper_price=2.0,
        size=10.0,
        pending_side=OrderSide.BUY,
        mode=None,
        entry_price=0.0
    )
    strategy.zones = [zone]
    
    cloid = Cloid(100)
    strategy.active_order_map[cloid] = 0
    zone.order_id = cloid
    
    # Fill BUY (We bought at 1.0)
    fill = OrderFill(
        side=OrderSide.BUY,
        size=10.0,
        price=1.0,
        fee=0.1,
        cloid=cloid
    )
    
    strategy.on_order_filled(fill, context)
    
    # Logic: Buy Filled -> Switch to Sell (at Upper Price)
    assert zone.pending_side == OrderSide.SELL
    assert zone.entry_price == 1.0
    assert strategy.inventory_base == 10.0
    
    # Verify counter order
    args = context.place_order.call_args[0][0]
    assert args.side == OrderSide.SELL
    assert args.price == 2.0
    
    # Verify Inventory Update
    # Initial was 0 base (manually set in test logic, but strategy might have initialized differently if we used full init flow)
    # But here we manually modified zones.
    # Let's assume inventory was 0.
    # BUY Fill 10.0 @ 1.0. 
    # Base += 10.0
    # Quote -= 10.0 * 1.0 = 10.0
    assert strategy.inventory_base == 10.0
    assert strategy.inventory_quote == -10.0 # Started at 0 in this specific manual test setup




def test_get_summary(spot_config, context):
    """
    Test get_summary maps inventory to base_balance/quote_balance.
    """
    strategy = SpotGridStrategy(spot_config)
    strategy.initialize_zones(1.6, context)
    
    # After init, inventory_base = 0.0 (from test_initialization_rust_logic)
    # inventory_quote > 0.0
    
    summary = strategy.get_summary(context)
    
    # Assert get_summary fields match inventory fields
    assert summary.base_balance == strategy.inventory_base
    assert summary.quote_balance == strategy.inventory_quote
    assert summary.base_balance == 0.0
    assert summary.quote_balance > 0.0

def test_initialization_out_of_range_error(spot_config, context):
    """
    Test that ValueError is raised if we need to acquire assets but are out of range.
    Price = 0.5 (Below Grid 1.0-2.0).
    Grid: 1.0, 1.5, 2.0.
    Zones: [1.0-1.5, 1.5-2.0]
    
    If we are at 0.5, and we trigger acquisition (e.g. need base asset), it should fail.
    However, if we are below grid, we usually need Quote to buy.
    Logic:
    If below (0.5), Zones are ABOVE us. We want to BUY at lower.
    Zone 0 lower=1.0. 1.0 > 0.5. Pending Side = SELL.
    Required Base += size.
    
    So we need Base.
    If we don't have enough Base (avail_base=0), we need to ACQUIRE base.
    To acquire base, we normally look for a level LOWER than market to buy at.
    But market is 0.5. All levels (1.0, 1.5) are > 0.5.
    Candidates < 0.5 is EMPTY.
    Should raise ValueError.
    """
    # We need to pass upfront validation (Total Value >= 100)
    # But fail acquisition (Base Deficit, no way to buy).
    # So give enough Quote to cover TOTAL investment, but 0 base.
    def get_balance(asset):
        if asset == "LIT": return 0.0
        if asset == "USDC": return 200.0
        return 0.0
    
    context.get_spot_available.side_effect = get_balance
    
    strategy = SpotGridStrategy(spot_config)

    # We expect ValueError
    with pytest.raises(ValueError, match="Current price 0.5 is below grid range"):
        strategy.initialize_zones(0.5, context)
