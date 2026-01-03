from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Button, Static, Label, Select
from textual.containers import Container, Vertical, Horizontal
from textual.screen import Screen
from dotenv import load_dotenv

# Load Environment Variables immediately
load_dotenv()

from src.ui.screens.config import ConfigScreen
from src.ui.screens.simulation import SimulationScreen

from typing import ClassVar, Callable, Any

class TradingBotApp(App):
    """The Lighter Trading Bot TUI Application."""

    CSS_PATH = "tui.css"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("d", "toggle_dark", "Toggle Dark Mode"),
    ]
    
    SCREENS: ClassVar[dict[str, Callable[[], Screen[Any]]]] = {
        "config": ConfigScreen,
    }

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header()
        yield Footer()
        
    def on_mount(self) -> None:
        """Called when app starts."""
        self.push_screen("config")
        
    def action_toggle_dark(self) -> None:
        """An action to toggle dark mode."""
        self.theme = "dark" if self.theme == "light" else "light"

if __name__ == "__main__":
    app = TradingBotApp()
    app.run()
