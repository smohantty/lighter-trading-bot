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
from typing import List, Optional, Union, Any

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

@dataclass
class GridState:
    symbol: str
    strategy_type: str
    current_price: float
    grid_bias: Optional[str]
    zones: List[ZoneInfo]

@dataclass
class PerpGridSummary:
    symbol: str
    price: float
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
    grid_spacing_pct: float
    roundtrips: int
    margin_balance: float
    initial_entry_price: Optional[float]

@dataclass
class SpotGridSummary:
    # Placeholder for spot grid summary if needed
    symbol: str

StrategySummary = Union[PerpGridSummary, SpotGridSummary]
