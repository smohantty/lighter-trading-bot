import json
import logging
import os
from decimal import Decimal
from enum import Enum
from typing import Annotated, Dict, Literal, Optional, Union

import yaml
from pydantic import BaseModel, BeforeValidator, Field, field_validator, model_validator

from src.strategy.types import GridBias, GridType


def case_insensitive_enum_validator(v: Union[str, Enum]) -> Union[str, Enum]:
    if isinstance(v, str):
        return v.lower()
    return v


class SimulationConfig(BaseModel):
    """
    Configuration for simulation engine modes.
    """

    balance_mode: Literal["real", "unlimited", "override"] = "unlimited"
    execution_mode: Literal["single_step", "continuous"] = "single_step"

    # For override balance mode - map asset symbol to balance
    balance_overrides: Dict[str, Decimal] = Field(default_factory=dict)

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

        # Extract balance_overrides from "balances" key if present (legacy support/custom format)
        if "balances" in data:
            data["balance_overrides"] = data.pop("balances")

        return SimulationConfig(**data)

    except Exception as e:
        logging.getLogger(__name__).warning(
            f"Failed to load simulation config from {path}: {e}. Using defaults."
        )
        return SimulationConfig()


class BaseGridConfig(BaseModel):
    symbol: str
    grid_range_high: Decimal
    grid_range_low: Decimal
    grid_type: Annotated[GridType, BeforeValidator(case_insensitive_enum_validator)]
    total_investment: Decimal
    grid_count: Optional[int] = None
    spread_bips: Optional[Decimal] = None
    trigger_price: Optional[Decimal] = None

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        # Basic check, mainly for Spot but good generally to have structure
        if "/" not in v and "PERP" not in v and len(v) < 3:
            # Just a heuristic warning or check? Keeping strict for spot mostly.
            pass
        return v

    @field_validator("total_investment")
    @classmethod
    def validate_investment(cls, v: Decimal) -> Decimal:
        if v <= Decimal("0.0"):
            raise ValueError("Total investment must be positive.")
        return v

    @field_validator("trigger_price")
    @classmethod
    def validate_trigger_price_positive(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is not None and v <= Decimal("0.0"):
            raise ValueError("Trigger price must be positive.")
        return v

    @model_validator(mode="after")
    def validate_grid_params(self) -> "BaseGridConfig":
        if self.grid_count is None and self.spread_bips is None:
            raise ValueError("Either grid_count or spread_bips must be provided.")

        if self.spread_bips is not None:
            if self.grid_type != GridType.GEOMETRIC:
                raise ValueError(
                    "spread_bips can only be used with GEOMETRIC grid type."
                )
            if self.spread_bips < Decimal("15"):
                raise ValueError("spread_bips must be at least 15 bips.")

        if self.grid_count is not None:
            if self.grid_count <= 2:
                raise ValueError(
                    f"Grid count {self.grid_count} must be greater than 2."
                )

        if self.grid_range_high <= self.grid_range_low:
            raise ValueError(
                f"Upper price {self.grid_range_high} must be greater than lower price {self.grid_range_low}."
            )

        if self.trigger_price is not None:
            if (
                self.trigger_price < self.grid_range_low
                or self.trigger_price > self.grid_range_high
            ):
                raise ValueError(
                    f"Trigger price {self.trigger_price} is outside the grid range [{self.grid_range_low}, {self.grid_range_high}]."
                )

        return self


class SpotGridConfig(BaseGridConfig):
    type: Literal["spot_grid"] = "spot_grid"

    @field_validator("symbol")
    @classmethod
    def validate_spot_symbol(cls, v: str) -> str:
        if "/" not in v or len(v) < 3:
            raise ValueError("Spot symbol must be in 'Base/Quote' format")
        return v


class PerpGridConfig(BaseGridConfig):
    leverage: int
    grid_bias: Annotated[GridBias, BeforeValidator(case_insensitive_enum_validator)]
    is_isolated: bool = False
    type: Literal["perp_grid"] = "perp_grid"

    @field_validator("leverage")
    @classmethod
    def validate_leverage(cls, v: int) -> int:
        if v <= 0 or v > 50:
            raise ValueError("Leverage must be between 1 and 50")
        return v


class WalletConfig(BaseModel):
    baseUrl: str
    accountIndex: int
    privateKeys: dict
    # We can rely on Pydantic to validate types,
    # but the explicit dict structure for keys might need a custom validator or type if we want to be strict.


class ExchangeConfig(BaseModel):
    # Account Identity
    master_account_address: str  # L1 Address
    account_index: int  # Account Index (Integer)

    # Agent Credentials
    agent_private_key: str = Field(repr=False)
    agent_key_index: int

    # Network Config
    network: str
    base_url: str

    @staticmethod
    def from_env() -> "ExchangeConfig":
        config_path = os.getenv("LIGHTER_WALLET_CONFIG_FILE")
        if not config_path:
            raise ValueError(
                "LIGHTER_WALLET_CONFIG_FILE environment variable must be set"
            )

        network = os.getenv("LIGHTER_NETWORK", "mainnet").lower()
        if network not in ["mainnet", "testnet"]:
            raise ValueError(
                f"Invalid LIGHTER_NETWORK: {network}. Must be 'mainnet' or 'testnet'."
            )

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
                raise ValueError(
                    f"'baseUrl' is required in the '{network}' section of the wallet config."
                )

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
                master_account_address=master_account_address,
            )

        except Exception as e:
            raise ValueError(
                f"Failed to load wallet config from {config_path}: {e}"
            ) from e


Config = Union[SpotGridConfig, PerpGridConfig]
StrategyConfig = Config


def load_config(path: str) -> Config:
    with open(path, "r") as f:
        data = yaml.safe_load(f)

    strategy_type = data.get("type")

    if strategy_type == "spot_grid":
        return SpotGridConfig(**data)
    elif strategy_type == "perp_grid":
        return PerpGridConfig(**data)
    else:
        raise ValueError(f"Unknown strategy type: {strategy_type}")
