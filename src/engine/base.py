import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import lighter
from lighter.nonce_manager import NonceManagerType

from src.config import ExchangeConfig, StrategyConfig
from src.engine.context import MarketInfo, StrategyContext
from src.model import Order, Trade, TradeDetails
from src.strategy.base import Strategy

logger = logging.getLogger(__name__)

# Constants
AUTH_TOKEN_EXPIRY = 8 * 60 * 60  # 8 hours
TOKEN_REFRESH_BUFFER = 60 * 60  # 1 hours


class BaseEngine:
    """
    Base class for trading engines (Live and Simulation).
    Handles common state initialization and market data loading.
    """

    def __init__(
        self,
        config: StrategyConfig,
        exchange_config: ExchangeConfig,
        strategy: Strategy,
    ):
        self.strategy_config = config
        self.exchange_config = exchange_config
        self.strategy = strategy

        # Common State
        self.ctx: Optional[StrategyContext] = None
        self.markets: Dict[str, MarketInfo] = {}
        self.market_map: Dict[str, int] = {}  # Symbol -> MarketID
        self.reverse_market_map: Dict[int, str] = {}  # MarketID -> Symbol

        # Client placeholders (subclasses initialize them)
        self.api_client: Optional[lighter.ApiClient] = None
        self.signer_client: Optional[lighter.SignerClient] = None
        self.account_index: Optional[int] = None

        # API Auth Token Cache
        self._api_token: Optional[str] = None
        self._api_token_expiry: float = 0

        if self.exchange_config.account_index > 0:
            self.account_index = self.exchange_config.account_index

        # Cache for symbol parsing (symbol -> (base_asset, quote_asset))
        self._symbol_cache: Dict[str, Tuple[str, str]] = {}

    def _init_signer(self):
        """Initialize SignerClient if credentials are available."""
        if self.account_index and self.exchange_config.agent_private_key:
            self.signer_client = lighter.SignerClient(
                url=self.exchange_config.base_url,
                account_index=self.account_index,
                api_private_keys={
                    self.exchange_config.agent_key_index: self.exchange_config.agent_private_key
                },
                nonce_management_type=NonceManagerType.API,
            )

    async def _get_fresh_token(self) -> Optional[str]:
        """Token provider for QueueWsClient to refresh auth on reconnection."""
        logger.info("Generating fresh auth token for WebSocket...")
        if not self.signer_client:
            logger.error("Signer client not initialized")
            return None

        # Use 8 hours (maximum allowed) or similar long duration
        auth_token, error = self.signer_client.create_auth_token_with_expiry(
            deadline=AUTH_TOKEN_EXPIRY
        )
        if error:
            logger.error(f"Failed to refresh auth token: {error}")
            return None
        return str(auth_token) if auth_token else None

    async def _get_api_token(self) -> Optional[str]:
        import time

        now = time.time()

        if self._api_token and now < (self._api_token_expiry - TOKEN_REFRESH_BUFFER):
            return self._api_token

        logger.info("Refreshing API auth token...")
        auth_token = await self._get_fresh_token()

        if not auth_token:
            return None

        self._api_token = auth_token
        self._api_token_expiry = now + AUTH_TOKEN_EXPIRY

        return self._api_token

    async def _load_markets(self):
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

            if hasattr(response, "order_book_details"):
                if response.order_book_details:
                    all_details.extend(response.order_book_details)

            if hasattr(response, "spot_order_book_details"):
                if response.spot_order_book_details:
                    all_details.extend(response.spot_order_book_details)

            elif isinstance(response, dict):
                perp_details = response.get("order_book_details")
                if perp_details and isinstance(perp_details, list):
                    all_details.extend(perp_details)

                spot_details = response.get("spot_order_book_details")
                if spot_details and isinstance(spot_details, list):
                    all_details.extend(spot_details)

            if not all_details:
                logger.warning("No market details found in response.")

            for item in all_details:
                if not isinstance(item, dict):
                    if hasattr(item, "to_dict"):
                        item = item.to_dict()
                    elif hasattr(item, "dict"):
                        item = item.dict()

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
                    raise ValueError(
                        f"Unknown item type in order_books list: {type(item)}"
                    )

                if symbol and market_id is not None:
                    mid_int = int(market_id)

                    # Store raw map
                    self.market_map[symbol] = mid_int
                    self.reverse_market_map[mid_int] = symbol

                    # Create MarketInfo
                    info = MarketInfo(
                        symbol=symbol,
                        coin=symbol.split("/")[0] if "/" in symbol else symbol,
                        market_id=mid_int,
                        price_decimals=int(price_decimals),
                        sz_decimals=int(size_decimals),
                        market_type=market_type,
                        base_asset_id=int(base_asset_id),
                        quote_asset_id=int(quote_asset_id),
                        min_base_amount=Decimal(str(min_base_amount_str)),
                        min_quote_amount=Decimal(str(min_quote_amount_str)),
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
                by="index", value=str(self.exchange_config.account_index)
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
                        available=available_balance,
                    )
                    logger.info(
                        f"Spot Balance: {asset.symbol} - Total: {total_balance}, Available: {available_balance}"
                    )

            # Update perp balances (collateral)
            if account.collateral:
                collateral = float(account.collateral)
                # Ensure we handle different possible attribute names or missing fields gracefully
                available = (
                    float(account.available_balance)
                    if hasattr(account, "available_balance")
                    and account.available_balance
                    else collateral
                )

                # For perps, we track USDC collateral
                self.ctx.update_perp_balance(
                    asset="USDC", total=collateral, available=available
                )
                logger.info(
                    f"Perp Collateral: USDC - Total: {collateral}, Available: {available}"
                )

        except Exception as e:
            logger.error(f"Failed to fetch account balances: {e}")

    async def get_active_orders(
        self, market_id: int, owner_account_index: Optional[int] = None
    ) -> List[Any]:
        """Fetch active orders from the exchange."""
        if not self.api_client:
            logger.warning("API client not initialized")
            return []

        # Use provided account index or fallback to self.account_index
        account_index = (
            owner_account_index
            if owner_account_index is not None
            else self.account_index
        )
        if account_index is None:
            logger.error("No account index available for fetching orders")
            return []

        try:
            # Generate or retrieve cached auth token
            auth_token = await self._get_api_token()

            order_api = lighter.OrderApi(self.api_client)

            # Note: account_active_orders returns Orders object which has .orders list
            # Pass auth_token as 'authorization' header if provided
            response = await order_api.account_active_orders(
                account_index=account_index,
                market_id=market_id,
                authorization=auth_token,
            )

            return response.orders or []

        except Exception as e:
            logger.error(f"Failed to fetch active orders: {e}")
            return []

    async def get_inactive_orders(
        self, limit: int, market_id: int, owner_account_index: Optional[int] = None
    ) -> List[Any]:
        """Fetch inactive orders from the exchange."""
        if not self.api_client:
            logger.warning("API client not initialized")
            return []

        # Use provided account index or fallback to self.account_index
        account_index = (
            owner_account_index
            if owner_account_index is not None
            else self.account_index
        )
        if account_index is None:
            logger.error("No account index available for fetching orders")
            return []

        try:
            # Generate or retrieve cached auth token
            auth_token = await self._get_api_token()

            order_api = lighter.OrderApi(self.api_client)

            response = await order_api.account_inactive_orders(
                account_index=account_index,
                limit=limit,
                market_id=market_id,
                authorization=auth_token,
            )
            return response.orders or []

        except Exception as e:
            logger.error(f"Failed to fetch inactive orders: {e}")
            return []

    def _parse_order(self, order_data: dict) -> Order:
        """
        Parses a raw order dictionary into a typed Order dataclass.
        """
        return Order(
            order_id=order_data["order_index"],
            cloid_id=order_data["client_order_index"],
            market_index=order_data["market_index"],
            owner_account_index=order_data["owner_account_index"],
            initial_base_amount=order_data["initial_base_amount"],
            price=order_data["price"],
            remaining_base_amount=order_data["remaining_base_amount"],
            is_ask=order_data["is_ask"],
            base_size=order_data["base_size"],
            base_price=order_data["base_price"],
            filled_base_amount=order_data["filled_base_amount"],
            filled_quote_amount=order_data["filled_quote_amount"],
            side=order_data["side"],
            type=order_data["type"],
            time_in_force=order_data["time_in_force"],
            reduce_only=order_data["reduce_only"],
            status=order_data["status"],
        )

    def _is_canceled_status(self, status: str) -> bool:
        """
        Determines if an order status represents a cancellation or failure terminal state.
        """
        canceled_statuses = {
            "canceled",
            "canceled-post-only",
            "canceled-reduce-only",
            "canceled-position-not-allowed",
            "canceled-margin-not-allowed",
            "canceled-too-much-slippage",
            "canceled-not-enough-liquidity",
            "canceled-self-trade",
            "canceled-expired",
            "canceled-oco",
            "canceled-child",
            "canceled-liquidation",
            "canceled-invalid-balance",
        }
        return status in canceled_statuses

    def _parse_trade(self, trade_data: dict) -> Trade:
        """
        Parses a raw trade dictionary into a typed Trade dataclass.
        """
        return Trade(
            type=trade_data["type"],
            market_id=trade_data["market_id"],
            size=Decimal(str(trade_data["size"])),
            price=Decimal(str(trade_data["price"])),
            usd_amount=Decimal(str(trade_data["usd_amount"])),
            ask_id=trade_data["ask_id"],
            bid_id=trade_data["bid_id"],
            ask_account_id=trade_data["ask_account_id"],
            bid_account_id=trade_data["bid_account_id"],
            is_maker_ask=trade_data["is_maker_ask"],
            taker_fee=trade_data.get("taker_fee", 0),
            maker_fee=trade_data.get("maker_fee", 0),
        )

    def _get_assets_from_symbol(self, symbol: str) -> Tuple[str, str]:
        """
        Parses symbol into (base_asset, quote_asset) with caching.
        """
        if not symbol:
            return "UNKNOWN", "UNKNOWN"

        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]

        if "/" in symbol:
            base, quote = symbol.split("/")
            self._symbol_cache[symbol] = (base, quote)
            return base, quote

        # Default for perps or simple symbols
        self._symbol_cache[symbol] = (symbol, "USDC")
        return symbol, "USDC"

    def _get_base_asset_from_symbol(self, symbol: str) -> str:
        """
        Extracts base asset from a symbol string.
        """
        return self._get_assets_from_symbol(symbol)[0]

    def _get_base_asset(self, market_index: int) -> str:
        """
        Get base asset from market index.
        Returns 'UNKNOWN' if not found.
        """
        symbol = self.reverse_market_map.get(market_index)
        if not symbol:
            return "UNKNOWN"
        return self._get_base_asset_from_symbol(symbol)

    def _calculate_fee_usd(self, details: TradeDetails) -> Decimal:
        """
        Calculates the fee in USD (Decimal) based on the fee rate and trade volume.

        Logic:
        - `details.fee` is the Fee Rate in integer format, scaled by 1,000,000.
          (Example: 20 = 0.00002 = 0.002%, 200 = 0.0002 = 0.02%)
        - `details.size` * `details.price` = Trade Volume in USD (Quote).

        Formula:
             FeeUSD = (details.fee / 1_000_000) * (details.size * details.price)
        """
        if details.fee == 0:
            return Decimal("0.0")

        # Calculate Fee Amount in USD
        # 1. Get Fee Rate
        fee_rate = Decimal(str(details.fee)) / Decimal("1000000.0")

        # 2. Get Trade Volume (USD)
        trade_volume_usd = details.size * details.price

        # 3. Calculate Fee
        fee_usd = fee_rate * trade_volume_usd

        # 4. Round to 4 decimals (standard for USD/USDC on this exchange)
        return round(fee_usd, 4)
