from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, auto
from typing import TYPE_CHECKING, List, Optional, Tuple, Union

if TYPE_CHECKING:
    from src.model import Cloid, OrderSide


class GridBias(str, Enum):
    LONG = "long"
    SHORT = "short"


class GridType(str, Enum):
    ARITHMETIC = "arithmetic"
    GEOMETRIC = "geometric"


class ZoneMode(Enum):
    LONG = "long"
    SHORT = "short"


class StrategyState(Enum):
    Initializing = auto()
    WaitingForTrigger = auto()
    AcquiringAssets = auto()
    Running = auto()


class Spread:
    """
    Represents a percentage spread for markup/markdown calculations.

    0.1 means 0.1% (pips).
    stored as Decimal('0.1')
    """

    def __init__(self, percentage: Union[float, str, Decimal]):
        self.value = Decimal(str(percentage))

    def markup(self, value: Union[Decimal, float, int]) -> Decimal:
        """Returns value * (1 + spread/100)"""
        d_val = Decimal(str(value))
        return d_val * (Decimal("1") + (self.value / Decimal("100")))

    def markdown(self, value: Union[Decimal, float, int]) -> Decimal:
        """Returns value * (1 - spread/100)"""
        d_val = Decimal(str(value))
        return d_val * (Decimal("1") - (self.value / Decimal("100")))

    def __repr__(self) -> str:
        return f"Spread({self.value}%)"


@dataclass
class ZoneInfo:
    index: int
    buy_price: Decimal
    sell_price: Decimal
    size: Decimal
    order_side: str
    has_order: bool
    is_reduce_only: bool
    entry_price: Decimal
    roundtrip_count: int


@dataclass
class GridZone:
    index: int
    buy_price: Decimal
    sell_price: Decimal
    size: Decimal
    order_side: OrderSide
    mode: Optional[ZoneMode] = None
    entry_price: Decimal = Decimal("0")
    cloid: Optional[Cloid] = None
    roundtrip_count: int = 0
    retry_count: int = 0


@dataclass
class GridState:
    symbol: str
    strategy_type: str
    grid_bias: Optional[str]
    zones: List[ZoneInfo]


@dataclass
class PerpGridSummary:
    symbol: str
    state: str
    uptime: str
    position_size: Decimal
    position_side: str
    matched_profit: Decimal
    total_profit: Decimal
    total_fees: Decimal
    leverage: int
    grid_bias: str
    grid_count: int
    grid_range_low: Decimal
    grid_range_high: Decimal
    grid_spacing_pct: Tuple[Decimal, Decimal]
    roundtrips: int
    margin_balance: Decimal
    initial_entry_price: Optional[Decimal]


@dataclass
class SpotGridSummary:
    symbol: str
    state: str
    uptime: str
    position_size: Decimal
    matched_profit: Decimal
    total_profit: Decimal
    total_fees: Decimal
    initial_entry_price: Optional[Decimal]
    grid_count: int
    grid_range_low: Decimal
    grid_range_high: Decimal
    grid_spacing_pct: Tuple[Decimal, Decimal]
    roundtrips: int
    base_balance: Decimal
    quote_balance: Decimal


StrategySummary = Union[PerpGridSummary, SpotGridSummary, None]
