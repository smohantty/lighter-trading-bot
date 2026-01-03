from typing import List, Optional
from src.strategy.types import StrategySummary, GridState, PerpGridSummary, SpotGridSummary
from src.model import OrderRequest, OrderSide

class ConsoleRenderer:
    @staticmethod
    def render(summary: Optional[StrategySummary], grid: Optional[GridState], orders: List[OrderRequest]):
        print(f"\n{'='*60}")
        print(" SIMULATION DRY RUN REPORT")
        print(f"{'='*60}\n")
        
        if summary:
            ConsoleRenderer._render_summary(summary)
            
        print("\n" + "-"*60 + "\n")
        
        if grid:
            ConsoleRenderer._render_grid(grid)
            
        print("\n" + "-"*60 + "\n")
        
        ConsoleRenderer._render_action_plan(orders)
        
        print(f"\n{'='*60}\n")

    @staticmethod
    def _render_summary(s: StrategySummary):
        print(f"STRATEGY: {s.symbol}")
        print(f"Price:    {s.price}")
        print(f"State:    {s.state}")
        
        if isinstance(s, SpotGridSummary):
            print(f"Type:     SPOT GRID")
            print(f"Balance:  {s.base_balance:.4f} {s.symbol.split('/')[0]} | {s.quote_balance:.2f} USDC")
            print(f"Inv Base: {s.position_size:.4f}")
        elif isinstance(s, PerpGridSummary):
            print(f"Type:     PERP GRID ({s.grid_bias})")
            print(f"Margin:   {s.margin_balance:.2f} USDC")
            print(f"Position: {s.position_size:.4f} ({s.position_side})")
            print(f"Leverage: {s.leverage}x")

    @staticmethod
    def _render_grid(g: GridState):
        print(f"GRID STATE ({len(g.zones)} Zones)")
        print(f"{'IDX':<4} | {'RANGE':<20} | {'SIZE':<8} | {'SIDE':<6} | {'STATUS'}")
        print("-" * 60)
        
        # Limit to first few, last few if too many?
        display_zones = g.zones
        if len(display_zones) > 20:
             display_zones = display_zones[:10] + display_zones[-10:]
             
        for z in display_zones:
            rng = f"{z.lower_price:.2f}-{z.upper_price:.2f}"
            status = "ACTIVE" if z.has_order else "WAITING"
            if z.has_order: status += " (RO)" if z.is_reduce_only else ""
            
            # Simple highlight
            caret = " "
            if z.lower_price <= g.current_price <= z.upper_price:
                caret = "*"
            
            print(f"{caret}{z.index:<3} | {rng:<20} | {z.size:<8} | {z.pending_side:<6} | {status}")
            
        if len(g.zones) > 20:
            print(f"... (Hiding {len(g.zones)-20} zones) ...")

    @staticmethod
    def _render_action_plan(orders: List[OrderRequest]):
        print("PROPOSED ACTIONS (What would happen next):")
        if not orders:
            print("  [WAIT] No immediate orders generated.")
            return

        for o in orders:
            ro_tag = " [ReduceOnly]" if o.reduce_only else ""
            print(f"  [ORDER] {o.side} {o.sz} @ {o.price} {ro_tag}")
