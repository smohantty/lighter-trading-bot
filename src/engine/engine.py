import asyncio
import logging
import time
from collections import OrderedDict
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, cast

import lighter

import src.broadcast.types as btypes
from src.broadcast.server import StatusBroadcaster
from src.config import ExchangeConfig, StrategyConfig
from src.constants import (
    COMPLETED_CLOID_CACHE_MAX_SIZE,
    COMPLETED_CLOID_CACHE_TTL_SECONDS,
    DEGRADED_MODE_COOLDOWN_SECONDS,
    MAX_BATCH_SIZE,
    OID_CLOID_CACHE_MAX_SIZE,
    OID_CLOID_CACHE_TTL_SECONDS,
    ORDER_LOST_TIMEOUT_SECONDS,
    ORDER_PROPAGATION_GRACE_SECONDS,
    RECONCILIATION_ENABLED,
    RECONCILIATION_HEALTHY_SNAPSHOTS_TO_EXIT_DEGRADED,
    RECONCILIATION_INTERVAL_SECONDS,
    UNRESOLVED_OID_FILL_ALERT_THRESHOLD,
)
from src.engine.base import BaseEngine
from src.engine.context import StrategyContext
from src.logging_utils import env_bool
from src.model import (
    CancelOrderRequest,
    Cloid,
    LimitOrderRequest,
    MarketOrderRequest,
    OrderFailure,
    OrderFill,
    OrderSide,
    PendingOrder,
    TradeRole,
)
from src.strategy.base import Strategy
from src.strategy.types import PerpGridSummary, SpotGridSummary

logger = logging.getLogger("LiveEngine")

LOG_VERBOSE_ORDER_STREAM = env_bool("LIGHTER_LOG_VERBOSE_ORDER_STREAM", default=False)
LOG_RECONCILIATION_HEALTHY = env_bool(
    "LIGHTER_LOG_RECONCILIATION_HEALTHY", default=False
)


class Engine(BaseEngine):
    def __init__(
        self,
        config: StrategyConfig,
        exchange_config: ExchangeConfig,
        strategy: Strategy,
        broadcaster: Optional[StatusBroadcaster] = None,
    ):
        super().__init__(config, exchange_config, strategy)
        self.broadcaster = broadcaster

        # Clients (api_client and signer_client inherited from BaseEngine)
        self.ws_client: Optional[lighter.WsClient] = None
        self.running = False

        self.event_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._shutdown_event = asyncio.Event()

        # Track last price per symbol to avoid duplicate on_tick calls
        self.last_price: Dict[str, Decimal] = {}

        # Partial fill tracking (mirroring Rust SDK)
        self.pending_orders: Dict[Cloid, PendingOrder] = {}
        self.completed_cloids: OrderedDict[Cloid, float] = OrderedDict()
        self.last_completed_cloid_cache_log_ts = 0.0
        self.oid_to_cloid: OrderedDict[int, Tuple[Cloid, float]] = OrderedDict()
        self.unresolved_oid_fill_counts: Dict[int, int] = {}
        self.unresolved_oid_fills_total = 0
        # Reconciliation guard: require repeated misses before declaring lost.
        self.reconciliation_miss_counts: Dict[Cloid, int] = {}
        # Degraded mode gate: pause exchange submissions until snapshots recover.
        self.degraded_mode = False
        self.degraded_until_ts = 0.0
        self.healthy_reconciliation_snapshots = 0
        self.last_degraded_skip_log_ts = 0.0
        self.last_untracked_orders_log_ts = 0.0
        self._stop_called = False

    async def initialize(self):
        logger.info("Initializing Engine...")

        # 1. Setup API Client (from BaseEngine)
        self._init_api_client()

        # 2. Validate Account Index (already set in BaseEngine.__init__)
        if not self.account_index:
            raise ValueError("Account Index must be provided in the configuration.")
        logger.info("Using account index=%s", self.account_index)

        # 3. Setup Signer Client
        self._init_signer()

        # 4. Load Metadata (Markets)
        await self._load_markets()

        target_symbol = self.strategy_config.symbol
        if target_symbol not in self.markets:
            raise ValueError(
                f"Symbol {target_symbol} not found in Lighter markets: {list(self.markets.keys())}"
            )

        self.ctx = StrategyContext(self.markets)

        # 5. Fetch Account Balances
        await self._fetch_account_balances()

        market_info = self.ctx.market_info(target_symbol)
        logger.info(
            "Loaded market info symbol=%s market=%s", target_symbol, market_info
        )

        # 6. Connect WS
        market_id = self.market_map[target_symbol]
        logger.info(
            "Subscribing to order book symbol=%s market_id=%s",
            self.strategy_config.symbol,
            market_id,
        )

        # Generate auth token for authenticated channels
        # Use 8 hours (maximum allowed) since this is only for reading data
        if not self.signer_client:
            logger.error("SignerClient not initialized in Engine. Cannot subscribe.")
            return

        auth_token, error = self.signer_client.create_auth_token_with_expiry(
            deadline=8 * 60 * 60  # 8 hours in seconds
        )
        if error:
            logger.error(f"Failed to create auth token: {error}")
            auth_token = None

        self.ws_client = lighter.QueueWsClient(
            order_book_ids=[market_id],
            account_ids=[self.account_index],
            queue=self.event_queue,
            auth_token=auth_token,
            token_provider=self._get_fresh_token,
        )  # type: ignore

        if self.broadcaster:
            # Broadcast Config
            try:
                config_dict = self.strategy_config.model_dump(
                    mode="json", warnings=False
                )

                if self.strategy_config.symbol in self.market_map:
                    market_id = self.market_map[self.strategy_config.symbol]
                    m = self.markets[self.strategy_config.symbol]
                    config_dict["sz_decimals"] = m.sz_decimals
                    config_dict["px_decimals"] = m.price_decimals

                self.broadcaster.send(btypes.config_event(config_dict))
                logger.info("Broadcasted initial StrategyConfig.")

            except Exception as e:
                logger.error(f"Failed to broadcast initial config: {e}", exc_info=True)

            # Broadcast Info
            try:
                self.broadcaster.send(
                    btypes.info_event(self.exchange_config.network)
                )  # Using Network Name
                logger.info("Broadcasted initial SystemInfo.")
            except Exception as e:
                logger.error(f"Failed to broadcast initial info: {e}", exc_info=True)

    async def _message_processor(self):
        logger.info("Message Processor Started...")
        while not self._shutdown_event.is_set():
            try:
                # Use wait_for or similar to make it interruptible if needed,
                # but get() is already an awaitable that can be cancelled.
                try:
                    msg_task = asyncio.create_task(self.event_queue.get())
                    shutdown_task = asyncio.create_task(self._shutdown_event.wait())

                    done, pending = await asyncio.wait(
                        [msg_task, shutdown_task], return_when=asyncio.FIRST_COMPLETED
                    )

                    if self._shutdown_event.is_set():
                        for p in pending:
                            p.cancel()
                        break

                    if msg_task in done:
                        msg_type, target_id, data = msg_task.result()
                    else:
                        msg_task.cancel()
                        break
                except asyncio.CancelledError:
                    break

                if msg_type == "mid_price":
                    # data is the mid price float
                    await self._handle_mid_price_msg(target_id, data)
                elif msg_type == "open_orders":
                    # data is the orders update from account_all_orders channel
                    await self._handle_open_orders_msg(target_id, data)
                elif msg_type == "user_fills":
                    # data is the trades update from account_all_trades channel
                    await self._handle_user_fills_msg(target_id, data)

                self.event_queue.task_done()

            except ValueError as e:
                # Fatal Error (e.g. Strategy Initialization Failure)
                logger.error(f"Fatal Error in message processor: {e}")

                # Signal shutdown
                asyncio.create_task(self.stop())
                break

            except Exception as e:
                logger.error(f"Error in message processor: {e}")
                await asyncio.sleep(1)

    async def _broadcast_summary_loop(self):
        """Broadcasts strategy summary and grid state every 1 second."""
        if not self.broadcaster:
            return

        while True:
            try:
                await asyncio.sleep(1.0)
                if not self.ctx or not self.strategy:
                    continue

                assert self.ctx is not None

                # Summary
                summary = self.strategy.get_summary(self.ctx)
                if summary:
                    # Determine type
                    if isinstance(summary, PerpGridSummary):  # Perp
                        self.broadcaster.send(btypes.perp_grid_summary_event(summary))
                    else:
                        # Assuming SpotGridSummary if not Perp
                        self.broadcaster.send(
                            btypes.spot_grid_summary_event(
                                cast(SpotGridSummary, summary)
                            )
                        )

                # Grid State
                grid_state = self.strategy.get_grid_state(self.ctx)
                if grid_state:
                    self.broadcaster.send(btypes.grid_state_event(grid_state))

                # logger.debug("Broadcasted summary and grid state")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in broadcast loop: {e}", exc_info=True)
                await asyncio.sleep(5.0)

    @staticmethod
    def _as_positive_int(value: Any) -> Optional[int]:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        if parsed <= 0:
            return None
        return parsed

    def _prune_oid_cloid_cache(self, now: Optional[float] = None):
        prune_now = time.time() if now is None else now
        removed = 0

        while self.oid_to_cloid:
            first_oid = next(iter(self.oid_to_cloid))
            _, first_seen_at = self.oid_to_cloid[first_oid]
            if (prune_now - first_seen_at) <= OID_CLOID_CACHE_TTL_SECONDS:
                break
            self.oid_to_cloid.pop(first_oid, None)
            self.unresolved_oid_fill_counts.pop(first_oid, None)
            removed += 1

        while len(self.oid_to_cloid) > OID_CLOID_CACHE_MAX_SIZE:
            oldest_oid, _ = self.oid_to_cloid.popitem(last=False)
            self.unresolved_oid_fill_counts.pop(oldest_oid, None)
            removed += 1

        if removed:
            logger.info(
                f"[OID_CLOID_CACHE] pruned={removed} size={len(self.oid_to_cloid)}"
            )

    def _remember_oid_cloid(self, oid: Any, cloid: Optional[Cloid]):
        if not cloid:
            return

        cloid_int = cloid.as_int()
        if cloid_int <= 0:
            return

        oid_int = self._as_positive_int(oid)
        if oid_int is None:
            return

        now = time.time()
        self.oid_to_cloid[oid_int] = (cloid, now)
        self.oid_to_cloid.move_to_end(oid_int)
        self.unresolved_oid_fill_counts.pop(oid_int, None)
        self._prune_oid_cloid_cache(now)

    def _sync_oid_cloid_map_from_orders(self, orders: List[Any]):
        for order in orders:
            cloid_int = self._as_positive_int(
                getattr(order, "client_order_index", None)
            )
            oid_int = self._as_positive_int(getattr(order, "order_index", None))
            if oid_int is None:
                oid_int = self._as_positive_int(getattr(order, "order_id", None))

            if cloid_int is None or oid_int is None:
                continue

            self._remember_oid_cloid(oid_int, Cloid(cloid_int))

    def _prune_completed_cloids(self, now: Optional[float] = None):
        prune_now = time.time() if now is None else now
        removed = 0

        while self.completed_cloids:
            first_cloid = next(iter(self.completed_cloids))
            first_seen_at = self.completed_cloids[first_cloid]
            if (prune_now - first_seen_at) <= COMPLETED_CLOID_CACHE_TTL_SECONDS:
                break
            self.completed_cloids.pop(first_cloid, None)
            removed += 1

        while len(self.completed_cloids) > COMPLETED_CLOID_CACHE_MAX_SIZE:
            self.completed_cloids.popitem(last=False)
            removed += 1

        if removed and (
            prune_now - self.last_completed_cloid_cache_log_ts >= 60
            or not self.last_completed_cloid_cache_log_ts
        ):
            logger.info(
                f"[COMPLETED_CLOID_CACHE] pruned={removed} size={len(self.completed_cloids)}"
            )
            self.last_completed_cloid_cache_log_ts = prune_now

    def _mark_cloid_completed(self, cloid: Optional[Cloid]):
        if not cloid:
            return

        now = time.time()
        self.completed_cloids[cloid] = now
        self.completed_cloids.move_to_end(cloid)
        self._prune_completed_cloids(now)

    def _is_cloid_completed(self, cloid: Cloid) -> bool:
        seen_at = self.completed_cloids.get(cloid)
        if seen_at is None:
            return False

        now = time.time()
        if now - seen_at > COMPLETED_CLOID_CACHE_TTL_SECONDS:
            self.completed_cloids.pop(cloid, None)
            return False

        self.completed_cloids.move_to_end(cloid)
        return True

    def _infer_cloid_for_unresolved_fill(
        self, side: OrderSide, trade_size: Decimal
    ) -> Optional[Cloid]:
        candidates: List[Cloid] = []
        for cloid, pending in self.pending_orders.items():
            if pending.side != side:
                continue
            if pending.oid is not None:
                continue
            if trade_size > (pending.target_size * Decimal("1.05")):
                continue
            candidates.append(cloid)

        if len(candidates) == 1:
            return candidates[0]
        return None

    def _emit_unresolved_oid_fill(
        self,
        oid: int,
        side: OrderSide,
        trade_size: Decimal,
        trade_price: Decimal,
        fee: Decimal,
        role: TradeRole,
    ):
        self.unresolved_oid_fills_total += 1
        unresolved_count = self.unresolved_oid_fill_counts.get(oid, 0) + 1
        self.unresolved_oid_fill_counts[oid] = unresolved_count

        logger.warning(
            f"[UNRESOLVED_OID_FILL] oid={oid} side={side} size={trade_size} price={trade_price} "
            f"role={role} count={unresolved_count} total={self.unresolved_oid_fills_total}"
        )
        if unresolved_count >= UNRESOLVED_OID_FILL_ALERT_THRESHOLD:
            logger.error(
                f"[UNRESOLVED_OID_FILL_ALERT] oid={oid} reached count={unresolved_count}"
            )

        try:
            if self.ctx:
                self.strategy.on_order_filled(
                    OrderFill(
                        side=side,
                        size=trade_size,
                        price=trade_price,
                        fee=fee,
                        role=role,
                        cloid=None,
                        reduce_only=None,
                        raw_dir="unresolved_oid",
                    ),
                    self.ctx,
                )
        except Exception as e:
            logger.error(f"Strategy unresolved fill handler error: {e}")

    def _find_cloid_by_oid(self, oid: int) -> Optional[Cloid]:
        """
        Finds the CLOID associated with a given Order ID (OID).
        """
        self._prune_oid_cloid_cache()

        cached = self.oid_to_cloid.get(oid)
        if cached is not None:
            cloid, seen_at = cached
            now = time.time()
            if now - seen_at <= OID_CLOID_CACHE_TTL_SECONDS:
                self.oid_to_cloid[oid] = (cloid, now)
                self.oid_to_cloid.move_to_end(oid)
                return cloid
            self.oid_to_cloid.pop(oid, None)

        for cloid, pending in self.pending_orders.items():
            if pending.oid == oid:
                self._remember_oid_cloid(oid, cloid)
                return cloid
        return None

    async def _handle_mid_price_msg(self, market_id: str, mid_price: float):
        market_id_int = int(market_id)
        symbol = self.reverse_market_map.get(market_id_int)
        if not symbol or symbol != self.strategy_config.symbol:
            return

        if self.ctx:
            # Get market info to round price to correct decimals
            market_info = self.ctx.market_info(symbol)
            if not market_info:
                return

            # Round price to market's price decimals to avoid floating-point precision issues
            # mid_price comes as float from websocket
            rounded_price = round(Decimal(str(mid_price)), market_info.price_decimals)

            # Check if price actually changed
            last_price = self.last_price.get(symbol)
            if last_price is not None and rounded_price == last_price:
                # Price hasn't changed meaningfully, skip processing
                return

            # Update last price
            self.last_price[symbol] = rounded_price

            # logger.info(f"Price Update: {symbol} @ ${rounded_price:.{market_info.price_decimals}f}")

            if self.broadcaster:
                # Broadcast expects float usually? Let's cast for broadcast only if needed
                # But let's check if btypes can handle Decimal or if we should cast.
                # Usually JSON serialization handles int/float/str. Decimal might need casting.
                # For safety, cast to float or str for broadcast.
                self.broadcaster.send(
                    btypes.market_update_event(
                        btypes.MarketEvent(price=float(rounded_price))
                    )
                )

            try:
                self.strategy.on_tick(rounded_price, self.ctx)
                await self.process_order_queue()
            except ValueError as e:
                # ValueError during initialization indicates a fatal configuration error
                # Let it propagate to the upper layer (main.py) which will handle shutdown
                logger.error("Strategy initialization error: %s", e, exc_info=True)
                raise
            except Exception as e:
                logger.error("Strategy on_tick error: %s", e, exc_info=True)

    async def _handle_open_orders_msg(self, account_id: str, orders_data: dict):
        """Process open orders update."""
        if not self.ctx:
            return

        # Extract orders
        # Format: {"channel": "...", "orders": {"{MARKET_INDEX}": [Order]}, "type": "..."}
        orders_by_market = orders_data.get("orders", {})

        processed_orders = 0
        tracked_orders = 0
        new_oid_mappings = 0
        canceled_orders = 0
        untracked_orders = 0

        # Process all orders in the message
        for market_index, orders_list in orders_by_market.items():
            for order_dict in orders_list:
                try:
                    processed_orders += 1
                    order = self._parse_order(order_dict)
                    logger.debug("open_order_event order=%s", order)
                    if LOG_VERBOSE_ORDER_STREAM:
                        logger.info("open_order_event order=%s", order)

                    if order.cloid_id == 0:
                        logger.debug(
                            "Ignoring non-bot order in lifecycle tracking order_id=%s",
                            order.order_id,
                        )
                        continue

                    if not order.cloid_id:
                        logger.warning(
                            "Order missing cloid_id order_id=%s", order.order_id
                        )
                        continue

                    cloid = Cloid(order.cloid_id)
                    self._remember_oid_cloid(order.order_id, cloid)

                    # Only process if we're tracking this order
                    if cloid not in self.pending_orders:
                        untracked_orders += 1
                        logger.debug(
                            "Open order not tracked cloid=%s oid=%s status=%s",
                            cloid.as_int(),
                            order.order_id,
                            order.status,
                        )
                        continue

                    tracked_orders += 1
                    pending = self.pending_orders[cloid]

                    # 1. Update OID if missing (Crucial for later cancellation/audit)
                    if order.order_id and not pending.oid:
                        pending.oid = order.order_id
                        self.reconciliation_miss_counts.pop(cloid, None)
                        new_oid_mappings += 1

                        logger.debug(
                            "Order tracking linked cloid=%s oid=%s side=%s size=%s symbol=%s price=%s",
                            cloid.as_int(),
                            order.order_id,
                            pending.side,
                            pending.target_size,
                            self._get_base_asset(int(market_index)),
                            pending.price,
                        )

                        if self.broadcaster:
                            self.broadcaster.send(
                                btypes.order_update_event(
                                    btypes.OrderEvent(
                                        oid=order.order_id,
                                        cloid=str(cloid),
                                        side=str(pending.side),
                                        price=float(pending.price),
                                        size=float(pending.target_size),
                                        status="open",
                                        fee=0.0,
                                        is_taker=False,
                                    )
                                )
                            )

                    # 3. Check order status
                    if self._is_canceled_status(order.status):
                        canceled_orders += 1
                        logger.info(
                            "[ORDER_CANCELED] cloid=%s oid=%s status=%s filled=%s",
                            cloid.as_int(),
                            pending.oid or order.order_id,
                            order.status,
                            pending.filled_size,
                        )

                        if self.broadcaster:
                            self.broadcaster.send(
                                btypes.order_update_event(
                                    btypes.OrderEvent(
                                        oid=pending.oid or 0,
                                        cloid=str(cloid),
                                        side=str(pending.side),
                                        price=0.0,
                                        size=float(pending.filled_size),
                                        status="cancelled",
                                        fee=0.0,
                                        is_taker=False,
                                    )
                                )
                            )

                        del self.pending_orders[cloid]
                        self.reconciliation_miss_counts.pop(cloid, None)

                        try:
                            failure = OrderFailure(
                                cloid=cloid,
                                side=pending.side,
                                target_size=pending.target_size,
                                filled_size=pending.filled_size,
                                filled_price=pending.weighted_avg_px,
                                accumulated_fees=pending.accumulated_fees,
                                failure_reason=order.status,
                                reduce_only=pending.reduce_only,
                            )
                            self.strategy.on_order_failed(failure, self.ctx)
                        except Exception as e:
                            logger.error(
                                "Strategy on_order_failed callback error cloid=%s: %s",
                                cloid.as_int(),
                                e,
                                exc_info=True,
                            )

                        self._mark_cloid_completed(cloid)

                except Exception as e:
                    logger.error(
                        "Error processing open order event: %s order_data=%s",
                        e,
                        order_dict,
                        exc_info=True,
                    )

        if processed_orders:
            logger.debug(
                "open_orders_update processed=%d tracked=%d mapped=%d canceled=%d untracked=%d pending_total=%d",
                processed_orders,
                tracked_orders,
                new_oid_mappings,
                canceled_orders,
                untracked_orders,
                len(self.pending_orders),
            )

        if processed_orders and (new_oid_mappings or canceled_orders):
            logger.info(
                "open_orders_update mapped=%d canceled=%d pending_total=%d",
                new_oid_mappings,
                canceled_orders,
                len(self.pending_orders),
            )

        if untracked_orders:
            now = time.time()
            if now - self.last_untracked_orders_log_ts >= 60:
                logger.warning(
                    "open_orders_update detected_untracked_orders=%d processed=%d pending_total=%d",
                    untracked_orders,
                    processed_orders,
                    len(self.pending_orders),
                )
                self.last_untracked_orders_log_ts = now

    async def _handle_user_fills_msg(self, account_id: str, trades_data: dict):
        """Process user fills update."""
        if not self.ctx:
            return

        trades_by_market = trades_data.get("trades", {})

        # Process all trades across all markets
        for _market_index, trades_list in trades_by_market.items():
            for trade_dict in trades_list:
                try:
                    trade = self._parse_trade(trade_dict)

                    # Match trade to our account to find CLOID and Side
                    # Use self.account_index directly as it is the source of truth
                    assert self.account_index is not None
                    details = trade.get_trade_details(self.account_index)
                    if not details:
                        logger.debug(
                            "Ignored trade not involving account=%s trade=%s",
                            self.account_index,
                            trade,
                        )
                        continue

                    side = details.side
                    oid = details.oid
                    role = details.role

                    # Calculate Fee in USD
                    fee = self._calculate_fee_usd(details)

                    # Resolve CLOID from OID
                    cloid = self._find_cloid_by_oid(oid)

                    if not cloid:
                        cloid = self._infer_cloid_for_unresolved_fill(side, trade.size)
                        if cloid:
                            logger.warning(
                                "[OID_RECOVERY] inferred_cloid=%s oid=%s side=%s size=%s",
                                cloid.as_int(),
                                oid,
                                side,
                                trade.size,
                            )
                            pending = self.pending_orders.get(cloid)
                            if pending and pending.oid is None:
                                pending.oid = oid
                            self._remember_oid_cloid(oid, cloid)
                        else:
                            self._emit_unresolved_oid_fill(
                                oid=oid,
                                side=side,
                                trade_size=trade.size,
                                trade_price=trade.price,
                                fee=fee,
                                role=role,
                            )
                            continue

                    logger.debug(
                        "Trade event cloid=%s side=%s role=%s size=%s price=%s oid=%s",
                        cloid.as_int(),
                        side,
                        role,
                        trade.size,
                        trade.price,
                        oid,
                    )

                    # Idempotency check
                    if cloid and self._is_cloid_completed(cloid):
                        logger.debug(
                            "Ignored duplicate fill for completed cloid=%s", cloid
                        )
                        continue

                    # Process fill
                    if cloid and cloid in self.pending_orders:
                        # Accumulate partial fill
                        pending = self.pending_orders[cloid]

                        new_total_size = pending.filled_size + trade.size
                        if new_total_size <= 0:
                            logger.warning(
                                "Ignoring fill with non-positive cumulative size cloid=%s trade_size=%s existing_filled=%s",
                                cloid.as_int(),
                                trade.size,
                                pending.filled_size,
                            )
                            continue

                        # Calculate weighted average price
                        # pending.weighted_avg_px is Decimal, trade.price is Decimal
                        pending.weighted_avg_px = (
                            (pending.weighted_avg_px * pending.filled_size)
                            + (trade.price * trade.size)
                        ) / new_total_size

                        pending.filled_size = new_total_size
                        pending.accumulated_fees += fee

                        # Check if fully filled (using 0.9999 threshold like Rust SDK)
                        is_fully_filled = pending.filled_size >= (
                            pending.target_size * Decimal("0.9999")
                        )

                        if is_fully_filled:
                            # Order is fully filled - notify strategy
                            logger.info(
                                f"[ORDER_FILLED] cloid={cloid.as_int()} oid={pending.oid} {side} {pending.filled_size} @ {pending.weighted_avg_px} (Fee: {pending.accumulated_fees:.4f})"
                            )

                            if self.broadcaster:
                                self.broadcaster.send(
                                    btypes.order_update_event(
                                        btypes.OrderEvent(
                                            oid=pending.oid or 0,
                                            cloid=str(cloid),
                                            side=str(side),
                                            price=float(pending.weighted_avg_px),
                                            size=float(pending.filled_size),
                                            status="filled",
                                            fee=float(pending.accumulated_fees),
                                            is_taker=role.is_taker(),
                                        )
                                    )
                                )

                            final_px = pending.weighted_avg_px
                            final_sz = pending.filled_size
                            final_fee = pending.accumulated_fees
                            pending_reduce_only = pending.reduce_only

                            # Remove from pending
                            del self.pending_orders[cloid]
                            self.reconciliation_miss_counts.pop(cloid, None)

                            # Call strategy callback
                            try:
                                self.strategy.on_order_filled(
                                    OrderFill(
                                        side=side,
                                        size=final_sz,
                                        price=final_px,
                                        fee=final_fee,
                                        role=role,
                                        cloid=cloid,
                                        reduce_only=pending_reduce_only,
                                        raw_dir=None,
                                    ),
                                    self.ctx,
                                )
                            except Exception as e:
                                logger.error(
                                    "Strategy on_order_filled callback error cloid=%s: %s",
                                    cloid.as_int(),
                                    e,
                                    exc_info=True,
                                )

                            # Mark as completed for idempotency
                            self._mark_cloid_completed(cloid)
                        else:
                            logger.info(
                                f"[ORDER_FILL_PARTIAL] cloid={cloid.as_int()} oid={pending.oid} {side} {trade.size} @ {trade.price} "
                                f"(Fee: {fee:.4f}) progress={pending.filled_size}/{pending.target_size}"
                            )

                    elif cloid:
                        logger.info(
                            f"[ORDER_FILL_UNTRACKED] cloid={cloid.as_int()} oid={oid} {side} {trade.size} @ {trade.price} (Fee: {fee:.4f})"
                        )

                        try:
                            self.strategy.on_order_filled(
                                OrderFill(
                                    side=side,
                                    size=trade.size,
                                    price=trade.price,
                                    fee=fee,
                                    role=role,
                                    cloid=cloid,
                                    reduce_only=None,  # Unknown for untracked orders
                                    raw_dir=None,
                                ),
                                self.ctx,
                            )
                        except Exception as e:
                            logger.error(
                                "Strategy on_order_filled callback error for untracked fill cloid=%s: %s",
                                cloid.as_int(),
                                e,
                                exc_info=True,
                            )

                    else:
                        # No cloid - log and notify immediately
                        logger.info(
                            f"[ORDER_FILL_NOCLID] {side} {trade.size} @ {trade.price} (Fee: {fee})"
                        )

                        try:
                            self.strategy.on_order_filled(
                                OrderFill(
                                    side=side,
                                    size=trade.size,
                                    price=trade.price,
                                    fee=fee,
                                    role=role,
                                    cloid=None,
                                    reduce_only=None,
                                    raw_dir=None,
                                ),
                                self.ctx,
                            )
                        except Exception as e:
                            logger.error(
                                "Strategy on_order_filled callback error for no-cloid fill: %s",
                                e,
                                exc_info=True,
                            )

                except Exception as e:
                    logger.error(
                        "Error processing trade event: %s trade_data=%s",
                        e,
                        trade_dict,
                        exc_info=True,
                    )

    async def handle_reconciliation(self):
        """
        Reconcile local pending orders with exchange state.
        Handles missed fills and cancellations.
        """
        if not self.api_client or not self.account_index or not self.ctx:
            return

        # Degraded mode recovery probe:
        # with zero pending orders, still probe API snapshot health so degraded
        # mode can self-clear after connectivity recovers.
        if not self.pending_orders:
            if not self.degraded_mode:
                return

            market_id = self.market_map.get(self.strategy_config.symbol)
            if not market_id:
                return

            active_probe = await self.get_active_orders(market_id=market_id)
            if active_probe is None:
                self._enter_degraded_mode("reconciliation_active_probe_failed")
                return

            inactive_probe = await self.get_inactive_orders(
                limit=1, market_id=market_id
            )
            if inactive_probe is None:
                self._enter_degraded_mode("reconciliation_inactive_probe_failed")
                return

            self._record_healthy_reconciliation_snapshot()
            return

        # Snapshots to avoid modification during iteration if we remove items
        pending_snapshot = list(self.pending_orders.items())

        # 1. Fetch Active Orders
        try:
            market_id = self.market_map.get(self.strategy_config.symbol)
            if not market_id:
                return

            active_orders_list = await self.get_active_orders(market_id=market_id)
            if active_orders_list is None:
                logger.warning(
                    "[RECONCILIATION] Skipping cycle: failed to fetch active orders."
                )
                self._enter_degraded_mode("reconciliation_active_orders_fetch_failed")
                return
            self._sync_oid_cloid_map_from_orders(active_orders_list)
            active_orders_map = {o.client_order_index: o for o in active_orders_list}

        except Exception as e:
            logger.error(
                "Reconciliation failed to fetch active orders: %s",
                e,
                exc_info=True,
            )
            self._enter_degraded_mode("reconciliation_active_orders_exception")
            return

        # 2. Iterate pending orders to identify missing ones vs active ones
        now = time.time()
        missing_cloids: List[Cloid] = []

        for cloid, pending in pending_snapshot:
            # Skip recently created orders (grace period for propagation)
            if now - pending.created_at < ORDER_PROPAGATION_GRACE_SECONDS:
                continue

            cloid_int = cloid.as_int()

            if cloid_int not in active_orders_map:
                missing_cloids.append(cloid)
            else:
                self.reconciliation_miss_counts.pop(cloid, None)

        if not missing_cloids:
            if LOG_RECONCILIATION_HEALTHY:
                logger.info(
                    "[RECONCILIATION] healthy pending=%d active_snapshot=%d",
                    len(pending_snapshot),
                    len(active_orders_map),
                )
            else:
                logger.debug(
                    "[RECONCILIATION] healthy pending=%d active_snapshot=%d",
                    len(pending_snapshot),
                    len(active_orders_map),
                )
            if not self.degraded_mode:
                return

        inactive_orders_map = {}
        try:
            inactive_orders_list = await self.get_inactive_orders(
                limit=50, market_id=market_id
            )
            if inactive_orders_list is None:
                logger.warning(
                    "[RECONCILIATION] Skipping cycle: failed to fetch inactive orders."
                )
                self._enter_degraded_mode("reconciliation_inactive_orders_fetch_failed")
                return
            if inactive_orders_list:
                self._sync_oid_cloid_map_from_orders(inactive_orders_list)
                inactive_orders_map = {
                    o.client_order_index: o for o in inactive_orders_list
                }
        except Exception as e:
            logger.error(
                "Reconciliation error fetching inactive orders: %s", e, exc_info=True
            )
            self._enter_degraded_mode("reconciliation_inactive_orders_exception")
            return

        # Safety check for degraded snapshots: if both endpoints return no rows while
        # we have many tracked pending orders, avoid false "lost" cascades.
        if (
            pending_snapshot
            and not active_orders_map
            and not inactive_orders_map
            and len(pending_snapshot) >= 5
        ):
            logger.warning(
                "[RECONCILIATION] Skipping cycle due to empty active/inactive snapshots while pending orders exist."
            )
            self._enter_degraded_mode("reconciliation_empty_snapshots")
            return

        self._record_healthy_reconciliation_snapshot()

        for cloid in missing_cloids:
            cloid_int = cloid.as_int()
            missing_pending = self.pending_orders.get(cloid)

            if not missing_pending:
                continue

            if cloid_int in inactive_orders_map:
                found_inactive = inactive_orders_map[cloid_int]
                status = found_inactive.status
                try:
                    filled_amount = Decimal(str(found_inactive.filled_base_amount))
                except Exception:
                    filled_amount = Decimal("0")
                try:
                    filled_quote_amount = Decimal(
                        str(getattr(found_inactive, "filled_quote_amount", "0"))
                    )
                except Exception:
                    filled_quote_amount = Decimal("0")

                if status == "filled":
                    logger.info(
                        f"[RECONCILIATION] Order {cloid} found FILLED in history."
                    )

                    reconciled_size = (
                        filled_amount
                        if filled_amount > 0
                        else missing_pending.filled_size
                    )
                    if reconciled_size <= 0:
                        reconciled_size = missing_pending.target_size

                    if filled_amount > 0 and filled_quote_amount > 0:
                        reconciled_price = filled_quote_amount / filled_amount
                    elif missing_pending.weighted_avg_px > 0:
                        reconciled_price = missing_pending.weighted_avg_px
                        logger.info(
                            "[RECONCILIATION] reconciled_fill_with_fallback_price using weighted average."
                        )
                    else:
                        reconciled_price = missing_pending.price
                        logger.info(
                            "[RECONCILIATION] reconciled_fill_with_fallback_price using limit price."
                        )

                    if cloid in self.pending_orders:
                        del self.pending_orders[cloid]
                    self.reconciliation_miss_counts.pop(cloid, None)

                    self.strategy.on_order_filled(
                        OrderFill(
                            side=missing_pending.side,
                            size=reconciled_size,
                            price=reconciled_price,
                            fee=Decimal("0"),
                            role=TradeRole.TAKER,
                            cloid=cloid,
                            reduce_only=missing_pending.reduce_only,
                            raw_dir=None,
                        ),
                        self.ctx,
                    )
                    self._mark_cloid_completed(cloid)

                elif self._is_canceled_status(status):
                    logger.info(
                        f"[RECONCILIATION] Order {cloid} found CANCELED/FAILED ({status})."
                    )
                    if cloid in self.pending_orders:
                        del self.pending_orders[cloid]
                    self.reconciliation_miss_counts.pop(cloid, None)

                    self.strategy.on_order_failed(
                        OrderFailure(
                            cloid=cloid,
                            side=missing_pending.side,
                            target_size=missing_pending.target_size,
                            filled_size=filled_amount,
                            filled_price=missing_pending.weighted_avg_px,
                            accumulated_fees=missing_pending.accumulated_fees,
                            failure_reason=status,
                            reduce_only=missing_pending.reduce_only,
                        ),
                        self.ctx,
                    )
                    self._mark_cloid_completed(cloid)
            else:
                if now - missing_pending.created_at > ORDER_LOST_TIMEOUT_SECONDS:
                    miss_count = self.reconciliation_miss_counts.get(cloid, 0) + 1
                    self.reconciliation_miss_counts[cloid] = miss_count

                    if miss_count < 2:
                        logger.warning(
                            f"[RECONCILIATION] Order {cloid} missing snapshot {miss_count}/2. Waiting one more cycle before marking lost."
                        )
                        continue

                    logger.warning(
                        f"[RECONCILIATION] Order {cloid} lost (not found in active or recent history). Marking failed."
                    )
                    if cloid in self.pending_orders:
                        del self.pending_orders[cloid]
                    self.reconciliation_miss_counts.pop(cloid, None)

                    self.strategy.on_order_failed(
                        OrderFailure(
                            cloid=cloid,
                            side=missing_pending.side,
                            target_size=missing_pending.target_size,
                            filled_size=missing_pending.filled_size,
                            filled_price=missing_pending.weighted_avg_px,
                            accumulated_fees=missing_pending.accumulated_fees,
                            failure_reason="lost_in_reconciliation",
                            reduce_only=missing_pending.reduce_only,
                        ),
                        self.ctx,
                    )

    async def _reconciliation_loop(self):
        if not RECONCILIATION_ENABLED:
            logger.info("Reconciliation Loop disabled via RECONCILIATION_ENABLED flag")
            return

        logger.info("Reconciliation Loop Started...")
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=RECONCILIATION_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

            if self._shutdown_event.is_set():
                break

            try:
                await self.handle_reconciliation()
            except Exception as e:
                logger.error(f"Error in reconciliation loop: {e}", exc_info=True)

            # Periodic balance snapshot for production visibility
            try:
                await self._log_balance_snapshot()
            except Exception as e:
                logger.error(f"Error logging balance snapshot: {e}")

    async def _log_balance_snapshot(self):
        """Log periodic balance snapshot for production monitoring."""
        if not self.ctx:
            return
        try:
            await self._fetch_account_balances()
            symbol = self.strategy_config.symbol
            summary = self.strategy.get_summary(self.ctx)
            logger.info(
                "[BALANCE_SNAPSHOT] symbol=%s pending_orders=%d degraded=%s summary=%s",
                symbol,
                len(self.pending_orders),
                self.degraded_mode,
                {
                    "state": getattr(summary, "state", None),
                    "matched_profit": str(getattr(summary, "matched_profit", 0)),
                    "total_fees": str(getattr(summary, "total_fees", 0)),
                    "total_profit": str(getattr(summary, "total_profit", 0)),
                },
            )
        except ValueError:
            # get_summary raises ValueError before strategy init; expected
            logger.debug("[BALANCE_SNAPSHOT] skipped: strategy not yet initialized")
        except Exception as e:
            logger.error("[BALANCE_SNAPSHOT] failed: %s", e, exc_info=True)

    def _enter_degraded_mode(self, reason: str):
        self.degraded_mode = True
        self.degraded_until_ts = time.time() + DEGRADED_MODE_COOLDOWN_SECONDS
        self.healthy_reconciliation_snapshots = 0
        logger.warning(
            f"[DEGRADED_MODE] Enabled for {DEGRADED_MODE_COOLDOWN_SECONDS}s. reason={reason}"
        )

    def _record_healthy_reconciliation_snapshot(self):
        self.healthy_reconciliation_snapshots += 1
        if (
            self.degraded_mode
            and time.time() >= self.degraded_until_ts
            and self.healthy_reconciliation_snapshots
            >= RECONCILIATION_HEALTHY_SNAPSHOTS_TO_EXIT_DEGRADED
        ):
            self.degraded_mode = False
            logger.info(
                f"[DEGRADED_MODE] Disabled after {self.healthy_reconciliation_snapshots} healthy reconciliation snapshot(s)."
            )

    @staticmethod
    def _is_transient_batch_code(code: int) -> bool:
        return code in {429, 500, 502, 503, 504}

    def _requeue_orders_to_front(self, orders: List[Any]):
        if not self.ctx or not orders:
            return
        self.ctx.order_queue = list(orders) + self.ctx.order_queue

    def _sign_orders(
        self,
        orders_to_process: list,
        tx_types: List[int],
        tx_infos: List[str],
        signed_entries: List[Dict[str, Any]],
        failed_cloids: set,
    ) -> None:
        """Sign all orders in the batch. Raises on unrecoverable errors (e.g. SSL).

        Populates failed_cloids with CLOIDs that already had failures reported
        via _notify_order_submit_failed (e.g. signing_error), so callers can
        avoid double-reporting on crash recovery.
        """
        assert self.ctx is not None
        batch_api_key_index = None
        nonce = 0
        signed_count = 0

        for order in orders_to_process:
            market_id = self.market_map.get(order.symbol)
            if market_id is None:
                logger.error("Market ID not found for symbol=%s", order.symbol)
                continue

            if not self.signer_client:
                logger.error("Signer client not initialized")
                continue

            # Use Cloid directly as client_order_index
            # The strategy generates Cloid which is already an integer identifier
            if not order.cloid:
                logger.error("Order missing cloid order=%s", order)
                continue

            client_order_index = order.cloid.as_int()

            if signed_count == 0:
                batch_api_key_index, nonce = (
                    self.signer_client.nonce_manager.next_nonce()
                )
                assert batch_api_key_index is not None
            else:
                nonce += 1

            assert batch_api_key_index is not None

            error = None
            tx_type = None
            tx_info = None
            pending_template: Optional[PendingOrder] = None

            if isinstance(order, LimitOrderRequest):
                pending_template = PendingOrder(
                    target_size=order.sz,
                    side=order.side,
                    filled_size=Decimal("0"),
                    weighted_avg_px=Decimal("0"),
                    accumulated_fees=Decimal("0"),
                    reduce_only=order.reduce_only,
                    oid=None,
                    created_at=0.0,
                    price=order.price,
                )

                info = self.ctx.market_info(order.symbol)
                if not info:
                    logger.error("Market info not found for symbol=%s", order.symbol)
                    continue

                tx_type, tx_info, _, error = self.signer_client.sign_create_order(
                    market_index=market_id,
                    client_order_index=client_order_index,
                    base_amount=info.to_sdk_size(order.sz),
                    price=info.to_sdk_price(order.price),
                    is_ask=order.side.is_sell(),
                    order_type=self.signer_client.ORDER_TYPE_LIMIT,
                    time_in_force=self.signer_client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,  # GTC
                    reduce_only=order.reduce_only,
                    trigger_price=0,
                    nonce=nonce,
                    api_key_index=batch_api_key_index,
                )

            elif isinstance(order, MarketOrderRequest):
                info = self.ctx.market_info(order.symbol)
                if not info:
                    logger.error("Market info not found for symbol=%s", order.symbol)
                    continue

                # For Market orders, 'price' is the worst acceptable price (slippage limit)
                tx_type, tx_info, _, error = self.signer_client.sign_create_order(
                    market_index=market_id,
                    client_order_index=client_order_index,
                    base_amount=info.to_sdk_size(order.sz),
                    price=info.to_sdk_price(order.price),
                    is_ask=order.side.is_sell(),
                    order_type=self.signer_client.ORDER_TYPE_MARKET,
                    time_in_force=self.signer_client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                    reduce_only=order.reduce_only,
                    trigger_price=0,
                    nonce=nonce,
                    api_key_index=batch_api_key_index,
                )

            elif isinstance(order, CancelOrderRequest):
                # Use the Cloid directly as the order_index to cancel
                target_index = order.cloid.as_int()

                tx_type, tx_info, _, error = self.signer_client.sign_cancel_order(
                    market_index=market_id,
                    order_index=target_index,
                    nonce=nonce,
                    api_key_index=batch_api_key_index,
                )

            if error:
                logger.error(
                    "Order signing error cloid=%s symbol=%s err=%s",
                    order.cloid.as_int() if order.cloid else None,
                    order.symbol,
                    error,
                )
                if not isinstance(order, CancelOrderRequest):
                    self._notify_order_submit_failed(order, "signing_error")
                    failed_cloids.add(order.cloid)
                continue

            assert tx_type is not None
            tx_types.append(cast(int, tx_type))
            tx_infos.append(str(tx_info) if tx_info is not None else "")
            signed_entries.append({"order": order, "pending": pending_template})
            signed_count += 1

    def _notify_order_submit_failed(self, order: Any, reason: str):
        if not self.ctx:
            return

        cloid = getattr(order, "cloid", None)
        if not cloid:
            return

        logger.warning(
            "[ORDER_SUBMIT_FAILED] cloid=%s side=%s size=%s reason=%s",
            cloid.as_int(),
            getattr(order, "side", None),
            getattr(order, "sz", None),
            reason,
        )

        if cloid in self.pending_orders:
            del self.pending_orders[cloid]
        self.reconciliation_miss_counts.pop(cloid, None)

        if isinstance(order, LimitOrderRequest):
            try:
                self.strategy.on_order_failed(
                    OrderFailure(
                        cloid=cloid,
                        side=order.side,
                        target_size=order.sz,
                        filled_size=Decimal("0"),
                        filled_price=Decimal("0"),
                        accumulated_fees=Decimal("0"),
                        failure_reason=reason,
                        reduce_only=order.reduce_only,
                    ),
                    self.ctx,
                )
            except Exception as e:
                logger.error(
                    "Strategy on_order_failed callback error cloid=%s: %s",
                    cloid.as_int(),
                    e,
                    exc_info=True,
                )
        elif isinstance(order, MarketOrderRequest):
            try:
                self.strategy.on_order_failed(
                    OrderFailure(
                        cloid=cloid,
                        side=order.side,
                        target_size=order.sz,
                        filled_size=Decimal("0"),
                        filled_price=Decimal("0"),
                        accumulated_fees=Decimal("0"),
                        failure_reason=reason,
                        reduce_only=order.reduce_only,
                    ),
                    self.ctx,
                )
            except Exception as e:
                logger.error(
                    "Strategy on_order_failed callback error cloid=%s: %s",
                    cloid.as_int(),
                    e,
                    exc_info=True,
                )

    async def process_order_queue(self):
        if not self.ctx or not self.ctx.order_queue:
            return

        if self.degraded_mode:
            now = time.time()
            if now - self.last_degraded_skip_log_ts >= 10:
                logger.warning(
                    "[DEGRADED_MODE] Skipping order submission while exchange connectivity is unstable."
                )
                self.last_degraded_skip_log_ts = now
            return

        assert self.ctx is not None
        assert self.ctx.order_queue is not None  # If order_queue is Optional

        # Drain queue
        orders_to_process = list(self.ctx.order_queue)
        self.ctx.order_queue.clear()
        if len(orders_to_process) >= 5:
            logger.info(
                "Submitting queued orders count=%d pending_tracked=%d",
                len(orders_to_process),
                len(self.pending_orders),
            )
        else:
            logger.debug(
                "Submitting queued orders count=%d pending_tracked=%d",
                len(orders_to_process),
                len(self.pending_orders),
            )

        tx_types: List[int] = []
        tx_infos: List[str] = []
        # Signed entries preserve order for chunk mapping.
        signed_entries: List[Dict[str, Any]] = []
        failed_cloids: set = set()

        try:
            self._sign_orders(
                orders_to_process, tx_types, tx_infos, signed_entries, failed_cloids
            )
        except Exception as exc:
            logger.error(
                "[ORDER_SIGNING_CRASH] Signing loop failed: %s. "
                "Notifying strategy for %d unsent orders.",
                exc,
                len(orders_to_process),
            )
            # Notify strategy for every dequeued order that was never successfully
            # signed or already reported as failed, so zone.cloid gets reset.
            already_handled = failed_cloids | {
                e["order"].cloid
                for e in signed_entries
                if e.get("order") and getattr(e["order"], "cloid", None)
            }
            for order in orders_to_process:
                if isinstance(order, CancelOrderRequest):
                    continue
                cloid = getattr(order, "cloid", None)
                if cloid and cloid not in already_handled:
                    self._notify_order_submit_failed(order, "signing_crash")
            self._enter_degraded_mode("order_signing_crash")

        if not tx_types:
            return

        for batch_start in range(0, len(tx_types), MAX_BATCH_SIZE):
            batch_end = min(batch_start + MAX_BATCH_SIZE, len(tx_types))
            batch_tx_types = tx_types[batch_start:batch_end]
            batch_tx_infos = tx_infos[batch_start:batch_end]
            batch_entries = signed_entries[batch_start:batch_end]
            remaining_entries = signed_entries[batch_start:]
            batch_num = (batch_start // MAX_BATCH_SIZE) + 1
            total_batches = (len(tx_types) + MAX_BATCH_SIZE - 1) // MAX_BATCH_SIZE

            try:
                assert self.signer_client is not None
                response = await self.signer_client.send_tx_batch(
                    tx_types=batch_tx_types, tx_infos=batch_tx_infos
                )

                if response.code == 200:
                    logger.info(
                        f"Batch {batch_num}/{total_batches} accepted: {len(response.tx_hash)} orders"
                    )
                    created_at = time.time()
                    for entry in batch_entries:
                        pending = entry.get("pending")
                        order_entry = entry.get("order")
                        if (
                            isinstance(pending, PendingOrder)
                            and order_entry
                            and order_entry.cloid
                        ):
                            pending.created_at = created_at
                            self.pending_orders[order_entry.cloid] = pending
                else:
                    affected_cloids = [
                        e["order"].cloid.as_int()
                        for e in batch_entries
                        if e.get("order") and getattr(e["order"], "cloid", None)
                    ]
                    logger.error(
                        f"Batch {batch_num}/{total_batches} failed: code={response.code}, "
                        f"message={response.message}, affected_cloids={affected_cloids}"
                    )
                    if self._is_transient_batch_code(response.code):
                        self._enter_degraded_mode(f"batch_send_http_{response.code}")
                        self._requeue_orders_to_front(
                            [e["order"] for e in remaining_entries]
                        )
                        break

                    for entry in batch_entries:
                        self._notify_order_submit_failed(
                            entry["order"], f"batch_rejected_{response.code}"
                        )

            except Exception as exc:
                affected_cloids = [
                    entry["order"].cloid.as_int()
                    for entry in remaining_entries
                    if entry.get("order") and getattr(entry["order"], "cloid", None)
                ]
                logger.error(
                    f"Failed to send batch {batch_num}/{total_batches}: {exc} affected_cloids={affected_cloids}"
                )
                self._enter_degraded_mode("batch_send_exception")
                self._requeue_orders_to_front(
                    [entry["order"] for entry in remaining_entries]
                )
                break

    async def _cancel_pending_orders_on_shutdown(self):
        if not self.pending_orders:
            logger.info(
                "Shutdown order cleanup skipped: no pending bot orders tracked."
            )
            return

        if not self.signer_client:
            logger.warning(
                "Shutdown order cleanup skipped: signer client unavailable pending_tracked=%d",
                len(self.pending_orders),
            )
            return

        cancel_requests = [
            CancelOrderRequest(cloid=cloid, symbol=self.strategy_config.symbol)
            for cloid in list(self.pending_orders.keys())
        ]
        logger.info(
            "Shutdown order cleanup started pending_tracked=%d symbol=%s",
            len(cancel_requests),
            self.strategy_config.symbol,
        )

        tx_types: List[int] = []
        tx_infos: List[str] = []
        signed_entries: List[Dict[str, Any]] = []
        failed_cloids: set = set()

        try:
            self._sign_orders(
                cancel_requests, tx_types, tx_infos, signed_entries, failed_cloids
            )
        except Exception as exc:
            logger.error(
                "Shutdown order cleanup signing failed pending=%d err=%s",
                len(cancel_requests),
                exc,
                exc_info=True,
            )
            return

        if not tx_types:
            logger.warning(
                "Shutdown order cleanup produced no cancel transactions pending=%d",
                len(cancel_requests),
            )
            return

        accepted_cancels = 0
        failed_cancels = 0

        for batch_start in range(0, len(tx_types), MAX_BATCH_SIZE):
            batch_end = min(batch_start + MAX_BATCH_SIZE, len(tx_types))
            batch_tx_types = tx_types[batch_start:batch_end]
            batch_tx_infos = tx_infos[batch_start:batch_end]
            batch_num = (batch_start // MAX_BATCH_SIZE) + 1
            total_batches = (len(tx_types) + MAX_BATCH_SIZE - 1) // MAX_BATCH_SIZE

            try:
                assert self.signer_client is not None
                response = await self.signer_client.send_tx_batch(
                    tx_types=batch_tx_types, tx_infos=batch_tx_infos
                )

                if response.code == 200:
                    accepted_cancels += len(batch_tx_types)
                    logger.info(
                        "Shutdown cancel batch accepted %d/%d size=%d",
                        batch_num,
                        total_batches,
                        len(batch_tx_types),
                    )
                else:
                    failed_cancels += len(batch_tx_types)
                    logger.error(
                        "Shutdown cancel batch failed %d/%d code=%s message=%s size=%d",
                        batch_num,
                        total_batches,
                        response.code,
                        response.message,
                        len(batch_tx_types),
                    )
            except Exception as exc:
                failed_cancels += len(batch_tx_types)
                logger.error(
                    "Shutdown cancel batch exception %d/%d err=%s size=%d",
                    batch_num,
                    total_batches,
                    exc,
                    len(batch_tx_types),
                    exc_info=True,
                )

        logger.info(
            "Shutdown order cleanup finished attempted=%d accepted=%d failed=%d tracked_pending=%d",
            len(tx_types),
            accepted_cancels,
            failed_cancels,
            len(self.pending_orders),
        )

    async def run(self):
        await self.initialize()
        self.running = True
        logger.info("Engine Running...")

        # Start WS Loop and Message Processor
        if self.ws_client:
            tasks = [
                asyncio.create_task(self.ws_client.run_async()),
                asyncio.create_task(self._message_processor()),
                asyncio.create_task(self._broadcast_summary_loop()),
                asyncio.create_task(self._reconciliation_loop()),
            ]

            if self.broadcaster:
                tasks.append(asyncio.create_task(self.broadcaster.start()))

            try:
                await self._shutdown_event.wait()
            except asyncio.CancelledError:
                logger.info("Engine run cancelled.")
            finally:
                logger.info("Shutting down engine tasks...")
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

                # Clean up API clients
                await self.cleanup()

                logger.info("Engine Stopped.")

    async def stop(self):
        if self._stop_called:
            return
        self._stop_called = True

        logger.info("Stopping Engine...")
        self.running = False
        self._shutdown_event.set()

        try:
            await self._cancel_pending_orders_on_shutdown()
        except Exception as e:
            logger.error("Shutdown order cleanup failed: %s", e, exc_info=True)

        try:
            if self.ws_client:
                # self.ws_client.stop() # method might not exist, relying on task cancellation
                pass
            if self.broadcaster:
                await self.broadcaster.stop()
        finally:
            # Clean up API clients from BaseEngine
            await self.cleanup()
