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
class WalletConfig:
    baseUrl: str
    accountIndex: int
    privateKeys: dict

@dataclass
class ExchangeConfig:
    private_key: str
    wallet_address: str
    network: str
    base_url: str = "https://api.lighter.xyz"
    api_key_index: int = 0
    master_account_address: str = ""

    @staticmethod
    def from_env():
        import json
        
        config_path = os.getenv("LIGHTER_WALLET_CONFIG_FILE")
        if not config_path:
            raise ValueError("LIGHTER_WALLET_CONFIG_FILE environment variable must be set")
        
        network = os.getenv("LIGHTER_NETWORK", "mainnet").lower()
        if network not in ["mainnet", "testnet"]:
            raise ValueError(f"Invalid LIGHTER_NETWORK: {network}. Must be 'mainnet' or 'testnet'.")

        try:
            with open(config_path, "r") as f:
                full_config = json.load(f)
            
            # Read shared fields from root
            account_index = int(full_config["accountIndex"])
            master_account_address = full_config.get("masterAccountAddress", "") # Optional for now or required?

            if network in full_config:
                data = full_config[network]
            else:
                 raise ValueError(f"Section '{network}' not found in wallet config.")

            base_url = data.get("baseUrl")
            if not base_url:
                base_url = "https://api.lighter.xyz" if network == "mainnet" else "https://testnet.zklighter.elliot.ai"

            private_keys = {int(k): v for k, v in data["agentApiKeys"].items()}
            
            # Use Key Index 0 by default for single-key setup, or first available
            api_key_index = 0
            private_key = private_keys.get(0)
            if not private_key:
                # Fallback to first key
                if not private_keys:
                     raise ValueError("No private keys found in config")
                api_key_index = next(iter(private_keys.keys()))
                private_key = private_keys[api_key_index]

            return ExchangeConfig(
                private_key=private_key, 
                wallet_address=str(account_index), 
                network=network, 
                base_url=base_url, 
                api_key_index=api_key_index,
                master_account_address=master_account_address
            )
            
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
