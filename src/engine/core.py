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
        # Determine host based on some config or default to testnet?
        # User environment didn't specify. Assuming Testnet for now as getting started.
        # But should be configurable.
        # self.api_client = lighter.ApiClient(configuration=lighter.Configuration(host="https://testnet.zklighter.elliot.ai"))
        # Using default from config if not provided?
        # Let's assume production/default unless specified.
        # lighter-python Configuration.get_default() might be set.
        self.api_client = lighter.ApiClient() 
        
        # 2. Fetch Account Index (Already in Config)
        if self.exchange_config.account_index > 0:
            self.account_index = self.exchange_config.account_index
            logger.info(f"Using Account Index from Config: {self.account_index}")
        else:
             # Legacy or address provided?
             logger.info(f"Fetching account index for {self.exchange_config.master_account_address}...")
             account_api = lighter.AccountApi(self.api_client)
        try:
            account_info = await account_api.account(by="l1_address", value=self.exchange_config.master_account_address)
            # Response structure: {'index': 65, 'l1_address': '...', ...}
            # Or maybe inside a 'data' field?
            # Assuming direct dict or object. 
            if isinstance(account_info, dict):
                self.account_index = int(account_info['index'])
            else:
                self.account_index = int(account_info.index) # type: ignore
            logger.info(f"Account Index: {self.account_index}")
        except Exception as e:
            logger.error(f"Failed to fetch account info: {e}")
            raise

        # 3. Setup Signer Client
        # Using configured API Key Index
        self.signer_client = lighter.SignerClient(
            url=self.exchange_config.base_url,
            account_index=self.account_index,
            api_private_keys={self.exchange_config.agent_key_index: self.exchange_config.agent_private_key}
        )
        
        # 4. Load Metadata (Markets)
        logger.info("Loading Market Metadata...")
        order_api = lighter.OrderApi(self.api_client)
        order_books_result = await order_api.order_books()
        # Mypy/SDK: returns (order_books, headers) or similar tuple?
        # If tuple, take first element.
        if isinstance(order_books_result, tuple):
             order_books = order_books_result[0]
        else:
             order_books = order_books_result
        
        markets_info = {}
        
        # Handle order_books structure.
        # It might be list of dicts.
        if isinstance(order_books, dict):
             iterator = order_books.values()
        else:
             iterator = order_books

        for ob_raw in iterator:
            ob: Any = ob_raw
            # Struct: {'id': 0, 'symbol': 'ETH-USDC', 'price_precision': 2, 'size_precision': 4, ...}
            # Need to verify actual fields.
            # Assuming 'symbol' and 'id'.
            # Based on examples, index=0 implies ID.
            mid = ob.get('id')
            symbol = ob.get('symbol')
            # Lighter might strictly use ID.
            if mid is not None:
                # Mocking symbol if not present because SDK examples use ID.
                # If symbol not in OB, we need static map or fetch another endpoint.
                # Assuming 'symbol' exists or we can infer.
                if not symbol: 
                     # Fallback or error.
                     # For now, if we match config symbol by ID provided in config? 
                     # No, config has "ETH/USDC". Lighter might use "ETH-USDC".
                     # We might need a mapping.
                     pass
                
                # Adapting symbol format
                normalized_symbol = symbol.replace("-", "/") if symbol else f"Unknown-{mid}"
                
                self.market_map[normalized_symbol] = mid
                self.reverse_market_map[mid] = normalized_symbol
                
                # Create MarketInfo
                # precisions?
                # Assuming default or fetching from API
                # ob might have 'pricePrecision', 'sizePrecision'
                price_decimals = ob.get('pricePrecision', 2)
                sz_decimals = ob.get('sizePrecision', 4)
                
                markets_info[normalized_symbol] = MarketInfo(
                    symbol=normalized_symbol,
                    coin=normalized_symbol.split('/')[0],
                    asset_index=mid,
                    price_decimals=price_decimals,
                    sz_decimals=sz_decimals
                )

        target_symbol = self.config.symbol
        if target_symbol not in markets_info:
             # Try to find partial match
             found = False
             for s, info in markets_info.items():
                 if s.replace("/", "") == target_symbol.replace("/", ""):
                     markets_info[target_symbol] = info
                     self.market_map[target_symbol] = info.asset_index
                     self.reverse_market_map[info.asset_index] = target_symbol
                     found = True
                     break
             if not found:
                 raise ValueError(f"Symbol {target_symbol} not found in Lighter markets: {list(markets_info.keys())}")

        self.ctx = StrategyContext(markets_info)
        
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
            if self.ctx:
                info = self.ctx.market_info(symbol)
                if info:
                    info.last_price = mid_price
                
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
