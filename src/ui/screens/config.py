from textual.screen import Screen
from textual.app import ComposeResult
from textual.widgets import Header, Footer, Input, Button, Label, Select, Static
from textual.containers import Container, Vertical, Horizontal
from textual.message import Message

from src.strategy.types import GridType, GridBias
from src.config import SpotGridConfig, PerpGridConfig
from src.ui.screens.simulation import SimulationScreen
from typing import Union

class ConfigScreen(Screen):
    """Screen for configuring the strategy."""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Label("Strategy Configuration", id="config-title"),
            Select(
                [(t, t) for t in ["spot_grid", "perp_grid"]],
                prompt="Select Strategy Type",
                id="strategy-type-select",
                value="spot_grid"
            ),
            Vertical(
                # Common Fields
                self._input_field("Symbol", "symbol", "LIT/USDC"),
                self._input_field("Lower Price", "lower_price", "0.5"),
                self._input_field("Upper Price", "upper_price", "1.5"),
                self._input_field("Grid Count", "grid_count", "10"),
                self._input_field("Total Investment", "total_investment", "100.0"),
                self._input_field("Trigger Price (Optional)", "trigger_price", ""),
                
                Horizontal(
                    Label("Grid Type:"),
                    Select([(g.value, g.value) for g in GridType], value=GridType.ARITHMETIC.value, id="grid_type"),
                    classes="input-container"
                ),
                
                # Perp Specific (Hidden by default or toggled)
                Vertical(
                    self._input_field("Leverage", "leverage", "1"),
                    Horizontal(
                        Label("Grid Bias:"),
                        Select([(b.value, b.value) for b in GridBias], value=GridBias.LONG.value, id="grid_bias"),
                        classes="input-container"
                    ),
                    id="perp-specific-fields",
                    classes="hidden" # We will toggle display via CSS/JS logic
                ),
                
                Button("Start Simulation", variant="primary", id="start-btn"),
                id="config-form"
            )
        )
        yield Footer()

    def _input_field(self, label: str, id: str, placeholder: str) -> Horizontal:
        return Horizontal(
            Label(label),
            Input(placeholder=placeholder, id=id),
            classes="input-container"
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "strategy-type-select":
            is_perp = event.value == "perp_grid"
            self.query_one("#perp-specific-fields").display = is_perp

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-btn":
            self._validate_and_submit()

    def _validate_and_submit(self):
        try:
            strategy_type = self.query_one("#strategy-type-select", Select).value
            symbol = self.query_one("#symbol", Input).value
            lower = float(self.query_one("#lower_price", Input).value)
            upper = float(self.query_one("#upper_price", Input).value)
            count = int(self.query_one("#grid_count", Input).value)
            inv = float(self.query_one("#total_investment", Input).value)
            
            trigger_val = self.query_one("#trigger_price", Input).value
            trigger = float(trigger_val) if trigger_val else None
            
            # Grid Type
            g_type_name = self.query_one("#grid_type", Select).value
            # Map back to value from name if needed, or Select returns value if (value, label) provided
            # Wait, Select options are (value, label), so .value returns the value.
            # GridType enum values are "Arithmetic", "Geometric"
            grid_type = GridType(g_type_name)

            if strategy_type == "spot_grid":
                config: Union[SpotGridConfig, PerpGridConfig] = SpotGridConfig(
                    symbol=symbol,
                    upper_price=upper,
                    lower_price=lower,
                    grid_type=grid_type,
                    grid_count=count,
                    total_investment=inv,
                    trigger_price=trigger
                )
            else:
                lev = int(self.query_one("#leverage", Input).value)
                bias_val = self.query_one("#grid_bias", Select).value
                bias = GridBias(bias_val)
                config = PerpGridConfig(
                    symbol=symbol,
                    upper_price=upper,
                    lower_price=lower,
                    grid_type=grid_type,
                    grid_count=count,
                    total_investment=inv,
                    trigger_price=trigger,
                    leverage=lev,
                    grid_bias=bias
                )
            
            config.validate()
            
            # Switch to Simulation Screen with config
            self.app.push_screen(SimulationScreen(config))
            
        except ValueError as e:
            self.notify(str(e), severity="error")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")
