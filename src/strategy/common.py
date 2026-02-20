from datetime import timedelta
from decimal import Decimal
from typing import List, Tuple

from src.strategy.types import GridType


def format_uptime(duration: timedelta) -> str:
    total_secs = int(duration.total_seconds())
    days = total_secs // 86400
    hours = (total_secs % 86400) // 3600
    minutes = (total_secs % 3600) // 60
    seconds = total_secs % 60

    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"


def check_trigger(
    current_price: Decimal, trigger_price: Decimal, start_price: Decimal
) -> bool:
    if start_price < trigger_price:
        # Waiting for price to go UP to trigger
        if current_price >= trigger_price:
            return True
    else:
        # Waiting for price to go DOWN to trigger
        if current_price <= trigger_price:
            return True
    return False


def calculate_grid_prices(
    grid_type: GridType,
    grid_range_low: Decimal,
    grid_range_high: Decimal,
    grid_count: int,
) -> List[Decimal]:
    prices: List[Decimal] = []
    if grid_count <= 1:
        return prices

    n_minus_1 = Decimal(grid_count - 1)

    if grid_type == GridType.ARITHMETIC:
        step = (grid_range_high - grid_range_low) / n_minus_1
        for i in range(grid_count):
            price = grid_range_low + (Decimal(i) * step)
            prices.append(price)

    elif grid_type == GridType.GEOMETRIC:
        # Decimal pow: (ratio) = (up/low) ** (1/n-1)
        ratio = (grid_range_high / grid_range_low) ** (Decimal("1") / n_minus_1)
        for i in range(grid_count):
            price = grid_range_low * (ratio ** Decimal(i))
            prices.append(price)

    return prices


def calculate_grid_prices_by_spread(
    grid_range_low: Decimal, grid_range_high: Decimal, spread_bips: Decimal
) -> List[Decimal]:
    prices: List[Decimal] = []
    if grid_range_low >= grid_range_high:
        return prices

    # 1 bip = 0.01% = 0.0001
    # 100 bips = 1% = 0.01
    # ratio = 1 + (spread_bips / 10000)
    ratio = Decimal("1") + (spread_bips / Decimal("10000"))

    current_price = grid_range_low
    while current_price <= grid_range_high:
        prices.append(current_price)
        current_price = current_price * ratio

    return prices


def calculate_grid_spacing_pct(
    grid_type: GridType,
    grid_range_low: Decimal,
    grid_range_high: Decimal,
    grid_count: int,
) -> Tuple[Decimal, Decimal]:
    n = Decimal(grid_count)

    if grid_type == GridType.GEOMETRIC:
        ratio = (grid_range_high / grid_range_low) ** (Decimal("1") / n)
        spacing_pct = (ratio - Decimal("1")) * Decimal("100")
        return (spacing_pct, spacing_pct)
    else:
        spacing = (grid_range_high - grid_range_low) / n
        min_pct = (spacing / grid_range_high) * Decimal("100")
        max_pct = (spacing / grid_range_low) * Decimal("100")
        return (min_pct, max_pct)
