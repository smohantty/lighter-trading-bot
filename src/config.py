import yaml
import os
from dataclasses import dataclass
from typing import Optional, Literal, Union
from src.strategy.types import GridBias, GridType

@dataclass
class SpotGridConfig:
    symbol: str
    upper_price: float
    lower_price: float
    grid_type: GridType
    grid_count: int
    total_investment: float
    trigger_price: Optional[float] = None
    type: Literal["spot_grid"] = "spot_grid"

    def validate(self):
        if self.grid_count <= 2:
            raise ValueError(f"Grid count {self.grid_count} must be greater than 2.")
        if self.upper_price <= self.lower_price:
            raise ValueError(f"Upper price {self.upper_price} must be greater than lower price {self.lower_price}.")
        if self.trigger_price is not None:
            if self.trigger_price < self.lower_price or self.trigger_price > self.upper_price:
                raise ValueError(f"Trigger price {self.trigger_price} is outside the grid range [{self.lower_price}, {self.upper_price}].")
            if self.trigger_price <= 0.0:
                raise ValueError("Trigger price must be positive.")
        if "/" not in self.symbol or len(self.symbol) < 3:
            raise ValueError("Spot symbol must be in 'Base/Quote' format")
        if self.total_investment <= 0.0:
            raise ValueError("Total investment must be positive.")

@dataclass
class PerpGridConfig:
    symbol: str
    leverage: int
    upper_price: float
    lower_price: float
    grid_type: GridType
    grid_count: int
    total_investment: float
    grid_bias: GridBias
    is_isolated: bool = False
    trigger_price: Optional[float] = None
    type: Literal["perp_grid"] = "perp_grid"

    def validate(self):
        if self.grid_count <= 2:
            raise ValueError(f"Grid count {self.grid_count} must be greater than 2.")
        if self.upper_price <= self.lower_price:
            raise ValueError(f"Upper price {self.upper_price} must be greater than lower price {self.lower_price}.")
        if self.trigger_price is not None:
            if self.trigger_price < self.lower_price or self.trigger_price > self.upper_price:
                raise ValueError(f"Trigger price {self.trigger_price} is outside the grid range [{self.lower_price}, {self.upper_price}].")
            if self.trigger_price <= 0.0:
                raise ValueError("Trigger price must be positive.")
        if self.leverage <= 0 or self.leverage > 50:
            raise ValueError("Leverage must be between 1 and 50")
        if self.total_investment <= 0.0:
            raise ValueError("Total investment must be positive.")

@dataclass
class WalletInfo:
    mainnet: str
    testnet: str

@dataclass
class WalletConfig:
    master_account_address: str
    agent_private_key: WalletInfo

@dataclass
class ExchangeConfig:
    private_key: str
    wallet_address: str
    network: str

    @staticmethod
    def from_env():
        import json
        
        config_path = os.getenv("LIGHTER_WALLET_CONFIG_FILE")
        if not config_path:
            raise ValueError("LIGHTER_WALLET_CONFIG_FILE environment variable must be set")
        
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            
            # Parse structure matching Rust bot:
            # {
            #   "master_account_address": "...",
            #   "agent_private_key": { "mainnet": "...", "testnet": "..." }
            # }
            master_address = data["master_account_address"]
            keys = data["agent_private_key"]
            
            network = os.getenv("LIGHTER_NETWORK", "mainnet")
            
            if network == "mainnet":
                pk = keys["mainnet"]
            else:
                pk = keys["testnet"]
                
            return ExchangeConfig(private_key=pk, wallet_address=master_address, network=network)
            
        except Exception as e:
            raise ValueError(f"Failed to load wallet config from {config_path}: {e}")

def load_config(path: str) -> Union[SpotGridConfig, PerpGridConfig]:
    with open(path, "r") as f:
        data = yaml.safe_load(f)
    
    strategy_type = data.get("type")
    
    # Convert enums
    if "grid_type" in data:
        data["grid_type"] = GridType(data["grid_type"])
    
    if strategy_type == "spot_grid":
        cfg = SpotGridConfig(**data)
        cfg.validate()
        return cfg
    elif strategy_type == "perp_grid":
        if "grid_bias" in data:
            data["grid_bias"] = GridBias(data["grid_bias"])
        cfg = PerpGridConfig(**data)
        cfg.validate()
        return cfg
    else:
        raise ValueError(f"Unknown strategy type: {strategy_type}")
