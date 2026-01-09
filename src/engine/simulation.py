import asyncio
import logging
from decimal import Decimal
from typing import Dict, List, Optional

import lighter

from src.config import ExchangeConfig, SimulationConfig, StrategyConfig
from src.engine.base import BaseEngine
from src.engine.context import StrategyContext
from src.model import (
    Cloid,
    LimitOrderRequest,
    OrderFill,
    OrderRequest,
    OrderSide,
    TradeRole,
)
from src.strategy.base import Strategy
from src.strategy.types import GridState, StrategySummary

logger = logging.getLogger(__name__)


class SimulationEngine(BaseEngine):
    """
    Simulation engine with configurable balance and execution modes.

    Balance Modes:
        - "real": Fetch actual balances from backend
        - "unlimited": Use unlimited/configurable fake balances

    Execution Modes:
        - "single_step": One price tick (dry run preview)
        - "continuous": Loop with live price feed and fill simulation
    """

    def __init__(
        self,
        config: StrategyConfig,
        exchange_config: ExchangeConfig,
        strategy: Strategy,
        simulation_config: Optional[SimulationConfig] = None,
    ):
        super().__init__(config, exchange_config, strategy)
        self.sim_config = simulation_config or SimulationConfig()

        # Pending orders for fill simulation (continuous mode)
        self.pending_orders: Dict[Cloid, LimitOrderRequest] = {}

        # Control flag for continuous mode
        self._running = False

        # Track current price for fill checks
        self._current_price: Decimal = Decimal("0")

    async def initialize(self):
        """
        Setup cache, fetch market info and balances based on mode.
        """
        # 1. Setup API Client (from BaseEngine)
        self._init_api_client()

        # Setup Signer Client if possible (for authenticated reads)
        self._init_signer()

        # 2. Load Markets
        await self._load_markets()

        if self.strategy_config.symbol not in self.markets:
            raise ValueError(
                f"Symbol {self.strategy_config.symbol} not found in available markets."
            )

        # 3. Context
        self.ctx = StrategyContext(self.markets)

        # 4. Initialize Balances based on mode
        if self.sim_config.balance_mode == "real":
            # Pure real balances from backend
            if self.exchange_config.account_index > 0:
                await self._fetch_account_balances()
            else:
                logger.warning("No valid account index provided, using 0 balances.")

        elif self.sim_config.balance_mode == "unlimited":
            # Unlimited balances for all assets
            self._inject_unlimited_balances()

        elif self.sim_config.balance_mode == "override":
            # Hybrid: fetch real balances first, then override specified assets
            if self.exchange_config.account_index > 0:
                await self._fetch_account_balances()
            self._apply_balance_overrides()

    def _inject_unlimited_balances(self):
        """Inject unlimited balances for simulation (all assets get unlimited_amount)."""
        if not self.ctx:
            return

        # Parse base/quote from symbol
        symbol = self.strategy_config.symbol
        base_asset, quote_asset = self._get_assets_from_symbol(symbol)

        amount = Decimal(str(self.sim_config.unlimited_amount))

        # Inject spot balances
        self.ctx.update_spot_balance(quote_asset, amount, amount)
        self.ctx.update_spot_balance(base_asset, amount, amount)

        # Also update perp margin
        self.ctx.update_perp_balance("USDC", amount, amount)

        logger.info(
            f"[SIMULATION] Injected unlimited balances: {base_asset}={amount}, {quote_asset}={amount}"
        )

    def _apply_balance_overrides(self):
        """Apply per-asset balance overrides from config. Assets not in overrides keep real balance."""
        if not self.ctx or not self.sim_config.balance_overrides:
            return

        for asset, balance in self.sim_config.balance_overrides.items():
            # Update spot balance for this asset, converting to Decimal
            d_balance = Decimal(str(balance))
            self.ctx.update_spot_balance(asset, d_balance, d_balance)
            logger.info(f"[SIMULATION] Override balance: {asset}={balance}")

            # If it's USDC, also update perp margin
            if asset.upper() == "USDC":
                self.ctx.update_perp_balance("USDC", d_balance, d_balance)

    async def run_single_step(self) -> float:
        """
        Fetches current price and runs a single on_tick.
        Returns the fetched price.
        """
        current_price_f = await self._fetch_current_price()
        if current_price_f <= 0:
            raise ValueError("Could not determine current market price.")

        current_price = Decimal(str(current_price_f))
        self._current_price = current_price

        if self.ctx:
            self.strategy.on_tick(current_price, self.ctx)
            # Process any orders placed by strategy
            self._process_order_queue()

        return current_price_f

    async def run(self):
        """
        Continuous execution mode - run loop with price fetching and fill simulation.
        """
        if self.sim_config.execution_mode != "continuous":
            # Fallback to single step
            await self.run_single_step()
            return

        self._running = True
        tick_interval = self.sim_config.tick_interval_ms / 1000.0

        logger.info(
            f"[SIMULATION] Starting continuous mode (tick_interval={tick_interval}s)"
        )

        try:
            while self._running:
                try:
                    # 1. Fetch current price
                    current_price_f = await self._fetch_current_price()
                    if current_price_f <= 0:
                        logger.warning("Failed to fetch price, retrying...")
                        await asyncio.sleep(tick_interval)
                        continue

                    current_price = Decimal(str(current_price_f))
                    self._current_price = current_price

                    # 2. Check and simulate fills for pending orders
                    if self.sim_config.simulate_fills:
                        self._check_and_simulate_fills(current_price)

                    # 3. Run strategy tick
                    if self.ctx:
                        self.strategy.on_tick(current_price, self.ctx)

                        # 4. Process new orders from strategy
                        self._process_order_queue()

                    # 5. Wait for next tick
                    await asyncio.sleep(tick_interval)

                except asyncio.CancelledError:
                    logger.info("[SIMULATION] Run loop cancelled")
                    break
                except Exception as e:
                    logger.error(f"[SIMULATION] Error in run loop: {e}")
                    await asyncio.sleep(tick_interval)

        finally:
            self._running = False
            logger.info("[SIMULATION] Continuous mode stopped")

    def stop(self):
        """Stop the continuous run loop."""
        self._running = False

    def _process_order_queue(self):
        """
        Move orders from context queue to pending orders.
        In single_step mode, orders just stay in queue for inspection.
        In continuous mode, we track them for fill simulation.
        """
        if not self.ctx:
            return

        while self.ctx.order_queue:
            order = self.ctx.order_queue.pop(0)

            if isinstance(order, LimitOrderRequest) and order.cloid:
                self.pending_orders[order.cloid] = order
                logger.info(
                    f"[SIMULATION] Order queued: {order.side} {order.sz} @ {order.price} (cloid={order.cloid.as_int()})"
                )

    def _check_and_simulate_fills(self, current_price: Decimal):
        """
        Check pending orders and simulate fills if price has crossed order price.

        - BUY orders fill when current_price <= order.price
        - SELL orders fill when current_price >= order.price
        """
        if not self.ctx:
            return

        filled_cloids: List[Cloid] = []

        for cloid, order in self.pending_orders.items():
            should_fill = False

            if order.side == OrderSide.BUY and current_price <= order.price:
                should_fill = True
            elif order.side == OrderSide.SELL and current_price >= order.price:
                should_fill = True

            if should_fill:
                # Simulate the fill
                fill_price = order.price  # Limit order fills at order price (Decimal)
                fee = order.sz * fill_price * Decimal(str(self.sim_config.fee_rate))

                fill = OrderFill(
                    side=order.side,
                    size=order.sz,
                    price=fill_price,
                    fee=fee,
                    role=TradeRole.MAKER,  # Limit orders are maker
                    cloid=cloid,
                    reduce_only=order.reduce_only,
                )

                logger.info(
                    f"[SIMULATION] Order filled: {order.side} {order.sz} @ {fill_price} (cloid={cloid.as_int()})"
                )

                # Notify strategy
                self.strategy.on_order_filled(fill, self.ctx)

                # Update simulated balances
                self._update_balances_on_fill(order, fill)

                filled_cloids.append(cloid)

        # Remove filled orders
        for cloid in filled_cloids:
            del self.pending_orders[cloid]

    def _update_balances_on_fill(self, order: LimitOrderRequest, fill: OrderFill):
        """Update simulated balances after a fill."""
        if not self.ctx:
            return

        # Parse base/quote from symbol
        symbol = order.symbol
        base_asset, quote_asset = self._get_assets_from_symbol(symbol)

        quote_amount = fill.size * fill.price

        if fill.side == OrderSide.BUY:
            # Bought base, spent quote
            current_base = self.ctx.get_spot_available(base_asset)
            current_quote = self.ctx.get_spot_available(quote_asset)

            self.ctx.update_spot_balance(
                base_asset, current_base + fill.size, current_base + fill.size
            )
            self.ctx.update_spot_balance(
                quote_asset,
                current_quote - quote_amount - fill.fee,
                current_quote - quote_amount - fill.fee,
            )
        else:
            # Sold base, received quote
            current_base = self.ctx.get_spot_available(base_asset)
            current_quote = self.ctx.get_spot_available(quote_asset)

            self.ctx.update_spot_balance(
                base_asset, current_base - fill.size, current_base - fill.size
            )
            self.ctx.update_spot_balance(
                quote_asset,
                current_quote + quote_amount - fill.fee,
                current_quote + quote_amount - fill.fee,
            )

    def get_summary(self) -> Optional[StrategySummary]:
        if self.ctx:
            return self.strategy.get_summary(self.ctx)
        return None

    def get_grid_state(self) -> Optional[GridState]:
        if self.ctx:
            return self.strategy.get_grid_state(self.ctx)
        return None

    def get_orders(self) -> List[OrderRequest]:
        """Get all pending orders (both queued and tracked)."""
        orders: List[OrderRequest] = []

        # Orders still in context queue
        if self.ctx:
            orders.extend(list(self.ctx.order_queue))

        # Pending orders being tracked
        orders.extend(list(self.pending_orders.values()))

        return orders

    def get_current_price(self) -> Decimal:
        """Get the last fetched price."""
        return self._current_price

    async def cleanup(self):
        """Clean up simulation resources."""
        self._running = False
        await super().cleanup()

    async def _fetch_current_price(self) -> float:
        """Fetch price from orderbook midprice."""
        if not self.api_client:
            return 0.0
        try:
            market_id = self.markets[self.strategy_config.symbol].market_id
            order_api = lighter.OrderApi(self.api_client)
            ob = await order_api.order_book_orders(market_id=market_id, limit=10)
            if isinstance(ob, tuple):
                ob = ob[0]

            best_ask = float(ob.asks[0].price) if ob.asks else 0.0
            best_bid = float(ob.bids[0].price) if ob.bids else 0.0

            if best_ask > 0 and best_bid > 0:
                return (best_ask + best_bid) / 2.0
            return best_ask or best_bid
        except Exception as e:
            logger.error(f"Price fetch failed: {e}")
            return 0.0
