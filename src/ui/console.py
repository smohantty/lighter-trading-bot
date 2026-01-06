from typing import List, Optional
from src.config import StrategyConfig, SpotGridConfig, PerpGridConfig
from src.strategy.types import StrategySummary, GridState, PerpGridSummary, SpotGridSummary
from src.model import OrderRequest, OrderSide, LimitOrderRequest, MarketOrderRequest, CancelOrderRequest

class ConsoleRenderer:
    @staticmethod
    def render(
        config: StrategyConfig, 
        summary: Optional[StrategySummary], 
        grid: Optional[GridState], 
        orders: List[OrderRequest],
        current_price: Optional[float] = None
    ):
        print(f"\n{'='*60}")
        print(" SIMULATION DRY RUN REPORT")
        print(f"{'='*60}")
        
        # Section 1: Grid State
        if grid:
            print()
            ConsoleRenderer._render_grid(grid)
        
        # Section 2: Proposed Actions
        print(f"\n{'-'*60}")
        ConsoleRenderer._render_action_plan(orders)
        
        # Section 3: Configuration & Summary
        print(f"\n{'='*60}")
        ConsoleRenderer._render_config(config)
        
        if current_price:
            print(f"\nCurrent Price: {current_price:.4f}")
        
        print(f"\n{'-'*60}")
        if summary:
            ConsoleRenderer._render_summary(summary)
        
        print(f"\n{'='*60}\n")        

    @staticmethod
    def _render_config(c: StrategyConfig):
        print("CONFIGURATION")
        print(f"Symbol:      {c.symbol}")
        print(f"Type:        {c.type}")
        
        if hasattr(c, "grid_type"):
            print(f"Grid Type:   {c.grid_type.name}")

        if hasattr(c, "grid_bias"):
            print(f"Grid Bias:   {c.grid_bias.name}")
            
        if hasattr(c, "total_investment"):
            print(f"Total Inv:   {c.total_investment}")
        
        if hasattr(c, "leverage"):
            print(f"Leverage:    {c.leverage}x")
            
        if hasattr(c, "grid_count"):
            print(f"Grid Count:  {c.grid_count}")
            
        if isinstance(c, (SpotGridConfig, PerpGridConfig)):
             print(f"Range:       {c.lower_price} - {c.upper_price}")
             
        if hasattr(c, "trigger_price") and c.trigger_price:
             print(f"Trigger:     {c.trigger_price}")

    @staticmethod
    def _render_summary(s: StrategySummary):
        if not s: return  # MyPy safety
        if not s: return  # MyPy safety
        print(f"STRATEGY: {s.symbol}")
        # 'price' is not in the Summary types anymore, removing it
        print(f"State:    {s.state}")
        
        if hasattr(s, "grid_spacing_pct"):
            val = s.grid_spacing_pct
            if isinstance(val, tuple):
                 print(f"Spacing:  {val[0]:.2f}% - {val[1]:.2f}%")
            else:
                 print(f"Spacing:  {val:.2f}%")
        
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
        print(f"{'IDX':<4} | {'RANGE':<20} | {'SPD %':<8} | {'SIZE':<8} | {'EXP_PNL':<8} | {'SIDE':<6} | {'STATUS'}")
        print("-" * 85)
        
        # Limit to first few, last few if too many?
        display_zones = g.zones
        if len(display_zones) > 100:
             display_zones = display_zones[:50] + display_zones[-50:]
             
        for z in display_zones:
            rng = f"{z.buy_price:.2f}-{z.sell_price:.2f}"
            status = "ACTIVE" if z.has_order else "WAITING"
            if z.has_order: status += " (RO)" if z.is_reduce_only else ""
            
            # Simple highlight
            caret = " "
            # g.current_price doesn't exist in GridState definition, removing highlight logic requiring it
            # if z.lower_price <= g.current_price <= z.upper_price:
            #    caret = "*"
                
            # Calculations
            spread_pct = ((z.sell_price - z.buy_price) / z.buy_price) * 100
            exp_pnl = (z.sell_price - z.buy_price) * z.size
            
            print(f"{caret}{z.index:<3} | {rng:<20} | {spread_pct:<8.2f} | {z.size:<8} | {exp_pnl:<8.4f} | {z.order_side:<6} | {status}")
            
        if len(g.zones) > 100:
            print(f"... (Hiding {len(g.zones)-100} zones) ...")

    @staticmethod
    def _render_action_plan(orders: List[OrderRequest]):
        print("PROPOSED ACTIONS (What would happen next):")
        if not orders:
            print("  [WAIT] No immediate orders generated.")
            return

        for o in orders:
            ro_tag = ""
            if isinstance(o, (LimitOrderRequest)):
                 if o.reduce_only: ro_tag = " [ReduceOnly]"
                 
            # Extract common attributes safely or check instance
            side = o.side if hasattr(o, "side") else "?"
            sz = o.sz if hasattr(o, "sz") else "?"
            price = o.price if hasattr(o, "price") else "?"
            
            if isinstance(o, (LimitOrderRequest, MarketOrderRequest)):
                print(f"  [ORDER] {o.side} {o.sz} @ {o.price} {ro_tag}")
            elif isinstance(o, CancelOrderRequest):
                print(f"  [CANCEL] CLOID {o.cloid}")
