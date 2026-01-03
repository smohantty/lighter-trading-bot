from textual.screen import Screen
from textual.app import ComposeResult
from textual.widgets import Header, Footer, Static, DataTable, Log
from textual.containers import Container, Grid
from textual.reactive import reactive

from src.config import StrategyConfig, SpotGridConfig, PerpGridConfig, ExchangeConfig
from src.strategy.base import Strategy
from src.strategy.spot_grid import SpotGridStrategy
from src.strategy.perp_grid import PerpGridStrategy
from src.engine.simulation import SimulationEngine
import asyncio
from typing import Optional, cast

class SimulationScreen(Screen):
    """Screen for running the simulation."""
    
    BINDINGS = [("space", "step", "Single Step"), ("r", "run", "Run Auto")]

    def __init__(self, config: StrategyConfig):
        super().__init__()
        self.config = config
        self.engine: Optional[SimulationEngine] = None
        self.auto_run = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Grid(
            Static("Stats Panel (Loading...)", id="stats-panel"),
            DataTable(id="grid-table"),
            Log(id="log-panel"),
            id="simulation-grid"
        )
        yield Footer()

    async def on_mount(self):
        # Initialize Engine
        # We need a dummy exchange config or load from env
        ex_config = ExchangeConfig.from_env()
        
        if self.config.type == "spot_grid":
            strategy: Strategy = SpotGridStrategy(cast(SpotGridConfig, self.config))
        else:
            strategy = PerpGridStrategy(cast(PerpGridConfig, self.config))
            
        self.engine = SimulationEngine(self.config, ex_config, strategy)
        if self.engine:
            await self.engine.initialize()
        
        self.log_panel = self.query_one("#log-panel", Log)
        self.log_panel.write_line(f"Simulation Initialized for {self.config.symbol}")
        
        # Setup Table
        table = self.query_one("#grid-table", DataTable)
        table.add_columns("Idx", "Price", "Size", "Side", "Status")

        self.update_ui()

    async def action_step(self):
        """Run a single simulation step."""
        if self.engine:
            try:
                price = await self.engine.run_single_step()
                self.log_panel.write_line(f"Step: Price={price}")
                self.update_ui()
            except Exception as e:
                self.log_panel.write_line(f"Error: {e}")

    def update_ui(self):
        if not self.engine: return
        
        # Update Stats
        summary = self.engine.get_summary()
        stats = self.query_one("#stats-panel", Static)
        if summary:
            # Format nicely
            content = f"PnL Realized: {summary.realized_pnl:.4f}\n"
            content += f"PnL Unreal: {summary.unrealized_pnl:.4f}\n"
            if isinstance(self.config, SpotGridConfig):
                 content += f"Balance: Base={getattr(summary, 'base_balance', 0):.4f} Quote={getattr(summary, 'quote_balance', 0):.4f}"
            stats.update(content)
            
        # Update Grid Table
        grid = self.engine.get_grid_state()
        table = self.query_one("#grid-table", DataTable)
        table.clear()
        
        if grid:
            for zone in grid.zones:
                # Highlight current zone?
                status = "ACTIVE" if zone.has_order else "IDLE"
                table.add_row(
                    str(zone.index),
                    f"{zone.lower_price:.4f} - {zone.upper_price:.4f}",
                    f"{zone.size:.4f}",
                    str(zone.pending_side),
                    status
                )

    async def on_unload(self):
        if self.engine:
            await self.engine.cleanup()
