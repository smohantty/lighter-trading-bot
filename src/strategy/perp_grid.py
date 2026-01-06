import logging
import time
from typing import List, Dict, Optional
from datetime import timedelta
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, auto

from src.strategy.base import Strategy
from src.engine.context import StrategyContext, MarketInfo
from src.model import Cloid, OrderFill, OrderSide, LimitOrderRequest, OrderFailure
from src.config import PerpGridConfig
from src.strategy.types import GridBias, ZoneMode, StrategySummary, PerpGridSummary, GridState, ZoneInfo
from src.strategy import common

logger = logging.getLogger(__name__)

class StrategyState(Enum):
    Initializing = auto()
    WaitingForTrigger = auto()
    AcquiringAssets = auto()
    Running = auto()

@dataclass
class GridZone:
    index: int
    lower_price: Decimal
    upper_price: Decimal
    size: Decimal
    pending_side: OrderSide
    mode: ZoneMode
    entry_price: Decimal
    order_id: Optional[Cloid] = None
    roundtrip_count: int = 0

class PerpGridStrategy(Strategy):
    """
    Perpetual Grid Trading Strategy.
    
    Operates with a directional bias (LONG or SHORT).
    - Long Bias: 
        - Buys (Open) when price drops.
        - Sells (Close) when price rises.
    - Short Bias: 
        - Sells (Open) when price rises.
        - Buys (Close) when price drops.
        
    Uses 'USDC' as collateral.
    """
    def __init__(self, config: PerpGridConfig):
        self.config = config
        self.symbol = config.symbol
        self.leverage = config.leverage
        self.grid_count = config.grid_count
        self.total_investment = config.total_investment # This is Margin amount (USDC)
        self.grid_bias = config.grid_bias
        
        self.zones: List[GridZone] = []
        self.active_order_map: Dict[Cloid, int] = {} # Cloid -> Zone Index
        self.pending_retry_zones: set[int] = set()  # Zones needing order placement
        
        self.trade_count = 0
        self.state = StrategyState.Initializing
        
        # Performance Metrics
        self.realized_pnl = Decimal("0.0")
        self.total_fees = Decimal("0.0")
        self.unrealized_pnl = Decimal("0.0")
        
        # Position Tracking
        self.position_size = Decimal("0.0") 
        self.avg_entry_price = Decimal("0.0")
        
        # Internal State
        self.current_price = Decimal("0.0")
        self.start_time = time.time()
        self.initial_entry_price: Optional[Decimal] = None
        self.trigger_reference_price: Optional[Decimal] = None
        
        # Acquisition State
        self.acquisition_cloid: Optional[Cloid] = None
        self.acquisition_target_size: Decimal = Decimal("0.0")
        
        self.grid_spacing_pct = common.calculate_grid_spacing_pct(
            self.config.grid_type,
            self.config.lower_price,
            self.config.upper_price,
            self.config.grid_count
        )

    def calculate_grid_plan(self, market_info: MarketInfo, reference_price: Decimal) -> tuple[List[GridZone], Decimal]:
        """
        Calculates grid zones and required position size.
        Returns: (zones, required_position_size)
        """
        # 1. Generate Levels
        prices = common.calculate_grid_prices(
            self.config.grid_type,
            self.config.lower_price,
            self.config.upper_price,
            self.config.grid_count
        )
        prices = [market_info.round_price(p) for p in prices] # Rounding

        # 2. Calculate Size per Zone
        # Total Investment is Margin. Total Notional = Margin * Leverage
        # Deduct 0.05% buffer from total investment to ensure we cover fees
        adjusted_investment = self.total_investment * Decimal("0.9995")
        total_notional = adjusted_investment * Decimal(str(self.leverage))
        
        # Division with Decimal can be precise/long, so we quantize or int logic depends on strategy.
        # Original logic: int(total_notional / float(grid_count - 1))
        # We can keep simpler Decimal math:
        notional_per_zone = total_notional / Decimal(str(self.config.grid_count - 1))
        
        # Validation
        # Estimate size at lowest price to be safe
        max_size_estimate = notional_per_zone / Decimal(str(self.config.lower_price))
        min_size_limit = market_info.min_base_amount
        if max_size_estimate < min_size_limit:
             # Just logging warning, will clamp later
             logger.warning(f"[PERP_GRID] improving size estimate: {max_size_estimate} < min {min_size_limit}")

        initial_price = self.config.trigger_price if self.config.trigger_price else reference_price
        
        # 3. Build Zones & Calculate Initial Requirement
        zones = []
        required_position_size = Decimal("0.0") # Positive for Long, Negative for Short
        
        for i in range(self.config.grid_count - 1):
            lower = prices[i]
            upper = prices[i+1]
            
            # Use lower price for conservative Notional -> Size conversion
            ref_price_for_size = lower
            raw_size = notional_per_zone / ref_price_for_size
            size = market_info.round_size(raw_size)
            
            # Logic for Bias
            pending_side = OrderSide.BUY # Placeholder
            mode = ZoneMode.LONG # Placeholder
            entry_price = Decimal("0.0")
            
            if self.grid_bias == GridBias.LONG:
                mode = ZoneMode.LONG
                # GRID LOGIC:
                # If Price is ABOVE zone: We should have ALREADY bought. Zone is Waiting to SELL (Close).
                # If Price is BELOW zone: We have NOT bought. Zone is Waiting to BUY (Open).
                
                if lower < initial_price:
                    # WE HOLD THIS ZONE
                    pending_side = OrderSide.SELL # Target is to close at Upper
                    required_position_size += size
                    entry_price = initial_price # Mock entry price
                else:
                    # WE DO NOT HOLD
                    pending_side = OrderSide.BUY # Target is to open at Lower
                    entry_price = Decimal("0.0")
                    
            elif self.grid_bias == GridBias.SHORT:
                mode = ZoneMode.SHORT
                # GRID LOGIC:
                # If Price is BELOW zone: We should have ALREADY sold. Zone is Waiting to BUY (Close).
                # If Price is ABOVE zone: We have NOT sold. Zone is Waiting to SELL (Open).
                
                if upper > initial_price:
                   # WE HOLD THIS ZONE (SHORT)
                   pending_side = OrderSide.BUY # Target is to close at Lower
                   required_position_size -= size # Negative for short
                   entry_price = initial_price
                else:
                   # WE DO NOT HOLD
                   pending_side = OrderSide.SELL # Target is to open at Upper
                   entry_price = Decimal("0.0")

            zones.append(GridZone(
                index=i,
                lower_price=lower,
                upper_price=upper,
                size=size,
                pending_side=pending_side,
                mode=mode,
                entry_price=entry_price
            ))
            
        return zones, required_position_size

    def initialize_zones(self, price: Decimal, ctx: StrategyContext):
        self.current_price = price
        market_info = ctx.market_info(self.symbol)
        if not market_info:
            raise ValueError(f"No market info for {self.symbol}")

        self.zones, required_position_size = self.calculate_grid_plan(market_info, price)
            
        logger.info(f"[PERP_GRID] Setup Complete. Bias: {self.grid_bias}. Required Net Position: {required_position_size:.4f}")
        
        # 4. Check Initial Acquisition
        # We assume initial position is 0.0 (or that we build on top of whatever exists)
        self.check_initial_acquisition(ctx, market_info, required_position_size)



    def check_initial_acquisition(
        self,
        ctx: StrategyContext,
        market_info: MarketInfo,
        target_position: Decimal
    ):
        """
        Calculates required acquisition based on target position.
        Assumes starting from 0 internal position.
        """
        # Since we use self.position_size (internal tracking), it starts at 0.0.
        # So needed_change is exactly target_position.
        needed_change = target_position
        
        minimal_size = market_info.min_base_amount
        
        if abs(needed_change) < minimal_size:
            # Negligible
            if self.config.trigger_price:
                 logger.info("[PERP_GRID] Position OK. Waiting for Trigger.")
                 self.state = StrategyState.WaitingForTrigger
                 self.trigger_reference_price = self.current_price
            else:
                 logger.info("[PERP_GRID] Position OK. Starting.")
                 self.state = StrategyState.Running
                 self.refresh_orders(ctx)
            return

        # Need to Acquire
        side = OrderSide.BUY if needed_change > 0 else OrderSide.SELL
        size = abs(needed_change)
        size = market_info.round_size(size)
        
        # Price determination
        initial_price = self.config.trigger_price if self.config.trigger_price else self.current_price
        
        # For immediate acquisition, we usually use Market or Aggressive Limit.
        # But if trigger is set, we might use Trigger Price.
        price = initial_price
        
        # Logic: If trigger is present, wait for trigger? 
        # If we need position NOW to be "in grid", we should acquire NOW.
        # The user's request "work with allocated investment amount" implies we take the position.
        
        logger.info(f"[PERP_GRID] Acquiring Initial Position: {side} {size} @ {price}")
        
        cloid = ctx.generate_cloid()
        self.state = StrategyState.AcquiringAssets
        self.acquisition_cloid = cloid
        self.acquisition_target_size = size
        
        # Using Limit order at current/trigger price. 
        # Ideally should be marketable if we want immediate entry, but limit is safer.
        ctx.place_order(LimitOrderRequest(
            symbol=self.symbol,
            side=side,
            price=market_info.round_price(price),
            sz=size,
            reduce_only=False,
            cloid=cloid
        ))

    def place_zone_order(self, idx: int, ctx: StrategyContext):
        """Place an order for a zone based on its current state."""
        zone = self.zones[idx]
        if zone.order_id is not None:
            return
        
        side = zone.pending_side
        price = zone.lower_price if side.is_buy() else zone.upper_price
        
        # Determine reduce_only based on zone mode
        reduce_only = False
        if zone.mode == ZoneMode.LONG and side.is_sell():
            reduce_only = True
        if zone.mode == ZoneMode.SHORT and side.is_buy():
            reduce_only = True
        
        cloid = ctx.generate_cloid()
        zone.order_id = cloid
        self.active_order_map[cloid] = idx
        
        logger.info(f"[ORDER_REQUEST] [PERP_GRID] GRID_ZONE_{idx} cloid: {cloid.as_int()} LIMIT {side} {zone.size} @ {price}")
        ctx.place_order(LimitOrderRequest(
            symbol=self.symbol,
            side=side,
            price=price,
            sz=zone.size,
            reduce_only=reduce_only,
            cloid=cloid
        ))

    def refresh_orders(self, ctx: StrategyContext):
        """Place orders for all zones that don't have one."""
        for idx, zone in enumerate(self.zones):
            if zone.order_id is None:
                self.place_zone_order(idx, ctx)

    def _process_pending_retries(self, ctx: StrategyContext):
        """Process only zones that are queued for retry (efficient for large grids)."""
        if not self.pending_retry_zones:
            return
        
        zones_to_process = list(self.pending_retry_zones)
        self.pending_retry_zones.clear()
        
        for idx in zones_to_process:
            self.place_zone_order(idx, ctx)

    def on_tick(self, price: Decimal, ctx: StrategyContext):
        self.current_price = price
        if self.state == StrategyState.Initializing:
            self.initialize_zones(price, ctx)
            
        elif self.state == StrategyState.WaitingForTrigger:
             if self.config.trigger_price and self.trigger_reference_price:
                 if common.check_trigger(price, self.config.trigger_price, self.trigger_reference_price):
                     logger.info(f"[PERP_GRID] Triggered at {price}")
                     self.state = StrategyState.Running
                     self.refresh_orders(ctx)
                     
        elif self.state == StrategyState.Running:
             # Process only zones in the retry queue (efficient for large grids)
             if self.pending_retry_zones:
                 self._process_pending_retries(ctx)
             # Full refresh only needed if no orders are active at all
             elif not self.active_order_map and self.zones:
                 self.refresh_orders(ctx)

    def on_order_filled(self, fill: OrderFill, ctx: StrategyContext):
        if fill.cloid:
            # 1. Acquisition Fill
            if self.state == StrategyState.AcquiringAssets and fill.cloid == self.acquisition_cloid:
                self.total_fees += fill.fee
                
                # Update Internal Position
                if fill.side.is_buy():
                    self.position_size += fill.size
                else:
                    self.position_size -= fill.size
                
                self.avg_entry_price = fill.price # Reset avg entry for initial
                
                # Update Zones that were "holding"
                for zone in self.zones:
                    # If Long Bias and zone is selling (Close) -> it implies we bought it.
                    if zone.mode == ZoneMode.LONG and zone.pending_side.is_sell():
                        zone.entry_price = fill.price
                    # If Short Bias and zone is buying (Close) -> it implies we sold it.
                    if zone.mode == ZoneMode.SHORT and zone.pending_side.is_buy():
                        zone.entry_price = fill.price
                
                logger.info(f"[PERP_GRID] Acquisition Complete. Pos: {self.position_size}. AvgEntry: {self.avg_entry_price}")
                self.state = StrategyState.Running
                self.initial_entry_price = fill.price
                self.refresh_orders(ctx)
                return

            # 2. Grid Fill
            if fill.cloid in self.active_order_map:
                idx = self.active_order_map.pop(fill.cloid)
                zone = self.zones[idx]
                zone.order_id = None
                self.total_fees += fill.fee
                
                # Update Position
                old_pos = self.position_size
                if fill.side.is_buy():
                    self.position_size += fill.size
                else:
                    self.position_size -= fill.size
                
                is_opening = False
                # Long Open: Buy
                if zone.mode == ZoneMode.LONG and fill.side.is_buy(): is_opening = True
                # Short Open: Sell
                if zone.mode == ZoneMode.SHORT and fill.side.is_sell(): is_opening = True
                
                # Update Avg Entry
                if is_opening:
                     # Add to position -> Standard weighted average
                     # abs() to handle short sizes being negative
                     current_abs = abs(self.position_size)
                     fill_val = fill.size * fill.price
                     old_val = abs(old_pos) * self.avg_entry_price
                     if current_abs > 0:
                         self.avg_entry_price = (old_val + fill_val) / current_abs
                else:
                    # Closing -> Avg Entry doesn't change, but PnP realized
                    pass

                # Handle PnL and State Flip
                pnl = Decimal("0.0")
                market_info = ctx.market_info(self.symbol)
                
                if zone.mode == ZoneMode.LONG:
                    if fill.side.is_buy():
                        # Filled OPEN (Buy at Lower) -> Next: Close at Upper
                        zone.entry_price = fill.price
                        zone.pending_side = OrderSide.SELL
                        logger.info(f"[PERP_GRID] Z{idx} BUY (Open) @ {fill.price}. Next: SELL @ {zone.upper_price}")
                        self.place_zone_order(idx, ctx)
                    else:
                        # Filled CLOSE (Sell at Upper) -> Next: Open at Lower
                        pnl = (fill.price - zone.entry_price) * fill.size
                        zone.pending_side = OrderSide.BUY
                        zone.roundtrip_count += 1
                        logger.info(f"[PERP_GRID] Z{idx} SELL (Close) @ {fill.price}. PnL: {pnl:.4f}. Next: BUY @ {zone.lower_price}")
                        self.place_zone_order(idx, ctx)
                        
                elif zone.mode == ZoneMode.SHORT:
                    if fill.side.is_sell():
                        # Filled OPEN (Sell at Upper) -> Next: Close at Lower
                        zone.entry_price = fill.price
                        zone.pending_side = OrderSide.BUY
                        logger.info(f"[PERP_GRID] Z{idx} SELL (Open) @ {fill.price}. Next: BUY @ {zone.lower_price}")
                        self.place_zone_order(idx, ctx)
                    else:
                        # Filled CLOSE (Buy at Lower) -> Next: Open at Upper
                        pnl = (zone.entry_price - fill.price) * fill.size
                        zone.pending_side = OrderSide.SELL
                        zone.roundtrip_count += 1
                        logger.info(f"[PERP_GRID] Z{idx} BUY (Close) @ {fill.price}. PnL: {pnl:.4f}. Next: SELL @ {zone.upper_price}")
                        self.place_zone_order(idx, ctx)

                self.realized_pnl += pnl

    def on_order_failed(self, failure: OrderFailure, ctx: StrategyContext):
        cloid = failure.cloid
        if cloid in self.active_order_map:
            idx = self.active_order_map.pop(cloid)
            zone = self.zones[idx]
            zone.order_id = None
            
            logger.warning(f"[ORDER_FAILED][PERP_GRID] GRID_ZONE_{idx} cloid: {cloid.as_int()} "
                          f"reason: {failure.failure_reason}. Queued for retry on next tick.")
            
            # Queue for retry on next tick
            self.pending_retry_zones.add(idx)

    def get_summary(self, ctx: StrategyContext) -> PerpGridSummary:
        unrealized = Decimal("0.0")
        if self.position_size != 0:
            diff = self.current_price - self.avg_entry_price
            if self.position_size < 0: diff = -diff
            unrealized = diff * abs(self.position_size)

        return PerpGridSummary(
            symbol=self.symbol,
            state=self.state.name,
            uptime=common.format_uptime(timedelta(seconds=time.time() - self.start_time)),
            position_size=self.position_size,
            position_side="Long" if self.position_size > 0 else "Short",
            avg_entry_price=self.avg_entry_price,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            total_fees=self.total_fees,
            leverage=self.leverage,
            grid_bias=self.config.grid_bias.value,
            grid_count=len(self.zones),
            range_low=self.config.lower_price,
            range_high=self.config.upper_price,
            grid_spacing_pct=self.grid_spacing_pct,
            roundtrips=sum(z.roundtrip_count for z in self.zones),
            margin_balance=ctx.get_perp_available("USDC"),
            initial_entry_price=self.initial_entry_price
        )

    def get_grid_state(self, ctx: StrategyContext) -> GridState:
        return GridState(
            symbol=self.symbol,
            strategy_type="perp_grid",
            grid_bias=self.config.grid_bias.value,
            zones=[
                ZoneInfo(
                    index=z.index,
                    lower_price=z.lower_price,
                    upper_price=z.upper_price,
                    size=z.size,
                    pending_side=str(z.pending_side),
                    has_order=z.order_id is not None,
                    is_reduce_only=(z.mode == ZoneMode.LONG and z.pending_side.is_sell()) or (z.mode == ZoneMode.SHORT and z.pending_side.is_buy()),
                    entry_price=z.entry_price,
                    roundtrip_count=z.roundtrip_count
                ) for z in self.zones
            ]
        )
