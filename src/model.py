from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Union

class Cloid:
    """
    Client Order ID - matches Lighter SDK's client_order_index (integer).
    """
    def __init__(self, order_id: int):
        if not isinstance(order_id, int):
            raise TypeError(f"Cloid must be initialized with int, got {type(order_id)}")
        self._id = order_id

    def as_int(self) -> int:
        return self._id

    def __str__(self) -> str:
        return str(self._id)
    
    def __repr__(self) -> str:
        return f"Cloid({self._id})"

    def __eq__(self, other):
        if isinstance(other, Cloid):
            return self._id == other._id
        return False

    def __hash__(self):
        return hash(self._id)


class OrderSide(Enum):
    BUY = "Buy"
    SELL = "Sell"

    def is_buy(self) -> bool:
        return self == OrderSide.BUY

    def is_sell(self) -> bool:
        return self == OrderSide.SELL

    def __str__(self):
        return self.value

@dataclass
class OrderFill:
    side: OrderSide
    size: float
    price: float
    fee: float
    cloid: Optional[Cloid] = None
    reduce_only: Optional[bool] = None
    raw_dir: Optional[str] = None

@dataclass
class LimitOrderRequest:
    symbol: str
    side: OrderSide
    price: float
    sz: float
    reduce_only: bool
    cloid: Optional[Cloid] = None

@dataclass
class MarketOrderRequest:
    symbol: str
    side: OrderSide
    sz: float
    cloid: Optional[Cloid] = None

@dataclass
class CancelOrderRequest:
    cloid: Cloid
    symbol: str # Added symbol as it's often needed for context, though often implicit in Cloid lookup

OrderRequest = Union[LimitOrderRequest, MarketOrderRequest, CancelOrderRequest]
