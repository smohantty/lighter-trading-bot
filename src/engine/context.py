import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Union

from src.engine.precision import Precision
from src.model import Cloid, OrderRequest


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
    min_base_amount: Decimal
    min_quote_amount: Decimal

    def __post_init__(self):
        self.price_precision = Precision(self.price_decimals)
        self.size_precision = Precision(self.sz_decimals)

    def round_price(self, price: Decimal) -> Decimal:
        return self.price_precision.round(price)

    def round_size(self, sz: Decimal) -> Decimal:
        return self.size_precision.round(sz)

    def to_sdk_price(self, price: Decimal) -> int:
        return self.price_precision.to_int(price)

    def to_sdk_size(self, sz: Decimal) -> int:
        return self.size_precision.to_int(sz)


@dataclass
class Balance:
    total: Decimal
    available: Decimal


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

    def place_order(self, order: OrderRequest) -> Cloid:
        self._cloid_counter += 1
        cloid = Cloid(self._cloid_counter)

        order.cloid = cloid
        self.order_queue.append(order)
        return cloid

    def cancel_order(self, cloid: Cloid):
        self.cancellation_queue.append(cloid)

    def update_spot_balance(
        self, asset: str, total: Union[float, Decimal], available: Union[float, Decimal]
    ):
        self.spot_balances[asset] = Balance(
            total=Decimal(str(total)), available=Decimal(str(available))
        )

    def update_perp_balance(
        self, asset: str, total: Union[float, Decimal], available: Union[float, Decimal]
    ):
        self.perp_balances[asset] = Balance(
            total=Decimal(str(total)), available=Decimal(str(available))
        )

    def get_spot_total(self, asset: str) -> Decimal:
        b = self.spot_balances.get(asset)
        return b.total if b else Decimal("0")

    def get_spot_available(self, asset: str) -> Decimal:
        b = self.spot_balances.get(asset)
        return b.available if b else Decimal("0")

    def get_perp_total(self, asset: str) -> Decimal:
        b = self.perp_balances.get(asset)
        return b.total if b else Decimal("0")

    def get_perp_available(self, asset: str) -> Decimal:
        b = self.perp_balances.get(asset)
        return b.available if b else Decimal("0")
