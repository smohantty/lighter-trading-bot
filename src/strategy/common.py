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
    grid_type: GridType, lower_price: Decimal, upper_price: Decimal, grid_count: int
) -> List[Decimal]:
    prices: List[Decimal] = []
    if grid_count <= 1:
        return prices

    n_minus_1 = Decimal(grid_count - 1)

    if grid_type == GridType.ARITHMETIC:
        step = (upper_price - lower_price) / n_minus_1
        for i in range(grid_count):
            price = lower_price + (Decimal(i) * step)
            prices.append(price)

    elif grid_type == GridType.GEOMETRIC:
        # Decimal pow: (ratio) = (up/low) ** (1/n-1)
        ratio = (upper_price / lower_price) ** (Decimal("1") / n_minus_1)
        for i in range(grid_count):
            price = lower_price * (ratio ** Decimal(i))
            prices.append(price)

    return prices


def calculate_grid_prices_by_spread(
    lower_price: Decimal, upper_price: Decimal, spread_bips: Decimal
) -> List[Decimal]:
    prices: List[Decimal] = []
    if lower_price >= upper_price:
        return prices

    # 1 bip = 0.01% = 0.0001
    # 100 bips = 1% = 0.01
    # ratio = 1 + (spread_bips / 10000)
    ratio = Decimal("1") + (spread_bips / Decimal("10000"))

    current_price = lower_price
    while current_price <= upper_price:
        prices.append(current_price)
        current_price = current_price * ratio

    return prices


def calculate_grid_spacing_pct(
    grid_type: GridType, lower_price: Decimal, upper_price: Decimal, grid_count: int
) -> Tuple[Decimal, Decimal]:
    n = Decimal(grid_count)

    if grid_type == GridType.GEOMETRIC:
        ratio = (upper_price / lower_price) ** (Decimal("1") / n)
        spacing_pct = (ratio - Decimal("1")) * Decimal("100")
        return (spacing_pct, spacing_pct)
    else:
        spacing = (upper_price - lower_price) / n
        min_pct = (spacing / upper_price) * Decimal("100")
        max_pct = (spacing / lower_price) * Decimal("100")
        return (min_pct, max_pct)
