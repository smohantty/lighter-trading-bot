from typing import Dict, Optional, List
from dataclasses import dataclass, field
import math
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

    def clamp_to_min_notional(self, size: float, price: float, min_notional: float) -> float:
        """
        Ensure size is large enough to meet the minimum notional requirement (quote units).
        Also respects the exchange-defined min_base_amount and min_quote_amount.
        """
        # Minimum quote amount required (notional)
        target_min_notional = max(min_notional, self.min_quote_amount)
        
        # Start with requested size or exchange minimum
        sz = max(size, self.min_base_amount)
        
        # If notional is too low, increase size
        if price > 0.0 and (sz * price < target_min_notional):
            sz = target_min_notional / price
            
        # Round and ensure we didn't drop below min_base_amount due to rounding
        rounded_sz = self.round_size(sz)
        return max(rounded_sz, self.round_size(self.min_base_amount))

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
        return Cloid.new_random() # type: ignore

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
