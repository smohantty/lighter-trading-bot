import logging
import math
import time
from typing import List, Optional, Dict
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum, auto

from src.strategy.base import Strategy
from src.strategy import common
from src.engine.context import StrategyContext, MarketInfo
from src.model import OrderRequest, LimitOrderRequest, OrderSide, OrderFill, Cloid, PendingOrder
from src.strategy.types import GridZone, GridType, GridBias, StrategySummary, PerpGridSummary, ZoneInfo, ZoneStatus, SpotGridSummary, GridState

logger = logging.getLogger("src.strategy.spot_grid")

class StrategyState(Enum):
    Initializing = auto()
    WaitingForTrigger = auto()
    AcquiringAssets = auto()
    Running = auto()

class SpotGridStrategy(Strategy):
    """
    Spot Grid Trading Strategy. (Aligned with Rust Implementation)
    
    Places a grid of limit orders.
    - Buy orders below current price
    - Sell orders above current price
    
    For Spot:
    - Requires QUOTE asset (e.g. USDC) to place BUY orders.
    - Requires BASE asset (e.g. WETH) to place SELL orders.
    - No leverage, no liquidation logic.
    - No ZoneMode (Long/Short). Bias is implied by price relation.
    """
    def __init__(self, config):
        self.config = config
        self.symbol = config.symbol
        self.grid_count = config.grid_count
        self.total_investment = config.total_investment
        
        # Spot grid specific: base/quote splitting
        self.zones: List[GridZone] = []
        self.state = StrategyState.Initializing
        
        self.active_order_map: Dict[Cloid, int] = {} # Cloid -> Zone Index
        
        # Performance tracking
        self.realized_pnl = 0.0
        self.total_fees = 0.0
        self.unrealized_pnl = 0.0
        
        # Position Tracking
        self.position_size = 0.0
        self.avg_entry_price = 0.0
        
        self.start_time = time.time()
        self.initial_entry_price: Optional[float] = None
        self.trigger_reference_price: Optional[float] = None
        
        self.acquisition_cloid: Optional[Cloid] = None
        self.acquisition_target_size: float = 0.0
        
        self.current_price = 0.0

    def initialize_zones(self, price: float, ctx: StrategyContext):
        # 1. Get initial data
        market_info = ctx.market_info(self.config.symbol)
        if not market_info:
            raise ValueError(f"No market info for {self.config.symbol}")
        
        last_price = price
        
        # Parse symbol to get Base/Quote assets
        try:
            base_asset, quote_asset = self.config.symbol.split("/")
        except ValueError:
             logger.error(f"Invalid symbol format: {self.config.symbol}. Expected BASE/QUOTE")
             base_asset = self.config.symbol
             quote_asset = "USDC"

        # Generate Levels
        prices = common.calculate_grid_prices(
            self.config.grid_type,
            self.config.lower_price,
            self.config.upper_price,
            self.config.grid_count
        )
        # Round prices
        prices = [market_info.round_price(p) for p in prices]

        num_zones = self.config.grid_count - 1
        investment_per_zone_quote = self.config.total_investment / float(num_zones)

        # Validation: Check minimum order size
        min_order_size = market_info.min_quote_amount
        if investment_per_zone_quote < min_order_size:
            msg = f"Investment per zone ({investment_per_zone_quote:.2f} {quote_asset}) is less than minimum order value ({min_order_size}). Increase total_investment or decrease grid_count."
            logger.error(f"[SPOT_GRID] {msg}")
            raise ValueError(msg)

        # Seed inventory
        avail_base = ctx.get_spot_available(base_asset)
        avail_quote = ctx.get_spot_available(quote_asset)

        
        initial_price = self.config.trigger_price if self.config.trigger_price else last_price
        


        # Upfront Total Investment Validation
        total_wallet_value = (avail_base * initial_price) + avail_quote
        if total_wallet_value < self.config.total_investment:
             msg = f"Insufficient Total Portfolio Value! Required: {self.config.total_investment:.2f}, Have approx: {total_wallet_value:.2f} (Base: {avail_base}, Quote: {avail_quote})"
             logger.error(f"[SPOT_GRID] {msg}")
             raise ValueError(msg)

        self.zones = []
        required_base = 0.0
        required_quote = 0.0

        for i in range(num_zones):
            lower = prices[i]
            upper = prices[i+1]
            mid_price = (lower + upper) / 2.0
            
            # Calculate size based on quote investment per zone using MID PRICE (as per Rust logic roughly, though Rust uses lower for size calc in generate_grid_levels)
            # Rust: let raw_size = quote_per_zone / lower;
            # Let's align with Rust:
            raw_size = investment_per_zone_quote / lower
            size = market_info.round_size(raw_size)
            
            # Zone ABOVE (or AT) price line (lower > initial): Acquired base -> Sell at upper
            # Zone BELOW price line: Have quote -> Waiting to buy at lower
            
            if lower > initial_price:
                 pending_side = OrderSide.SELL
                 required_base += size
                 entry_price = initial_price
            else:
                 pending_side = OrderSide.BUY
                 required_quote += (size * lower)
                 entry_price = 0.0

            self.zones.append(GridZone(
                index=i,
                lower_price=lower,
                upper_price=upper,
                size=size,
                pending_side=pending_side,
                mode=None, # No ZoneMode for Spot
                entry_price=entry_price,
                roundtrip_count=0
            ))

        logger.info(f"[SPOT_GRID] Setup completed. Required: {required_base:.4f} {base_asset}, {required_quote:.2f} {quote_asset}")
        self.position_size = min(avail_base, required_base)
        
        if self.position_size > 0.0:
            # Mark to Market existing inventory
            self.avg_entry_price = initial_price
            logger.info(f"[SPOT_GRID] Initial Position Size: {self.position_size} {base_asset}. Setting avg_entry to {self.avg_entry_price}")
        
        # Check Assets & Rebalance
        base_deficit = required_base - avail_base
        quote_deficit = required_quote - avail_quote
        
        self.check_initial_acquisition(ctx, market_info, required_base, required_quote)

    def check_initial_acquisition(
        self, 
        ctx: StrategyContext, 
        market_info: MarketInfo, 
        total_base_required: float, 
        total_quote_required: float
    ) -> None:
        available_base = ctx.get_spot_available(self.symbol.split('/')[0] if '/' in self.symbol else self.symbol) # Approximate asset extraction if needed, but easier to just use what we had
        # Re-fetch balances to be safe or pass them in? Rust passes ctx.
        # In initialize_zones we had local vars avail_base/avail_quote.
        # Let's re-fetch from ctx to be clean, or rely on what's in ctx.
        # Note: initialize_zones used local vars but didn't update ctx. 
        # Actually context has the balances.
        
        # Parse symbol again or store assets in __init__? 
        # initialize_zones did parsing. Let's do it robustly or use the ones from config if available.
        try:
            base_asset, quote_asset = self.config.symbol.split("/")
        except ValueError:
            base_asset = self.config.symbol
            quote_asset = "USDC"
            
        available_base = ctx.get_spot_available(base_asset)
        available_quote = ctx.get_spot_available(quote_asset)

        base_deficit = total_base_required - available_base
        quote_deficit = total_quote_required - available_quote

        # Use trigger_price if available, otherwise last_price (or current_price)
        # In initialize_zones we had 'initial_price' passed in or calculated.
        # Rust uses self.config.trigger_price.unwrap_or(market_info.last_price)
        # We don't have market_info.last_price directly attached to market_info usually in this bot (it's in Strategy.current_price or passed in).
        # But we can use self.current_price if initialized, or pass it.
        # However, initialize_zones has 'price' argument. 
        # Let's use self.current_price which is updated in on_tick before initialize_zones is called.
        initial_price = self.config.trigger_price if self.config.trigger_price else self.current_price

        if base_deficit > 0.0:
            # Case 1: Not enough base asset. Need to BUY base asset.
            acquisition_price = initial_price

            if self.config.trigger_price:
                acquisition_price = market_info.round_price(self.config.trigger_price)
            else:
                # Find nearest level LOWER than market to buy at?
                # Rust logic: 
                # let nearest_level = self.zones.iter().filter(|z| z.lower_price < market_info.last_price).map(|z| z.lower_price).fold(0.0, f64::max);
                nearest_level = 0.0
                candidates = [z.lower_price for z in self.zones if z.lower_price < self.current_price]
                if candidates:
                    nearest_level = max(candidates)
                
                if nearest_level > 0.0:
                    acquisition_price = market_info.round_price(nearest_level)
                elif self.zones:
                     # Fallback to first zone lower price
                    acquisition_price = market_info.round_price(self.zones[0].lower_price)

            rounded_deficit = market_info.clamp_to_min_notional(base_deficit, acquisition_price)

            if rounded_deficit > 0.0:
                estimated_cost = rounded_deficit * acquisition_price
                
                if available_quote < estimated_cost:
                    msg = f"Insufficient Quote Balance for acquisition! Need ~{estimated_cost:.2f} {quote_asset}, Have {available_quote:.2f} {quote_asset}. Base Deficit: {rounded_deficit} {base_asset}"
                    logger.error(f"[SPOT_GRID] {msg}")
                    raise ValueError(msg)

                logger.info(f"[ORDER_REQUEST] [SPOT_GRID] REBALANCING: LIMIT BUY {rounded_deficit} {base_asset} @ {acquisition_price}")
                cloid = ctx.generate_cloid()
                self.state = StrategyState.AcquiringAssets
                self.acquisition_cloid = cloid
                self.acquisition_target_size = rounded_deficit

                ctx.place_order(LimitOrderRequest(
                    symbol=self.config.symbol,
                    side=OrderSide.BUY,
                    price=acquisition_price,
                    sz=rounded_deficit,
                    reduce_only=False,
                    cloid=cloid
                ))
                return

        elif quote_deficit > 0.0:
            # Case 2: Enough base asset, but NOT enough quote asset. Need to SELL base.
            acquisition_price = initial_price

            if self.config.trigger_price:
                 acquisition_price = market_info.round_price(self.config.trigger_price)
            else:
                 # Find nearest level ABOVE market to sell at
                 # Rust: self.zones.iter().filter(|z| z.upper_price > market_info.last_price).map(|z| z.upper_price).fold(f64::INFINITY, f64::min);
                 nearest_sell_level = float('inf')
                 candidates = [z.upper_price for z in self.zones if z.upper_price > self.current_price]
                 if candidates:
                     nearest_sell_level = min(candidates)
                
                 if nearest_sell_level != float('inf'):
                     acquisition_price = market_info.round_price(nearest_sell_level)
                 elif self.zones:
                     # Fallback to last zone upper price
                     acquisition_price = market_info.round_price(self.zones[-1].upper_price)

            base_to_sell = quote_deficit / acquisition_price
            rounded_sell_sz = market_info.clamp_to_min_notional(base_to_sell, acquisition_price)

            if rounded_sell_sz > 0.0:
                 estimated_proceeds = rounded_sell_sz * acquisition_price
                 logger.info(f"[SPOT_GRID] Quote deficit detected: deficit={quote_deficit} {quote_asset}, need to sell ~{rounded_sell_sz} {base_asset} (~${estimated_proceeds:.2f}) @ price {acquisition_price}")

                 if available_base < rounded_sell_sz:
                      msg = f"Insufficient Base Balance for rebalancing! Need to sell {rounded_sell_sz} {base_asset}, Have {available_base} {base_asset}. Quote Deficit: {quote_deficit} {quote_asset}"
                      logger.error(f"[SPOT_GRID] {msg}")
                      raise ValueError(msg)

                 logger.info(f"[ORDER_REQUEST] [SPOT_GRID] REBALANCING: LIMIT SELL {rounded_sell_sz} {base_asset} @ {acquisition_price}")
                 cloid = ctx.generate_cloid()
                 self.state = StrategyState.AcquiringAssets
                 self.acquisition_cloid = cloid
                 self.acquisition_target_size = rounded_sell_sz

                 ctx.place_order(LimitOrderRequest(
                    symbol=self.config.symbol,
                    side=OrderSide.SELL,
                    price=acquisition_price,
                    sz=rounded_sell_sz,
                    reduce_only=False,
                    cloid=cloid
                 ))
                 return

        # No Deficit (or negligible)
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


    def refresh_orders(self, ctx: StrategyContext):
        market_info = ctx.market_info(self.config.symbol)
        if not market_info:
            return

        for idx, zone in enumerate(self.zones):
            if zone.order_id is None:
                side = zone.pending_side
                price = zone.lower_price if side.is_buy() else zone.upper_price
                size = zone.size
                
                cloid = ctx.generate_cloid()
                zone.order_id = cloid
                self.active_order_map[cloid] = idx

                logger.info(f"[ORDER_REQUEST] [SPOT_GRID] GRID_LVL_{idx}: LIMIT {side} {size} {self.config.symbol} @ {price}")
                ctx.place_order(LimitOrderRequest(
                    symbol=self.config.symbol,
                    side=side,
                    price=market_info.round_price(price),
                    sz=market_info.round_size(size),
                    reduce_only=False,
                    cloid=cloid
                ))

    def on_tick(self, price: float, ctx: StrategyContext):
        self.current_price = price
        if self.state == StrategyState.Initializing:
             self.initialize_zones(price, ctx)
        elif self.state == StrategyState.WaitingForTrigger:
             if self.config.trigger_price and self.trigger_reference_price:
                 trigger = self.config.trigger_price
                 start = self.trigger_reference_price
                 if common.check_trigger(price, trigger, start):
                      logger.info(f"[SPOT_GRID] Triggered at {price}")
                      self.initial_entry_price = price
                      self.state = StrategyState.Running
                      self.refresh_orders(ctx)
        elif self.state == StrategyState.Running:
             if not self.active_order_map and len(self.zones) > 0:
                  self.refresh_orders(ctx)

    def on_order_filled(self, fill: OrderFill, ctx: StrategyContext):
        if fill.cloid:
            # Acquisition Fill
            if self.state == StrategyState.AcquiringAssets and fill.cloid == self.acquisition_cloid:
                 logger.info(f"[SPOT_GRID] Acquisition filled @ {fill.price}")
                 self.total_fees += fill.fee
                 if fill.side.is_buy():
                      self.position_size += fill.size
                      if self.position_size > 0.0:
                           self.avg_entry_price = (self.avg_entry_price * (self.position_size - fill.size) + fill.price * fill.size) / self.position_size
                 else:
                      self.position_size = max(0.0, self.position_size - fill.size)
                 
                 # Determine entry price for zones now that we have inventory
                 for zone in self.zones:
                      if zone.pending_side.is_sell():
                           zone.entry_price = fill.price
                 
                 self.state = StrategyState.Running
                 self.initial_entry_price = fill.price
                 self.refresh_orders(ctx)
                 return

            # Grid Fill
            if fill.cloid in self.active_order_map:
                 idx = self.active_order_map.pop(fill.cloid)
                 zone = self.zones[idx]
                 zone.order_id = None
                 self.total_fees += fill.fee
                 
                 if zone.pending_side.is_buy():
                      # Buy Fill
                      logger.info(f"[SPOT_GRID] Zone {idx} BUY Filled @ {fill.price}")
                      self.position_size += fill.size
                      # Update avg entry
                      if self.position_size > 0.0:
                           self.avg_entry_price = (self.avg_entry_price * (self.position_size - fill.size) + fill.price * fill.size) / self.position_size
                      
                      zone.pending_side = OrderSide.SELL
                      zone.entry_price = fill.price
                      # Next order: Sell at Upper
                      self.place_counter_order(idx, zone.upper_price, OrderSide.SELL, ctx)
                 else:
                      # Sell Fill
                      pnl = (fill.price - zone.entry_price) * fill.size
                      logger.info(f"[SPOT_GRID] Zone {idx} SELL Filled @ {fill.price}. PnL: {pnl:.4f}")
                      self.realized_pnl += pnl
                      self.position_size = max(0.0, self.position_size - fill.size)
                      zone.roundtrip_count += 1
                      
                      zone.pending_side = OrderSide.BUY
                      zone.entry_price = 0.0
                      # Next order: Buy at Lower
                      self.place_counter_order(idx, zone.lower_price, OrderSide.BUY, ctx)

    def place_counter_order(self, idx: int, price: float, side: OrderSide, ctx: StrategyContext):
        zone = self.zones[idx]
        cloid = ctx.generate_cloid()
        zone.order_id = cloid
        self.active_order_map[cloid] = idx
        
        logger.info(f"[ORDER_REQUEST] [SPOT_GRID] COUNTER: LIMIT {side} {zone.size} @ {price}")
        ctx.place_order(LimitOrderRequest(
            symbol=self.config.symbol,
            side=side,
            price=price,
            sz=zone.size,
            reduce_only=False,
            cloid=cloid,
        ))

    def on_order_failed(self, cloid: Cloid, ctx: StrategyContext):
        if cloid in self.active_order_map:
             idx = self.active_order_map.pop(cloid)
             self.zones[idx].order_id = None
             logger.warning(f"[SPOT_GRID] Order failed for Zone {idx}")

    def get_summary(self, ctx: StrategyContext) -> SpotGridSummary:
        current_price = self.current_price
             
        # Approx unrealized pnl
        unrealized = 0.0
        if self.position_size > 0.0 and self.avg_entry_price > 0.0:
             unrealized = (current_price - self.avg_entry_price) * self.position_size
             
        grid_spacing_pct = common.calculate_grid_spacing_pct(
            self.config.grid_type,
            self.config.lower_price,
            self.config.upper_price,
            self.config.grid_count
        )
             
        return SpotGridSummary(
            symbol=self.symbol,
            price=current_price,
            state=self.state.name,
            uptime=common.format_uptime(timedelta(seconds=time.time() - self.start_time)),
            position_size=self.position_size,
            avg_entry_price=self.avg_entry_price,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            total_fees=self.total_fees,
            initial_entry_price=self.initial_entry_price,
            grid_count=len(self.zones),
            range_low=self.config.lower_price,
            range_high=self.config.upper_price,
            grid_spacing_pct=grid_spacing_pct,
            roundtrips=sum(z.roundtrip_count for z in self.zones),
            base_balance=ctx.get_spot_available(self.symbol.split('/')[0] if '/' in self.symbol else self.symbol), # Approx
            quote_balance=ctx.get_spot_available("USDC")
        )

    def get_grid_state(self, ctx: StrategyContext) -> GridState:
        current_price = self.current_price
             
        zones_info = [
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
        
        return GridState(
             symbol=self.symbol,
             strategy_type="spot_grid",
             current_price=current_price,
             grid_bias=None,
             zones=zones_info
        )
