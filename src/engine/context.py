from typing import Dict, Optional, List
from dataclasses import dataclass, field
import math
import time
from src.model import Cloid, OrderRequest, CancelOrderRequest



@dataclass
class MarketInfo:
    symbol: str
    coin: str
    market_id: int
    sz_decimals: int
    price_decimals: int
    market_type: str
    base_asset_id: int
    quote_asset_id: int
    min_base_amount: float
    min_quote_amount: float

    def round_price(self, price: float) -> float:
        return round(price, self.price_decimals)

    def round_size(self, sz: float) -> float:
        return round(sz, self.sz_decimals)

    def to_sdk_price(self, price: float) -> int:
        """Convert float price to SDK integer format."""
        return int(round(price * (10 ** self.price_decimals)))

    def to_sdk_size(self, sz: float) -> int:
        """Convert float size to SDK integer format."""
        return int(round(sz * (10 ** self.sz_decimals)))

    def clamp_to_min_size(self, size: float) -> float:
        """
        Ensure size meets the exchange's minimum base amount requirement.
        """
        clamped = max(size, self.min_base_amount)
        return self.round_size(clamped)

    def clamp_to_min_notional(self, size: float, price: float) -> float:
        """
        Ensure size * price >= min_notional (min_quote_amount).
        If size is too small, returns a size s.t. size * price >= min_quote_amount.
        """
        if size * price >= self.min_quote_amount:
            return self.round_size(size)
        
        # Calculate required size
        if price == 0: return self.round_size(size) # Prevent div/0

        required_size = self.min_quote_amount / price
        # Round up to ensure we meet the threshold
        return self.round_size(max(size, required_size) * 1.01) # Add tiny buffer

@dataclass
class Balance:
    total: float
    available: float

class StrategyContext:
    def __init__(self, markets: Dict[str, MarketInfo]):
        self.markets = markets
        self.spot_balances: Dict[str, Balance] = {}
        self.perp_balances: Dict[str, Balance] = {}
        self.order_queue: List[OrderRequest] = []
        self.cancellation_queue: List[Cloid] = []
        
        # Counter for generating unique client order IDs
        self._cloid_counter = int(time.time() * 1000) % 10000000

    def market_info(self, symbol: str) -> Optional[MarketInfo]:
        return self.markets.get(symbol)

    def place_order(self, order: OrderRequest):
        self.order_queue.append(order)

    def cancel_order(self, cloid: Cloid):
        # We assume symbol knowledge isn't strictly needed for the internal queue for now, 
        # but the request needs it. For simple cancellation by Cloid, we might need a lookup later.
        # But Rust implementation just pushes Cloid.
        self.cancellation_queue.append(cloid)

    def generate_cloid(self) -> Cloid:
        """Generate a unique client order ID using a counter."""
        self._cloid_counter += 1
        return Cloid(self._cloid_counter)

    def update_spot_balance(self, asset: str, total: float, available: float):
        self.spot_balances[asset] = Balance(total=total, available=available)

    def update_perp_balance(self, asset: str, total: float, available: float):
        self.perp_balances[asset] = Balance(total=total, available=available)

    def get_spot_total(self, asset: str) -> float:
        b = self.spot_balances.get(asset)
        return b.total if b else 0.0

    def get_spot_available(self, asset: str) -> float:
        b = self.spot_balances.get(asset)
        return b.available if b else 0.0

    def get_perp_total(self, asset: str) -> float:
        b = self.perp_balances.get(asset)
        return b.total if b else 0.0

    def get_perp_available(self, asset: str) -> float:
        b = self.perp_balances.get(asset)
        return b.available if b else 0.0
