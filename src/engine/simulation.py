import asyncio
import logging
from typing import Optional, Dict, Any, List, Union

import lighter
from src.config import StrategyConfig, ExchangeConfig
from src.strategy.base import Strategy
from src.engine.context import StrategyContext, MarketInfo, Balance
from src.strategy.types import StrategySummary, GridState
from src.model import OrderRequest

logger = logging.getLogger(__name__)

class SimulationEngine:
    """
    A lightweight engine for running strategies in simulation mode (dry run).
    It reuses the EXACT SAME strategy instance as the real engine, but
    intercepts execution (orders) and provides simulated feedback.
    """
    def __init__(self, config: StrategyConfig, exchange_config: ExchangeConfig, strategy: Strategy):
        self.config = config
        self.exchange_config = exchange_config
        self.strategy = strategy
        
        self.ctx: Optional[StrategyContext] = None
        self.markets: Dict[str, MarketInfo] = {}
        self.api_client: Optional[lighter.ApiClient] = None

    async def initialize(self):
        """
        Setup cache, fetch market info and balances.
        """
        # 1. Setup API Client (Read Only)
        api_config = lighter.Configuration(host=self.exchange_config.base_url)
        self.api_client = lighter.ApiClient(configuration=api_config)
        
        # 2. Load Markets
        await self._load_markets()
        
        if self.config.symbol not in self.markets:
             raise ValueError(f"Symbol {self.config.symbol} not found in available markets.")

        # 3. Context
        self.ctx = StrategyContext(self.markets)
        
        # 4. Mock Order Placement
        # We override place_order to just log/store locally instead of queueing for the real engine
        # Although StrategyContext stores in a queue anyway, we just won't have a background processor draining it.
        # So we can just inspect self.ctx.order_queue after a step.
        
        # 5. Fetch Account Balances
        if self.exchange_config.account_index > 0:
            await self._fetch_account_balances()
        else:
            logger.warning("No valid account index provided, using 0 balances.")

    async def run_single_step(self) -> float:
        """
        Fetches current price and runs a single on_tick.
        Returns the fetched price.
        """
        current_price = await self._fetch_current_price()
        if current_price <= 0:
            raise ValueError("Could not determine current market price.")
            
        if self.ctx:
             self.strategy.on_tick(current_price, self.ctx)
             
        return current_price

    def get_summary(self) -> Optional[StrategySummary]:
        if self.ctx:
            return self.strategy.get_summary(self.ctx)
        return None

    def get_grid_state(self) -> Optional[GridState]:
        if self.ctx:
            return self.strategy.get_grid_state(self.ctx)
        return None
    
    def get_orders(self) -> List[OrderRequest]:
        if self.ctx:
            return list(self.ctx.order_queue)
        return []

    async def cleanup(self):
        if self.api_client:
            await self.api_client.close()

    async def _load_markets(self):
        # ... Reuse logic from core.py or extract shared utility ...
        # For now, quick duplication to keep self-contained or better: refactor core.py later.
        # We will duplicate minimal logic to load markets.
        
        try:
            order_api = lighter.OrderApi(self.api_client)
            response = await order_api.order_book_details()
            if isinstance(response, tuple): response = response[0]
            
            # Simple flattener
            all_details = []
            if hasattr(response, "order_book_details"): all_details.extend(response.order_book_details or [])
            if hasattr(response, "spot_order_book_details"): all_details.extend(response.spot_order_book_details or [])
            
            for item in all_details:
                if hasattr(item, "to_dict"): item = item.to_dict()
                if item.get("symbol"):
                    info = MarketInfo(
                        symbol=item["symbol"],
                        coin=item["symbol"].split('/')[0],
                        market_id=int(item["market_id"]),
                        sz_decimals=int(item.get("size_decimals", 4)),
                        price_decimals=int(item.get("price_decimals", 2)),
                        market_type=item.get("market_type", "perp"),
                        base_asset_id=int(item.get("base_asset_id", 0)),
                        quote_asset_id=int(item.get("quote_asset_id", 0)),
                        min_base_amount=float(item.get("min_base_amount", 0)),
                        min_quote_amount=float(item.get("min_quote_amount", 0))
                    )
                    self.markets[info.symbol] = info
                    
        except Exception as e:
            logger.error(f"Failed to load markets: {e}")
            raise

    async def _fetch_account_balances(self):
        try:
            account_api = lighter.AccountApi(self.api_client)
            # Use string for value because SDK expects string for BigInt/Index sometimes
            account_data = await account_api.account(by="index", value=str(self.exchange_config.account_index))
            
            if not account_data or not account_data.accounts: return
            
            account = account_data.accounts[0]
            if account.assets and self.ctx:
                 for asset in account.assets:
                     if float(asset.balance) > 0:
                         self.ctx.update_spot_balance(asset.symbol, float(asset.balance), float(asset.balance) - float(asset.locked_balance))
            
            # [SIMULATION TRICK]
            # If balances are too low for testing, we can inject "Paper Money" for the dry run context.
            # Currently, we just use real balances. If user wants to test with fake money, we should add a config flag.
            # For now, let's inject 10,000 USDC and 10 ETH if the real balance is near zero, to allow "What If" testing.
            if self.ctx and self.ctx.get_spot_available("USDC") < 100:
                logger.warning("Simulation: Injecting Paper USDC for Dry Run.")
                self.ctx.update_spot_balance("USDC", 10000.0, 10000.0)
            
            if self.ctx and self.ctx.get_spot_available("LIT") < 0.1:
                self.ctx.update_spot_balance("LIT", 100.0, 100.0)

            if account.collateral and self.ctx:
                 self.ctx.update_perp_balance("USDC", float(account.collateral), float(account.available_balance or account.collateral))
                 
            if self.ctx and self.ctx.get_perp_available("USDC") < 100:
                 logger.warning("Simulation: Injecting Paper Margin for Dry Run.")
                 self.ctx.update_perp_balance("USDC", 10000.0, 10000.0)
                 
        except Exception as e:
            logger.error(f"Failed to fetch balances: {e}")

    async def _fetch_current_price(self) -> float:
        # Fetch price from orderbook midprice
        if not self.api_client: return 0.0
        try:
            market_id = self.markets[self.config.symbol].market_id
            order_api = lighter.OrderApi(self.api_client)
            ob = await order_api.order_book_orders(market_id=market_id, limit=10)
            if isinstance(ob, tuple): ob = ob[0]
            
            best_ask = float(ob.asks[0].price) if ob.asks else 0.0
            best_bid = float(ob.bids[0].price) if ob.bids else 0.0
            
            if best_ask > 0 and best_bid > 0:
                return (best_ask + best_bid) / 2.0
            return best_ask or best_bid
        except Exception as e:
            logger.error(f"Price fetch failed: {e}")
            return 0.0
