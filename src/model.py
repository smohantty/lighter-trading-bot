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
class TradeRole(Enum):
    MAKER = "Maker"
    TAKER = "Taker"

    def is_maker(self) -> bool:
        return self == TradeRole.MAKER

    def is_taker(self) -> bool:
        return self == TradeRole.TAKER

    def __str__(self):
        return self.value

@dataclass
class OrderFill:
    side: OrderSide
    size: float
    price: float
    fee: float
    role: Optional[TradeRole] = None
    cloid: Optional[Cloid] = None
    reduce_only: Optional[bool] = None
    raw_dir: Optional[str] = None

@dataclass
class PendingOrder:
    """Tracks an order that may fill in multiple parts."""
    target_size: float
    side: OrderSide
    filled_size: float = 0.0
    weighted_avg_px: float = 0.0
    accumulated_fees: float = 0.0
    reduce_only: bool = False
    oid: Optional[int] = None
    created_at: float = 0.0  # Timestamp when order was placed
    price: float = 0.0


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
    reduce_only: bool = False
    price: float = 0.0 # Worst price / slippage limit
    cloid: Optional[Cloid] = None

@dataclass
class CancelOrderRequest:
    cloid: Cloid
    symbol: str # Added symbol as it's often needed for context, though often implicit in Cloid lookup

OrderRequest = Union[LimitOrderRequest, MarketOrderRequest, CancelOrderRequest]

@dataclass
class Order:
    order_id: int
    cloid_id: int
    market_index: int
    owner_account_index: int
    initial_base_amount: str
    price: str
    remaining_base_amount: str
    is_ask: bool
    base_size: int
    base_price: int
    filled_base_amount: str
    filled_quote_amount: str
    side: str
    type: str
    time_in_force: str
    reduce_only: bool
    status: str


@dataclass
class Trade:
    trade_id: int
    tx_hash: str
    type: str
    market_id: int
    size: str
    price: str
    usd_amount: str
    ask_id: int
    bid_id: int
    ask_account_id: int
    bid_account_id: int
    is_maker_ask: bool
    taker_fee: Optional[int] = None
    taker_position_size_before: Optional[str] = None
    taker_entry_quote_before: Optional[str] = None
    taker_initial_margin_fraction_before: Optional[int] = None
    taker_position_sign_changed: Optional[bool] = None
    maker_fee: Optional[int] = None
    maker_position_size_before: Optional[str] = None
    maker_entry_quote_before: Optional[str] = None
    maker_initial_margin_fraction_before: Optional[int] = None
    maker_position_sign_changed: Optional[bool] = None

    def get_side_and_oid(self, account_id: int) -> Optional[tuple[OrderSide, int]]:
        """
        Determines if the trade involves the given account.
        Returns (side: OrderSide, order_id: int) if involved, else None.
        """
        if self.bid_account_id == account_id:
            return OrderSide.BUY, self.bid_id
        elif self.ask_account_id == account_id:
            return OrderSide.SELL, self.ask_id
        return None

