from enum import Enum, auto

class GridBias(str, Enum):
    LONG = "Long"
    SHORT = "Short"
    # NEUTRAL = "Neutral" # Removed as per recent changes in Rust bot history

class GridType(str, Enum):
    ARITHMETIC = "Arithmetic"
    GEOMETRIC = "Geometric"

class ZoneMode(Enum):
    LONG = "Long"
    SHORT = "Short"
    # NEUTRAL = "Neutral"

from dataclasses import dataclass
from typing import List, Optional, Any, Optional, Union, Any
from decimal import Decimal

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
    pending_side: str
    has_order: bool
    is_reduce_only: bool
    entry_price: Decimal
    roundtrip_count: int

class ZoneStatus(Enum):
    Idle = auto()
    Active = auto()

@dataclass
class GridZone:
    index: int
    buy_price: Decimal
    sell_price: Decimal
    size: Decimal
    pending_side: Any # OrderSide
    mode: Optional[ZoneMode] = None
    entry_price: Decimal = Decimal("0")
    order_id: Optional[Any] = None # Cloid
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
    avg_entry_price: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_fees: Decimal
    leverage: int
    grid_bias: str
    grid_count: int
    range_low: Decimal
    range_high: Decimal
    grid_spacing_pct: Any
    roundtrips: int
    margin_balance: Decimal
    initial_entry_price: Optional[Decimal]

@dataclass
class SpotGridSummary:
    symbol: str
    state: str
    uptime: str
    position_size: Decimal
    avg_entry_price: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_fees: Decimal
    initial_entry_price: Optional[Decimal]
    grid_count: int
    range_low: Decimal
    range_high: Decimal
    grid_spacing_pct: Any # (min, max)
    roundtrips: int
    base_balance: Decimal
    quote_balance: Decimal

StrategySummary = Union[PerpGridSummary, SpotGridSummary, None]
