import yaml
import os
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Literal, Union, Dict
from src.strategy.types import GridBias, GridType


@dataclass
class SimulationConfig:
    """
    Configuration for simulation engine modes.
    
    balance_mode:
        - "real": Fetch actual balances from backend
        - "unlimited": Use unlimited balances (1M each asset)
        - "override": Use balance_overrides for specified assets, real for others
    
    execution_mode:
        - "single_step": One price tick (dry run preview)
        - "continuous": Loop with live price feed and fill simulation
    
    balance_overrides:
        Dictionary mapping asset symbols to their simulated balance.
        Only used when balance_mode is "override".
        Assets not in this dict will use real balance from backend.
        Example: {"LIT": 1000.0, "USDC": 50000.0}
    """
    balance_mode: Literal["real", "unlimited", "override"] = "unlimited"
    execution_mode: Literal["single_step", "continuous"] = "single_step"
    
    # For override balance mode - map asset symbol to balance
    balance_overrides: Dict[str, Decimal] = field(default_factory=dict)
    
    # For unlimited balance mode (default fallback)
    unlimited_amount: Decimal = Decimal("1000000.0")
    
    # For continuous execution mode
    tick_interval_ms: int = 1000
    
    # Fill simulation (only in continuous mode)
    simulate_fills: bool = True
    fee_rate: Decimal = Decimal("0.0005")  # 0.05% default fee


def load_simulation_config(path: Optional[str] = None) -> SimulationConfig:
    """
    Load simulation configuration from a JSON file.
    
    Resolution order:
    1. Explicit path argument
    2. LIGHTER_SIMULATION_CONFIG_FILE environment variable
    3. Default: 'simulation_config.json' in current directory
    
    If file doesn't exist, returns default SimulationConfig (unlimited balance, single_step).
    
    Expected JSON format:
    ```json
    {
      "balance_mode": "unlimited",  // "real", "unlimited", or "override"
      "execution_mode": "single_step",  // "single_step" or "continuous"
      "balances": {  // Only used when balance_mode is "override"
        "LIT": 500.0,
        "USDC": 10000.0
      },
      "tick_interval_ms": 1000,
      "simulate_fills": true,
      "fee_rate": 0.0005
    }
    ```
    """
    
    # Resolution order: arg > env > default
    if path is None:
        path = os.getenv("LIGHTER_SIMULATION_CONFIG_FILE", "simulation_config.json")
    
    # If file doesn't exist, return defaults
    if not os.path.exists(path):
        return SimulationConfig()
    
    try:
        with open(path, "r") as f:
            data = json.load(f)
        
        # Filter out comment keys (those starting with //)
        data = {k: v for k, v in data.items() if not k.startswith("//")}
        
        # Extract balance_overrides from "balances" key
        balance_overrides = {}
        if "balances" in data:
            balance_overrides = {str(k): Decimal(str(v)) for k, v in data["balances"].items()}
            del data["balances"]
        
        # Map known fields
        config = SimulationConfig(
            balance_mode=data.get("balance_mode", "unlimited"),
            execution_mode=data.get("execution_mode", "single_step"),
            balance_overrides=balance_overrides,
            unlimited_amount=Decimal(str(data.get("unlimited_amount", 1_000_000.0))),
            tick_interval_ms=data.get("tick_interval_ms", 1000),
            simulate_fills=data.get("simulate_fills", True),
            fee_rate=Decimal(str(data.get("fee_rate", 0.0005))),
        )
        
        return config
        
    except Exception as e:
        logging.getLogger(__name__).warning(f"Failed to load simulation config from {path}: {e}. Using defaults.")
        return SimulationConfig()

@dataclass
class SpotGridConfig:
    symbol: str
    upper_price: Decimal
    lower_price: Decimal
    grid_type: GridType
    total_investment: Decimal
    grid_count: Optional[int] = None
    spread_bips: Optional[Decimal] = None
    trigger_price: Optional[Decimal] = None
    type: Literal["spot_grid"] = "spot_grid"

    def validate(self):
        # Validate grid_count vs spread_bips
        if self.grid_count is None and self.spread_bips is None:
             raise ValueError("Either grid_count or spread_bips must be provided.")
        
        if self.spread_bips is not None:
             if self.grid_type != GridType.GEOMETRIC:
                  raise ValueError("spread_bips can only be used with GEOMETRIC grid type.")
             if self.spread_bips < Decimal("15"):
                  raise ValueError("spread_bips must be at least 15 bips.")
        
        if self.grid_count is not None:
             if self.grid_count <= 2:
                  raise ValueError(f"Grid count {self.grid_count} must be greater than 2.")

        if self.upper_price <= self.lower_price:
            raise ValueError(f"Upper price {self.upper_price} must be greater than lower price {self.lower_price}.")
        if self.trigger_price is not None:
            if self.trigger_price < self.lower_price or self.trigger_price > self.upper_price:
                raise ValueError(f"Trigger price {self.trigger_price} is outside the grid range [{self.lower_price}, {self.upper_price}].")
            if self.trigger_price <= Decimal("0.0"):
                raise ValueError("Trigger price must be positive.")
        if "/" not in self.symbol or len(self.symbol) < 3:
            raise ValueError("Spot symbol must be in 'Base/Quote' format")
        if self.total_investment <= Decimal("0.0"):
            raise ValueError("Total investment must be positive.")

@dataclass
class PerpGridConfig:
    symbol: str
    leverage: int
    upper_price: Decimal
    lower_price: Decimal
    grid_type: GridType
    total_investment: Decimal
    grid_bias: GridBias
    grid_count: Optional[int] = None
    spread_bips: Optional[Decimal] = None
    is_isolated: bool = False
    trigger_price: Optional[Decimal] = None
    type: Literal["perp_grid"] = "perp_grid"

    def validate(self):
        # Validate grid_count vs spread_bips
        if self.grid_count is None and self.spread_bips is None:
             raise ValueError("Either grid_count or spread_bips must be provided.")
        
        if self.spread_bips is not None:
             if self.grid_type != GridType.GEOMETRIC:
                  raise ValueError("spread_bips can only be used with GEOMETRIC grid type.")
             if self.spread_bips < Decimal("15"):
                  raise ValueError("spread_bips must be at least 15 bips.")
        
        if self.grid_count is not None:
             if self.grid_count <= 2:
                  raise ValueError(f"Grid count {self.grid_count} must be greater than 2.")

        if self.upper_price <= self.lower_price:
            raise ValueError(f"Upper price {self.upper_price} must be greater than lower price {self.lower_price}.")
        if self.trigger_price is not None:
            if self.trigger_price < self.lower_price or self.trigger_price > self.upper_price:
                raise ValueError(f"Trigger price {self.trigger_price} is outside the grid range [{self.lower_price}, {self.upper_price}].")
            if self.trigger_price <= Decimal("0.0"):
                raise ValueError("Trigger price must be positive.")
        if self.leverage <= 0 or self.leverage > 50:
            raise ValueError("Leverage must be between 1 and 50")
        if self.total_investment <= Decimal("0.0"):
            raise ValueError("Total investment must be positive.")


@dataclass
class WalletConfig:
    baseUrl: str
    accountIndex: int
    privateKeys: dict

@dataclass
class ExchangeConfig:
    # Account Identity
    master_account_address: str # L1 Address
    account_index: int          # Account Index (Integer)

    # Agent Credentials
    agent_private_key: str = field(repr=False)
    agent_key_index: int

    # Network Config
    network: str
    base_url: str
    

    @staticmethod
    def from_env():
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
            
            # masterAccountAddress is the L1 Address - Now Required
            master_account_address = full_config.get("masterAccountAddress")
            if not master_account_address:
                 raise ValueError("masterAccountAddress is required in wallet config.")

            if network in full_config:
                data = full_config[network]
            else:
                 raise ValueError(f"Section '{network}' not found in wallet config.")

            base_url = data.get("baseUrl")
            if not base_url:
                raise ValueError(f"'baseUrl' is required in the '{network}' section of the wallet config.")

            private_keys = {int(k): v for k, v in data["agentApiKeys"].items()}
            
            # Use Key Index 0 by default for single-key setup, or first available
            agent_key_index = 0
            agent_private_key = private_keys.get(0)
            if not agent_private_key:
                # Fallback to first key
                if not private_keys:
                     raise ValueError("No private keys found in config")
                agent_key_index = next(iter(private_keys.keys()))
                agent_private_key = private_keys[agent_key_index]

            return ExchangeConfig(
                agent_private_key=agent_private_key, 
                account_index=account_index,
                network=network, 
                base_url=base_url, 
                agent_key_index=agent_key_index,
                master_account_address=master_account_address
            )
            
        except Exception as e:
            raise ValueError(f"Failed to load wallet config from {config_path}: {e}")

Config = Union[SpotGridConfig, PerpGridConfig]
StrategyConfig = Config

def load_config(path: str) -> Config:
    with open(path, "r") as f:
        data = yaml.safe_load(f)
    
    strategy_type = data.get("type")
    
    # Convert enums
    if "grid_type" in data:
        data["grid_type"] = GridType(data["grid_type"])
    
    # Common parsing
    if "upper_price" in data: data["upper_price"] = Decimal(str(data["upper_price"]))
    if "lower_price" in data: data["lower_price"] = Decimal(str(data["lower_price"]))
    if "total_investment" in data: data["total_investment"] = Decimal(str(data["total_investment"]))
    if "trigger_price" in data and data["trigger_price"] is not None: 
        data["trigger_price"] = Decimal(str(data["trigger_price"]))
    if "spread_bips" in data and data["spread_bips"] is not None:
        data["spread_bips"] = Decimal(str(data["spread_bips"]))

    if strategy_type == "spot_grid":
        spot_cfg = SpotGridConfig(**data)
        spot_cfg.validate()
        return spot_cfg
    elif strategy_type == "perp_grid":
        if "grid_bias" in data:
            data["grid_bias"] = GridBias(data["grid_bias"])

        perp_cfg = PerpGridConfig(**data)
        perp_cfg.validate()
        return perp_cfg
    else:
        raise ValueError(f"Unknown strategy type: {strategy_type}")
