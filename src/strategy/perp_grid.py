import logging
import time
from datetime import timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from src.config import PerpGridConfig
from src.constants import (
    ACQUISITION_SPREAD,
    INVESTMENT_BUFFER_PERP,
    MAX_ORDER_RETRIES,
)
from src.engine.context import MarketInfo, StrategyContext
from src.model import Cloid, LimitOrderRequest, OrderFailure, OrderFill, OrderSide
from src.strategy import common
from src.strategy.base import Strategy
from src.strategy.types import (
    GridBias,
    GridState,
    GridZone,
    PerpGridSummary,
    StrategyState,
    ZoneInfo,
    ZoneMode,
)

logger = logging.getLogger("PerpGrid")


class PerpGridStrategy(Strategy):
    """
    Perpetual Grid Trading Strategy.

    Operates with a directional bias: Long Bias (Buy to Open, Sell to Close) or Short Bias (Sell to Open, Buy to Close).

    Uses 'USDC' as collateral.
    """

    # =========================================================================
    # INITIALIZATION
    # =========================================================================

    def __init__(self, config: PerpGridConfig):
        self.config = config
        self.symbol = config.symbol
        self.leverage = config.leverage
        self.total_investment = (
            config.total_investment
        )  # Max notional exposure at grid extremes
        self.grid_bias = config.grid_bias

        self.zones: List[GridZone] = []
        self.active_order_map: Dict[Cloid, GridZone] = {}

        self.trade_count = 0
        self.state = StrategyState.Initializing

        # Performance Metrics
        self.matched_profit = Decimal("0")
        self.total_fees = Decimal("0")
        self.initial_equity = Decimal("0")

        # Position Tracking
        self.position_size = Decimal("0")
        self.avg_entry_price = Decimal("0")

        # Internal State
        self.current_price = Decimal("0")
        self.start_time = time.time()
        self.initial_entry_price: Optional[Decimal] = None
        self.trigger_reference_price: Optional[Decimal] = None
        self.trigger_activated = False

        # Acquisition State
        self.acquisition_cloid: Optional[Cloid] = None
        self.acquisition_target_size: Decimal = Decimal("0")
        self.target_position_size = Decimal("0")
        self.market: MarketInfo = None  # type: ignore
        self._last_dead_zone_warning_ts = 0.0

        if self.config.spread_bips:
            # Calculate grid count based on spread
            prices = common.calculate_grid_prices_by_spread(
                self.config.grid_range_low,
                self.config.grid_range_high,
                self.config.spread_bips,
            )
            self.grid_count = len(prices)
            spacing = self.config.spread_bips / Decimal("100")
            self.grid_spacing_pct = (spacing, spacing)
        else:
            assert self.config.grid_count is not None
            self.grid_count = self.config.grid_count
            self.grid_spacing_pct = common.calculate_grid_spacing_pct(
                self.config.grid_type,
                self.config.grid_range_low,
                self.config.grid_range_high,
                self.config.grid_count,
            )

    # =========================================================================
    # STRATEGY LIFECYCLE (Base Class Interface)
    # =========================================================================

    def on_tick(self, price: Decimal, ctx: StrategyContext):
        self.current_price = price
        if self.state == StrategyState.Initializing:
            self.initialize_zones(price, ctx)

        elif self.state == StrategyState.WaitingForTrigger:
            if self.config.trigger_price:
                if self.trigger_reference_price is None:
                    self.trigger_reference_price = price
                if self._is_trigger_satisfied(price):
                    logger.info(
                        "[PERP_GRID] Triggered at price=%s trigger_price=%s reference_price=%s",
                        price,
                        self.config.trigger_price,
                        self.trigger_reference_price,
                    )
                    self._resume_deferred_acquisition_after_trigger(ctx)

        elif self.state == StrategyState.Running:
            # Continuously ensure orders are active
            self.refresh_orders(ctx)

    def on_order_filled(self, fill: OrderFill, ctx: StrategyContext):
        if self.state == StrategyState.Initializing:
            return

        if fill.cloid:
            # 1. Acquisition Fill
            if (
                self.state == StrategyState.AcquiringAssets
                and fill.cloid == self.acquisition_cloid
            ):
                self._handle_acquisition_fill(fill, ctx)
                return

            # 2. Grid Fill
            if fill.cloid in self.active_order_map:
                zone = self.active_order_map.pop(fill.cloid)
                zone.cloid = None
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
                    self._handle_long_bias_fill(zone, fill, ctx)
                elif zone.mode == ZoneMode.SHORT:
                    self._handle_short_bias_fill(zone, fill, ctx)

    def on_order_failed(self, failure: OrderFailure, ctx: StrategyContext):
        if self.state == StrategyState.Initializing:
            return

        cloid = failure.cloid
        if cloid in self.active_order_map:
            zone = self.active_order_map.pop(cloid)
            idx = zone.index
            zone.cloid = None

            logger.warning(
                f"[ORDER_FAILED][PERP_GRID] GRID_ZONE_{idx} cloid: {cloid.as_int()} "
                f"reason: {failure.failure_reason}. Retry count: {zone.retry_count + 1}/{MAX_ORDER_RETRIES}"
            )

            zone.retry_count += 1

    def get_summary(self, ctx: StrategyContext) -> PerpGridSummary:
        if self.state == StrategyState.Initializing:
            raise ValueError("Strategy not initialized")

        unrealized = Decimal("0")
        if self.position_size != 0:
            diff = self.current_price - self.avg_entry_price
            if self.position_size < 0:
                diff = (
                    -diff
                )  # Short: (Entry - Current) * Size = -(Current - Entry) * Size
            unrealized = diff * abs(self.position_size)

        # Total Profit = (MarginBalance + Unrealized) - InitialEquity
        # Assumption: get_perp_available return Wallet Balance.
        margin_balance = ctx.get_perp_available("USDC")
        total_profit = (margin_balance + unrealized) - self.initial_equity

        return PerpGridSummary(
            symbol=self.symbol,
            state=self.state.name,
            uptime=common.format_uptime(
                timedelta(seconds=time.time() - self.start_time)
            ),
            position_size=self.position_size,
            position_side="Long" if self.position_size > 0 else "Short",
            matched_profit=self.matched_profit,
            total_profit=total_profit,
            total_fees=self.total_fees,
            leverage=self.leverage,
            grid_bias=self.config.grid_bias.value,
            grid_count=len(self.zones),
            grid_range_low=self.config.grid_range_low,
            grid_range_high=self.config.grid_range_high,
            grid_spacing_pct=self.grid_spacing_pct,
            roundtrips=sum(z.roundtrip_count for z in self.zones),
            margin_balance=margin_balance,
            initial_entry_price=self.initial_entry_price,
        )

    def get_grid_state(self, ctx: StrategyContext) -> GridState:
        if self.state == StrategyState.Initializing:
            raise ValueError("Strategy not initialized")

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
                    order_side=str(z.order_side),
                    has_order=z.cloid is not None,
                    is_reduce_only=self._is_reduce_only(z),
                    entry_price=z.entry_price,
                    roundtrip_count=z.roundtrip_count,
                )
                for z in self.zones
            ],
        )

    # =========================================================================
    # GRID SETUP & INITIALIZATION
    # =========================================================================

    def calculate_grid_plan(
        self, reference_price: Decimal
    ) -> tuple[List[GridZone], Decimal]:
        """
        Calculates grid zones and required position size.
        Returns: (zones, required_position_size)
        """
        # 1. Generate Levels
        if self.config.spread_bips:
            prices = common.calculate_grid_prices_by_spread(
                self.config.grid_range_low,
                self.config.grid_range_high,
                self.config.spread_bips,
            )
            # Update grid_count
            self.grid_count = len(prices)
        else:
            assert self.config.grid_count is not None
            prices = common.calculate_grid_prices(
                self.config.grid_type,
                self.config.grid_range_low,
                self.config.grid_range_high,
                self.config.grid_count,
            )
        prices = [self.market.round_price(p) for p in prices]

        # 2. Calculate Size per Zone
        adjusted_investment = INVESTMENT_BUFFER_PERP.markdown(self.total_investment)
        notional_per_zone = adjusted_investment / Decimal(str(self.grid_count - 1))

        # Validation
        max_size_estimate = notional_per_zone / Decimal(str(self.config.grid_range_low))
        min_size_limit = self.market.min_base_amount
        if max_size_estimate < min_size_limit:
            logger.warning(
                f"[PERP_GRID] Size estimate below minimum: {max_size_estimate} < {min_size_limit}"
            )

        initial_price = (
            self.config.trigger_price if self.config.trigger_price else reference_price
        )

        # 3. Build Zones & Calculate Initial Requirement
        zones = []
        required_position_size = Decimal("0.0")

        for i in range(self.grid_count - 1):
            zone_buy_price = prices[i]
            zone_sell_price = prices[i + 1]

            # Use buy price for conservative Notional -> Size conversion
            raw_size = notional_per_zone / zone_buy_price
            size = self.market.round_size(raw_size)

            order_side, mode, entry_price, position_delta = (
                self._calculate_zone_initial_state(
                    zone_buy_price, zone_sell_price, initial_price, size
                )
            )
            required_position_size += position_delta

            zones.append(
                GridZone(
                    index=i,
                    buy_price=zone_buy_price,
                    sell_price=zone_sell_price,
                    size=size,
                    order_side=order_side,
                    mode=mode,
                    entry_price=entry_price,
                )
            )

        return zones, required_position_size

    def initialize_zones(self, price: Decimal, ctx: StrategyContext):
        self.current_price = price
        market_info = ctx.market_info(self.symbol)
        if not market_info:
            raise ValueError(f"No market info for {self.symbol}")
        self.market = market_info

        # Validate balance: available_usdc * leverage must cover total_investment
        available_usdc = ctx.get_perp_available("USDC")
        max_notional = available_usdc * Decimal(str(self.leverage))
        if max_notional < self.total_investment:
            margin_required = self.total_investment / Decimal(str(self.leverage))
            msg = (
                f"Insufficient margin! Available: {available_usdc:.2f} USDC (max notional: {max_notional:.2f}). "
                f"Required: {margin_required:.2f} USDC for {self.total_investment:.2f} notional at {self.leverage}x leverage."
            )
            logger.error(f"[PERP_GRID] {msg}")
            raise ValueError(msg)

        startup_price = price
        self.zones, required_position_size = self.calculate_grid_plan(startup_price)
        self.target_position_size = required_position_size

        planning_price = (
            self.config.trigger_price if self.config.trigger_price else startup_price
        )

        logger.info(
            "[PERP_GRID] Setup complete. startup_current_price=%s planning_price=%s Bias=%s Required Net Position=%.4f",
            startup_price,
            planning_price,
            self.grid_bias,
            required_position_size,
        )

        # Log grid plan for production debugging
        holding = sum(
            1
            for z in self.zones
            if z.order_side.is_sell()
            if self.grid_bias == GridBias.LONG
        )
        if self.grid_bias == GridBias.SHORT:
            holding = sum(1 for z in self.zones if z.order_side.is_buy())
        waiting = len(self.zones) - holding
        logger.info(
            "[PERP_GRID] [GRID_PLAN] zones=%d holding=%d waiting=%d spacing_pct=(%s, %s) price=%s range=[%s, %s]",
            len(self.zones),
            holding,
            waiting,
            float(self.grid_spacing_pct[0]),
            float(self.grid_spacing_pct[1]),
            planning_price,
            self.config.grid_range_low,
            self.config.grid_range_high,
        )
        for z in self.zones:
            logger.debug(
                "[PERP_GRID] [GRID_PLAN] zone=%d buy=%s sell=%s size=%s side=%s mode=%s entry=%s",
                z.index,
                z.buy_price,
                z.sell_price,
                z.size,
                z.order_side,
                z.mode,
                z.entry_price,
            )

        # Capture Initial Equity
        # Equity = Margin Balance + Unrealized PnL (starts at 0 if pos=0)
        self.initial_equity = available_usdc

        # Check Initial Acquisition
        self.check_initial_acquisition(ctx, required_position_size)

    def check_initial_acquisition(
        self,
        ctx: StrategyContext,
        target_position: Decimal,
        allow_wait_for_trigger: bool = True,
    ) -> None:
        """
        Calculates required acquisition based on target position.
        """
        if self.config.trigger_price and self.trigger_reference_price is None:
            self.trigger_reference_price = self.current_price
            logger.info(
                "[PERP_GRID] Trigger reference initialized trigger_price=%s reference_price=%s",
                self.config.trigger_price,
                self.trigger_reference_price,
            )

        needed_change = target_position - self.position_size
        minimal_size = self.market.min_base_amount

        if abs(needed_change) < minimal_size:
            # Negligible
            if (
                self.config.trigger_price
                and allow_wait_for_trigger
                and not self._is_trigger_satisfied(self.current_price)
            ):
                direction = (
                    "above"
                    if self.config.trigger_price > self.current_price
                    else "below"
                )
                logger.info(
                    "[PERP_GRID] Position OK. Waiting for Trigger. trigger_price=%s direction=%s reference_price=%s",
                    self.config.trigger_price,
                    direction,
                    self.current_price,
                )
                self.state = StrategyState.WaitingForTrigger
            else:
                if self.config.trigger_price:
                    logger.info(
                        "[PERP_GRID] Position OK. Trigger satisfied. Starting. trigger_price=%s reference_price=%s current_price=%s",
                        self.config.trigger_price,
                        self.trigger_reference_price,
                        self.current_price,
                    )
                else:
                    logger.info("[PERP_GRID] Position OK. Starting.")
                self.state = StrategyState.Running
                self.initial_entry_price = self.current_price
                self.start_time = time.time()
                self.refresh_orders(ctx)
            return

        # Need to Acquire
        side = OrderSide.BUY if needed_change > 0 else OrderSide.SELL
        should_acquire_now, policy_reason = self._acquisition_policy_decision(
            side, allow_wait_for_trigger
        )
        logger.info(
            "[PERP_GRID] [ACQUISITION_POLICY] side=%s action=%s reason=%s current=%s trigger=%s reference=%s",
            side,
            "acquire_now" if should_acquire_now else "wait_for_trigger",
            policy_reason,
            self.current_price,
            self.config.trigger_price,
            self.trigger_reference_price,
        )
        if not should_acquire_now:
            self.state = StrategyState.WaitingForTrigger
            return

        size = self.market.round_size(abs(needed_change))

        # Price determination
        price = self._calculate_acquisition_price(side, self.current_price)

        logger.info(
            f"[ORDER_REQUEST] [PERP_GRID] [ACQUISITION] {side} {size} @ {price}"
        )

        cloid = ctx.place_order(
            LimitOrderRequest(
                symbol=self.symbol, side=side, price=price, sz=size, reduce_only=False
            )
        )

        self.state = StrategyState.AcquiringAssets
        self.acquisition_cloid = cloid
        self.acquisition_target_size = size

    # =========================================================================
    # ORDER MANAGEMENT
    # =========================================================================

    def place_zone_order(self, zone: GridZone, ctx: StrategyContext) -> bool:
        """Place an order for a zone based on its current state."""
        if zone.cloid is not None:
            return False

        idx = zone.index
        side = zone.order_side
        price = zone.buy_price if side.is_buy() else zone.sell_price
        reduce_only = self._is_reduce_only(zone)

        cloid = ctx.place_order(
            LimitOrderRequest(
                symbol=self.config.symbol,
                side=side,
                price=price,
                sz=zone.size,
                reduce_only=reduce_only,
            )
        )

        zone.cloid = cloid
        self.active_order_map[cloid] = zone

        logger.debug(
            "[ORDER_REQUEST] [PERP_GRID] GRID_ZONE_%s cloid=%s LIMIT %s %s @ %s reduce_only=%s",
            idx,
            cloid.as_int(),
            side,
            zone.size,
            price,
            reduce_only,
        )
        return True

    def refresh_orders(self, ctx: StrategyContext):
        """Place orders for all zones that don't have one and haven't exceeded max retries."""
        placed = 0
        dead_zones = 0
        for zone in self.zones:
            if zone.cloid is None:
                if zone.retry_count < MAX_ORDER_RETRIES:
                    if self.place_zone_order(zone, ctx):
                        placed += 1
                else:
                    dead_zones += 1

        if placed:
            logger.info(
                "[PERP_GRID] Activated zone orders count=%d active_orders=%d",
                placed,
                len(self.active_order_map),
            )
        if dead_zones and (time.time() - self._last_dead_zone_warning_ts) >= 60:
            logger.warning(
                "[PERP_GRID] Zones stuck at retry limit count=%d max_retries=%d",
                dead_zones,
                MAX_ORDER_RETRIES,
            )
            self._last_dead_zone_warning_ts = time.time()

    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================

    def _calculate_zone_initial_state(
        self,
        zone_buy_price: Decimal,
        zone_sell_price: Decimal,
        initial_price: Decimal,
        size: Decimal,
    ) -> tuple[OrderSide, ZoneMode, Decimal, Decimal]:
        """
        Calculate initial zone state based on bias and price position.
        Returns: (order_side, mode, entry_price, position_delta)
        """
        order_side = OrderSide.BUY
        mode = ZoneMode.LONG
        entry_price = Decimal("0")
        position_delta = Decimal("0")

        if self.grid_bias == GridBias.LONG:
            mode = ZoneMode.LONG

            if zone_buy_price > initial_price:
                # WE HOLD THIS ZONE - already opened long
                order_side = OrderSide.SELL  # Target is to close at sell_price
                position_delta = size
                entry_price = initial_price
            else:
                # WE DO NOT HOLD - waiting to open long
                order_side = OrderSide.BUY  # Target is to open at buy_price
                entry_price = Decimal("0")

        elif self.grid_bias == GridBias.SHORT:
            mode = ZoneMode.SHORT

            if zone_sell_price < initial_price:
                # WE HOLD THIS ZONE (SHORT) - already opened short
                order_side = OrderSide.BUY  # Target is to close at buy_price
                position_delta = -size  # Negative for short
                entry_price = initial_price
            else:
                # WE DO NOT HOLD - waiting to open short
                order_side = OrderSide.SELL  # Target is to open at sell_price
                entry_price = Decimal("0")

        return order_side, mode, entry_price, position_delta

    def _is_reduce_only(self, zone: GridZone) -> bool:
        """Determine if a zone order should be reduce_only."""
        if zone.mode == ZoneMode.LONG and zone.order_side.is_sell():
            return True
        if zone.mode == ZoneMode.SHORT and zone.order_side.is_buy():
            return True
        return False

    def _update_avg_entry(
        self, zone: GridZone, fill: OrderFill, old_pos: Decimal
    ) -> None:
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

    def _is_trigger_satisfied(self, price: Decimal) -> bool:
        if not self.config.trigger_price:
            return True
        if self.trigger_activated:
            return True
        if self.trigger_reference_price is None:
            return False
        is_hit = common.check_trigger(
            price, self.config.trigger_price, self.trigger_reference_price
        )
        if is_hit:
            self.trigger_activated = True
        return is_hit

    def _acquisition_policy_decision(
        self, side: OrderSide, allow_wait_for_trigger: bool
    ) -> tuple[bool, str]:
        if not self.config.trigger_price:
            return True, "no_trigger"
        if self._is_trigger_satisfied(self.current_price):
            return True, "trigger_satisfied"
        if not allow_wait_for_trigger:
            return True, "trigger_fired_resume_acquire_now"

        trigger_price = self.config.trigger_price
        if side.is_buy():
            if self.current_price < trigger_price:
                return True, "buy_below_trigger_acquire_now"
            return False, "buy_above_trigger_wait"

        if self.current_price > trigger_price:
            return True, "sell_above_trigger_acquire_now"
        return False, "sell_below_trigger_wait"

    def _resume_deferred_acquisition_after_trigger(self, ctx: StrategyContext) -> None:
        logger.info(
            "[PERP_GRID] [TRIGGER_RESUME] recompute position deficit target_position=%s current_position=%s",
            self.target_position_size,
            self.position_size,
        )
        self.check_initial_acquisition(
            ctx,
            self.target_position_size,
            allow_wait_for_trigger=False,
        )

    def _calculate_acquisition_price(
        self, side: OrderSide, current_price: Decimal
    ) -> Decimal:
        """Calculate optimal price for acquiring assets during initial setup."""
        if side == OrderSide.BUY:
            candidates = [
                z.buy_price for z in self.zones if z.buy_price < current_price
            ]
            if candidates:
                price = self.market.round_price(max(candidates))
                logger.info(
                    "[PERP_GRID] [ACQUISITION_PRICE] side=%s method=nearest_level price=%s current=%s candidates=%d",
                    side,
                    price,
                    current_price,
                    len(candidates),
                )
                return price
            elif self.zones:
                price = self.market.round_price(
                    ACQUISITION_SPREAD.markdown(current_price)
                )
                logger.info(
                    "[PERP_GRID] [ACQUISITION_PRICE] side=%s method=spread_markdown price=%s current=%s",
                    side,
                    price,
                    current_price,
                )
                return price
        else:  # SELL
            candidates = [
                z.sell_price for z in self.zones if z.sell_price > current_price
            ]
            if candidates:
                price = self.market.round_price(min(candidates))
                logger.info(
                    "[PERP_GRID] [ACQUISITION_PRICE] side=%s method=nearest_level price=%s current=%s candidates=%d",
                    side,
                    price,
                    current_price,
                    len(candidates),
                )
                return price
            elif self.zones:
                price = self.market.round_price(
                    ACQUISITION_SPREAD.markup(current_price)
                )
                logger.info(
                    "[PERP_GRID] [ACQUISITION_PRICE] side=%s method=spread_markup price=%s current=%s",
                    side,
                    price,
                    current_price,
                )
                return price

        price = self.market.round_price(current_price)
        logger.info(
            "[PERP_GRID] [ACQUISITION_PRICE] side=%s method=fallback_current price=%s",
            side,
            price,
        )
        return price

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
            if zone.mode == ZoneMode.LONG and zone.order_side.is_sell():
                zone.entry_price = fill.price
            # If Short Bias and zone is buying (Close) -> it implies we sold it.
            if zone.mode == ZoneMode.SHORT and zone.order_side.is_buy():
                zone.entry_price = fill.price

        logger.info(
            f"[PERP_GRID] Acquisition Complete. Pos: {self.position_size}. AvgEntry: {self.avg_entry_price}"
        )

        self.acquisition_cloid = None
        self.acquisition_target_size = Decimal("0")

        if self.config.trigger_price and not self._is_trigger_satisfied(
            self.current_price
        ):
            logger.info(
                "[PERP_GRID] [ACQUISITION] post_fill action=wait_for_trigger current=%s trigger=%s reference=%s",
                self.current_price,
                self.config.trigger_price,
                self.trigger_reference_price,
            )
            self.state = StrategyState.WaitingForTrigger
            return

        logger.info(
            "[PERP_GRID] [ACQUISITION] post_fill action=running current=%s trigger=%s reference=%s",
            self.current_price,
            self.config.trigger_price,
            self.trigger_reference_price,
        )
        self.state = StrategyState.Running
        self.start_time = time.time()
        self.initial_entry_price = fill.price
        self.refresh_orders(ctx)

    def _handle_long_bias_fill(
        self, zone: GridZone, fill: OrderFill, ctx: StrategyContext
    ) -> None:
        """Handle fill logic for LONG bias zones."""
        pnl = Decimal("0.0")

        if fill.side.is_buy():
            # Buy to Open -> Sell to Close
            zone.entry_price = fill.price
            zone.order_side = OrderSide.SELL
            logger.info(
                f"[PERP_GRID] Z{zone.index} Buy to Open @ {fill.price} cloid={fill.cloid.as_int() if fill.cloid else None}. "
                f"Next: Sell to Close @ {zone.sell_price}. pos={self.position_size}"
            )
            zone.retry_count = 0
            self.place_zone_order(zone, ctx)
        else:
            # Sell to Close -> Buy to Open
            pnl = (fill.price - zone.entry_price) * fill.size
            zone.order_side = OrderSide.BUY
            zone.roundtrip_count += 1
            logger.info(
                f"[PERP_GRID] Z{zone.index} Sell to Close @ {fill.price} cloid={fill.cloid.as_int() if fill.cloid else None}. "
                f"PnL: {pnl:.4f}. matched_total={self.matched_profit + pnl:.4f} fees={self.total_fees:.4f}. "
                f"Next: Buy to Open @ {zone.buy_price}. pos={self.position_size}"
            )
            zone.retry_count = 0
            self.place_zone_order(zone, ctx)

        self.matched_profit += pnl

    def _handle_short_bias_fill(
        self, zone: GridZone, fill: OrderFill, ctx: StrategyContext
    ) -> None:
        """Handle fill logic for SHORT bias zones."""
        pnl = Decimal("0.0")

        if fill.side.is_sell():
            # Sell to Open -> Buy to Close
            zone.entry_price = fill.price
            zone.order_side = OrderSide.BUY
            logger.info(
                f"[PERP_GRID] Z{zone.index} Sell to Open @ {fill.price} cloid={fill.cloid.as_int() if fill.cloid else None}. "
                f"Next: Buy to Close @ {zone.buy_price}. pos={self.position_size}"
            )
            zone.retry_count = 0
            self.place_zone_order(zone, ctx)
        else:
            # Buy to Close -> Sell to Open
            pnl = (zone.entry_price - fill.price) * fill.size
            zone.order_side = OrderSide.SELL
            zone.roundtrip_count += 1
            logger.info(
                f"[PERP_GRID] Z{zone.index} Buy to Close @ {fill.price} cloid={fill.cloid.as_int() if fill.cloid else None}. "
                f"PnL: {pnl:.4f}. matched_total={self.matched_profit + pnl:.4f} fees={self.total_fees:.4f}. "
                f"Next: Sell to Open @ {zone.sell_price}. pos={self.position_size}"
            )
            zone.retry_count = 0
            self.place_zone_order(zone, ctx)

        self.matched_profit += pnl
