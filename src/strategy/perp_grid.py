import logging
import time
from typing import List, Dict, Optional, Tuple
from datetime import timedelta
from dataclasses import dataclass
from enum import Enum, auto

from src.strategy.base import Strategy
from src.engine.context import StrategyContext
from src.model import Cloid, OrderFill, OrderSide, OrderRequest, LimitOrderRequest
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
    lower_price: float
    upper_price: float
    size: float
    pending_side: OrderSide
    mode: ZoneMode
    entry_price: float
    order_id: Optional[Cloid] = None
    roundtrip_count: int = 0

class PerpGridStrategy(Strategy):
    def __init__(self, config: PerpGridConfig):
        self.config = config
        self.zones: List[GridZone] = []
        self.active_orders: Dict[Cloid, int] = {}
        self.trade_count = 0
        self.state = StrategyState.Initializing
        self.initial_entry_price: Optional[float] = None
        self.trigger_reference_price: Optional[float] = None
        self.start_time = time.monotonic()
        
        # Acquisition state
        self.acquisition_cloid: Optional[Cloid] = None
        self.acquisition_target_size: float = 0.0

        # Performance Metrics
        self.realized_pnl = 0.0
        self.total_fees = 0.0
        self.unrealized_pnl = 0.0

        # Position Tracking
        self.position_size = 0.0
        self.avg_entry_price = 0.0

    def initialize_zones(self, price: float, ctx: StrategyContext):
        self.config.validate()

        # 1. Get initial data
        market_info = ctx.market_info(self.config.symbol)
        if not market_info:
            raise ValueError(f"No market info for {self.config.symbol}")
        
        last_price = price

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
        investment_per_zone = self.config.total_investment / float(num_zones)

        # Validation: Check minimum order size
        min_order_size = market_info.min_quote_amount
        if investment_per_zone < min_order_size:
            msg = f"Investment per zone ({investment_per_zone:.2f}) is less than minimum order value ({min_order_size}). Increase total_investment or decrease grid_count."
            logger.error(f"[PERP_GRID] {msg}")
            raise ValueError(msg)

        initial_price = self.config.trigger_price if self.config.trigger_price else last_price

        # Validation: Check if wallet has enough margin
        wallet_balance = ctx.get_perp_available("USDC")
        max_notional = wallet_balance * self.config.leverage

        # Note: In Rust this error was logged and returned Err.
        if max_notional < self.config.total_investment:
            msg = f"Insufficient Margin! Balance: {wallet_balance:.2f}, Lev: {self.config.leverage}, Max Notional: {max_notional:.2f}, Required: {self.config.total_investment:.2f}."
            logger.error(f"[PERP_GRID] {msg}")
            raise ValueError(msg)

        self.zones = []
        total_position_required = 0.0

        for i in range(num_zones):
            lower = prices[i]
            upper = prices[i+1]
            mid_price = (lower + upper) / 2.0

            raw_size = investment_per_zone / mid_price
            size = market_info.clamp_to_min_notional(raw_size, mid_price)

            # Zone classification
            if self.config.grid_bias == GridBias.LONG:
                if lower > initial_price:
                    pending_side = OrderSide.SELL
                    mode = ZoneMode.LONG
                else:
                    pending_side = OrderSide.BUY
                    mode = ZoneMode.LONG
            else: # Short Bias
                if upper < initial_price:
                    pending_side = OrderSide.BUY
                    mode = ZoneMode.SHORT
                else:
                    pending_side = OrderSide.SELL
                    mode = ZoneMode.SHORT

            if mode == ZoneMode.LONG and pending_side.is_sell():
                total_position_required += size
            if mode == ZoneMode.SHORT and pending_side.is_buy():
                total_position_required -= size

            self.zones.append(GridZone(
                index=i,
                lower_price=lower,
                upper_price=upper,
                size=size,
                pending_side=pending_side,
                mode=mode,
                entry_price=0.0,
                roundtrip_count=0
            ))

        # 3. Determine Initial Bias Orders
        # Trigger Reference
        self.trigger_reference_price = last_price

        # Initial Entry if already in range
        if last_price >= self.config.lower_price and last_price <= self.config.upper_price:
             self.initial_entry_price = last_price

        logger.info(f"[PERP_GRID] Setup completed. Net position required: {total_position_required}")

        if abs(total_position_required) > 0.0:
            logger.info(f"[PERP_GRID] Acquiring initial position: {total_position_required}")
            cloid = ctx.generate_cloid()
            
            market_price = last_price
            side = OrderSide.BUY if total_position_required > 0.0 else OrderSide.SELL

            if self.config.trigger_price:
                trigger = self.config.trigger_price
                is_safe_limit = (trigger < market_price) if side.is_buy() else (trigger > market_price)
                
                if not is_safe_limit:
                    msg = f"Invalid Trigger Price! Side: {side}, Trigger: {trigger}, Market: {market_price}. Bot requires a resting limit order."
                    logger.error(f"[PERP_GRID] {msg}")
                    raise ValueError(msg)
                
                logger.info(f"[PERP_GRID] Using Trigger Price {trigger} for initial entry.")
                price_to_use = trigger
            else:
                # Calculate safe limit price
                if side.is_buy():
                    candidate_prices = [z.lower_price for z in self.zones if z.lower_price < market_price]
                    grid_price = max(candidate_prices) if candidate_prices else market_price
                    limit_price = market_price * (1.0 - 0.001)
                    price_to_use = max(grid_price, limit_price)
                else:
                    # Min of (grid prices > market) or (market + 0.1%)
                    candidate_prices = [z.upper_price for z in self.zones if z.upper_price > market_price]
                    grid_price = min(candidate_prices) if candidate_prices else market_price
                    limit_price = market_price * (1.0 + 0.001)
                    price_to_use = min(grid_price, limit_price)

            activation_price = market_info.round_price(price_to_use)
            target_size = market_info.round_size(abs(total_position_required))

            if target_size > 0.0:
                self.state = StrategyState.AcquiringAssets
                self.acquisition_cloid = cloid
                self.acquisition_target_size = target_size
                
                logger.info(f"[ORDER_REQUEST] [PERP_GRID] REBALANCING: LIMIT {side} {target_size} {self.config.symbol} @ {activation_price}")
                
                ctx.place_order(LimitOrderRequest(
                    symbol=self.config.symbol,
                    side=side,
                    price=activation_price,
                    sz=target_size,
                    reduce_only=False,
                    cloid=cloid
                ))
                return

        self.initial_entry_price = initial_price
        self.state = StrategyState.Running
        try:
            self.refresh_orders(ctx)
        except Exception as e:
            logger.warning(f"[PERP_GRID] Failed to refresh orders: {e}") 

    def refresh_orders(self, ctx: StrategyContext):
        market_info = ctx.market_info(self.config.symbol)
        if not market_info:
            raise ValueError(f"No market info for {self.config.symbol}")

        for idx, zone in enumerate(self.zones):
            if zone.order_id is None:
                side = zone.pending_side
                price = zone.lower_price if side.is_buy() else zone.upper_price
                size = zone.size
                
                # reduce_only logic
                reduce_only = False
                if zone.mode == ZoneMode.SHORT:
                    reduce_only = side.is_buy() # Buy closes short
                elif zone.mode == ZoneMode.LONG:
                    reduce_only = side.is_sell() # Sell closes long

                cloid = ctx.generate_cloid()
                zone.order_id = cloid
                self.active_orders[cloid] = idx

                logger.info(f"[ORDER_REQUEST] [PERP_GRID] GRID_LVL_{idx}: LIMIT {side} {size} {self.config.symbol} @ {price}{' (RO)' if reduce_only else ''}")

                info = ctx.market_info(self.config.symbol)
                if not info:
                    logger.error(f"Market info not found for {self.config.symbol}")
                    continue
                
                price = info.round_price(price)
                size = info.round_size(size)

                ctx.place_order(LimitOrderRequest(
                    symbol=self.config.symbol,
                    side=side,
                    price=price,
                    sz=size,
                    reduce_only=reduce_only,
                    cloid=cloid
                ))

    def validate_fill_assertions(self, zone: GridZone, fill: OrderFill, zone_idx: int):
        expected_side = zone.pending_side
        if fill.side != expected_side:
            logger.error(f"[PERP_GRID] ASSERTION FAILED: Zone {zone_idx} expected side {expected_side} but got {fill.side}")
        
        # In Python we assume no raw_dir check for now as we build model, unless we passed it
        
        expected_reduce_only = False
        if zone.mode == ZoneMode.LONG:
            expected_reduce_only = (zone.pending_side == OrderSide.SELL)
        elif zone.mode == ZoneMode.SHORT:
            expected_reduce_only = (zone.pending_side == OrderSide.BUY)
        
        if fill.reduce_only is not None:
             if fill.reduce_only != expected_reduce_only:
                 logger.error(f"[PERP_GRID] ASSERTION FAILED: Zone {zone_idx} expected reduce_only={expected_reduce_only} but got {fill.reduce_only}")

    def place_counter_order(self, zone_idx: int, price: float, side: OrderSide, ctx: StrategyContext):
        zone = self.zones[zone_idx]
        next_cloid = ctx.generate_cloid()
        market_info = ctx.market_info(self.config.symbol)
        if not market_info: return

        rounded_price = market_info.round_price(price)
        rounded_size = market_info.round_size(zone.size)

        reduce_only = False
        if zone.mode == ZoneMode.SHORT:
            reduce_only = side.is_buy()
        elif zone.mode == ZoneMode.LONG:
            reduce_only = side.is_sell()

        logger.info(f"[ORDER_REQUEST] [PERP_GRID] COUNTER_ORDER: LIMIT {side} {rounded_size} {self.config.symbol} @ {rounded_price}{' (RO)' if reduce_only else ''}")

        self.active_orders[next_cloid] = zone_idx
        zone.order_id = next_cloid

        ctx.place_order(LimitOrderRequest(
            symbol=self.config.symbol,
            side=side,
            price=rounded_price,
            sz=rounded_size,
            reduce_only=reduce_only,
            cloid=next_cloid
        ))

    def on_tick(self, price: float, ctx: StrategyContext):
        if self.state == StrategyState.Initializing:
            self.initialize_zones(price, ctx)
        
        elif self.state == StrategyState.WaitingForTrigger:
             if self.config.trigger_price:
                 start = self.trigger_reference_price
                 if start is None:
                     # This should not happen if logic is correct
                     return
                 
                 if common.check_trigger(price, self.config.trigger_price, start):
                     logger.info(f"[PERP_GRID] Price {price} crossed trigger {self.config.trigger_price}. Starting.")
                     logger.info("[PERP_GRID] Triggered! Re-initializing zones.")
                     self.zones.clear()
                     self.initialize_zones(price, ctx)
             else:
                 self.state = StrategyState.Running
        
        elif self.state == StrategyState.AcquiringAssets:
            pass 
        
        elif self.state == StrategyState.Running:
            if not self.active_orders and len(self.zones) > 0:
                 # If we have zones but no active orders (and not empty), mostly likely need refresh
                 # But self.active_orders tracks cloids. 
                 # refresh_orders checks zone.order_id is None.
                 self.refresh_orders(ctx)

    def on_order_filled(self, fill: OrderFill, ctx: StrategyContext):
        if fill.cloid:
            # Check for Acquisition Fill
            if self.state == StrategyState.AcquiringAssets and fill.cloid == self.acquisition_cloid:
                logger.info(f"[PERP_GRID] Acquisition filled @ {fill.price}")
                self.total_fees += fill.fee
                
                old_pos = self.position_size
                if fill.side.is_buy():
                    self.position_size += fill.size
                else:
                    self.position_size -= fill.size
                
                # Update avg entry price
                if abs(self.position_size) > 0.0:
                    self.avg_entry_price = (abs(old_pos) * self.avg_entry_price + fill.size * fill.price) / abs(self.position_size)

                # Update Zones Entry Price
                for zone in self.zones:
                    if zone.mode == ZoneMode.LONG and zone.pending_side.is_sell():
                        zone.entry_price = fill.price
                    if zone.mode == ZoneMode.SHORT and zone.pending_side.is_buy():
                        zone.entry_price = fill.price
                
                self.initial_entry_price = fill.price
                self.state = StrategyState.Running
                self.refresh_orders(ctx)
                return

            # Check Zone Fills
            zone_idx = self.active_orders.pop(fill.cloid, None)
            if zone_idx is not None:
                self.trade_count += 1
                self.total_fees += fill.fee
                
                zone = self.zones[zone_idx]
                zone.order_id = None
                
                self.validate_fill_assertions(zone, fill, zone_idx)

                is_opening = False
                if (zone.pending_side == OrderSide.BUY and zone.mode == ZoneMode.LONG) or \
                   (zone.pending_side == OrderSide.SELL and zone.mode == ZoneMode.SHORT):
                   is_opening = True

                old_pos = self.position_size
                if zone.pending_side.is_buy():
                    self.position_size += fill.size
                else:
                    self.position_size -= fill.size

                if is_opening and abs(self.position_size) > 0.0:
                    self.avg_entry_price = (abs(old_pos) * self.avg_entry_price + fill.size * fill.price) / abs(self.position_size)
                elif abs(self.position_size) < 0.0001:
                    self.avg_entry_price = 0.0

                pnl = None
                next_px = 0.0
                next_side = OrderSide.BUY # placeholder

                if zone.pending_side == OrderSide.BUY and zone.mode == ZoneMode.LONG:
                    logger.info(f"[PERP_GRID] Zone {zone_idx} | BUY (Open Long) Filled @ {fill.price} | Next: SELL @ {zone.upper_price}")
                    next_side = OrderSide.SELL
                    next_px = zone.upper_price
                    zone.entry_price = fill.price
                
                elif zone.pending_side == OrderSide.SELL and zone.mode == ZoneMode.LONG:
                    pnl = (fill.price - zone.entry_price) * fill.size
                    zone.roundtrip_count += 1
                    logger.info(f"[PERP_GRID] Zone {zone_idx} | SELL (Close Long) Filled @ {fill.price} | PnL: {pnl:.4f} | Next: BUY @ {zone.lower_price}")
                    next_side = OrderSide.BUY
                    next_px = zone.lower_price
                    zone.entry_price = 0.0

                elif zone.pending_side == OrderSide.SELL and zone.mode == ZoneMode.SHORT:
                    logger.info(f"[PERP_GRID] Zone {zone_idx} | SELL (Open Short) Filled @ {fill.price} | Next: BUY @ {zone.lower_price}")
                    next_side = OrderSide.BUY
                    next_px = zone.lower_price
                    zone.entry_price = fill.price
                
                elif zone.pending_side == OrderSide.BUY and zone.mode == ZoneMode.SHORT:
                    pnl = (zone.entry_price - fill.price) * fill.size
                    zone.roundtrip_count += 1
                    logger.info(f"[PERP_GRID] Zone {zone_idx} | BUY (Close Short) Filled @ {fill.price} | PnL: {pnl:.4f} | Next: SELL @ {zone.upper_price}")
                    next_side = OrderSide.SELL
                    next_px = zone.upper_price
                    zone.entry_price = 0.0
                
                zone.pending_side = next_side
                
                if pnl is not None:
                    self.realized_pnl += pnl
                
                self.place_counter_order(zone_idx, next_px, next_side, ctx)
            else:
                logger.info(f"[PERP_GRID] Fill for unknown CLOID: {fill.cloid}")

    def on_order_failed(self, cloid: Cloid, ctx: StrategyContext):
        pass

    def get_summary(self, ctx: StrategyContext) -> StrategySummary:
        market_info = ctx.market_info(self.config.symbol)
        current_price = market_info.last_price if market_info else 0.0
        
        total_roundtrips = sum(z.roundtrip_count for z in self.zones)
        
        position_side = "Flat"
        unrealized_pnl = 0.0
        if self.position_size > 0.0:
            position_side = "Long"
            unrealized_pnl = (current_price - self.avg_entry_price) * self.position_size
        elif self.position_size < 0.0:
            position_side = "Short"
            unrealized_pnl = (self.avg_entry_price - current_price) * abs(self.position_size)
            
        grid_spacing_min, _ = common.calculate_grid_spacing_pct(
            self.config.grid_type, self.config.lower_price, self.config.upper_price, self.config.grid_count
        )
        
        uptime = common.format_uptime(timedelta(seconds=time.monotonic() - self.start_time))
        
        return PerpGridSummary(
            symbol=self.config.symbol,
            price=current_price,
            state=str(self.state),
            uptime=uptime,
            position_size=self.position_size,
            position_side=position_side,
            avg_entry_price=self.avg_entry_price,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_fees=self.total_fees,
            leverage=self.config.leverage,
            grid_bias=self.config.grid_bias.value,
            grid_count=len(self.zones),
            range_low=self.config.lower_price,
            range_high=self.config.upper_price,
            grid_spacing_pct=grid_spacing_min,
            roundtrips=total_roundtrips,
            margin_balance=ctx.get_perp_available("USDC"),
            initial_entry_price=self.initial_entry_price
        )

    def get_grid_state(self, ctx: StrategyContext) -> GridState:
        market_info = ctx.market_info(self.config.symbol)
        current_price = market_info.last_price if market_info else 0.0
        
        zones_info = []
        for z in self.zones:
            is_reduce_only = False
            if z.mode == ZoneMode.SHORT: is_reduce_only = z.pending_side.is_buy()
            if z.mode == ZoneMode.LONG: is_reduce_only = z.pending_side.is_sell()
            
            zones_info.append(ZoneInfo(
                index=z.index,
                lower_price=z.lower_price,
                upper_price=z.upper_price,
                size=z.size,
                pending_side=str(z.pending_side),
                has_order=(z.order_id is not None),
                is_reduce_only=is_reduce_only,
                entry_price=z.entry_price,
                roundtrip_count=z.roundtrip_count
            ))

        return GridState(
            symbol=self.config.symbol,
            strategy_type="perp_grid",
            current_price=current_price,
            grid_bias=self.config.grid_bias.value,
            zones=zones_info
        )
