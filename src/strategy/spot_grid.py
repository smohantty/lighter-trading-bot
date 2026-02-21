import logging
import time
from datetime import timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from src.constants import (
    ACQUISITION_SPREAD,
    FEE_BUFFER,
    INVESTMENT_BUFFER_SPOT,
    MAX_ORDER_RETRIES,
)
from src.engine.context import MarketInfo, StrategyContext
from src.model import (
    Cloid,
    LimitOrderRequest,
    OrderFailure,
    OrderFill,
    OrderSide,
)
from src.strategy import common
from src.strategy.base import Strategy
from src.strategy.types import (
    GridState,
    GridZone,
    SpotGridSummary,
    StrategyState,
    ZoneInfo,
)

logger = logging.getLogger("SpotGrid")


class SpotGridStrategy(Strategy):
    # =========================================================================
    # INITIALIZATION
    # =========================================================================

    def __init__(self, config):
        self.config = config
        self.symbol = config.symbol
        self.total_investment = config.total_investment

        # Spot grid specific: base/quote splitting
        try:
            self.base_asset, self.quote_asset = self.config.symbol.split("/")
        except ValueError:
            logger.error(
                f"Invalid symbol format: {self.config.symbol}. Expected BASE/QUOTE"
            )
            self.base_asset = self.config.symbol
            self.quote_asset = "USDC"

        self.zones: List[GridZone] = []
        self.state = StrategyState.Initializing
        self.active_order_map: Dict[Cloid, GridZone] = {}  # Cloid -> GridZone

        # Performance tracking
        self.matched_profit = Decimal("0")
        self.total_fees = Decimal("0")
        self.initial_equity = Decimal("0")

        if self.config.spread_bips:
            # Calculate grid count based on spread
            prices = common.calculate_grid_prices_by_spread(
                self.config.grid_range_low,
                self.config.grid_range_high,
                self.config.spread_bips,
            )
            # grid_count describes levels, so len(prices)
            self.grid_count = len(prices)

            # grid_spacing_pct for Spread is basically spread_bips / 100
            # but let's be consistent and store it as one value or just calculate
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

        # Position Tracking
        self.inventory_base = Decimal("0")
        self.inventory_quote = Decimal("0")

        self.start_time = time.time()
        self.initial_entry_price: Optional[Decimal] = None
        self.trigger_reference_price: Optional[Decimal] = None
        self.trigger_activated = False

        self.acquisition_cloid: Optional[Cloid] = None
        self.acquisition_target_size: Decimal = Decimal("0")

        # Acquisition State Tracking
        self.initial_avail_base = Decimal("0")
        self.initial_avail_quote = Decimal("0")
        self.required_base = Decimal("0")
        self.required_quote = Decimal("0")

        self.current_price = Decimal("0")
        self.market: MarketInfo = None  # type: ignore
        self._last_dead_zone_warning_ts = 0.0

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
                        "[SPOT_GRID] Triggered at price=%s trigger_price=%s reference_price=%s",
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
            # Acquisition Fill
            if (
                self.state == StrategyState.AcquiringAssets
                and fill.cloid == self.acquisition_cloid
            ):
                self._handle_acquisition_fill(fill, ctx)
                return

            # Grid Fill
            if fill.cloid in self.active_order_map:
                zone = self.active_order_map.pop(fill.cloid)
                idx = zone.index
                zone.cloid = None
                self.total_fees += fill.fee
                zone.retry_count = 0

                if zone.order_side.is_buy():
                    # Buy Fill
                    self.inventory_base += fill.size
                    self.inventory_quote -= fill.size * fill.price
                    # Flip to SELL at upper price
                    zone.order_side = OrderSide.SELL
                    zone.entry_price = fill.price
                    logger.info(
                        f"[ORDER_FILLED][SPOT_GRID] Z{idx} BUY {fill.size} {self.base_asset} @ {fill.price} "
                        f"cloid={fill.cloid.as_int()}. Next: SELL @ {zone.sell_price}. "
                        f"inv_base={self.inventory_base} inv_quote={self.inventory_quote:.2f}"
                    )
                    self.place_zone_order(zone, ctx)
                else:
                    # Sell Fill
                    pnl = (fill.price - zone.entry_price) * fill.size
                    self.matched_profit += pnl
                    self.inventory_base = max(
                        Decimal("0"), self.inventory_base - fill.size
                    )
                    self.inventory_quote += fill.size * fill.price
                    zone.roundtrip_count += 1

                    # Flip to BUY at lower price
                    zone.order_side = OrderSide.BUY
                    zone.entry_price = Decimal("0.0")
                    logger.info(
                        f"[ORDER_FILLED][SPOT_GRID] Z{idx} SELL {fill.size} {self.base_asset} @ {fill.price:.{self.market.price_decimals}f} "
                        f"cloid={fill.cloid.as_int()}. PnL: {pnl:.4f}. matched_total={self.matched_profit:.4f} fees={self.total_fees:.4f}. "
                        f"Next: BUY @ {zone.buy_price}. inv_base={self.inventory_base} inv_quote={self.inventory_quote:.2f}"
                    )
                    self.place_zone_order(zone, ctx)

    def on_order_failed(self, failure: OrderFailure, ctx: StrategyContext):
        if self.state == StrategyState.Initializing:
            return

        cloid = failure.cloid
        if cloid in self.active_order_map:
            zone = self.active_order_map.pop(cloid)
            idx = zone.index
            zone.cloid = None

            logger.warning(
                f"[ORDER_FAILED][SPOT_GRID] GRID_ZONE_{idx} cloid: {cloid.as_int()} "
                f"reason: {failure.failure_reason}. Retry count: {zone.retry_count + 1}/{MAX_ORDER_RETRIES}"
            )

            zone.retry_count += 1

    def get_summary(self, ctx: StrategyContext) -> SpotGridSummary:
        if self.state == StrategyState.Initializing:
            raise ValueError("Strategy not initialized")

        if self.state == StrategyState.Running:
            current_equity = (
                self.inventory_base * self.current_price
            ) + self.inventory_quote
            total_profit = current_equity - self.initial_equity - self.total_fees
        else:
            total_profit = Decimal("0")

        return SpotGridSummary(
            symbol=self.symbol,
            state=self.state.name,
            uptime=common.format_uptime(
                timedelta(seconds=time.time() - self.start_time)
            ),
            position_size=self.inventory_base,
            matched_profit=self.matched_profit,
            total_profit=total_profit,
            total_fees=self.total_fees,
            initial_entry_price=self.initial_entry_price,
            grid_count=len(self.zones),
            grid_range_low=self.config.grid_range_low,
            grid_range_high=self.config.grid_range_high,
            grid_spacing_pct=self.grid_spacing_pct,
            roundtrips=sum(z.roundtrip_count for z in self.zones),
            base_balance=self.inventory_base,
            quote_balance=self.inventory_quote,
        )

    def get_grid_state(self, ctx: StrategyContext) -> GridState:
        if self.state == StrategyState.Initializing:
            raise ValueError("Strategy not initialized")

        return GridState(
            symbol=self.symbol,
            strategy_type="spot_grid",
            grid_bias=None,
            zones=[
                ZoneInfo(
                    index=z.index,
                    buy_price=z.buy_price,
                    sell_price=z.sell_price,
                    size=z.size,
                    order_side=str(z.order_side),
                    has_order=z.cloid is not None,
                    is_reduce_only=False,
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
    ) -> tuple[List[GridZone], Decimal, Decimal]:
        if self.config.spread_bips:
            prices = common.calculate_grid_prices_by_spread(
                self.config.grid_range_low,
                self.config.grid_range_high,
                self.config.spread_bips,
            )
            # Update grid_count to match actual generated levels
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

        adjusted_investment = INVESTMENT_BUFFER_SPOT.markdown(self.total_investment)
        investment_per_zone_quote = adjusted_investment / Decimal(self.grid_count - 1)

        min_order_size = self.market.min_quote_amount
        if investment_per_zone_quote < min_order_size:
            msg = f"Investment per zone ({investment_per_zone_quote:.2f} {self.quote_asset}) is less than minimum order value ({min_order_size}). Increase total_investment or decrease grid_count."
            logger.error(f"[SPOT_GRID] {msg}")
            raise ValueError(msg)

        zones = []
        required_base = Decimal("0")
        required_quote = Decimal("0")

        initial_price = (
            self.config.trigger_price if self.config.trigger_price else reference_price
        )

        for i in range(self.grid_count - 1):
            zone_buy_price = prices[i]
            zone_sell_price = prices[i + 1]
            size = self.market.round_size(investment_per_zone_quote / zone_buy_price)

            if zone_buy_price > initial_price:
                order_side = OrderSide.SELL
                required_base += size
                entry_price = zone_buy_price
            else:
                order_side = OrderSide.BUY
                required_quote += size * zone_buy_price
                entry_price = Decimal("0.0")

            zones.append(
                GridZone(
                    index=i,
                    buy_price=zone_buy_price,
                    sell_price=zone_sell_price,
                    size=size,
                    order_side=order_side,
                    mode=None,
                    entry_price=entry_price,
                    roundtrip_count=0,
                )
            )

        return zones, required_base, required_quote

    def initialize_zones(self, initial_price: Decimal, ctx: StrategyContext):
        self.current_price = initial_price
        startup_price = initial_price
        market_info = ctx.market_info(self.config.symbol)
        if not market_info:
            raise ValueError(f"No market info for {self.config.symbol}")
        self.market = market_info

        # Calculate Grid
        self.zones, required_base, required_quote = self.calculate_grid_plan(
            startup_price
        )

        # Seed inventory
        avail_base = ctx.get_spot_available(self.base_asset)
        avail_quote = ctx.get_spot_available(self.quote_asset)

        planning_price = (
            self.config.trigger_price if self.config.trigger_price else startup_price
        )

        # Upfront Total Investment Validation
        total_wallet_value = (avail_base * planning_price) + avail_quote
        if total_wallet_value < self.total_investment:
            msg = f"Insufficient Total Portfolio Value! Required: {self.total_investment:.2f}, Have approx: {total_wallet_value:.2f} (Base: {avail_base}, Quote: {avail_quote})"
            logger.error(f"[SPOT_GRID] {msg}")
            raise ValueError(msg)

        self.required_base = required_base
        self.required_quote = required_quote
        self.initial_avail_base = avail_base
        self.initial_avail_quote = avail_quote

        logger.info(
            "[SPOT_GRID] Setup completed. startup_current_price=%s planning_price=%s Required: %.4f %s, %.2f %s",
            startup_price,
            planning_price,
            required_base,
            self.base_asset,
            required_quote,
            self.quote_asset,
        )

        # Log grid plan for production debugging
        holding = sum(1 for z in self.zones if z.order_side.is_sell())
        waiting = sum(1 for z in self.zones if z.order_side.is_buy())
        logger.info(
            "[SPOT_GRID] [GRID_PLAN] zones=%d holding=%d waiting=%d spacing_pct=(%s, %s) price=%s range=[%s, %s]",
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
                "[SPOT_GRID] [GRID_PLAN] zone=%d buy=%s sell=%s size=%s side=%s entry=%s",
                z.index,
                z.buy_price,
                z.sell_price,
                z.size,
                z.order_side,
                z.entry_price,
            )
        self.inventory_base = min(avail_base, required_base)
        self.inventory_quote = min(avail_quote, required_quote)

        # Capture Initial Equity (managed assets only)
        self.initial_equity = (
            self.inventory_base * planning_price
        ) + self.inventory_quote

        # Check Assets & Rebalance
        self.check_initial_acquisition(
            ctx, required_base, required_quote, avail_base, avail_quote
        )

    def check_initial_acquisition(
        self,
        ctx: StrategyContext,
        total_base_required: Decimal,
        total_quote_required: Decimal,
        available_base: Decimal,
        available_quote: Decimal,
        allow_wait_for_trigger: bool = True,
    ) -> None:
        if self.config.trigger_price and self.trigger_reference_price is None:
            self.trigger_reference_price = self.current_price
            logger.info(
                "[SPOT_GRID] Trigger reference initialized trigger_price=%s reference_price=%s",
                self.config.trigger_price,
                self.trigger_reference_price,
            )

        self.inventory_base = min(available_base, total_base_required)
        self.inventory_quote = min(available_quote, total_quote_required)

        base_deficit = total_base_required - available_base
        quote_deficit = total_quote_required - available_quote

        if base_deficit > 0:
            # Case 1: Not enough base asset. Need to BUY base asset.
            should_acquire_now, policy_reason = self._acquisition_policy_decision(
                OrderSide.BUY, allow_wait_for_trigger
            )
            logger.info(
                "[SPOT_GRID] [ACQUISITION_POLICY] side=%s action=%s reason=%s current=%s trigger=%s reference=%s",
                OrderSide.BUY,
                "acquire_now" if should_acquire_now else "wait_for_trigger",
                policy_reason,
                self.current_price,
                self.config.trigger_price,
                self.trigger_reference_price,
            )
            if not should_acquire_now:
                self.state = StrategyState.WaitingForTrigger
                return

            base_deficit = FEE_BUFFER.markup(base_deficit)

            base_deficit = max(base_deficit, self.market.min_base_amount)
            base_deficit = self.market.round_size(base_deficit)

            acquisition_price = self._calculate_acquisition_price(
                OrderSide.BUY, self.current_price
            )

            estimated_cost = base_deficit * acquisition_price

            if available_quote < estimated_cost:
                msg = f"Insufficient Quote Balance for acquisition! Need ~{estimated_cost:.2f} {self.quote_asset}, Have {available_quote:.2f} {self.quote_asset}. Base Deficit: {base_deficit} {self.base_asset}"
                logger.error(f"[SPOT_GRID] {msg}")
                raise ValueError(msg)

            self.initial_avail_base = available_base
            self.initial_avail_quote = available_quote
            cloid = ctx.place_order(
                LimitOrderRequest(
                    symbol=self.config.symbol,
                    side=OrderSide.BUY,
                    price=acquisition_price,
                    sz=base_deficit,
                    reduce_only=False,
                )
            )

            self.state = StrategyState.AcquiringAssets
            self.acquisition_cloid = cloid
            self.acquisition_target_size = base_deficit

            logger.info(
                f"[ORDER_REQUEST] [SPOT_GRID] [ACQUISITION] cloid: {cloid.as_int()}, LIMIT BUY {base_deficit} {self.base_asset} @ {acquisition_price}"
            )

            return

        elif quote_deficit > 0:
            should_acquire_now, policy_reason = self._acquisition_policy_decision(
                OrderSide.SELL, allow_wait_for_trigger
            )
            logger.info(
                "[SPOT_GRID] [ACQUISITION_POLICY] side=%s action=%s reason=%s current=%s trigger=%s reference=%s",
                OrderSide.SELL,
                "acquire_now" if should_acquire_now else "wait_for_trigger",
                policy_reason,
                self.current_price,
                self.config.trigger_price,
                self.trigger_reference_price,
            )
            if not should_acquire_now:
                self.state = StrategyState.WaitingForTrigger
                return

            acquisition_price = self._calculate_acquisition_price(
                OrderSide.SELL, self.current_price
            )

            base_to_sell = quote_deficit / acquisition_price
            base_to_sell = max(base_to_sell, self.market.min_base_amount)
            base_to_sell = self.market.round_size(base_to_sell)

            estimated_proceeds = base_to_sell * acquisition_price
            logger.info(
                f"[SPOT_GRID] Quote deficit detected: deficit={quote_deficit} {self.quote_asset}, need to sell ~{base_to_sell} {self.base_asset} (~${estimated_proceeds:.2f}) @ price {acquisition_price}"
            )

            if available_base < base_to_sell:
                msg = f"Insufficient Base Balance for rebalancing! Need to sell {base_to_sell} {self.base_asset}, Have {available_base} {self.base_asset}. Quote Deficit: {quote_deficit} {self.quote_asset}"
                logger.error(f"[SPOT_GRID] {msg}")
                raise ValueError(msg)

            self.initial_avail_base = available_base
            self.initial_avail_quote = available_quote
            logger.info(
                f"[ORDER_REQUEST] [SPOT_GRID] [ACQUISITION] LIMIT SELL {base_to_sell} {self.base_asset} @ {acquisition_price}"
            )

            cloid = ctx.place_order(
                LimitOrderRequest(
                    symbol=self.config.symbol,
                    side=OrderSide.SELL,
                    price=acquisition_price,
                    sz=base_to_sell,
                    reduce_only=False,
                )
            )

            self.state = StrategyState.AcquiringAssets
            self.acquisition_cloid = cloid
            self.acquisition_target_size = base_to_sell
            return

        # No Deficit (or negligible)
        if self.inventory_base > 0:
            logger.info(
                f"[SPOT_GRID] Initial Position Size: {self.inventory_base} {self.base_asset}. (No Rebalancing Needed)"
            )

        if (
            self.config.trigger_price
            and allow_wait_for_trigger
            and not self._is_trigger_satisfied(self.current_price)
        ):
            # Passive Wait Mode
            direction = (
                "above" if self.config.trigger_price > self.current_price else "below"
            )
            logger.info(
                "[SPOT_GRID] Assets sufficient. Waiting for Trigger. trigger_price=%s direction=%s reference_price=%s",
                self.config.trigger_price,
                direction,
                self.trigger_reference_price,
            )
            self.state = StrategyState.WaitingForTrigger
        else:
            # No Trigger, Assets OK -> Running
            if self.config.trigger_price:
                logger.info(
                    "[SPOT_GRID] Assets verified. Trigger satisfied. Starting Grid. trigger_price=%s reference_price=%s current_price=%s",
                    self.config.trigger_price,
                    self.trigger_reference_price,
                    self.current_price,
                )
            else:
                logger.info("[SPOT_GRID] Assets verified. Starting Grid.")
            self._transition_to_running(ctx, self.current_price)

    # =========================================================================
    # ORDER MANAGEMENT
    # =========================================================================

    def place_zone_order(self, zone: GridZone, ctx: StrategyContext) -> bool:
        """Place an order for a zone based on its current state."""
        if not self.market:
            return False

        if zone.cloid is not None:
            return False  # Already has an order

        side = zone.order_side
        price = zone.buy_price if side.is_buy() else zone.sell_price
        size = zone.size

        if side.is_sell():
            size = self.market.round_size(FEE_BUFFER.markdown(size))

        zone.cloid = ctx.place_order(
            LimitOrderRequest(
                symbol=self.config.symbol,
                side=side,
                price=price,
                sz=size,
                reduce_only=False,
            )
        )

        self.active_order_map[zone.cloid] = zone

        logger.debug(
            "[ORDER_REQUEST] [SPOT_GRID] GRID_ZONE_%s cloid=%s LIMIT %s %s %s @ %s",
            zone.index,
            zone.cloid.as_int(),
            side,
            size,
            self.base_asset,
            price,
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
                "[SPOT_GRID] Activated zone orders count=%d active_orders=%d",
                placed,
                len(self.active_order_map),
            )
        if dead_zones and (time.time() - self._last_dead_zone_warning_ts) >= 60:
            logger.warning(
                "[SPOT_GRID] Zones stuck at retry limit count=%d max_retries=%d",
                dead_zones,
                MAX_ORDER_RETRIES,
            )
            self._last_dead_zone_warning_ts = time.time()

    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================

    def _transition_to_running(self, ctx: StrategyContext, price: Decimal):
        self.initial_entry_price = price
        self.initial_equity = (self.inventory_base * price) + self.inventory_quote
        self.state = StrategyState.Running
        self.start_time = time.time()
        self.refresh_orders(ctx)

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
        available_base = ctx.get_spot_available(self.base_asset)
        available_quote = ctx.get_spot_available(self.quote_asset)
        logger.info(
            "[SPOT_GRID] [TRIGGER_RESUME] recompute deficits available_base=%s available_quote=%s required_base=%s required_quote=%s",
            available_base,
            available_quote,
            self.required_base,
            self.required_quote,
        )
        self.check_initial_acquisition(
            ctx,
            self.required_base,
            self.required_quote,
            available_base,
            available_quote,
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
                    "[SPOT_GRID] [ACQUISITION_PRICE] side=%s method=nearest_level price=%s current=%s candidates=%d",
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
                    "[SPOT_GRID] [ACQUISITION_PRICE] side=%s method=spread_markdown price=%s current=%s",
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
                    "[SPOT_GRID] [ACQUISITION_PRICE] side=%s method=nearest_level price=%s current=%s candidates=%d",
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
                    "[SPOT_GRID] [ACQUISITION_PRICE] side=%s method=spread_markup price=%s current=%s",
                    side,
                    price,
                    current_price,
                )
                return price

        logger.info(
            "[SPOT_GRID] [ACQUISITION_PRICE] side=%s method=fallback_current price=%s",
            side,
            current_price,
        )
        return current_price

    def _handle_acquisition_fill(self, fill: OrderFill, ctx: StrategyContext) -> None:
        self.total_fees += fill.fee

        if fill.side.is_buy():
            new_real_base = self.initial_avail_base + fill.size
            new_real_quote = self.initial_avail_quote - (fill.size * fill.price)
        else:
            new_real_base = self.initial_avail_base - fill.size
            new_real_quote = self.initial_avail_quote + (fill.size * fill.price)

        new_real_base = max(Decimal("0"), new_real_base)
        new_real_quote = max(Decimal("0"), new_real_quote)

        self.inventory_base = min(new_real_base, self.required_base)
        self.inventory_quote = min(new_real_quote, self.required_quote)

        logger.info(
            f"[SPOT_GRID] [ACQUISITION] Complete. Real Avail: {new_real_base:.4f} {self.base_asset}, {new_real_quote:.2f} {self.quote_asset}. Inventory Set: {self.inventory_base}."
        )

        self.acquisition_cloid = None
        self.acquisition_target_size = Decimal("0")

        if self.config.trigger_price and not self._is_trigger_satisfied(
            self.current_price
        ):
            logger.info(
                "[SPOT_GRID] [ACQUISITION] post_fill action=wait_for_trigger current=%s trigger=%s reference=%s",
                self.current_price,
                self.config.trigger_price,
                self.trigger_reference_price,
            )
            self.state = StrategyState.WaitingForTrigger
            return

        logger.info(
            "[SPOT_GRID] [ACQUISITION] post_fill action=running current=%s trigger=%s reference=%s",
            self.current_price,
            self.config.trigger_price,
            self.trigger_reference_price,
        )
        self._transition_to_running(ctx, fill.price)
