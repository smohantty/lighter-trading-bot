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
        self.position_size = avail_base
        
        initial_price = self.config.trigger_price if self.config.trigger_price else last_price
        
        if self.position_size > 0.0:
            # Mark to Market existing inventory
            self.avg_entry_price = initial_price
            logger.info(f"[SPOT_GRID] Initial inventory detected: {self.position_size} {base_asset}. Setting avg_entry to {self.avg_entry_price}")

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
        
        # Check Assets & Rebalance
        base_deficit = required_base - avail_base
        quote_deficit = required_quote - avail_quote
        
        if base_deficit > 0.0:
            # Case 1: Need Base. Buy it.
            acquisition_price = initial_price
            estimated_cost = base_deficit * acquisition_price
            
            if avail_quote < estimated_cost:
                 msg = f"Insufficient Quote to buy Base deficit. Need {estimated_cost:.2f}, Have {avail_quote:.2f}"
                 logger.error(f"[SPOT_GRID] {msg}")
                 raise ValueError(msg)
            
            logger.info(f"[SPOT_GRID] Acquiring {base_deficit} {base_asset}...")
            cloid = ctx.generate_cloid()
            self.state = StrategyState.AcquiringAssets
            self.acquisition_cloid = cloid
            self.acquisition_target_size = base_deficit
            
            activation_price = market_info.round_price(acquisition_price) 
            
            ctx.place_order(LimitOrderRequest(
                symbol=self.config.symbol,
                side=OrderSide.BUY,
                price=activation_price,
                sz=market_info.round_size(base_deficit),
                reduce_only=False,
                cloid=cloid
            ))
            return 
            
        elif quote_deficit > 0.0:
             # Case 2: Need Quote. Sell Base.
             base_to_sell = quote_deficit / initial_price
             if avail_base < base_to_sell:
                  msg = f"Insufficient Base to sell for Quote deficit. Need to sell {base_to_sell:.4f}, Have {avail_base:.4f}"
                  logger.error(f"[SPOT_GRID] {msg}")
                  raise ValueError(msg)
             
             logger.info(f"[SPOT_GRID] Selling {base_to_sell} {base_asset} for Quote...")
             cloid = ctx.generate_cloid()
             self.state = StrategyState.AcquiringAssets
             self.acquisition_cloid = cloid
             self.acquisition_target_size = base_to_sell
             
             activation_price = market_info.round_price(initial_price)
             
             ctx.place_order(LimitOrderRequest(
                symbol=self.config.symbol,
                side=OrderSide.SELL,
                price=activation_price,
                sz=market_info.round_size(base_to_sell),
                reduce_only=False,
                cloid=cloid
             ))
             return

        # No Deficit
        if self.config.trigger_price:
             logger.info("[SPOT_GRID] Waiting for trigger...")
             self.trigger_reference_price = last_price
             self.state = StrategyState.WaitingForTrigger
        else:
             logger.info("[SPOT_GRID] Starting Grid.")
             self.initial_entry_price = last_price
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
