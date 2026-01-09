from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from src.strategy.types import GridState, PerpGridSummary, SpotGridSummary


@dataclass
class SystemInfo:
    network: str
    exchange: str


@dataclass
class OrderEvent:
    oid: int
    cloid: Optional[str]
    side: str
    price: float
    size: float
    status: str
    fee: float
    is_taker: bool


@dataclass
class MarketEvent:
    price: float


@dataclass
class WSEvent:
    event_type: str
    data: Any

    def to_dict(self) -> Dict[str, Any]:
        data_dict = self.data
        if hasattr(self.data, "to_dict"):
            data_dict = self.data.to_dict()
        elif hasattr(self.data, "__dict__"):
            data_dict = asdict(self.data)

        return {"event_type": self.event_type, "data": data_dict}


# Helpers to construct events
def config_event(config: Dict[str, Any]) -> WSEvent:
    return WSEvent(event_type="config", data=config)


def info_event(network: str, exchange: str = "lighter") -> WSEvent:
    return WSEvent(
        event_type="info", data=SystemInfo(network=network, exchange=exchange)
    )


def spot_grid_summary_event(summary: SpotGridSummary) -> WSEvent:
    return WSEvent(event_type="spot_grid_summary", data=summary)


def perp_grid_summary_event(summary: PerpGridSummary) -> WSEvent:
    return WSEvent(event_type="perp_grid_summary", data=summary)


def grid_state_event(state: GridState) -> WSEvent:
    return WSEvent(event_type="grid_state", data=state)


def order_update_event(event: OrderEvent) -> WSEvent:
    return WSEvent(event_type="order_update", data=event)


def market_update_event(event: MarketEvent) -> WSEvent:
    return WSEvent(event_type="market_update", data=event)


def error_event(message: str) -> WSEvent:
    return WSEvent(event_type="error", data=message)
