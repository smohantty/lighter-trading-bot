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
    lower_price: float
    upper_price: float
    size: float
    pending_side: str
    has_order: bool
    is_reduce_only: bool
    entry_price: float
    roundtrip_count: int

class ZoneStatus(Enum):
    Idle = auto()
    Active = auto()

@dataclass
class GridZone:
    index: int
    lower_price: float
    upper_price: float
    size: float
    pending_side: Any # OrderSide
    mode: Optional[ZoneMode] = None
    entry_price: float = 0.0
    order_id: Optional[Any] = None # Cloid
    roundtrip_count: int = 0

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
    position_size: float
    position_side: str
    avg_entry_price: float
    realized_pnl: float
    unrealized_pnl: float
    total_fees: float
    leverage: int
    grid_bias: str
    grid_count: int
    range_low: float
    range_high: float
    grid_spacing_pct: Any
    roundtrips: int
    margin_balance: float
    initial_entry_price: Optional[float]

@dataclass
class SpotGridSummary:
    symbol: str
    state: str
    uptime: str
    position_size: float
    avg_entry_price: float
    realized_pnl: float
    unrealized_pnl: float
    total_fees: float
    initial_entry_price: Optional[float]
    grid_count: int
    range_low: float
    range_high: float
    grid_spacing_pct: Any # (min, max)
    roundtrips: int
    base_balance: float
    quote_balance: float

StrategySummary = Union[PerpGridSummary, SpotGridSummary, None]
