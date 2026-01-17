from typing import List, Optional

from src.config import PerpGridConfig, SpotGridConfig, StrategyConfig
from src.model import (
    CancelOrderRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    OrderRequest,
)
from src.strategy.types import (
    GridState,
    PerpGridSummary,
    SpotGridSummary,
    StrategySummary,
)


class ConsoleRenderer:
    @staticmethod
    def render(
        config: StrategyConfig,
        summary: Optional[StrategySummary],
        grid: Optional[GridState],
        orders: List[OrderRequest],
        current_price: Optional[float] = None,
    ):
        print(f"\n{'=' * 60}")
        print(" SIMULATION DRY RUN REPORT")
        print(f"{'=' * 60}")

        # Section 1: Grid State
        if grid:
            print()
            ConsoleRenderer._render_grid(grid)

        # Section 2: Proposed Actions
        print(f"\n{'-' * 60}")
        ConsoleRenderer._render_action_plan(orders)

        # Section 3: Configuration & Summary
        print(f"\n{'=' * 60}")
        grid_len = len(grid.zones) if grid else None
        ConsoleRenderer._render_config(config, grid_len)

        if current_price:
            print(f"\nCurrent Price: {current_price}")

        print(f"\n{'-' * 60}")
        if summary:
            ConsoleRenderer._render_summary(summary)

        print(f"\n{'=' * 60}\n")

    @staticmethod
    def _render_config(c: StrategyConfig, grid_len: Optional[int] = None):
        print("CONFIGURATION")
        print(f"Symbol:      {c.symbol}")
        print(f"Type:        {c.type}")

        if hasattr(c, "grid_type"):
            print(f"Grid Type:   {c.grid_type.name}")

        if hasattr(c, "grid_bias"):
            print(f"Grid Bias:   {c.grid_bias.name}")

        if hasattr(c, "total_investment"):
            print(f"Total Inv:   {c.total_investment:.3f}")

        if hasattr(c, "leverage"):
            print(f"Leverage:    {c.leverage}x")

        if hasattr(c, "spread_bips") and c.spread_bips:
            print(f"Spread:      {c.spread_bips} bips")

        if hasattr(c, "grid_count") and c.grid_count is not None:
            print(f"Grid Count:  {c.grid_count}")
        elif grid_len is not None:
            print(f"Grid Count:  {grid_len}")

        if isinstance(c, (SpotGridConfig, PerpGridConfig)):
            print(f"Range:       {c.lower_price:.3f} - {c.upper_price:.3f}")

        if hasattr(c, "trigger_price") and c.trigger_price:
            print(f"Trigger:     {c.trigger_price:.3f}")

    @staticmethod
    def _render_summary(s: StrategySummary):
        if not s:
            return
        print(f"STRATEGY: {s.symbol}")
        print(f"State:    {s.state}")

        if hasattr(s, "grid_spacing_pct"):
            val = s.grid_spacing_pct
            if isinstance(val, tuple):
                print(f"Spacing:  {val[0]:.3f}% - {val[1]:.3f}%")
            else:
                print(f"Spacing:  {val:.3f}%")

        if isinstance(s, SpotGridSummary):
            print("Type:     SPOT GRID")
            print(
                f"Balance:  {s.base_balance:.3f} {s.symbol.split('/')[0]} | {s.quote_balance:.3f} USDC"
            )
            print(f"Matched Profit:  {s.matched_profit:.4f}")

            # Show Net Profit if available
            if hasattr(s, "total_profit"):
                print(f"Net PnL:  {s.total_profit:.4f}")

        elif isinstance(s, PerpGridSummary):
            print(f"Type:     PERP GRID ({s.grid_bias})")
            print(f"Margin:   {s.margin_balance:.3f} USDC")
            print(f"Position: {s.position_size:.3f} ({s.position_side})")
            print(f"Matched Profit:  {s.matched_profit:.4f}")
            print(f"Net PnL:  {s.total_profit:.4f}")
            print(f"Leverage: {s.leverage}x")

    @staticmethod
    def _render_grid(g: GridState):
        print(f"GRID STATE ({len(g.zones)} Zones)")
        print(
            f"{'IDX':<4} | {'RANGE':<25} | {'SPD %':<10} | {'SIZE':<12} | {'EXP_PNL':<12} | {'SIDE':<6} | {'STATUS'}"
        )
        print("-" * 100)

        for z in g.zones:
            rng = f"{z.buy_price}-{z.sell_price}"
            status = "ACTIVE" if z.has_order else "WAITING"
            if z.has_order:
                status += " (RO)" if z.is_reduce_only else ""

            caret = " "

            # Calculations - format spread and pnl to 2 decimals
            spread_pct = ((z.sell_price - z.buy_price) / z.buy_price) * 100
            exp_pnl = (z.sell_price - z.buy_price) * z.size

            print(
                f"{caret}{z.index:<3} | {rng:<25} | {spread_pct:<10.2f} | {z.size:<12.3f} | {exp_pnl:<12.2f} | {z.order_side:<6} | {status}"
            )

    @staticmethod
    def _render_action_plan(orders: List[OrderRequest]):
        print("PROPOSED ACTIONS (What would happen next):")
        if not orders:
            print("  [WAIT] No immediate orders generated.")
            return

        # Separate orders by type for statistics
        buy_orders = []
        sell_orders = []
        cancel_orders = []

        for o in orders:
            if isinstance(o, (LimitOrderRequest, MarketOrderRequest)):
                if o.side.is_buy():
                    buy_orders.append(o)
                else:
                    sell_orders.append(o)
            elif isinstance(o, CancelOrderRequest):
                cancel_orders.append(o)

        # Render orders (first 5 and last 5 if more than 10)
        for i, o in enumerate(orders):
            # Check if this order should be displayed
            if len(orders) > 10 and i >= 5 and i < len(orders) - 5:
                if i == 5:  # Only print truncation message once
                    print(f"  ... ({len(orders) - 10} more orders) ...")
                continue

            ro_tag = ""
            if isinstance(o, (LimitOrderRequest)):
                if o.reduce_only:
                    ro_tag = " [ReduceOnly]"

            if isinstance(o, (LimitOrderRequest, MarketOrderRequest)):
                print(f"  [ORDER] {o.side} {o.sz:.3f} @ {o.price:.3f} {ro_tag}")
            elif isinstance(o, CancelOrderRequest):
                print(f"  [CANCEL] CLOID {o.cloid}")

        # Print summary statistics
        print()
        print("  ORDER SUMMARY:")
        print(f"    Total BUY orders:  {len(buy_orders)}")
        print(f"    Total SELL orders: {len(sell_orders)}")
        if cancel_orders:
            print(f"    Total CANCEL:      {len(cancel_orders)}")

        # Calculate average prices if all orders execute
        if buy_orders:
            total_buy_value = sum(o.price * o.sz for o in buy_orders)
            total_buy_size = sum(o.sz for o in buy_orders)
            avg_buy_price = (
                total_buy_value / total_buy_size if total_buy_size > 0 else 0
            )
            print(f"    Avg BUY price (if all execute):  {avg_buy_price:.4f}")

        if sell_orders:
            total_sell_value = sum(o.price * o.sz for o in sell_orders)
            total_sell_size = sum(o.sz for o in sell_orders)
            avg_sell_price = (
                total_sell_value / total_sell_size if total_sell_size > 0 else 0
            )
            print(f"    Avg SELL price (if all execute): {avg_sell_price:.4f}")
