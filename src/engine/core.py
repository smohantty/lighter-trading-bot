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
        
        market_info = self.ctx.market_info(target_symbol)
        logger.info(f"Market Info: {market_info}")
        
        # 5. Connect WS
        market_id = self.market_map[target_symbol]
        logger.info(f"Connecting WS for Market {market_id}...")
        
        self.ws_client = lighter.WsClient(
            order_book_ids=[market_id],
            account_ids=[self.account_index],
            on_order_book_update=self.on_order_book_update,
            on_account_update=self.on_account_update
        )

    def on_order_book_update(self, market_id: str, order_book: dict):
        # Handle Tick
        # order_book has 'asks', 'bids'.
        # Calculate Mid Price
        market_id_int = int(market_id)
        symbol = self.reverse_market_map.get(market_id_int)
        if not symbol or symbol != self.config.symbol:
            return

        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])
        
        best_bid = float(bids[0]['price']) if bids else 0.0
        best_ask = float(asks[0]['price']) if asks else 0.0
        
        mid_price = 0.0
        if best_bid > 0 and best_ask > 0:
            mid_price = (best_bid + best_ask) / 2.0
        elif best_bid > 0:
            mid_price = best_bid
        elif best_ask > 0:
            mid_price = best_ask
            
        if mid_price > 0:
            # Run Strategy Tick
            try:
                if self.ctx:
                    self.strategy.on_tick(mid_price, self.ctx)
                    # Ensure pending orders are processed
                    asyncio.create_task(self.process_order_queue())
            except Exception as e:
                logger.error(f"Strategy Error on_tick: {e}")

    def on_account_update(self, account_id: str, account_data: dict):
        # Parse account update for fills and balances
        # account_data structure: {'orders': [...], 'balances': [...]}
        # Or detailed event?
        # Lighter WS sends 'update/account_all' with full state or delta?
        # SDK ws_client merges state.
        
        # Need to detect Fills. 
        # Typically we diff orders or listen to specific events?
        # For now, let's look at orders processing.
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
                    base_amount=int(order.sz * (10**info.sz_decimals)), # Convert to Atomic Units?
                    price=int(order.price * (10**info.price_decimals)), # Convert
                    is_ask=order.side.is_sell(),
                    order_type=1, # Limit
                    time_in_force=200000, # GTC
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

        if tx_types:
             # Send Batch via WS
             # Note: WSClient doesn't have `send_batch_tx` method built-in in the class, 
             # but Utils has it. We should probably implement it in Engine or use raw ws.
             
             # Payload construction matches example
             payload = {
                 "type": "jsonapi/sendtxbatch",
                 "data": {
                     "id": f"batch_{int(time.time()*1000)}",
                     "tx_types": json.dumps(tx_types),
                     "tx_infos": json.dumps(tx_infos)
                 }
             }
             
             try:
                 # Ensure we have WS connection
                 if self.ws_client and self.ws_client.ws:
                     # ws_client.ws might be Sync protocol if using run() or Async if run_async?
                     # We assume run_async.
                     await self.ws_client.ws.send(json.dumps(payload))
                     # We can wait for response?
                     # response = await self.ws_client.ws.recv()
                     # logger.info(f"Batch Sent: {response}")
                     pass
                 else:
                     logger.error("WS Client not connected")
             except Exception as e:
                 logger.error(f"Failed to send batch: {e}")

    async def run(self):
        await self.initialize()
        self.running = True
        logger.info("Engine Running...")
        
        # Start WS Loop
        if self.ws_client:
            await self.ws_client.run_async()

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
