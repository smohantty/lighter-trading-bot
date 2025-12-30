import logging
import math
import time
from typing import List, Optional, Dict
from dataclasses import dataclass, field

from src.strategy.base import Strategy
from src.engine.context import StrategyContext, MarketInfo
from src.model import OrderRequest, LimitOrderRequest, OrderSide, OrderFill, Cloid, PendingOrder
from src.strategy.types import GridZone, GridState, ZoneStatus, GridMode, GridBias

logger = logging.getLogger("src.strategy.spot_grid")

class SpotGridStrategy(Strategy):
    """
    Spot Grid Trading Strategy.
    
    Places a grid of limit orders.
    - Buy orders below current price
    - Sell orders above current price
    
    For Spot:
    - Requires QUOTE asset (e.g. USDC) to place BUY orders.
    - Requires BASE asset (e.g. WETH) to place SELL orders.
    - No leverage, no liquidation logic.
    """
    def __init__(self, config):
        self.config = config
        self.symbol = config.symbol
        self.grid_count = config.grid_count
        self.grid_mode = config.grid_type # Map grid_type (arith/geo) to strategy prop if needed, or just use config directly. 
        # Actually, let's just keep reference to config.
        self.total_investment = config.total_investment
        
        # Spot grid specific: base/quote splitting
        # For simplicity, we assume investment is in Quote asset value terms for now
        # But we need Base asset for initial Sell orders.
        
        self.zones: List[GridZone] = []
        self.state = GridState.Initializing
        
        self.active_order_map: Dict[Cloid, int] = {} # Cloid -> Zone Index
        
        # Performance tracking
        self.realized_pnl = 0.0
        self.total_trades = 0
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
        # 1. Get initial data
        market_info = ctx.market_info(self.config.symbol)
        if not market_info:
            raise ValueError(f"No market info for {self.config.symbol}")
        
        last_price = price
        
        # Parse symbol to get Base/Quote assets (e.g. "LIT/USDC" -> Base="LIT", Quote="USDC")
        try:
            base_asset, quote_asset = self.config.symbol.split("/")
        except ValueError:
             # Fallback or error if symbol format differs
             logger.error(f"Invalid symbol format: {self.config.symbol}. Expected BASE/QUOTE")
             raise

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
            size = market_info.clamp_to_min_size(raw_size)

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
                
                # Spot has no reduce_only
                reduce_only = False

                cloid = ctx.generate_cloid()
                zone.order_id = cloid
                self.active_order_map[cloid] = idx

                logger.info(f"[ORDER_REQUEST] [SPOT_GRID] GRID_LVL_{idx}: LIMIT {side} {size} {self.config.symbol} @ {price}")

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
            logger.error(f"[SPOT_GRID] ASSERTION FAILED: Zone {zone_idx} expected side {expected_side} but got {fill.side}")
        
        # Spot: reduce_only is generally not relevant or forced to False.

    def place_counter_order(self, zone_idx: int, price: float, side: OrderSide, ctx: StrategyContext):
        zone = self.zones[zone_idx]
        next_cloid = ctx.generate_cloid()
        market_info = ctx.market_info(self.config.symbol)
        if not market_info: return

        rounded_price = market_info.round_price(price)
        rounded_size = market_info.round_size(zone.size)

        reduce_only = False

        logger.info(f"[ORDER_REQUEST] [SPOT_GRID] COUNTER_ORDER: LIMIT {side} {rounded_size} {self.config.symbol} @ {rounded_price}")

        self.active_order_map[next_cloid] = zone_idx
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
        # Trigger logic similar to Perp, but simpler state machine
        if self.state == GridState.Initializing:
            self.initialize_zones(price, ctx)
        
        elif self.state == GridState.WaitingForTrigger:
             # Basic implementation if trigger used
             if self.config.trigger_price:
                 # TODO: Add trigger logic if needed
                 pass
             else:
                 self.state = GridState.Running
        
        elif self.state == GridState.AcquiringAssets:
            # Waiting for market buy to fill
            pass 
        
        elif self.state == GridState.Running:
            if not self.active_order_map and len(self.zones) > 0:
                 self.refresh_orders(ctx)

    def on_order_filled(self, fill: OrderFill, ctx: StrategyContext):
        if fill.cloid:
            # Check for Acquisition Fill
            if self.state == GridState.AcquiringAssets and fill.cloid == self.acquisition_cloid:
                logger.info(f"[SPOT_GRID] Acquisition filled @ {fill.price}")
                self.total_fees += fill.fee
                
                # Spot: We bought Base asset.
                if fill.side.is_buy():
                    self.position_size += fill.size # Positive means long inventory
                
                # We can now start
                self.state = GridState.Running
                self.initial_entry_price = fill.price
                self.refresh_orders(ctx)
                return

            # Check Zone Fills
            zone_idx = self.active_order_map.pop(fill.cloid, None)
            if zone_idx is not None:
                self.total_trades += 1
                self.total_fees += fill.fee
                
                zone = self.zones[zone_idx]
                zone.order_id = None
                
                self.validate_fill_assertions(zone, fill, zone_idx)

                # Spot Grid (Long Only Mode) logic:
                # BUY -> Opening (Accumulating Base)
                # SELL -> Closing (Selling Base for Quote Profit)

                if zone.pending_side.is_buy():
                    # FILLED BUY -> Next is SELL (Take Profit)
                    logger.info(f"[SPOT_GRID] Zone {zone_idx} | BUY (Re-stock) Filled @ {fill.price} | Next: SELL @ {zone.upper_price}")
                    
                    self.position_size += fill.size
                    
                    # Update average entry price logic if needed, but for grid, FIFO/LIFO tracking is complex.
                    # We just track total inventory for now.
                    
                    zone.entry_price = fill.price
                    zone.pending_side = OrderSide.SELL
                    
                    self.place_counter_order(zone_idx, zone.upper_price, OrderSide.SELL, ctx)

                else:
                    # FILLED SELL -> Next is BUY (Re-entry)
                    pnl = (fill.price - zone.entry_price) * fill.size
                    # Note: entry_price might be 0 if we started with inventory.
                    # If 0, we can estimate pnl based on lower_price or treat as 0 base cost?
                    # For grid realized PnL, usually calculated as (Sell Price - Buy Price) * Size
                    if zone.entry_price == 0.0:
                         # Assume entry at lower bound or initial market price? 
                         # Let's use lower_price as proxy for "buy back level" profit calc?
                         # Or just ignore pnl for initial sell of existing inventory?
                         pnl = 0.0 
                    
                    logger.info(f"[SPOT_GRID] Zone {zone_idx} | SELL (Take Profit) Filled @ {fill.price} | PnL: {pnl:.4f} | Next: BUY @ {zone.lower_price}")
                    
                    self.position_size -= fill.size
                    self.realized_pnl += pnl
                    zone.roundtrip_count += 1
                    
                    zone.pending_side = OrderSide.BUY
                    self.place_counter_order(zone_idx, zone.lower_price, OrderSide.BUY, ctx)
            else:
                logger.info(f"[SPOT_GRID] Fill for unknown CLOID: {fill.cloid}")

    def on_order_failed(self, cloid: Cloid, ctx: StrategyContext):
        # If an order fails, we might want to retry or re-init zone
        logger.warning(f"[SPOT_GRID] Order failed: {cloid}")
        zone_idx = self.active_order_map.pop(cloid, None)
        if zone_idx is not None:
            # Simple retry mechanism: just clear order_id and let refresh_orders pick it up?
            self.zones[zone_idx].order_id = None

    def get_summary(self, ctx: StrategyContext) -> StrategySummary:
        market_info = ctx.market_info(self.config.symbol)
        current_price = market_info.last_price if market_info else 0.0
        
        total_roundtrips = sum(z.roundtrip_count for z in self.zones)
        
        # Spot PnL: Value of current inventory - Cost Basis
        unrealized_pnl = 0.0
        if self.position_size > 0.0:
            current_value = self.position_size * current_price
            cost_basis = self.position_size * self.avg_entry_price
            unrealized_pnl = current_value - cost_basis
            
        grid_spacing_min, _ = common.calculate_grid_spacing_pct(
            self.config.grid_type, self.config.lower_price, self.config.upper_price, self.config.grid_count
        )
        
        curr_time = time.monotonic()
        uptime_seconds = curr_time - self.start_time
        # Simple formatting
        m, s = divmod(uptime_seconds, 60)
        h, m = divmod(m, 60)
        uptime = f"{int(h):02d}:{int(m):02d}:{int(s):02d}"

        # Parse symbol for quote asset balance
        quote_asset = "USDC"
        if "/" in self.config.symbol:
            quote_asset = self.config.symbol.split("/")[1]

        # Use PerpGridSummary as base since it likely has most fields
        # Note: Ideally we should have SpotGridSummary, but PerpGridSummary structure is reusable if we ignore leverage.
        # Or better, simply omit leverage.
        
        return PerpGridSummary(
            symbol=self.config.symbol,
            price=current_price,
            state=str(self.state),
            uptime=uptime,
            position_size=self.position_size,
            position_side="Long" if self.position_size > 0 else "Flat",
            avg_entry_price=self.avg_entry_price,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_fees=self.total_fees,
            leverage=1.0, # Spot is 1x
            grid_bias="Long", # Spot is effectively Long logic
            grid_count=len(self.zones),
            range_low=self.config.lower_price,
            range_high=self.config.upper_price,
            grid_spacing_pct=grid_spacing_min,
            roundtrips=total_roundtrips,
            margin_balance=ctx.get_spot_available(quote_asset),
            initial_entry_price=self.initial_entry_price
        )

    def get_grid_state(self, ctx: StrategyContext) -> GridState:
        market_info = ctx.market_info(self.config.symbol)
        current_price = market_info.last_price if market_info else 0.0
        
        zones_info = []
        for z in self.zones:
            zones_info.append(ZoneInfo(
                index=z.index,
                lower_price=z.lower_price,
                upper_price=z.upper_price,
                size=z.size,
                pending_side=str(z.pending_side),
                has_order=(z.order_id is not None),
                is_reduce_only=False, # Spot 
                entry_price=z.entry_price,
                roundtrip_count=z.roundtrip_count
            ))

        return GridState(
            symbol=self.config.symbol,
            strategy_type="spot_grid",
            current_price=current_price,
            grid_bias="Long",
            zones=zones_info
        )
