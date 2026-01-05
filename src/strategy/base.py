from abc import ABC, abstractmethod
from src.engine.context import StrategyContext
from src.model import OrderFill, Cloid, OrderFailure
from src.strategy.types import StrategySummary, GridState
from typing import Union

class Strategy(ABC):
    @abstractmethod
    def on_tick(self, price: float, ctx: StrategyContext):
        pass

    @abstractmethod
    def on_order_filled(self, fill: OrderFill, ctx: StrategyContext):
        pass

    @abstractmethod
    def on_order_failed(self, failure: OrderFailure, ctx: StrategyContext):
        pass

    @abstractmethod
    def get_summary(self, ctx: StrategyContext) -> StrategySummary:
        pass

    @abstractmethod
    def get_grid_state(self, ctx: StrategyContext) -> GridState:
        pass
