# Lighter Trading Bot

A Python-based algorithmic trading bot for [Lighter.xyz](https://lighter.xyz) Perpetual DEX. 
This project is a direct port of the logic and architecture from the Rust-based `hyperliquid-trading-bot`, ensuring consistent strategy behavior while leveraging the `lighter-python` SDK.

## Features

- **Perp Grid Strategy**: Automated grid trading logic (Long/Short/Neutral bias) running on Lighter's order book.
- **Robustness**: Includes auto-reconnection logic for WebSockets to ensure 24/7 uptime.
- **Safety**: 
  - Validates startup configuration (margin requirements, grid range).
  - Uses local virtual environments for isolation.
- **Ported Logic**: 1:1 strategy implementation matching the proven Rust codebase.

## Prerequisites

- Python 3.10+
- A Lighter.xyz account (Private Key and Wallet Address)

## Installation

1.  **Clone/Enter the directory**:
    ```bash
    cd lighter-trading-bot
    ```

2.  **Install `uv`** (if not already installed):
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

3.  **Install Dependencies**:
    ```bash
    uv sync
    ```

## Configuration

1.  **Environment Variables**:
    Copy the example file and fill in your credentials.
    ```bash
    cp .env.example .env
    ```
    Edit `.env`:
    ```ini
    # Network Selection: mainnet (default) or testnet
    LIGHTER_NETWORK=mainnet
    
    # Path to your wallet configuration file
    LIGHTER_WALLET_CONFIG_FILE=wallet_config.json
    
    # Path to strategy configuration
    CONFIG_PATH=config.yaml
    ```

2.  **Wallet Config**:
    Lighter uses an **Account Index** and **Ed25519 API Keys** (not your Ethereum Private Key directly) for order signing.
    
    Create `wallet_config.json` with the following structure:
    ```json
    {
      "baseUrl": "https://api.lighter.xyz",
      "accountIndex": 1234,
      "privateKeys": {
        "0": "0xYourEd25519ApiKey"
      }
    }
    ```
    *To obtain these credentials, use the `system_setup.py` example script in the SDK or export them from the Lighter UI if available.*

3.  **Strategy Config**:
    Edit `config.yaml` to define your grid parameters.
    ```yaml
    type: perp_grid
    symbol: ETH-USDC    # Lighter symbol format
    upper_price: 3000.0
    lower_price: 2000.0
    grid_count: 10
    total_investment: 100.0
    grid_bias: Long
    ```

## Usage

**Run Manually:**
```bash
./start.sh
# OR
uv run python main.py
```

**Run Tests:**
```bash
uv run pytest tests
```

## SDK Management

The bot uses the `lighter-python` SDK directly from GitHub via `uv`.

**To Update the SDK:**
```bash
uv lock --upgrade-package lighter-python
uv sync
```

## Documentation

- [Deployment Guide](docs/deployment.md): Instructions for running the bot 24/7 using Systemd on Linux/VPS.
