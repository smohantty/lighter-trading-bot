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

2.  **Set up Virtual Environment**:
    ```bash
    python3 -m venv venv --without-pip
    curl -sS https://bootstrap.pypa.io/get-pip.py | ./venv/bin/python
    ```

3.  **Install Dependencies**:
    ```bash
    ./venv/bin/pip install -r requirements.txt
    ```

4.  **Install/Patch SDK**:
    The bot relies on a local version of `lighter-python` which may have patches (e.g., auto-reconnect).
    ```bash
    ./venv/bin/pip install --force-reinstall --no-deps ../lighter-python
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
    Create `wallet_config.json` (or point `LIGHTER_WALLET_CONFIG_FILE` to an existing one) with the following structure, matching the format used by the Rust bot:
    ```json
    {
      "master_account_address": "0xYourWalletAddress",
      "agent_private_key": {
        "mainnet": "0xYourMainnetPrivateKey",
        "testnet": "0xYourTestnetPrivateKey"
      }
    }
    ```

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
./venv/bin/python main.py
```

**Run Tests:**
```bash
PYTHONPATH=. ./venv/bin/pytest tests
```

## SDK Management

The bot uses a forked version of the SDK via Git Submodules.

**To Update the SDK:**
Run the helper script:
```bash
./update_sdk.sh
```

**Manual Update:**
```bash
cd vendor/lighter-python
git pull origin main
cd ../..
./venv/bin/pip install --force-reinstall --no-deps ./vendor/lighter-python
```

## Documentation

- [Deployment Guide](docs/deployment.md): Instructions for running the bot 24/7 using Systemd on Linux/VPS.
