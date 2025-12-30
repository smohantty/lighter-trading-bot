from datetime import timedelta
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

def check_trigger(current_price: float, trigger_price: float, start_price: float) -> bool:
    if start_price < trigger_price:
        # Waiting for price to go UP to trigger
        if current_price >= trigger_price:
            return True
    else:
        # Waiting for price to go DOWN to trigger
        if current_price <= trigger_price:
            return True
    return False

def calculate_grid_prices(grid_type: GridType, lower_price: float, upper_price: float, grid_count: int) -> List[float]:
    prices: List[float] = []
    if grid_count <= 1:
        return prices
    
    n_minus_1 = float(grid_count - 1)
    
    if grid_type == GridType.ARITHMETIC:
        step = (upper_price - lower_price) / n_minus_1
        for i in range(grid_count):
            price = lower_price + (i * step)
            prices.append(price)
            
    elif grid_type == GridType.GEOMETRIC:
        ratio = (upper_price / lower_price) ** (1.0 / n_minus_1)
        for i in range(grid_count):
            price = lower_price * (ratio ** i)
            prices.append(price)
            
    return prices

def calculate_grid_spacing_pct(grid_type: GridType, lower_price: float, upper_price: float, grid_count: int) -> Tuple[float, float]:
    n = float(grid_count)
    
    if grid_type == GridType.GEOMETRIC:
        ratio = (upper_price / lower_price) ** (1.0 / n)
        spacing_pct = (ratio - 1.0) * 100.0
        return (spacing_pct, spacing_pct)
    else:
        spacing = (upper_price - lower_price) / n
        min_pct = (spacing / upper_price) * 100.0
        max_pct = (spacing / lower_price) * 100.0
        return (min_pct, max_pct)
