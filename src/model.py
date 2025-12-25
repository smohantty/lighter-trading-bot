from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Union
import uuid

class Cloid:
    def __init__(self, raw_id: Union[uuid.UUID, str]):
        if isinstance(raw_id, str):
            try:
                # Try parsing as UUID first
                self._uuid = uuid.UUID(raw_id)
            except ValueError:
                # If not a valid UUID string, generate one (fallback) 
                # or raise error depending on strictness. 
                # For this port, we assume valid UUID strings.
                 raise ValueError(f"Invalid UUID string for Cloid: {raw_id}")
        elif isinstance(raw_id, uuid.UUID):
            self._uuid = raw_id
        else:
            raise TypeError("Cloid must be initialized with UUID or str")

    def as_uuid(self) -> uuid.UUID:
        return self._uuid

    def __str__(self) -> str:
        return str(self._uuid)
    
    def __repr__(self) -> str:
        return f"Cloid({self._uuid})"

    def __eq__(self, other):
        if isinstance(other, Cloid):
            return self._uuid == other._uuid
        return False

    def __hash__(self):
        return hash(self._uuid)
    
    @staticmethod
    def new_random():
        return Cloid(uuid.uuid4())

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
