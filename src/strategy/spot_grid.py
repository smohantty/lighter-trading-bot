import logging

import time
from typing import List, Optional, Dict
from decimal import Decimal
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum, auto

from src.strategy.base import Strategy
from src.strategy import common
from src.engine.context import StrategyContext, MarketInfo
from src.model import OrderRequest, LimitOrderRequest, OrderSide, OrderFill, Cloid, OrderFailure
from src.strategy.types import GridZone, GridType, GridBias, StrategySummary, ZoneInfo, ZoneStatus, SpotGridSummary, GridState, Spread

logger = logging.getLogger("src.strategy.spot_grid")

FEE_BUFFER = Spread("0.1") # 0.1% buffer
ACQUISITION_SPREAD = Spread("0.1") # 0.1% spread for off-grid acquisition
MAX_RETRIES = 5

class StrategyState(Enum):
    Initializing = auto()
    WaitingForTrigger = auto()
    AcquiringAssets = auto()
    Running = auto()

class SpotGridStrategy(Strategy):
    def __init__(self, config):
        self.config = config
        self.symbol = config.symbol
        self.grid_count = config.grid_count
        self.total_investment = config.total_investment
        
        # Spot grid specific: base/quote splitting
        try:
            self.base_asset, self.quote_asset = self.config.symbol.split("/")
        except ValueError:
            logger.error(f"Invalid symbol format: {self.config.symbol}. Expected BASE/QUOTE")
            self.base_asset = self.config.symbol
            self.quote_asset = "USDC"

        self.zones: List[GridZone] = []
        self.state = StrategyState.Initializing
        self.active_order_map: Dict[Cloid, GridZone] = {} # Cloid -> GridZone
        
        # Performance tracking
        self.realized_pnl = Decimal("0")
        self.total_fees = Decimal("0")
        self.unrealized_pnl = Decimal("0")
        
        self.grid_spacing_pct = common.calculate_grid_spacing_pct(
            self.config.grid_type,
            self.config.lower_price,
            self.config.upper_price,
            self.config.grid_count
        )
        
        # Position Tracking
        self.inventory_base = Decimal("0")
        self.inventory_quote = Decimal("0")
        self.avg_entry_price = Decimal("0")
        
        self.start_time = time.time()
        self.initial_entry_price: Optional[Decimal] = None
        self.trigger_reference_price: Optional[Decimal] = None
        
        self.acquisition_cloid: Optional[Cloid] = None
        self.acquisition_target_size: Decimal = Decimal("0")
        
        self.current_price = Decimal("0")

    def calculate_grid_plan(self, market_info: MarketInfo, reference_price: Decimal) -> tuple[List[GridZone], Decimal, Decimal]:
        """
        Calculates the grid structure and validates basic constraints.
        Returns: (zones, required_base, required_quote)
        """
        # Generate Levels
        prices = common.calculate_grid_prices(
            self.config.grid_type,
            self.config.lower_price,
            self.config.upper_price,
            self.config.grid_count
        )
        # Round prices
        prices = [market_info.round_price(p) for p in prices]

        # Deduct 0.05% buffer from total investment to ensure we cover fees/slippage
        adjusted_investment = self.total_investment * Decimal("0.9995")
        investment_per_zone_quote = adjusted_investment / Decimal(self.config.grid_count - 1)

        # Validation: Check minimum order size
        min_order_size = market_info.min_quote_amount
        if investment_per_zone_quote < min_order_size:
            msg = f"Investment per zone ({investment_per_zone_quote:.2f} {self.quote_asset}) is less than minimum order value ({min_order_size}). Increase total_investment or decrease grid_count."
            logger.error(f"[SPOT_GRID] {msg}")
            raise ValueError(msg)

        zones = []
        required_base = Decimal("0")
        required_quote = Decimal("0")

        initial_price = self.config.trigger_price if self.config.trigger_price else reference_price

        for i in range(self.config.grid_count - 1):
            zone_lower_price = prices[i]
            zone_upper_price = prices[i+1]
            # Calculate size based on quote investment per zone using zone_lower_price
            size = market_info.round_size(investment_per_zone_quote / zone_lower_price)
            
            # Determine initial state based on zone position relative to current market price:
            # 1. Zone is ABOVE price: We enter with Base asset -> Pending SELL at Upper Price.
            # 2. Zone is BELOW price: We enter with Quote asset -> Pending BUY at Lower Price.
            
            if zone_lower_price > initial_price:
                 pending_side = OrderSide.SELL
                 required_base += size
                 entry_price = initial_price
            else:
                 pending_side = OrderSide.BUY
                 required_quote += (size * zone_lower_price)
                 entry_price = Decimal("0.0")

            zones.append(GridZone(
                index=i,
                lower_price=zone_lower_price,
                upper_price=zone_upper_price,
                size=size,
                pending_side=pending_side,
                mode=None, # No ZoneMode for Spot
                entry_price=entry_price,
                roundtrip_count=0
            ))
            
        return zones, required_base, required_quote

    def _calculate_acquisition_price(self, side: OrderSide, current_price: Decimal, market_info: MarketInfo) -> Decimal:
        if self.config.trigger_price:
            return market_info.round_price(self.config.trigger_price)
            
        if side == OrderSide.BUY:
             # Find nearest level LOWER than market to buy at (Limit Buy below market)
             candidates = [z.lower_price for z in self.zones if z.lower_price < current_price]
             if candidates:
                 return market_info.round_price(max(candidates))
             elif self.zones:
                  # Fallback: Price is below grid. Return markdown of current price for BUY.
                  return market_info.round_price(ACQUISITION_SPREAD.markdown(current_price))
        else: # SELL
             # Find nearest level ABOVE market to sell at (Limit Sell above market)
             candidates = [z.upper_price for z in self.zones if z.upper_price > current_price]
             if candidates:
                 return market_info.round_price(min(candidates))
             elif self.zones:
                  # Fallback: Price is above grid. Return markup of current price for SELL.
                  return market_info.round_price(ACQUISITION_SPREAD.markup(current_price))
                  
        return current_price

    def initialize_zones(self, initial_price: Decimal, ctx: StrategyContext):
        self.current_price = initial_price
        market_info = ctx.market_info(self.config.symbol)
        if not market_info:
            raise ValueError(f"No market info for {self.config.symbol}")
        
        # Calculate Grid
        self.zones, required_base, required_quote = self.calculate_grid_plan(market_info, initial_price)

        # Seed inventory
        avail_base = ctx.get_spot_available(self.base_asset)
        avail_quote = ctx.get_spot_available(self.quote_asset)
        
        initial_price = self.config.trigger_price if self.config.trigger_price else initial_price

        # Upfront Total Investment Validation
        total_wallet_value = (avail_base * initial_price) + avail_quote
        if total_wallet_value < self.total_investment:
             msg = f"Insufficient Total Portfolio Value! Required: {self.total_investment:.2f}, Have approx: {total_wallet_value:.2f} (Base: {avail_base}, Quote: {avail_quote})"
             logger.error(f"[SPOT_GRID] {msg}")
             raise ValueError(msg)

        logger.info(f"[SPOT_GRID] Setup completed. Required: {required_base:.4f} {self.base_asset}, {required_quote:.2f} {self.quote_asset}")
        self.inventory_base = min(avail_base, required_base)
        self.inventory_quote = min(avail_quote, required_quote)
        
        if self.inventory_base > 0:
            # Mark to Market existing inventory
            self.avg_entry_price = initial_price
        
        # Check Assets & Rebalance
        self.check_initial_acquisition(ctx, market_info, required_base, required_quote, avail_base, avail_quote)



    def check_initial_acquisition(
        self, 
        ctx: StrategyContext, 
        market_info: MarketInfo, 
        total_base_required: Decimal, 
        total_quote_required: Decimal,
        available_base: Decimal,
        available_quote: Decimal
    ) -> None:
        """
        Pure logic to determine if we need to acquire assets.
        Balances are passed in to avoid side-effect fetching.
        """
        base_deficit = total_base_required - available_base
        quote_deficit = total_quote_required - available_quote

        # Use trigger_price if available, otherwise current_price
        initial_price = self.config.trigger_price if self.config.trigger_price else self.current_price

        if base_deficit > 0:
            # Case 1: Not enough base asset. Need to BUY base asset.
            acquisition_price = initial_price
            
            # Add 0.1% buffer for fees/rounding safety
            base_deficit = FEE_BUFFER.markup(base_deficit)
            
            base_deficit = max(base_deficit, market_info.min_base_amount)
            # Use ceiling round for extra safety? market_info.round_size usually rounds half-up.
            # But the buffer handles the slight edge case.
            base_deficit = market_info.round_size(base_deficit)

            acquisition_price = self._calculate_acquisition_price(OrderSide.BUY, self.current_price, market_info)

            estimated_cost = base_deficit * acquisition_price
            
            if available_quote < estimated_cost:
                msg = f"Insufficient Quote Balance for acquisition! Need ~{estimated_cost:.2f} {self.quote_asset}, Have {available_quote:.2f} {self.quote_asset}. Base Deficit: {base_deficit} {self.base_asset}"
                logger.error(f"[SPOT_GRID] {msg}")
                raise ValueError(msg)

            # Place Order
            # Cloid is generated and returned by context
            cloid = ctx.place_order(LimitOrderRequest(
                symbol=self.config.symbol,
                side=OrderSide.BUY,
                price=acquisition_price,
                sz=base_deficit,
                reduce_only=False
            ))
            
            self.state = StrategyState.AcquiringAssets
            self.acquisition_cloid = cloid
            self.acquisition_target_size = base_deficit

            logger.info(f"[ORDER_REQUEST] [SPOT_GRID] [ACQUISITION] cloid: {cloid.as_int()}, LIMIT BUY {base_deficit} {self.base_asset} @ {acquisition_price}")
            
            return

        elif quote_deficit > 0:
            # Case 2: Enough base asset, but NOT enough quote asset. Need to SELL base.
            acquisition_price = initial_price

            acquisition_price = self._calculate_acquisition_price(OrderSide.SELL, self.current_price, market_info)

            base_to_sell = quote_deficit / acquisition_price
            # Ensure min base amount and min notional
            base_to_sell = max(base_to_sell, market_info.min_base_amount)
            base_to_sell = market_info.round_size(base_to_sell)

            estimated_proceeds = base_to_sell * acquisition_price
            logger.info(f"[SPOT_GRID] Quote deficit detected: deficit={quote_deficit} {self.quote_asset}, need to sell ~{base_to_sell} {self.base_asset} (~${estimated_proceeds:.2f}) @ price {acquisition_price}")

            if available_base < base_to_sell:
                    msg = f"Insufficient Base Balance for rebalancing! Need to sell {base_to_sell} {self.base_asset}, Have {available_base} {self.base_asset}. Quote Deficit: {quote_deficit} {self.quote_asset}"
                    logger.error(f"[SPOT_GRID] {msg}")
                    raise ValueError(msg)

            logger.info(f"[ORDER_REQUEST] [SPOT_GRID] [ACQUISITION] LIMIT SELL {base_to_sell} {self.base_asset} @ {acquisition_price}")
            
            # Place Order
            # Let context generated cloid
            cloid = ctx.place_order(LimitOrderRequest(
                symbol=self.config.symbol,
                side=OrderSide.SELL,
                price=acquisition_price,
                sz=base_to_sell,
                reduce_only=False
            ))
            
            self.state = StrategyState.AcquiringAssets
            self.acquisition_cloid = cloid
            self.acquisition_target_size = base_to_sell
            return

        # No Deficit (or negligible)
        if self.inventory_base > 0:
             logger.info(f"[SPOT_GRID] Initial Position Size: {self.inventory_base} {self.base_asset}. Setting avg_entry to {self.avg_entry_price} (No Rebalancing Needed)")

        if self.config.trigger_price:
             # Passive Wait Mode
             logger.info("[SPOT_GRID] Assets sufficient. Entering WaitingForTrigger state.")
             self.trigger_reference_price = self.current_price
             self.state = StrategyState.WaitingForTrigger
        else:
             # No Trigger, Assets OK -> Running
             logger.info("[SPOT_GRID] Assets verified. Starting Grid.")
             self.initial_entry_price = self.current_price
             self.state = StrategyState.Running
             self.refresh_orders(ctx)

    def place_zone_order(self, zone: GridZone, ctx: StrategyContext):
        """Place an order for a zone based on its current state."""
        market_info = ctx.market_info(self.config.symbol)
        if not market_info:
            return
        
        if zone.order_id is not None:
            return  # Already has an order
        
        idx = zone.index
        side = zone.pending_side
        price = zone.lower_price if side.is_buy() else zone.upper_price
        size = zone.size
        
        # For SELL orders, use 99.95% of zone size to account for base asset fee deduction
        # (fees are deducted from the base asset when buying, so we have slightly less to sell)
        if side.is_sell():
            size = market_info.round_size(size * Decimal("0.9995"))
        
        # Order Request
        # Let context manage cloid
        cloid = ctx.place_order(LimitOrderRequest(
            symbol=self.config.symbol,
            side=side,
            price=price,
            sz=size,
            reduce_only=False
        ))
        
        zone.order_id = cloid
        self.active_order_map[cloid] = zone

        logger.info(f"[ORDER_REQUEST] [SPOT_GRID] GRID_ZONE_{idx} cloid: {cloid.as_int()} LIMIT {side} {size} {self.base_asset} @ {price}")

    def refresh_orders(self, ctx: StrategyContext):
        """Place orders for all zones that don't have one and haven't exceeded max retries."""
        for zone in self.zones:
            if zone.order_id is None:
                if zone.retry_count < MAX_RETRIES:
                     self.place_zone_order(zone, ctx)
                else:
                     # Optional: Log zombie state occasionally?
                     pass

    def on_tick(self, price: Decimal, ctx: StrategyContext):
        self.current_price = price
        if self.state == StrategyState.Initializing:
             self.initialize_zones(price, ctx)
        elif self.state == StrategyState.WaitingForTrigger:
             if self.config.trigger_price and self.trigger_reference_price:
                 if common.check_trigger(price, self.config.trigger_price, self.trigger_reference_price):
                      logger.info(f"[SPOT_GRID] [Triggered] at {price}")
                      self.initial_entry_price = price
                      self.state = StrategyState.Running
                      self.refresh_orders(ctx)
        elif self.state == StrategyState.Running:
             # Continuously ensure orders are active
             self.refresh_orders(ctx)

    def _handle_acquisition_fill(self, fill: OrderFill, ctx: StrategyContext) -> None:
         self.total_fees += fill.fee
         if fill.side.is_buy():
               self.inventory_base += fill.size
               self.inventory_quote -= (fill.size * fill.price)
         else:
               self.inventory_base = max(Decimal("0"), self.inventory_base - fill.size)
               self.inventory_quote += (fill.size * fill.price)
         
         if self.inventory_base > 0:
             # Reset avg entry to rebalancing price for the entire position as requested
             self.avg_entry_price = fill.price
         
         logger.info(f"[SPOT_GRID] [ACQUISITION] Complete. New Position Size: {self.inventory_base} {self.base_asset}. Acquisition Price: {fill.price}. Avg Entry: {self.avg_entry_price}")
         
         # Determine entry price for zones now that we have inventory
         for zone in self.zones:
              if zone.pending_side.is_sell():
                   zone.entry_price = fill.price
         
         self.state = StrategyState.Running
         self.initial_entry_price = fill.price
         self.refresh_orders(ctx)

    def on_order_filled(self, fill: OrderFill, ctx: StrategyContext):
        if fill.cloid:
            # Acquisition Fill
            if self.state == StrategyState.AcquiringAssets and fill.cloid == self.acquisition_cloid:
                 self._handle_acquisition_fill(fill, ctx)
                 return

            # Grid Fill
            if fill.cloid in self.active_order_map:
                 zone = self.active_order_map.pop(fill.cloid)
                 idx = zone.index
                 zone.order_id = None
                 self.total_fees += fill.fee
                 
                 market_info = ctx.market_info(self.symbol)
                 p_decimals = market_info.price_decimals if market_info else 4
                 
                 if zone.pending_side.is_buy():
                      # Buy Fill
                      logger.info(f"[ORDER_FILLED][SPOT_GRID] GRID_ZONE_{idx} cloid: {fill.cloid.as_int()} Filled BUY {fill.size} {self.base_asset} @ {fill.price:.{p_decimals}f}")
                      self.inventory_base += fill.size
                      self.inventory_quote -= (fill.size * fill.price)
                      # Update avg entry
                      if self.inventory_base > 0:
                           self.avg_entry_price = (self.avg_entry_price * (self.inventory_base - fill.size) + fill.price * fill.size) / self.inventory_base
                      
                      # Flip to SELL at upper price
                      zone.pending_side = OrderSide.SELL
                      zone.entry_price = fill.price
                      zone.retry_count = 0 # Reset retries on fill
                      self.place_zone_order(zone, ctx)
                 else:
                      # Sell Fill
                      pnl = (fill.price - zone.entry_price) * fill.size
                      logger.info(f"[ORDER_FILLED][SPOT_GRID] GRID_ZONE_{idx} cloid: {fill.cloid.as_int()} Filled SELL {fill.size} {self.base_asset} @ {fill.price:.{p_decimals}f}. PnL: {pnl:.4f}")
                      self.realized_pnl += pnl
                      self.inventory_base = max(Decimal("0"), self.inventory_base - fill.size)
                      self.inventory_quote += (fill.size * fill.price)
                      zone.roundtrip_count += 1
                      zone.retry_count = 0 # Reset retries on fill
                      
                      # Flip to BUY at lower price
                      zone.pending_side = OrderSide.BUY
                      zone.entry_price = Decimal("0.0")
                      self.place_zone_order(zone, ctx)

    def on_order_failed(self, failure: OrderFailure, ctx: StrategyContext):
        cloid = failure.cloid
        if cloid in self.active_order_map:
             zone = self.active_order_map.pop(cloid)
             idx = zone.index
             zone.order_id = None
             
             logger.warning(f"[ORDER_FAILED][SPOT_GRID] GRID_ZONE_{idx} cloid: {cloid.as_int()} "
                           f"reason: {failure.failure_reason}. Retry count: {zone.retry_count + 1}/{MAX_RETRIES}")
             
             zone.retry_count += 1

    def get_summary(self, ctx: StrategyContext) -> SpotGridSummary:
             
        # Approx unrealized pnl
        unrealized = (self.current_price - self.avg_entry_price) * self.inventory_base if (self.inventory_base > 0 and self.avg_entry_price > 0) else Decimal("0")
             
        return SpotGridSummary(
            symbol=self.symbol,
            state=self.state.name,
            uptime=common.format_uptime(timedelta(seconds=time.time() - self.start_time)),
            position_size=self.inventory_base,
            avg_entry_price=self.avg_entry_price,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            total_fees=self.total_fees,
            initial_entry_price=self.initial_entry_price,
            grid_count=len(self.zones),
            range_low=self.config.lower_price,
            range_high=self.config.upper_price,
            grid_spacing_pct=self.grid_spacing_pct,
            roundtrips=sum(z.roundtrip_count for z in self.zones),
            base_balance=self.inventory_base,
            quote_balance=self.inventory_quote
        )

    def get_grid_state(self, ctx: StrategyContext) -> GridState:
        return GridState(
             symbol=self.symbol,
             strategy_type="spot_grid",
             grid_bias=None,
             zones=[
                 ZoneInfo(
                    index=z.index,
                    lower_price=z.lower_price,
                    upper_price=z.upper_price,
                    size=z.size,
                    pending_side=str(z.pending_side),
                    has_order=z.order_id is not None,
                    is_reduce_only=False,
                    entry_price=z.entry_price,
                    roundtrip_count=z.roundtrip_count
                 ) for z in self.zones
             ]
        )
