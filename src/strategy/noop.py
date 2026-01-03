from dataclasses import dataclass
import logging
from src.strategy.base import Strategy
from src.engine.context import StrategyContext
from src.model import OrderFill, Cloid
from src.strategy.types import StrategySummary, GridState, GridType

logger = logging.getLogger(__name__)

class NoOpStrategy(Strategy):
    """
    No-Op Strategy for testing Engine isolation.
    Does nothing on ticks or events.
    """
    def __init__(self):
        logger.info("Initializing NoOpStrategy")

    def on_tick(self, price: float, ctx: StrategyContext):
        logger.info(f"NoOpStrategy Tick: {price}")
        pass

    def on_order_filled(self, fill: OrderFill, ctx: StrategyContext):
        logger.info(f"NoOpStrategy Order Filled: {fill}")

    def on_order_failed(self, cloid: Cloid, ctx: StrategyContext):
        logger.info(f"NoOpStrategy Order Failed: {cloid}")

    def get_summary(self, ctx: StrategyContext) -> StrategySummary:
        # Return dummy summary
        from src.strategy.types import SpotGridSummary # Or make a generic summary
        # Since Summary is often strategy specific, we might return None or a dummy object?
        # The Abstract Base Class expects a StrategySummary.
        # Let's verify what StrategySummary is. It might be a Union.
        # Check src/strategy/types.py again.
        return None # type: ignore

    def get_grid_state(self, ctx: StrategyContext) -> GridState:
        # Return dummy empty grid state
        return GridState(
            symbol="NOOP",
            strategy_type="noop",
            grid_bias=None,
            zones=[]
        )
