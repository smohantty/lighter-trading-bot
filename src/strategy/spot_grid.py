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

logger = logging.getLogger("src.strategy.spot_grid")


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
                self.config.lower_price,
                self.config.upper_price,
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
                self.config.lower_price,
                self.config.upper_price,
                self.config.grid_count,
            )

        # Position Tracking
        self.inventory_base = Decimal("0")
        self.inventory_quote = Decimal("0")

        self.start_time = time.time()
        self.initial_entry_price: Optional[Decimal] = None
        self.trigger_reference_price: Optional[Decimal] = None

        self.acquisition_cloid: Optional[Cloid] = None
        self.acquisition_target_size: Decimal = Decimal("0")

        # Acquisition State Tracking
        self.initial_avail_base = Decimal("0")
        self.initial_avail_quote = Decimal("0")
        self.required_base = Decimal("0")
        self.required_quote = Decimal("0")

        self.current_price = Decimal("0")
        self.market: MarketInfo = None  # type: ignore

    # =========================================================================
    # STRATEGY LIFECYCLE (Base Class Interface)
    # =========================================================================

    def on_tick(self, price: Decimal, ctx: StrategyContext):
        self.current_price = price
        if self.state == StrategyState.Initializing:
            self.initialize_zones(price, ctx)
        elif self.state == StrategyState.WaitingForTrigger:
            if self.config.trigger_price and self.trigger_reference_price:
                if common.check_trigger(
                    price, self.config.trigger_price, self.trigger_reference_price
                ):
                    logger.info(f"[SPOT_GRID] [Triggered] at {price}")
                    self._transition_to_running(ctx, price)
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
                    logger.info(
                        f"[ORDER_FILLED][SPOT_GRID] GRID_ZONE_{idx} cloid: {fill.cloid.as_int()} Filled BUY {fill.size} {self.base_asset} @ {fill.price}"
                    )
                    self.inventory_base += fill.size
                    self.inventory_quote -= fill.size * fill.price
                    # Flip to SELL at upper price
                    zone.order_side = OrderSide.SELL
                    zone.entry_price = fill.price
                    self.place_zone_order(zone, ctx)
                else:
                    # Sell Fill
                    pnl = (fill.price - zone.entry_price) * fill.size
                    logger.info(
                        f"[ORDER_FILLED][SPOT_GRID] GRID_ZONE_{idx} cloid: {fill.cloid.as_int()} Filled SELL {fill.size} {self.base_asset} @ {fill.price:.{self.market.price_decimals}f}. PnL: {pnl:.4f}"
                    )
                    self.matched_profit += pnl
                    self.inventory_base = max(
                        Decimal("0"), self.inventory_base - fill.size
                    )
                    self.inventory_quote += fill.size * fill.price
                    zone.roundtrip_count += 1

                    # Flip to BUY at lower price
                    zone.order_side = OrderSide.BUY
                    zone.entry_price = Decimal("0.0")
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
            range_low=self.config.lower_price,
            range_high=self.config.upper_price,
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
                self.config.lower_price,
                self.config.upper_price,
                self.config.spread_bips,
            )
            # Update grid_count to match actual generated levels
            self.grid_count = len(prices)
        else:
            assert self.config.grid_count is not None
            prices = common.calculate_grid_prices(
                self.config.grid_type,
                self.config.lower_price,
                self.config.upper_price,
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
        market_info = ctx.market_info(self.config.symbol)
        if not market_info:
            raise ValueError(f"No market info for {self.config.symbol}")
        self.market = market_info

        # Calculate Grid
        self.zones, required_base, required_quote = self.calculate_grid_plan(
            initial_price
        )

        # Seed inventory
        avail_base = ctx.get_spot_available(self.base_asset)
        avail_quote = ctx.get_spot_available(self.quote_asset)

        initial_price = (
            self.config.trigger_price if self.config.trigger_price else initial_price
        )

        # Upfront Total Investment Validation
        total_wallet_value = (avail_base * initial_price) + avail_quote
        if total_wallet_value < self.total_investment:
            msg = f"Insufficient Total Portfolio Value! Required: {self.total_investment:.2f}, Have approx: {total_wallet_value:.2f} (Base: {avail_base}, Quote: {avail_quote})"
            logger.error(f"[SPOT_GRID] {msg}")
            raise ValueError(msg)

        self.required_base = required_base
        self.required_quote = required_quote
        self.initial_avail_base = avail_base
        self.initial_avail_quote = avail_quote

        logger.info(
            f"[SPOT_GRID] Setup completed. Required: {required_base:.4f} {self.base_asset}, {required_quote:.2f} {self.quote_asset}"
        )
        self.inventory_base = min(avail_base, required_base)
        self.inventory_quote = min(avail_quote, required_quote)

        # Capture Initial Equity (managed assets only)
        self.initial_equity = (
            self.inventory_base * initial_price
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
    ) -> None:
        base_deficit = total_base_required - available_base
        quote_deficit = total_quote_required - available_quote

        if base_deficit > 0:
            # Case 1: Not enough base asset. Need to BUY base asset.

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

        if self.config.trigger_price:
            # Passive Wait Mode
            logger.info(
                "[SPOT_GRID] Assets sufficient. Entering WaitingForTrigger state."
            )
            self.trigger_reference_price = self.current_price
            self.state = StrategyState.WaitingForTrigger
        else:
            # No Trigger, Assets OK -> Running
            logger.info("[SPOT_GRID] Assets verified. Starting Grid.")
            self._transition_to_running(ctx, self.current_price)

    # =========================================================================
    # ORDER MANAGEMENT
    # =========================================================================

    def place_zone_order(self, zone: GridZone, ctx: StrategyContext):
        """Place an order for a zone based on its current state."""
        if not self.market:
            return

        if zone.cloid is not None:
            return  # Already has an order

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

        logger.info(
            f"[ORDER_REQUEST] [SPOT_GRID] GRID_ZONE_{zone.index} cloid: {zone.cloid.as_int()} LIMIT {side} {size} {self.base_asset} @ {price}"
        )

    def refresh_orders(self, ctx: StrategyContext):
        """Place orders for all zones that don't have one and haven't exceeded max retries."""
        for zone in self.zones:
            if zone.cloid is None:
                if zone.retry_count < MAX_ORDER_RETRIES:
                    self.place_zone_order(zone, ctx)
                else:
                    # Optional: Log zombie state occasionally?
                    pass

    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================

    def _transition_to_running(self, ctx: StrategyContext, price: Decimal):
        self.initial_entry_price = price
        self.initial_equity = (self.inventory_base * price) + self.inventory_quote
        self.state = StrategyState.Running
        self.refresh_orders(ctx)

    def _calculate_acquisition_price(
        self, side: OrderSide, current_price: Decimal
    ) -> Decimal:
        """Calculate optimal price for acquiring assets during initial setup."""
        if self.config.trigger_price:
            return self.market.round_price(self.config.trigger_price)

        if side == OrderSide.BUY:
            # Find nearest level LOWER than market to buy at (Limit Buy below market)
            candidates = [
                z.buy_price for z in self.zones if z.buy_price < current_price
            ]
            if candidates:
                return self.market.round_price(max(candidates))
            elif self.zones:
                # Fallback: Price is below grid. Return markdown of current price for BUY.
                return self.market.round_price(
                    ACQUISITION_SPREAD.markdown(current_price)
                )
        else:  # SELL
            # Find nearest level ABOVE market to sell at (Limit Sell above market)
            candidates = [
                z.sell_price for z in self.zones if z.sell_price > current_price
            ]
            if candidates:
                return self.market.round_price(min(candidates))
            elif self.zones:
                # Fallback: Price is above grid. Return markup of current price for SELL.
                return self.market.round_price(ACQUISITION_SPREAD.markup(current_price))

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

        self._transition_to_running(ctx, fill.price)
