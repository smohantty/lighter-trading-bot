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
    prices = []
    if grid_count <= 1:
        return prices
    
    # Rust uses grid_count as number of levels? Yes. 
    # Rust: (grid_count as f64 - 1.0)
    
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
    # Rust logic implementation seems slightly inconsistent with "grid_count - 1" levels vs segments? 
    # Rust common.rs: let n = grid_count as f64;
    # It divides space by n? 
    # Actually wait. `calculate_grid_prices` uses `grid_count - 1`.
    # `calculate_grid_spacing_pct` uses `grid_count`?
    # Let's check Rust code again.
    # line 99: let n = grid_count as f64;
    # line 114: let spacing = (upper_price - lower_price) / n;
    # That implies there are `n` zones if `grid_count` is the number of lines.
    # Ah, implementation plan says `num_zones = self.config.grid_count as usize - 1;` in PerpGrid.
    # So `grid_count` is number of lines (levels).
    # Then spacing should be over `n - 1` segments if we look at price difference between levels.
    # But `calculate_grid_spacing_pct` in Rust implementation uses `n`.
    # Maybe it's an estimation or it's implicitly `grid_count` as "number of zones" in some contexts?
    # But checking `calculate_grid_prices`, it definitely generates `grid_count` prices.
    # The Rust code for spacing pct:
    # 106: let ratio = (upper_price / lower_price).powf(1.0 / n);
    # That would mean `n` intervals. So `grid_count` + 1 prices?
    # No, if grid_count is lines, intervals = n - 1.
    # Using `n` (grid_count) implies grid_count is treated as intervals here?
    # I should copy the Rust implementation 1:1 regardless of correctness, unless it's obviously a bug I should fix and note.
    # It seems like a minor discrepancy in the Rust code or I'm misinterpreting. 
    # I will copy 1:1.
    
    if grid_type == GridType.GEOMETRIC:
        ratio = (upper_price / lower_price) ** (1.0 / n)
        spacing_pct = (ratio - 1.0) * 100.0
        return (spacing_pct, spacing_pct)
    else:
        spacing = (upper_price - lower_price) / n
        min_pct = (spacing / upper_price) * 100.0
        max_pct = (spacing / lower_price) * 100.0
        return (min_pct, max_pct)
