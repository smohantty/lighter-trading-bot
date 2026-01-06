import logging
from typing import Dict, Optional, List, Any
from decimal import Decimal
import lighter
from src.config import StrategyConfig, ExchangeConfig
from src.strategy.base import Strategy
from src.engine.context import StrategyContext, MarketInfo

logger = logging.getLogger(__name__)

class BaseEngine:
    """
    Base class for trading engines (Live and Simulation).
    Handles common state initialization and market data loading.
    """
    def __init__(self, config: StrategyConfig, exchange_config: ExchangeConfig, strategy: Strategy):
        self.strategy_config = config
        self.exchange_config = exchange_config
        self.strategy = strategy
        
        # Common State
        self.ctx: Optional[StrategyContext] = None
        self.markets: Dict[str, MarketInfo] = {}
        self.market_map: Dict[str, int] = {}       # Symbol -> MarketID
        self.reverse_market_map: Dict[int, str] = {} # MarketID -> Symbol
        
        # Client placeholders (subclasses initialize them)
        self.api_client: Optional[lighter.ApiClient] = None

    async def _load_markets(self):
        """
        Populate market_map and markets dictionary from API order books.
        Uses duplicate logic previously found in Engine/SimulationEngine.
        """
        if not self.api_client:
            logger.error("API Client not initialized, cannot load markets.")
            return

        logger.info("Loading Market Metadata via OrderApi...")
        self.market_map = {}
        self.reverse_market_map = {}
        self.markets = {} 

        try:
            order_api = lighter.OrderApi(self.api_client)
            response = await order_api.order_book_details()
            
            if isinstance(response, tuple):
                response = response[0]
            
            all_details: List[Any] = []
            
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
                        symbol=symbol,
                        coin=symbol.split('/')[0] if '/' in symbol else symbol,
                        market_id=mid_int,
                        price_decimals=int(price_decimals),
                        sz_decimals=int(size_decimals),
                        market_type=market_type,
                        base_asset_id=int(base_asset_id),
                        quote_asset_id=int(quote_asset_id),
                        min_base_amount=Decimal(str(min_base_amount_str)),
                        min_quote_amount=Decimal(str(min_quote_amount_str))
                    )
                    self.markets[symbol] = info

            # logger.info(f"Loaded {len(self.market_map)} symbols into market map.")
            
        except Exception as e:
            logger.error(f"Failed to populate market map: {e}")
            raise

    async def _fetch_account_balances(self):
        """Fetch account balances from the API and update StrategyContext."""
        if not self.api_client or not self.ctx:
            return
        
        try:
            account_api = lighter.AccountApi(self.api_client)
            account_data = await account_api.account(
                by="index",
                value=str(self.exchange_config.account_index)
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
                # Ensure we handle different possible attribute names or missing fields gracefully
                available = float(account.available_balance) if hasattr(account, "available_balance") and account.available_balance else collateral
                
                # For perps, we track USDC collateral
                self.ctx.update_perp_balance(
                    asset="USDC",
                    total=collateral,
                    available=available
                )
                logger.info(f"Perp Collateral: USDC - Total: {collateral}, Available: {available}")
                
        except Exception as e:
            logger.error(f"Failed to fetch account balances: {e}")
