import logging
import time
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum, auto

from src.strategy.base import Strategy
from src.engine.context import StrategyContext
from src.model import Cloid, OrderFill, OrderSide, OrderRequest, LimitOrderRequest
from src.config import SpotGridConfig
from src.strategy.types import StrategySummary, SpotGridSummary, GridState, ZoneInfo
from src.strategy import common

logger = logging.getLogger(__name__)

class StrategyState(Enum):
    Initializing = auto()
    WaitingForTrigger = auto()
    # AcquiringAssets state holds the Cloid of the rebalancing order
    # In Python, we can store simple state enum and track cloid separately
    AcquiringAssets = auto()
    Running = auto()

@dataclass
class GridZone:
    index: int
    lower_price: float
    upper_price: float
    size: float
    pending_side: OrderSide
    entry_price: float
    order_id: Optional[Cloid] = None
    roundtrip_count: int = 0

class SpotGridStrategy(Strategy):
    def __init__(self, config: SpotGridConfig):
        self.config = config
        
        parts = config.symbol.split('/')
        if len(parts) == 2:
            self.base_asset = parts[0]
            self.quote_asset = parts[1]
        else:
            logger.warning(f"Invalid symbol format: {config.symbol}. Assuming base/USDC.")
            self.base_asset = config.symbol
            self.quote_asset = "USDC"

        self.zones: List[GridZone] = []
        self.active_orders: Dict[Cloid, int] = {}
        self.trade_count = 0
        self.state = StrategyState.Initializing
        self.initial_entry_price: Optional[float] = None
        self.trigger_reference_price: Optional[float] = None
        self.start_time = time.monotonic()
        
        # Acquisition state
        self.acquisition_cloid: Optional[Cloid] = None

        # Performance Metrics
        self.realized_pnl = 0.0
        self.total_fees = 0.0

        # Position Tracking
        self.inventory = 0.0
        self.avg_entry_price = 0.0

    def initialize_zones(self, price: float, ctx: StrategyContext):
        self.config.validate()

        market_info = ctx.market_info(self.config.symbol)
        if not market_info:
            raise ValueError(f"No market info for {self.config.symbol}")
        
        # 1. Generate Zones
        total_base_required, total_quote_required = self.generate_grid_levels(price, market_info)

        logger.info(f"[SPOT_GRID] INITIALIZATION: Asset Required: {self.base_asset} ({total_base_required}), {self.quote_asset} ({total_quote_required})")

        # Seed inventory from current available
        available_base = ctx.get_spot_available(self.base_asset)
        available_quote = ctx.get_spot_available(self.quote_asset)
        self.inventory = available_base

        # Validation
        initial_price = self.config.trigger_price if self.config.trigger_price else price
        total_wallet_value = (available_base * initial_price) + available_quote
        
        if total_wallet_value < self.config.total_investment:
            msg = f"Insufficient Total Portfolio Value! Required: {self.config.total_investment:.2f} {self.quote_asset}, Have approx: {total_wallet_value:.2f}"
            logger.error(f"[SPOT_GRID] {msg}")
            raise ValueError(msg)

        # 2. Check Assets & Rebalance
        self.check_initial_acquisition(price, ctx, market_info, total_base_required, total_quote_required)

    def generate_grid_levels(self, price: float, market_info) -> Tuple[float, float]:
        prices = common.calculate_grid_prices(
            self.config.grid_type,
            self.config.lower_price,
            self.config.upper_price,
            self.config.grid_count
        )
        prices = [market_info.round_price(p) for p in prices]

        num_zones = self.config.grid_count - 1
        quote_per_zone = self.config.total_investment / float(num_zones)

        # Validation: Check minimum order size
        min_order_size = market_info.min_quote_amount
        if quote_per_zone < min_order_size:
            msg = f"Quote per zone ({quote_per_zone:.2f}) is less than minimum order value ({min_order_size}). Increase total_investment or decrease grid_count."
            logger.error(f"[SPOT_GRID] {msg}")
            raise ValueError(msg)

        initial_price = self.config.trigger_price if self.config.trigger_price else price
        
        self.zones.clear()
        total_base_required = 0.0
        total_quote_required = 0.0

        for i in range(num_zones):
            lower = prices[i]
            upper = prices[i+1]

            raw_size = quote_per_zone / lower
            size = market_info.round_size(raw_size)

            # Logic:
            # Zone ABOVE price line (lower > price): We acquired base -> Sell at upper
            # Zone BELOW price line: We have quote -> Buy at lower
            if lower > initial_price:
                pending_side = OrderSide.SELL
            else:
                pending_side = OrderSide.BUY

            if pending_side == OrderSide.SELL:
                total_base_required += size
            else:
                total_quote_required += size * lower

            entry_price = initial_price if pending_side == OrderSide.SELL else 0.0

            self.zones.append(GridZone(
                index=i,
                lower_price=lower,
                upper_price=upper,
                size=size,
                pending_side=pending_side,
                entry_price=entry_price,
                roundtrip_count=0
            ))

        return market_info.round_size(total_base_required), total_quote_required

    def check_initial_acquisition(self, price: float, ctx: StrategyContext, market_info, total_base_required: float, total_quote_required: float):
        available_base = ctx.get_spot_available(self.base_asset)
        available_quote = ctx.get_spot_available(self.quote_asset)

        base_deficit = total_base_required - available_base
        quote_deficit = total_quote_required - available_quote
        initial_price = self.config.trigger_price if self.config.trigger_price else price

        if base_deficit > 0.0:
            acquisition_price = initial_price
            if self.config.trigger_price:
                acquisition_price = market_info.round_price(self.config.trigger_price)
            else:
                # Find nearest level < market to buy, or just use market?
                # Rust extracts max(lower_price) < market
                candidates = [z.lower_price for z in self.zones if z.lower_price < price]
                if candidates:
                    acquisition_price = market_info.round_price(max(candidates))
                elif self.zones:
                    acquisition_price = market_info.round_price(self.zones[0].lower_price)

            rounded_deficit = market_info.clamp_to_min_size(base_deficit)

            if rounded_deficit > 0.0:
                estimated_cost = rounded_deficit * acquisition_price
                if available_quote < estimated_cost:
                    msg = f"Insufficient Quote Balance for acquisition! Need ~{estimated_cost:.2f}, Have {available_quote:.2f}."
                    logger.error(f"[SPOT_GRID] {msg}")
                    raise ValueError(msg)

                logger.info(f"[ORDER_REQUEST] [SPOT_GRID] REBALANCING: LIMIT BUY {rounded_deficit} {self.base_asset} @ {acquisition_price}")
                cloid = ctx.generate_cloid()
                self.state = StrategyState.AcquiringAssets
                self.acquisition_cloid = cloid

                ctx.place_order(LimitOrderRequest(
                    symbol=self.config.symbol,
                    side=OrderSide.BUY,
                    price=acquisition_price,
                    sz=rounded_deficit,
                    reduce_only=False, # Spot has no reduce_only
                    cloid=cloid
                ))
                return

        elif quote_deficit > 0.0:
             # Need to Sell Base
             acquisition_price = initial_price
             if self.config.trigger_price:
                 acquisition_price = market_info.round_price(self.config.trigger_price)
             else:
                 candidates = [z.upper_price for z in self.zones if z.upper_price > price]
                 if candidates:
                     acquisition_price = market_info.round_price(min(candidates))
                 elif self.zones:
                     acquisition_price = market_info.round_price(self.zones[-1].upper_price)
            
             base_to_sell = quote_deficit / acquisition_price
             rounded_sell_sz = market_info.clamp_to_min_size(base_to_sell)

             if rounded_sell_sz > 0.0:
                 if available_base < rounded_sell_sz:
                     msg = f"Insufficient Base Balance for rebalancing! Need to sell {rounded_sell_sz}, Have {available_base}."
                     logger.error(f"[SPOT_GRID] {msg}")
                     raise ValueError(msg)
                 
                 logger.info(f"[ORDER_REQUEST] [SPOT_GRID] REBALANCING: LIMIT SELL {rounded_sell_sz} {self.base_asset} @ {acquisition_price}")
                 cloid = ctx.generate_cloid()
                 self.state = StrategyState.AcquiringAssets
                 self.acquisition_cloid = cloid
                 
                 ctx.place_order(LimitOrderRequest(
                    symbol=self.config.symbol,
                    side=OrderSide.SELL,
                    price=acquisition_price,
                    sz=rounded_sell_sz,
                    reduce_only=False,
                    cloid=cloid
                 ))
                 return

        # No Deficit
        if self.config.trigger_price:
            logger.info("[SPOT_GRID] Assets sufficient. Entering WaitingForTrigger state.")
            self.trigger_reference_price = price
            self.state = StrategyState.WaitingForTrigger
        else:
            logger.info("[SPOT_GRID] Assets verified. Starting Grid.")
            self.initial_entry_price = price
            self.state = StrategyState.Running
            try:
                self.refresh_orders(ctx)
            except Exception as e:
                logger.warning(f"Failed to refresh orders: {e}")

    def refresh_orders(self, ctx: StrategyContext):
        market_info = ctx.market_info(self.config.symbol)
        if not market_info: return

        for idx, zone in enumerate(self.zones):
            if zone.order_id is None:
                side = zone.pending_side
                price = zone.lower_price if side == OrderSide.BUY else zone.upper_price
                
                cloid = ctx.generate_cloid()
                zone.order_id = cloid
                self.active_orders[cloid] = idx
                
                logger.info(f"[ORDER_REQUEST] [SPOT_GRID] GRID_LVL_{idx}: LIMIT {side} {zone.size} {self.config.symbol} @ {price}")
                
                ctx.place_order(LimitOrderRequest(
                    symbol=self.config.symbol,
                    side=side,
                    price=market_info.round_price(price),
                    sz=market_info.round_size(zone.size),
                    reduce_only=False,
                    cloid=cloid
                ))

    def handle_acquisition_fill(self, fill: OrderFill, ctx: StrategyContext):
        logger.info(f"[SPOT_GRID] Rebalancing fill: {fill.side} {fill.size} @ {fill.price}")
        self.total_fees += fill.fee
        
        if fill.side == OrderSide.BUY:
            self.inventory += fill.size
            if self.inventory > 0:
                 # Update Avg Entry logic if needed, but simplified
                 pass
        else:
            self.inventory = max(0.0, self.inventory - fill.size)
            
        # If we bought base, we now have inventory for SELL zones using this price
        if fill.side == OrderSide.BUY:
            for zone in self.zones:
                if zone.pending_side == OrderSide.SELL:
                    zone.entry_price = fill.price
                    
        self.initial_entry_price = fill.price
        self.state = StrategyState.Running
        self.refresh_orders(ctx)

    def on_tick(self, price: float, ctx: StrategyContext):
        if self.state == StrategyState.Initializing:
            self.initialize_zones(price, ctx)
        
        elif self.state == StrategyState.WaitingForTrigger:
            if self.config.trigger_price:
                 start = self.trigger_reference_price
                 if start and common.check_trigger(price, self.config.trigger_price, start):
                     logger.info(f"[SPOT_GRID] Triggered at {price}")
                     self.initial_entry_price = price
                     self.state = StrategyState.Running
                     self.refresh_orders(ctx)

        elif self.state == StrategyState.Running:
            if not self.active_orders and len(self.zones) > 0:
                self.refresh_orders(ctx)

    def on_order_filled(self, fill: OrderFill, ctx: StrategyContext):
        if fill.cloid:
             if self.state == StrategyState.AcquiringAssets and fill.cloid == self.acquisition_cloid:
                 self.handle_acquisition_fill(fill, ctx)
                 return
             
             zone_idx = self.active_orders.pop(fill.cloid, None)
             if zone_idx is not None:
                 self.trade_count += 1
                 self.total_fees += fill.fee
                 
                 zone = self.zones[zone_idx]
                 zone.order_id = None
                 
                 # Logic for BUY fill
                 if fill.side == OrderSide.BUY:
                     next_price = zone.upper_price
                     logger.info(f"[SPOT_GRID] Zone {zone_idx} | BUY Filled @ {fill.price} | Next: SELL @ {next_price}")
                     self.inventory += fill.size
                     
                     zone.pending_side = OrderSide.SELL
                     zone.entry_price = fill.price
                     
                     self.place_counter_order(zone_idx, next_price, OrderSide.SELL, ctx)
                 
                 # Logic for SELL fill
                 elif fill.side == OrderSide.SELL:
                     pnl = (fill.price - zone.entry_price) * fill.size
                     self.realized_pnl += pnl
                     next_price = zone.lower_price
                     zone.roundtrip_count += 1
                     
                     logger.info(f"[SPOT_GRID] Zone {zone_idx} | SELL Filled @ {fill.price} | PnL: {pnl:.4f} | Next: BUY @ {next_price}")
                     self.inventory = max(0.0, self.inventory - fill.size)
                     
                     zone.pending_side = OrderSide.BUY
                     zone.entry_price = 0.0
                     
                     self.place_counter_order(zone_idx, next_price, OrderSide.BUY, ctx)

    def place_counter_order(self, zone_idx: int, price: float, side: OrderSide, ctx: StrategyContext):
        zone = self.zones[zone_idx]
        next_cloid = ctx.generate_cloid()
        
        market_info = ctx.market_info(self.config.symbol)
        if not market_info: return
        
        rounded_price = market_info.round_price(price)
        rounded_size = market_info.round_size(zone.size)
        
        self.active_orders[next_cloid] = zone_idx
        zone.order_id = next_cloid
        
        logger.info(f"[ORDER_REQUEST] [SPOT_GRID] COUNTER_ORDER: LIMIT {side} {rounded_size} @ {rounded_price}")
        
        ctx.place_order(LimitOrderRequest(
            symbol=self.config.symbol,
            side=side,
            price=rounded_price,
            sz=rounded_size,
            reduce_only=False,
            cloid=next_cloid
        ))

    def on_order_failed(self, cloid: Cloid, ctx: StrategyContext):
        pass

    def get_summary(self, ctx: StrategyContext) -> StrategySummary:
         market_info = ctx.market_info(self.config.symbol)
         current_price = market_info.last_price if market_info else 0.0
         
         grid_spacing_min, _ = common.calculate_grid_spacing_pct(
            self.config.grid_type, self.config.lower_price, self.config.upper_price, self.config.grid_count
         )
         

         # Actually common.format_uptime expects timedelta.
         from datetime import timedelta
         uptime_str = common.format_uptime(timedelta(seconds=time.monotonic() - self.start_time))
         
         unrealized_pnl = 0.0
         if self.inventory > 0.0 and self.avg_entry_price > 0.0:
             unrealized_pnl = (current_price - self.avg_entry_price) * self.inventory

         return SpotGridSummary(
            symbol=self.config.symbol,
            price=current_price,
            state=str(self.state),
            uptime=uptime_str,
            position_size=self.inventory,
            position_side="Long" if self.inventory > 0 else "Flat",
            avg_entry_price=self.avg_entry_price,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_fees=self.total_fees,
            leverage=1, # Spot is 1x
            grid_bias="Neutral", # Spot implicit
            grid_count=len(self.zones),
            range_low=self.config.lower_price,
            range_high=self.config.upper_price,
            grid_spacing_pct=grid_spacing_min,
            roundtrips=sum(z.roundtrip_count for z in self.zones),
            margin_balance=0.0, # Placeholder
            initial_entry_price=self.initial_entry_price
         )

    def get_grid_state(self, ctx: StrategyContext) -> GridState:
        market_info = ctx.market_info(self.config.symbol)
        current_price = market_info.last_price if market_info else 0.0
        
        zones_info = [ZoneInfo(
            index=z.index,
            lower_price=z.lower_price,
            upper_price=z.upper_price,
            size=z.size,
            pending_side=str(z.pending_side),
            has_order=(z.order_id is not None),
            is_reduce_only=False,
            entry_price=z.entry_price,
            roundtrip_count=z.roundtrip_count
        ) for z in self.zones]

        return GridState(
            symbol=self.config.symbol,
            strategy_type="spot_grid",
            current_price=current_price,
            grid_bias=None,
            zones=zones_info
        )
