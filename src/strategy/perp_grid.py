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
from src.strategy.types import GridBias, ZoneMode, StrategySummary, PerpGridSummary, GridState, ZoneInfo, GridZone, Spread
from src.strategy import common

logger = logging.getLogger(__name__)

# Constants
FEE_BUFFER = Spread("0.05")  # 0.05% buffer for fees/slippage
INVESTMENT_BUFFER = Spread("0.05")  # 0.05% buffer from total investment
MAX_RETRIES = 5


class StrategyState(Enum):
    Initializing = auto()
    WaitingForTrigger = auto()
    AcquiringAssets = auto()
    Running = auto()


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

    # =========================================================================
    # INITIALIZATION
    # =========================================================================

    def __init__(self, config: PerpGridConfig):
        self.config = config
        self.symbol = config.symbol
        self.leverage = config.leverage
        self.grid_count = config.grid_count
        self.total_investment = config.total_investment  # This is Margin amount (USDC)
        self.grid_bias = config.grid_bias
        
        self.zones: List[GridZone] = []
        self.active_order_map: Dict[Cloid, GridZone] = {} 
        
        self.trade_count = 0
        self.state = StrategyState.Initializing
        
        # Performance Metrics
        self.realized_pnl = Decimal("0")
        self.total_fees = Decimal("0")
        self.unrealized_pnl = Decimal("0")
        
        # Position Tracking
        self.position_size = Decimal("0") 
        self.avg_entry_price = Decimal("0")
        
        # Internal State
        self.current_price = Decimal("0")
        self.start_time = time.time()
        self.initial_entry_price: Optional[Decimal] = None
        self.trigger_reference_price: Optional[Decimal] = None
        
        # Acquisition State
        self.acquisition_cloid: Optional[Cloid] = None
        self.acquisition_target_size: Decimal = Decimal("0")
        
        self.grid_spacing_pct = common.calculate_grid_spacing_pct(
            self.config.grid_type,
            self.config.lower_price,
            self.config.upper_price,
            self.config.grid_count
        )

    # =========================================================================
    # STRATEGY LIFECYCLE (Base Class Interface)
    # =========================================================================

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
             # Continuously ensure orders are active
             self.refresh_orders(ctx)

    def on_order_filled(self, fill: OrderFill, ctx: StrategyContext):
        if fill.cloid:
            # 1. Acquisition Fill
            if self.state == StrategyState.AcquiringAssets and fill.cloid == self.acquisition_cloid:
                self._handle_acquisition_fill(fill, ctx)
                return

            # 2. Grid Fill
            if fill.cloid in self.active_order_map:
                zone = self.active_order_map.pop(fill.cloid)
                idx = zone.index
                zone.order_id = None
                self.total_fees += fill.fee
                
                # Update Position
                old_pos = self.position_size
                if fill.side.is_buy():
                    self.position_size += fill.size
                else:
                    self.position_size -= fill.size
                
                # Update Avg Entry for opening trades
                self._update_avg_entry(zone, fill, old_pos)

                # Handle PnL and State Flip based on zone mode
                if zone.mode == ZoneMode.LONG:
                    self._handle_long_fill(zone, fill, ctx)
                elif zone.mode == ZoneMode.SHORT:
                    self._handle_short_fill(zone, fill, ctx)

    def on_order_failed(self, failure: OrderFailure, ctx: StrategyContext):
        cloid = failure.cloid
        if cloid in self.active_order_map:
            zone = self.active_order_map.pop(cloid)
            idx = zone.index
            zone.order_id = None
            
            logger.warning(f"[ORDER_FAILED][PERP_GRID] GRID_ZONE_{idx} cloid: {cloid.as_int()} "
                          f"reason: {failure.failure_reason}. Retry count: {zone.retry_count + 1}/{MAX_RETRIES}")
            
            zone.retry_count += 1

    def get_summary(self, ctx: StrategyContext) -> PerpGridSummary:
        unrealized = Decimal("0")
        if self.position_size != 0:
            diff = self.current_price - self.avg_entry_price
            if self.position_size < 0:
                diff = -diff
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
                    buy_price=z.buy_price,
                    sell_price=z.sell_price,
                    size=z.size,
                    pending_side=str(z.pending_side),
                    has_order=z.order_id is not None,
                    is_reduce_only=self._is_reduce_only(z),
                    entry_price=z.entry_price,
                    roundtrip_count=z.roundtrip_count
                ) for z in self.zones
            ]
        )

    # =========================================================================
    # GRID SETUP & INITIALIZATION
    # =========================================================================

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
        prices = [market_info.round_price(p) for p in prices]

        # 2. Calculate Size per Zone
        # total_investment = Total Notional Value (not margin)
        # Margin Required = total_investment / leverage (checked against available balance)
        adjusted_investment = INVESTMENT_BUFFER.markdown(self.total_investment)
        notional_per_zone = adjusted_investment / Decimal(str(self.config.grid_count - 1))
        
        # Validation
        max_size_estimate = notional_per_zone / Decimal(str(self.config.lower_price))
        min_size_limit = market_info.min_base_amount
        if max_size_estimate < min_size_limit:
             logger.warning(f"[PERP_GRID] Size estimate below minimum: {max_size_estimate} < {min_size_limit}")

        initial_price = self.config.trigger_price if self.config.trigger_price else reference_price
        
        # 3. Build Zones & Calculate Initial Requirement
        zones = []
        required_position_size = Decimal("0.0")
        
        for i in range(self.config.grid_count - 1):
            zone_buy_price = prices[i]
            zone_sell_price = prices[i+1]
            
            # Use buy price for conservative Notional -> Size conversion
            raw_size = notional_per_zone / zone_buy_price
            size = market_info.round_size(raw_size)
            
            pending_side, mode, entry_price, position_delta = self._calculate_zone_initial_state(
                zone_buy_price, zone_sell_price, initial_price, size
            )
            required_position_size += position_delta

            zones.append(GridZone(
                index=i,
                buy_price=zone_buy_price,
                sell_price=zone_sell_price,
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
        
        # Check Initial Acquisition
        self.check_initial_acquisition(ctx, market_info, required_position_size)

    def check_initial_acquisition(
        self,
        ctx: StrategyContext,
        market_info: MarketInfo,
        target_position: Decimal
    ) -> None:
        """
        Calculates required acquisition based on target position.
        Assumes starting from 0 internal position.
        """
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
        size = market_info.round_size(abs(needed_change))
        
        # Price determination
        price = self.config.trigger_price if self.config.trigger_price else self.current_price
        
        logger.info(f"[ORDER_REQUEST] [PERP_GRID] [ACQUISITION] {side} {size} @ {price}")
        
        cloid = ctx.place_order(LimitOrderRequest(
            symbol=self.symbol,
            side=side,
            price=price,
            sz=size,
            reduce_only=False
        ))
        
        self.state = StrategyState.AcquiringAssets
        self.acquisition_cloid = cloid
        self.acquisition_target_size = size

    # =========================================================================
    # ORDER MANAGEMENT
    # =========================================================================

    def place_zone_order(self, zone: GridZone, ctx: StrategyContext):
        """Place an order for a zone based on its current state."""
        if zone.order_id is not None:
            return
        
        idx = zone.index
        side = zone.pending_side
        price = zone.buy_price if side.is_buy() else zone.sell_price
        reduce_only = self._is_reduce_only(zone)
        
        cloid = ctx.place_order(LimitOrderRequest(
            symbol=self.config.symbol,
            side=side,
            price=price,
            sz=zone.size,
            reduce_only=reduce_only
        ))
        
        zone.order_id = cloid
        self.active_order_map[cloid] = zone
        
        logger.info(f"[ORDER_REQUEST] [PERP_GRID] GRID_ZONE_{idx} cloid: {cloid.as_int()} LIMIT {side} {zone.size} @ {price}")

    def refresh_orders(self, ctx: StrategyContext):
        """Place orders for all zones that don't have one and haven't exceeded max retries."""
        for zone in self.zones:
            if zone.order_id is None:
                if zone.retry_count < MAX_RETRIES:
                     self.place_zone_order(zone, ctx)

    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================

    def _calculate_zone_initial_state(
        self, 
        zone_buy_price: Decimal, 
        zone_sell_price: Decimal, 
        initial_price: Decimal, 
        size: Decimal
    ) -> tuple[OrderSide, ZoneMode, Decimal, Decimal]:
        """
        Calculate initial zone state based on bias and price position.
        Returns: (pending_side, mode, entry_price, position_delta)
        """
        pending_side = OrderSide.BUY
        mode = ZoneMode.LONG
        entry_price = Decimal("0")
        position_delta = Decimal("0")
        
        if self.grid_bias == GridBias.LONG:
            mode = ZoneMode.LONG
            # GRID LOGIC:
            # If zone is ABOVE price (buy_price > initial): We already bought. Wait to SELL (Close).
            # If zone is AT or BELOW price (buy_price <= initial): We haven't bought. Wait to BUY (Open).
            
            if zone_buy_price > initial_price:
                # WE HOLD THIS ZONE - already opened long
                pending_side = OrderSide.SELL  # Target is to close at sell_price
                position_delta = size
                entry_price = initial_price
            else:
                # WE DO NOT HOLD - waiting to open long
                pending_side = OrderSide.BUY  # Target is to open at buy_price
                entry_price = Decimal("0")
                
        elif self.grid_bias == GridBias.SHORT:
            mode = ZoneMode.SHORT
            # GRID LOGIC:
            # If zone is BELOW price (sell_price < initial): We already sold. Wait to BUY (Close).
            # If zone is AT or ABOVE price (sell_price >= initial): We haven't sold. Wait to SELL (Open).
            
            if zone_sell_price < initial_price:
               # WE HOLD THIS ZONE (SHORT) - already opened short
               pending_side = OrderSide.BUY  # Target is to close at buy_price
               position_delta = -size  # Negative for short
               entry_price = initial_price
            else:
               # WE DO NOT HOLD - waiting to open short
               pending_side = OrderSide.SELL  # Target is to open at sell_price
               entry_price = Decimal("0")
               
        return pending_side, mode, entry_price, position_delta

    def _is_reduce_only(self, zone: GridZone) -> bool:
        """Determine if a zone order should be reduce_only."""
        if zone.mode == ZoneMode.LONG and zone.pending_side.is_sell():
            return True
        if zone.mode == ZoneMode.SHORT and zone.pending_side.is_buy():
            return True
        return False

    def _update_avg_entry(self, zone: GridZone, fill: OrderFill, old_pos: Decimal) -> None:
        """Update average entry price for opening trades."""
        is_opening = False
        # Long Open: Buy
        if zone.mode == ZoneMode.LONG and fill.side.is_buy():
            is_opening = True
        # Short Open: Sell
        if zone.mode == ZoneMode.SHORT and fill.side.is_sell():
            is_opening = True
        
        if is_opening:
            # Add to position -> Standard weighted average
            current_abs = abs(self.position_size)
            fill_val = fill.size * fill.price
            old_val = abs(old_pos) * self.avg_entry_price
            if current_abs > 0:
                self.avg_entry_price = (old_val + fill_val) / current_abs

    def _handle_acquisition_fill(self, fill: OrderFill, ctx: StrategyContext) -> None:
        """Handle the fill of an acquisition order during initial setup."""
        self.total_fees += fill.fee
        
        # Update Internal Position
        if fill.side.is_buy():
            self.position_size += fill.size
        else:
            self.position_size -= fill.size
        
        self.avg_entry_price = fill.price  # Reset avg entry for initial
        
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

    def _handle_long_fill(self, zone: GridZone, fill: OrderFill, ctx: StrategyContext) -> None:
        """Handle fill logic for LONG bias zones."""
        idx = zone.index
        pnl = Decimal("0.0")
        
        if fill.side.is_buy():
            # Filled OPEN (Buy at Lower) -> Next: Close at Upper
            zone.entry_price = fill.price
            zone.pending_side = OrderSide.SELL
            logger.info(f"[PERP_GRID] Z{idx} BUY (Open) @ {fill.price}. Next: SELL @ {zone.sell_price}")
            zone.retry_count = 0
            self.place_zone_order(zone, ctx)
        else:
            # Filled CLOSE (Sell at Upper) -> Next: Open at Lower
            pnl = (fill.price - zone.entry_price) * fill.size
            zone.pending_side = OrderSide.BUY
            zone.roundtrip_count += 1
            logger.info(f"[PERP_GRID] Z{idx} SELL (Close) @ {fill.price}. PnL: {pnl:.4f}. Next: BUY @ {zone.buy_price}")
            zone.retry_count = 0
            self.place_zone_order(zone, ctx)
        
        self.realized_pnl += pnl

    def _handle_short_fill(self, zone: GridZone, fill: OrderFill, ctx: StrategyContext) -> None:
        """Handle fill logic for SHORT bias zones."""
        idx = zone.index
        pnl = Decimal("0.0")
        
        if fill.side.is_sell():
            # Filled OPEN (Sell at Upper) -> Next: Close at Lower
            zone.entry_price = fill.price
            zone.pending_side = OrderSide.BUY
            logger.info(f"[PERP_GRID] Z{idx} SELL (Open) @ {fill.price}. Next: BUY @ {zone.buy_price}")
            zone.retry_count = 0
            self.place_zone_order(zone, ctx)
        else:
            # Filled CLOSE (Buy at Lower) -> Next: Open at Upper
            pnl = (zone.entry_price - fill.price) * fill.size
            zone.pending_side = OrderSide.SELL
            zone.roundtrip_count += 1
            logger.info(f"[PERP_GRID] Z{idx} BUY (Close) @ {fill.price}. PnL: {pnl:.4f}. Next: SELL @ {zone.sell_price}")
            zone.retry_count = 0
            self.place_zone_order(zone, ctx)
        
        self.realized_pnl += pnl
