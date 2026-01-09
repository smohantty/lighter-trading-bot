import asyncio
import logging
import json
import time
from typing import Dict, List, Optional, Any, Union, cast
from decimal import Decimal
from dataclasses import asdict, is_dataclass
import websockets
import lighter
from lighter.nonce_manager import NonceManagerType

from src.config import StrategyConfig, ExchangeConfig
from src.model import Cloid, OrderRequest, LimitOrderRequest, MarketOrderRequest, CancelOrderRequest, OrderFill, OrderSide, PendingOrder, Order, Trade, TradeRole, TradeDetails, OrderFailure
from src.strategy.base import Strategy
from src.engine.context import StrategyContext, MarketInfo, Balance
from src.strategy.types import PerpGridSummary, SpotGridSummary, GridState
from src.broadcast.server import StatusBroadcaster
import src.broadcast.types as btypes
from src.engine.base import BaseEngine

logger = logging.getLogger(__name__)

# Constants (Move to Lighter Constants if available)
RECONCILIATION_INTERVAL_SECONDS = 30.0

class Engine(BaseEngine):
    def __init__(self, config: StrategyConfig, exchange_config: ExchangeConfig, strategy: Strategy, broadcaster: Optional[StatusBroadcaster] = None):
        super().__init__(config, exchange_config, strategy)
        self.broadcaster = broadcaster
        
        # Clients
        # Clients
        self.api_client: Optional[lighter.ApiClient] = None
        self.ws_client: Optional[lighter.WsClient] = None
        
        self.account_index: Optional[int] = None
        self.running = False
        
        self.event_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._shutdown_event = asyncio.Event()
        
        # Track last price per symbol to avoid duplicate on_tick calls
        self.last_price: Dict[str, Decimal] = {}
        
        # Partial fill tracking (mirroring Rust SDK)
        self.pending_orders: Dict[Cloid, PendingOrder] = {}
        self.completed_cloids: set[Cloid] = set()


        
    async def initialize(self):
        logger.info("Initializing Engine...")
        
        # 1. Setup API Client
        logger.info(f"Initializing API Client with URL: {self.exchange_config.base_url}")
        
        api_config = lighter.Configuration(host=self.exchange_config.base_url)
        self.api_client = lighter.ApiClient(configuration=api_config)
        
        # 2. Fetch Account Index (Already in Config)
        if self.exchange_config.account_index > 0:
            self.account_index = self.exchange_config.account_index
            logger.info(f"Using Account Index from Config: {self.account_index}")
        else:
            raise ValueError("Account Index must be provided in the configuration.")

        # 3. Setup Signer Client
        # Using configured API Key Index
        self._init_signer()
        
        # 4. Load Metadata (Markets)
        await self._load_markets()

        target_symbol = self.strategy_config.symbol
        if target_symbol not in self.markets:
             raise ValueError(f"Symbol {target_symbol} not found in Lighter markets: {list(self.markets.keys())}")

        self.ctx = StrategyContext(self.markets)
        
        # 5. Fetch Account Balances
        await self._fetch_account_balances()
        
        market_info = self.ctx.market_info(target_symbol)
        logger.info(f"Market Info: {market_info}")
        
        # 6. Connect WS
        market_id = self.market_map[target_symbol]
        logger.info(f"Subscribing to OrderBook for {self.strategy_config.symbol} (ID: {market_id})...")
        
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
            token_provider=self._get_fresh_token
        ) # type: ignore
        
        if self.broadcaster:
            # Broadcast Config and Info
            # Convert config to dict via json dump/load to handle custom types if any
            # Or assume to_dict/dict() works
            # We use json default serializer if needed, but config object should be standard
            # Broadcast Config
            try:
                # Custom serialization for Enums to match frontend schema (lowercase)
                
                # Check if it's a dataclass or object with __dict__
                if hasattr(self.strategy_config, "__dict__"):
                    config_dict = self.strategy_config.__dict__.copy()
                else:
                    # Fallback if somehow it's a dict or other
                    logger.warning(f"Config object {type(self.strategy_config)} lacks __dict__, using vars() or dict(self.strategy_config)")
                    if is_dataclass(self.strategy_config):
                         config_dict = asdict(self.strategy_config)
                    else:
                        try:
                            config_dict = dict(self.strategy_config)
                        except:
                            config_dict = vars(self.strategy_config)

                # Convert Enums to lowercase strings explicitly
                if "grid_type" in config_dict:
                     val = config_dict["grid_type"]
                     if hasattr(val, "value"):
                         config_dict["grid_type"] = str(val.value).lower()
                     else:
                         # Already a string or simple type
                         config_dict["grid_type"] = str(val).lower()
                
                if "grid_bias" in config_dict:
                     val = config_dict["grid_bias"]
                     if hasattr(val, "value"):
                         config_dict["grid_bias"] = str(val.value).lower()
                     elif val:
                         config_dict["grid_bias"] = str(val).lower()

                # Add decimals from market info
                if self.strategy_config.symbol in self.market_map:
                    market_id = self.market_map[self.strategy_config.symbol]
                    m = self.markets[self.strategy_config.symbol]
                    config_dict["sz_decimals"] = m.sz_decimals
                    config_dict["px_decimals"] = m.price_decimals

                self.broadcaster.send(btypes.config_event(config_dict))
                logger.info("Broadcasted initial StrategyConfig.")

            except Exception as e:
                logger.error(f"Failed to broadcast initial config: {e}", exc_info=True)
                # print(f"DEBUG: Config Broadcast Failed: {e}") # Fallback output

            # Broadcast Info
            try:
                self.broadcaster.send(btypes.info_event(self.exchange_config.network)) # Using Network Name
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
                        [msg_task, shutdown_task],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    if self._shutdown_event.is_set():
                        for p in pending: p.cancel()
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
                    if isinstance(summary, PerpGridSummary): # Perp
                        self.broadcaster.send(btypes.perp_grid_summary_event(summary))
                    else:
                        # Assuming SpotGridSummary if not Perp
                        self.broadcaster.send(btypes.spot_grid_summary_event(cast(SpotGridSummary, summary)))
                
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








    def _find_cloid_by_oid(self, oid: int) -> Optional[Cloid]:
        """
        Finds the CLOID associated with a given Order ID (OID).
        """
        for cloid, pending in self.pending_orders.items():
            if pending.oid == oid:
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
            
            #logger.info(f"Price Update: {symbol} @ ${rounded_price:.{market_info.price_decimals}f}")
            
            if self.broadcaster:
                # Broadcast expects float usually? Let's cast for broadcast only if needed
                # But let's check if btypes can handle Decimal or if we should cast.
                # Usually JSON serialization handles int/float/str. Decimal might need casting.
                # For safety, cast to float or str for broadcast.
                 self.broadcaster.send(btypes.market_update_event(btypes.MarketEvent(price=float(rounded_price))))

            try:
                self.strategy.on_tick(rounded_price, self.ctx)
                await self.process_order_queue()
            except ValueError as e:
                # ValueError during initialization indicates a fatal configuration error
                # Let it propagate to the upper layer (main.py) which will handle shutdown
                logger.error(f"Strategy Initialization Error: {e}")
                raise
            except Exception as e:
                logger.error(f"Strategy Error on_tick (mid_price): {e}")


    async def _handle_open_orders_msg(self, account_id: str, orders_data: dict):
        """
        Process orders updates from account_all_orders channel.
        Updates pending_orders state (OID, info) and handles failures.
        Fills are handled in _handle_user_fills_msg.
        """
        if not self.ctx:
             return
        

        
        # Extract orders
        # Format: {"channel": "...", "orders": {"{MARKET_INDEX}": [Order]}, "type": "..."}
        orders_by_market = orders_data.get("orders", {})
        
        # Process all orders in the message
        for market_index, orders_list in orders_by_market.items():
            for order_dict in orders_list:
                try:
                    order = self._parse_order(order_dict)
                    logger.info(f"[Order] {order}")
                    
                    if not order.cloid_id:
                        logger.warning(f"Order missing cloid_id: {order}")
                        continue
                    
                    
                    cloid = Cloid(order.cloid_id)
                    
                    # Only process if we're tracking this order
                    if cloid not in self.pending_orders:
                        logger.warning(f"[Order] not tracked: {order}")
                        continue
                    
                    pending = self.pending_orders[cloid]
                    
                    # 1. Update OID if missing (Crucial for later cancellation/audit)
                    if order.order_id and not pending.oid:
                        pending.oid = order.order_id
                        
                        # Resolve Symbol to get Base Asset
                        logger.info(f"[ORDER_TRACKING] cloid = {cloid.as_int()}, LIMIT {pending.side} {pending.target_size} {self._get_base_asset(int(market_index))} @ {pending.price}")
                        
                        if self.broadcaster:
                             self.broadcaster.send(btypes.order_update_event(btypes.OrderEvent(
                                 oid=order.order_id,
                                 cloid=str(cloid),
                                 side=str(pending.side),
                                 price=float(pending.price),
                                 size=float(pending.target_size),
                                 status="open",
                                 fee=0.0,
                                 is_taker=False
                             )))
                    

                    
                    # 3. Check order status
                    if self._is_canceled_status(order.status):
                        logger.info(f"[ORDER_CANCELED] {cloid} - status: {order.status}")
                        
                        if self.broadcaster:
                             self.broadcaster.send(btypes.order_update_event(btypes.OrderEvent(
                                 oid=pending.oid or 0,
                                 cloid=str(cloid),
                                 side=str(pending.side),
                                 price=0.0,
                                 size=float(pending.filled_size),
                                 status="cancelled",
                                 fee=0.0,
                                 is_taker=False
                             )))

                        del self.pending_orders[cloid] 
                        
                        try:
                            failure = OrderFailure(
                                cloid=cloid,
                                side=pending.side,
                                target_size=pending.target_size,
                                filled_size=pending.filled_size,
                                filled_price=pending.weighted_avg_px,
                                accumulated_fees=pending.accumulated_fees,
                                failure_reason=order.status,
                                reduce_only=pending.reduce_only
                            )
                            self.strategy.on_order_failed(failure, self.ctx)
                        except Exception as e:
                            logger.error(f"Strategy on_order_failed error: {e}")
                        
                        self.completed_cloids.add(cloid)
                    
                    # Note: We intentionally DO NOT handle "filled" or "closed" status here.
                    # We rely on _handle_user_fills_msg to parse the trade/fill event 
                    # and trigger on_order_filled. This avoids race conditions and duplicate events.

                except Exception as e:
                    logger.error(f"Error processing order: {e}, order data: {order_dict}")
        




    async def _handle_user_fills_msg(self, account_id: str, trades_data: dict):
        """
        Process trades updates from account_all_trades channel.
        This provides real-time fill/trade data.
        """
        if not self.ctx:
             return
             
        trades_by_market = trades_data.get("trades", {})
        
        # Process all trades across all markets
        for market_index, trades_list in trades_by_market.items():
            for trade_dict in trades_list:
                try:
                    trade = self._parse_trade(trade_dict)
                    
                    # Match trade to our account to find CLOID and Side
                    # Use self.account_index directly as it is the source of truth
                    assert self.account_index is not None
                    details = trade.get_trade_details(self.account_index)
                    if not details:
                         logger.warning(f"Ignored trade (not involving account {self.account_index}): {trade}")
                         continue

                         
                    side = details.side
                    oid = details.oid
                    role = details.role
                    
                    # Calculate Fee in USD
                    fee = self._calculate_fee_usd(details)
                        
                    # Resolve CLOID from OID
                    cloid = self._find_cloid_by_oid(oid)
                    
                    if not cloid:
                        logger.warning(f"Trade matched account but CLOID not found for OID {oid}: {trade}")
                        continue
                    
                    logger.info(f"[Trade] cloid = {cloid.as_int()}, {details}")
                    
                    # Idempotency check
                    if cloid and cloid in self.completed_cloids:
                        logger.info(f"Ignored duplicate fill for completed cloid: {cloid}")
                        continue
                    
                    # Process fill
                    if cloid and cloid in self.pending_orders:
                        # Accumulate partial fill
                        pending = self.pending_orders[cloid]
                        
                        new_total_size = pending.filled_size + trade.size
                        
                        # Calculate weighted average price
                        # pending.weighted_avg_px is Decimal, trade.price is Decimal
                        pending.weighted_avg_px = (
                            (pending.weighted_avg_px * pending.filled_size) + (trade.price * trade.size)
                        ) / new_total_size
                        
                        pending.filled_size = new_total_size
                        pending.accumulated_fees += fee
                        
                        # Check if fully filled (using 0.9999 threshold like Rust SDK)
                        is_fully_filled = pending.filled_size >= (pending.target_size * Decimal("0.9999"))
                        
                        if is_fully_filled:
                            # Order is fully filled - notify strategy
                            logger.info(
                                f"[ORDER_FILLED] {side} {pending.filled_size} @ {pending.weighted_avg_px} (Fee: {pending.accumulated_fees:.4f})"
                            )
                            
                            if self.broadcaster:
                                self.broadcaster.send(btypes.order_update_event(btypes.OrderEvent(
                                    oid=pending.oid or 0,
                                    cloid=str(cloid),
                                    side=str(side),
                                    price=float(pending.weighted_avg_px),
                                    size=float(pending.filled_size),
                                    status="filled",
                                    fee=float(pending.accumulated_fees),
                                    is_taker=role.is_taker()
                                )))
                            
                            final_px = pending.weighted_avg_px
                            final_sz = pending.filled_size
                            final_fee = pending.accumulated_fees
                            pending_reduce_only = pending.reduce_only
                            
                            # Remove from pending
                            del self.pending_orders[cloid]
                            
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
                                        raw_dir=None
                                    ),
                                    self.ctx
                                )
                            except Exception as e:
                                logger.error(f"Strategy on_order_filled error: {e}")
                            
                            # Mark as completed for idempotency
                            self.completed_cloids.add(cloid)
                        else:
                            logger.info(
                                f"[ORDER_FILL_PARTIAL] {side} {trade.size} @ {trade.price} (Fee: {fee:.4f})"
                            )
                    
                    elif cloid:
                        logger.info(
                            f"[ORDER_FILL_UNTRACKED] {side} {trade.size} @ {trade.price} (Fee: {fee:.4f})"
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
                                    raw_dir=None
                                ),
                                self.ctx
                            )
                        except Exception as e:
                            logger.error(f"Strategy on_order_filled error: {e}")
                    
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
                                    raw_dir=None
                                ),
                                self.ctx
                            )
                        except Exception as e:
                            logger.error(f"Strategy on_order_filled error: {e}")
                        
                except Exception as e:
                    logger.error(f"Error processing trade: {e}, trade data: {trade_dict}")



    
    
    async def handle_reconciliation(self):
        """
        Reconcile local pending orders with exchange state.
        Handles missed fills and cancellations.
        """
        if not self.pending_orders or not self.api_client or not self.account_index or not self.ctx:
            return

        # Snapshots to avoid modification during iteration if we remove items
        pending_snapshot = list(self.pending_orders.items())
        
        # 1. Fetch Active Orders
        try:
            market_id = self.market_map.get(self.strategy_config.symbol)
            if not market_id:
                return
            
            # Token generation and account index are now handled inside BaseEngine
            active_orders_list = await self.get_active_orders(
                market_id=market_id
            )
            # Active orders from SDK are lighter objects.
            # get_active_orders inherits from BaseEngine and returns List[lighter.models.Order] (or similar)
            # Use client_order_index as key
            active_orders_map = {o.client_order_index: o for o in active_orders_list}
            
        except Exception as e:
            logger.error(f"Reconciliation failed to fetch active orders: {e}")
            return

        # 2. Iterate pending orders to identify missing ones vs active ones
        now = time.time()
        missing_cloids: List[Cloid] = []
        
        for cloid, pending in pending_snapshot:
            # Skip recently created orders (grace period for propagation)
            if now - pending.created_at < 10:
                continue
                
            cloid_int = cloid.as_int()
            
            # Case A: Order is active on exchange
            if cloid_int in active_orders_map:
                active_order = active_orders_map[cloid_int]
                
                # Check for partial fills that might have been missed
                # active_order.filled_base_amount is a string in Lighter model
                filled_amount = Decimal(active_order.filled_base_amount)
                
                if filled_amount > pending.filled_size:
                    logger.info(f"[RECONCILIATION] Found missed fill for {cloid}: {filled_amount} > {pending.filled_size}")
                    
                    diff = filled_amount - pending.filled_size
                    pending.filled_size = filled_amount
                    
                    # Notify strategy of partial fill update?
                    pass
            
            # Case B: Order is not in active list -> potentially inactive (filled/canceled) or lost
            else:
                missing_cloids.append(cloid)

        # 3. Batch Fetch Inactive Orders if we have missing items
        if not missing_cloids:
            return

        inactive_orders_map = {}
        try:
             # Fetch a reasonable batch size of history. 
             # If we have many missing orders, we might need a larger limit or multiple pages, 
             # but 50-100 is usually sufficient for "recent" changes.
             inactive_orders_list = await self.get_inactive_orders(
                limit=100, 
                market_id=market_id
            )
             if inactive_orders_list:
                 inactive_orders_map = {o.client_order_index: o for o in inactive_orders_list}
        except Exception as e:
            logger.error(f"Reconciliation error fetching inactive orders: {e}")
            # If this fails, we can't safely judge missing orders this cycle.
            return

        # 4. Process Missing Orders
        for cloid in missing_cloids:
            cloid_int = cloid.as_int()
            missing_pending = self.pending_orders.get(cloid)
            
            # Re-check existence in case it was modified concurrently (unlikely in single event loop but good practice)
            if not missing_pending:
                continue

            if cloid_int in inactive_orders_map:
                found_inactive = inactive_orders_map[cloid_int]
                status = found_inactive.status
                filled_amount = Decimal(found_inactive.filled_base_amount)
                
                if status == "filled" or (filled_amount >= missing_pending.target_size * Decimal("0.9999")):
                     logger.info(f"[RECONCILIATION] Order {cloid} found FILLED in history.")
                     
                     diff_size = filled_amount - missing_pending.filled_size
                     missing_pending.filled_size = filled_amount
                     
                     # Remove from pending
                     if cloid in self.pending_orders:
                         del self.pending_orders[cloid]
                     
                     # Fallback price if weighted avg not fully tracked
                     try:
                        fill_price = Decimal(found_inactive.price) 
                     except:
                        fill_price = missing_pending.price

                     # Notify Strategy if we missed the fill event
                     if diff_size > 0:
                         self.strategy.on_order_filled(
                             OrderFill(
                                 side=missing_pending.side,
                                 size=diff_size,
                                 price=fill_price,
                                 fee=Decimal("0"), # Unknown fee
                                 role=TradeRole.TAKER, # Unknown role
                                 cloid=cloid,
                                 reduce_only=missing_pending.reduce_only,
                                 raw_dir=None
                             ),
                             self.ctx
                         )
                     self.completed_cloids.add(cloid)

                elif self._is_canceled_status(status):
                    logger.info(f"[RECONCILIATION] Order {cloid} found CANCELED/FAILED ({status}).")
                    if cloid in self.pending_orders:
                         del self.pending_orders[cloid]
                    
                    self.strategy.on_order_failed(
                        OrderFailure(
                            cloid=cloid,
                            side=missing_pending.side,
                            target_size=missing_pending.target_size,
                            filled_size=filled_amount,
                            filled_price=missing_pending.weighted_avg_px,
                            accumulated_fees=missing_pending.accumulated_fees,
                            failure_reason=status,
                            reduce_only=missing_pending.reduce_only
                        ),
                        self.ctx
                    )
                    self.completed_cloids.add(cloid)
            else:
                 # Case C: Not active, Not in recent inactive.
                 # Could be "Lost" or "Expired"
                 if now - missing_pending.created_at > 120:
                      logger.warning(f"[RECONCILIATION] Order {cloid} lost (not found in active or recent history). Marking failed.")
                      if cloid in self.pending_orders:
                          del self.pending_orders[cloid]
                      
                      self.strategy.on_order_failed(
                        OrderFailure(
                            cloid=cloid,
                            side=missing_pending.side,
                            target_size=missing_pending.target_size,
                            filled_size=missing_pending.filled_size,
                            filled_price=missing_pending.weighted_avg_px,
                            accumulated_fees=missing_pending.accumulated_fees,
                            failure_reason="lost_in_reconciliation",
                            reduce_only=missing_pending.reduce_only
                        ),
                        self.ctx
                    )

    async def _reconciliation_loop(self):
        logger.info("Reconciliation Loop Started...")
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=RECONCILIATION_INTERVAL_SECONDS)
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

    async def process_order_queue(self):
        if not self.ctx or not self.ctx.order_queue:
            return
        
        assert self.ctx is not None
        assert self.ctx.order_queue is not None # If order_queue is Optional
        
        # Drain queue
        orders_to_process = list(self.ctx.order_queue)
        self.ctx.order_queue.clear()
        
        # Batching logic
        # Lighter supports batch transactions.
        
        
        tx_types: List[int] = []
        tx_infos: List[str] = []
        
        # Context to map back results
        batch_context = [] 
        
        # Get API key for this batch - all orders must use the same API key
        # but each needs a unique nonce
        batch_api_key_index = None
        
        # Process Orders
        for i, order in enumerate(orders_to_process):
            market_id = self.market_map.get(order.symbol)
            if market_id is None:
                logger.error(f"Market ID not found for {order.symbol}")
                continue
                
            if not self.signer_client:
                logger.error("Signer client not initialized")
                continue
            
            # Use Cloid directly as client_order_index
            # The strategy generates Cloid which is already an integer identifier
            if not order.cloid:
                logger.error(f"Order missing cloid: {order}")
                continue
            
            client_order_index = order.cloid.as_int()
            
            # For first order, get a new API key. For subsequent orders, reuse the same key
            # All transactions in a batch must use the same API key but different nonces
            if i == 0:
                batch_api_key_index, nonce = self.signer_client.nonce_manager.next_nonce()
                assert batch_api_key_index is not None
            else:
                # Using ApiNonceManager, next_nonce() fetches from server which hasn't seen our txs yet
                # So we must manually increment the nonce for the batch
                nonce += 1
            
            assert batch_api_key_index is not None
            
            error = None
            tx_type = None
            tx_info = None
            
            if isinstance(order, LimitOrderRequest):
                # Track pending order for partial fill accumulation
                # PendingOrder uses Decimal fields
                # order.sz and order.price are already Decimal (from LimitOrderRequest in model.py)
                self.pending_orders[order.cloid] = PendingOrder(
                    target_size=order.sz,
                    side=order.side,
                    filled_size=Decimal("0"),
                    weighted_avg_px=Decimal("0"),
                    accumulated_fees=Decimal("0"),
                    reduce_only=order.reduce_only,
                    oid=None,  # Will be set when we get confirmation
                    created_at=time.time(),  # Track when order was placed
                    price=order.price,
                )
                
                info = self.ctx.market_info(order.symbol)
                if not info:
                     logger.error(f"Market info not found for {order.symbol}")
                     continue

                tx_type, tx_info, _, error = self.signer_client.sign_create_order(
                    market_index=market_id,
                    client_order_index=client_order_index,
                    base_amount=info.to_sdk_size(order.sz),
                    price=info.to_sdk_price(order.price),
                    is_ask=order.side.is_sell(),
                    order_type=self.signer_client.ORDER_TYPE_LIMIT,
                    time_in_force=self.signer_client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME, # GTC
                    reduce_only=order.reduce_only,
                    trigger_price=0,
                    nonce=nonce,
                    api_key_index=batch_api_key_index
                )

            
            elif isinstance(order, MarketOrderRequest):
                info = self.ctx.market_info(order.symbol)
                if not info:
                     logger.error(f"Market info not found for {order.symbol}")
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
                    api_key_index=batch_api_key_index
                )

            elif isinstance(order, CancelOrderRequest):
                # Use the Cloid directly as the order_index to cancel
                target_index = order.cloid.as_int()
                
                tx_type, tx_info, _, error = self.signer_client.sign_cancel_order(
                    market_index=market_id,
                    order_index=target_index,
                    nonce=nonce,
                    api_key_index=batch_api_key_index
                )


            if error:
                 logger.error(f"Signing Error: {error}")
                 # Callback?
                 continue
            
            assert tx_type is not None
            tx_types.append(cast(int, tx_type))
            # Ensure tx_info is valid (str) if present, else empty string or similar if allowed.
            # SDK likely returns str.
            tx_infos.append(str(tx_info) if tx_info is not None else "")
            batch_context.append(order)
        
        # Lighter supports up to 50 transactions per batch
        MAX_BATCH_SIZE = 49
        
        # Process orders in chunks of MAX_BATCH_SIZE using REST API
        for batch_start in range(0, len(tx_types), MAX_BATCH_SIZE):
            batch_end = min(batch_start + MAX_BATCH_SIZE, len(tx_types))
            batch_tx_types = tx_types[batch_start:batch_end]
            batch_tx_infos = tx_infos[batch_start:batch_end]
            batch_num = (batch_start // MAX_BATCH_SIZE) + 1
            total_batches = (len(tx_types) + MAX_BATCH_SIZE - 1) // MAX_BATCH_SIZE
            
            try:
                # Use REST API to send batch and get response
                assert self.signer_client is not None
                response = await self.signer_client.send_tx_batch(
                    tx_types=batch_tx_types,
                    tx_infos=batch_tx_infos
                )
                
                if response.code == 200:
                    logger.info(f"Batch {batch_num}/{total_batches} accepted: {len(response.tx_hash)} orders")
                    
                    # TODO: Track tx_hashes for order confirmation
                    # We can map client_order_index to tx_hash here
                else:
                    logger.error(f"Batch {batch_num}/{total_batches} failed: code={response.code}, message={response.message}")
                    
            except Exception as e:
                logger.error(f"Failed to send batch {batch_num}/{total_batches}: {e}")



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
                asyncio.create_task(self._reconciliation_loop())
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
                
                # Close sessions
                logger.info("Closing API client sessions...")
                if self.signer_client and self.signer_client.api_client:
                    await self.signer_client.api_client.close()
                if self.api_client:
                    await self.api_client.close()
                
                logger.info("Engine Stopped.")

    async def stop(self):
        logger.info("Stopping Engine...")
        self.running = False
        self._shutdown_event.set()
        if self.ws_client:
            # self.ws_client.stop() # method might not exist, relying on task cancellation
            pass
        if self.broadcaster:
            await self.broadcaster.stop()
