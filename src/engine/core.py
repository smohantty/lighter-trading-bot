import asyncio
import logging
import json
import time
from typing import Dict, List, Optional, Any
import websockets
import lighter

from src.config import StrategyConfig, ExchangeConfig
from src.model import Cloid, OrderRequest, LimitOrderRequest, MarketOrderRequest, CancelOrderRequest, OrderFill, OrderSide
from src.strategy.base import Strategy
from src.engine.context import StrategyContext, MarketInfo, Balance
from src.strategy.types import PerpGridSummary, GridState

logger = logging.getLogger(__name__)

# Constants (Move to Lighter Constants if available)
RECONNECT_DELAY = 5.0

class Engine:
    def __init__(self, config: StrategyConfig, exchange_config: ExchangeConfig, strategy: Strategy):
        self.config = config
        self.exchange_config = exchange_config
        self.strategy = strategy
        
        self.ctx: Optional[StrategyContext] = None
        self.market_map: Dict[str, int] = {} # Symbol -> MarketID
        self.reverse_market_map: Dict[int, str] = {} # MarketID -> Symbol
        self.markets: Dict[str, MarketInfo] = {} # Symbol -> MarketInfo
        
        self.cloid_to_index: Dict[Cloid, int] = {}
        self.index_to_cloid: Dict[int, Cloid] = {}
        
        # Clients
        self.api_client: Optional[lighter.ApiClient] = None
        self.signer_client: Optional[lighter.SignerClient] = None
        self.ws_client: Optional[lighter.WsClient] = None
        
        self.account_index: Optional[int] = None
        self.running = False
        
        # Counters
        self.order_client_index_counter = int(time.time() * 1000) % 10000000 # Random start
        
        self.event_queue = asyncio.Queue()
        self._shutdown_event = asyncio.Event()
        
    async def initialize(self):
        logger.info("Initializing Engine...")
        
        # 1. Setup API Client
        logger.info(f"Initializing API Client with URL: {self.exchange_config.base_url}")
        # Log simulation mode status
        if self.exchange_config.simulation_mode:
            logger.warning("[SIMULATION MODE ENABLED] Orders will be logged but NOT sent to the exchange")
        
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
        self.signer_client = lighter.SignerClient(
            url=self.exchange_config.base_url,
            account_index=self.account_index,
            api_private_keys={self.exchange_config.agent_key_index: self.exchange_config.agent_private_key}
        )
        
        # 4. Load Metadata (Markets)
        await self._load_markets()

        target_symbol = self.config.symbol
        if target_symbol not in self.markets:
             raise ValueError(f"Symbol {target_symbol} not found in Lighter markets: {list(self.markets.keys())}")

        self.ctx = StrategyContext(self.markets)
        
        # 5. Fetch Account Balances
        await self._fetch_account_balances()
        
        market_info = self.ctx.market_info(target_symbol)
        logger.info(f"Market Info: {market_info}")
        
        # 6. Connect WS
        market_id = self.market_map[target_symbol]
        logger.info(f"Connecting WS for Market {market_id}...")
        
        self.ws_client = lighter.QueueWsClient(
            order_book_ids=[market_id],
            account_ids=[self.account_index],
            queue=self.event_queue
        )

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
                elif msg_type == "account":
                    # data is the account update
                    await self._handle_account_msg(target_id, data)
                
                self.event_queue.task_done()
            except Exception as e:
                logger.error(f"Error in message processor: {e}")
                await asyncio.sleep(1)

    async def _fetch_account_balances(self):
        """Fetch account balances from the API and update StrategyContext."""
        if not self.api_client or not self.ctx:
            return
        
        try:
            account_api = lighter.AccountApi(self.api_client)
            account_data = await account_api.account(
                by="index",
                value=str(self.account_index)
            )
            
            if not account_data or not account_data.accounts:
                logger.warning("No account data returned from API")
                return
            
            # Get the first account (should be our account)
            account = account_data.accounts[0]
            
            # Update spot balances from assets
            if account.assets:
                for asset in account.assets:
                    total_balance = float(asset.balance)
                    locked_balance = float(asset.locked_balance)
                    available_balance = total_balance - locked_balance
                    
                    self.ctx.update_spot_balance(
                        asset=asset.symbol,
                        total=total_balance,
                        available=available_balance
                    )
                    logger.info(f"Spot Balance: {asset.symbol} - Total: {total_balance}, Available: {available_balance}")
            
            # Update perp balances (collateral)
            if account.collateral:
                collateral = float(account.collateral)
                available = float(account.available_balance) if account.available_balance else collateral
                
                # For perps, we track USDC collateral
                self.ctx.update_perp_balance(
                    asset="USDC",
                    total=collateral,
                    available=available
                )
                logger.info(f"Perp Collateral: USDC - Total: {collateral}, Available: {available}")
                
        except Exception as e:
            logger.error(f"Failed to fetch account balances: {e}")
            raise

    async def _handle_mid_price_msg(self, market_id: str, mid_price: float):
        market_id_int = int(market_id)
        symbol = self.reverse_market_map.get(market_id_int)
        if not symbol or symbol != self.config.symbol:
            return

        if self.ctx:
            try:
                self.strategy.on_tick(mid_price, self.ctx)
                await self.process_order_queue()
            except ValueError as e:
                # ValueError during initialization indicates a fatal configuration error
                # Let it propagate to the upper layer (main.py) which will handle shutdown
                logger.error(f"Strategy Initialization Error: {e}")
                raise
            except Exception as e:
                logger.error(f"Strategy Error on_tick (mid_price): {e}")

    async def _handle_account_msg(self, account_id: str, account_data: dict):
        # Process fills or balance updates if needed
        pass

        pass
    
    async def process_order_queue(self):
        if not self.ctx or not self.ctx.order_queue:
            return
        
        # Drain queue
        orders_to_process = list(self.ctx.order_queue)
        self.ctx.order_queue.clear()
        
        # Batching logic
        # Lighter supports batch transactions.
        
        tx_types = []
        tx_infos = []
        
        # Context to map back results
        batch_context = [] 
        
        # Process Orders
        for order in orders_to_process:
            self.order_client_index_counter += 1
            client_order_index = self.order_client_index_counter
            
            market_id = self.market_map.get(order.symbol)
            if market_id is None:
                logger.error(f"Market ID not found for {order.symbol}")
                continue
                
            if not self.signer_client:
                logger.error("Signer client not initialized")
                continue
            
            # nonce_manager.next_nonce() might return tuple (index, nonce)
            # Review SDK or assuming it returns (api_key_index, nonce)
            # Mypy checks:
            api_key_index, nonce = self.signer_client.nonce_manager.next_nonce()
            
            error = None
            tx_type = None
            tx_info = None
            
            if isinstance(order, LimitOrderRequest):
                if order.cloid:
                    self.cloid_to_index[order.cloid] = client_order_index
                    self.index_to_cloid[client_order_index] = order.cloid
                
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
                    api_key_index=api_key_index
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
                    api_key_index=api_key_index
                )

            elif isinstance(order, CancelOrderRequest):
                # We need order_index (client_order_index) for the order to cancel?
                # or exchange order ID?
                # sign_cancel_order takes `order_index`. Is it client index or exchange index?
                # Lighter uses the index stored in the order tree. 
                # If we tracked it, we use it.
                target_index = self.cloid_to_index.get(order.cloid)
                # Wait, if we use client_order_index, does cancel work by client index?
                # SDK example: order_index=123 (same as create).
                
                if target_index:
                    tx_type, tx_info, _, error = self.signer_client.sign_cancel_order(
                        market_index=market_id,
                        order_index=target_index,
                        nonce=nonce,
                        api_key_index=api_key_index
                    )
                else:
                    logger.error(f"Cannot cancel unknown Cloid: {order.cloid}")
                    continue

            if error:
                 logger.error(f"Signing Error: {error}")
                 # Callback?
                 continue
            
            tx_types.append(tx_type)
            tx_infos.append(tx_info)
            batch_context.append(order)

        # Check if simulation mode is enabled
        if self.exchange_config.simulation_mode:
            logger.info(f"[SIMULATION] Collected {len(tx_types)} orders (not sent to exchange)")
            for i, order in enumerate(batch_context):
                if isinstance(order, LimitOrderRequest):
                    logger.info(f"[SIMULATION] Order {i+1}: LIMIT {order.side} {order.sz} {order.symbol} @ {order.price} (reduce_only={order.reduce_only})")
                elif isinstance(order, MarketOrderRequest):
                    logger.info(f"[SIMULATION] Order {i+1}: MARKET {order.side} {order.sz} {order.symbol} @ {order.price}")
                elif isinstance(order, CancelOrderRequest):
                    logger.info(f"[SIMULATION] Order {i+1}: CANCEL {order.symbol} cloid={order.cloid}")
            return
        
        # Lighter supports up to 50 transactions per batch, but WebSocket has message size limits
        # Reduce batch size to avoid "message too big" errors
        MAX_BATCH_SIZE = 10
        
        # Process orders in chunks of MAX_BATCH_SIZE using REST API
        for batch_start in range(0, len(tx_types), MAX_BATCH_SIZE):
            batch_end = min(batch_start + MAX_BATCH_SIZE, len(tx_types))
            batch_tx_types = tx_types[batch_start:batch_end]
            batch_tx_infos = tx_infos[batch_start:batch_end]
            batch_num = (batch_start // MAX_BATCH_SIZE) + 1
            total_batches = (len(tx_types) + MAX_BATCH_SIZE - 1) // MAX_BATCH_SIZE
            
            try:
                # Use REST API to send batch and get response
                response = await self.signer_client.send_tx_batch(
                    tx_types=batch_tx_types,
                    tx_infos=batch_tx_infos
                )
                
                if response.code == 200:
                    logger.info(f"Batch {batch_num}/{total_batches} accepted: {len(response.tx_hash)} orders, tx_hashes: {response.tx_hash[:3]}...")
                    
                    # TODO: Track tx_hashes for order confirmation
                    # We can map client_order_index to tx_hash here
                else:
                    logger.error(f"Batch {batch_num}/{total_batches} failed: code={response.code}, message={response.message}")
                
                # Wait between batches to respect rate limits
                # Lighter allows 40 requests per 60 seconds = 1 request per 1.5s
                # Use 2s delay to be safe
                if batch_num < total_batches:
                    await asyncio.sleep(2.0)
                    
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
                asyncio.create_task(self._message_processor())
            ]
            
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
            self.ws_client.stop()

    async def _load_markets(self):
        """
        Populate market_map and markets dictionary from API order books.
        """
        logger.info("Loading Market Metadata via OrderApi...")
        self.market_map = {}
        self.reverse_market_map = {}
        self.markets = {} # Symbol -> MarketInfo

        try:
            order_api = lighter.OrderApi(self.api_client)
            response = await order_api.order_book_details()
            
            if isinstance(response, tuple):
                response = response[0]
            
            all_details = []
            
            # SDK returns OrderBookDetails object
            if hasattr(response, "order_book_details"):
                if response.order_book_details:
                    all_details.extend(response.order_book_details)
            
            if hasattr(response, "spot_order_book_details"):
                if response.spot_order_book_details:
                    all_details.extend(response.spot_order_book_details)
            
            # Response might be a dict (raw JSON or mock)
            elif isinstance(response, dict):
                # Perps
                perp_details = response.get("order_book_details")
                if perp_details and isinstance(perp_details, list):
                    all_details.extend(perp_details)
                    
                # Spots
                spot_details = response.get("spot_order_book_details")
                if spot_details and isinstance(spot_details, list):
                    all_details.extend(spot_details)
            
            if not all_details:
                 logger.warning("No market details found in response.")
            
            for item in all_details:
                # Normalize to dict if it's an object (SDK Model)
                if not isinstance(item, dict):
                    if hasattr(item, "to_dict"):
                        item = item.to_dict()
                    elif hasattr(item, "dict"):
                        item = item.dict()
                        
                # Strict Dict Parsing
                if isinstance(item, dict):
                    symbol = item.get("symbol")
                    market_id = item.get("market_id")
                    
                    price_decimals = item.get("price_decimals", 2)
                    size_decimals = item.get("size_decimals", 4)
                    
                    market_type = item.get("market_type", "perp")
                    base_asset_id = item.get("base_asset_id", 0)
                    quote_asset_id = item.get("quote_asset_id", 0)
                    min_base_amount_str = item.get("min_base_amount", "0.0")
                    min_quote_amount_str = item.get("min_quote_amount", "0.0")
                    
                else:
                        raise ValueError(f"Unknown item type in order_books list: {type(item)}")
                    
                if symbol and market_id is not None:
                    mid_int = int(market_id)
                    
                    # Store raw map
                    self.market_map[symbol] = mid_int
                    self.reverse_market_map[mid_int] = symbol
                    
                    # Create MarketInfo
                    info = MarketInfo(
                        symbol=symbol, # Use exact symbol from API
                        coin=symbol.split('/')[0] if '/' in symbol else symbol,
                        market_id=mid_int,
                        price_decimals=int(price_decimals),
                        sz_decimals=int(size_decimals),
                        market_type=market_type,
                        base_asset_id=int(base_asset_id),
                        quote_asset_id=int(quote_asset_id),
                        min_base_amount=float(min_base_amount_str),
                        min_quote_amount=float(min_quote_amount_str)
                    )
                    self.markets[symbol] = info

            logger.info(f"Loaded {len(self.market_map)} symbols into market map.")
            

            
        except Exception as e:
            logger.error(f"Failed to populate market map: {e}")
            pass
