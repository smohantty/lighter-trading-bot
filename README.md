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
- **Dry Run Mode Checks**: Simulates grid placement and validates asset requirements before execution.

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
    ```

2.  **Wallet Config**:
    Lighter uses an **Account Index** and **Ed25519 API Keys** (not your Ethereum Private Key directly) for order signing.
    
    Create `wallet_config.json` with the following structure:
    ```json
    {
        "accountIndex": 1234,
        "masterAccountAddress": "0xYourMasterAccountAddress",
        "mainnet": {
            "baseUrl": "https://api.lighter.xyz",
            "agentApiKeys": {
                "0": "0xYourEd25519ApiKey"
            }
        },
        "testnet": {
            "baseUrl": "https://testnet.zklighter.elliot.ai",
            "agentApiKeys": {
                "0": "0xYourEd25519ApiKey"
            }
        }
    }
    ```
    *To obtain these credentials, use the `system_setup.py` example script in the SDK or export them from the Lighter UI if available.*

3.  **Strategy Config**:
    We provide templates for both Spot and Perpetual grid strategies.
    
    *   **Spot Grid**: `configs/spot_grid.template.yaml`
    *   **Perp Grid**: `configs/perp_grid.template.yaml`
    
    To create your own strategy:
    
    ```bash
    cp configs/spot_grid.template.yaml configs/my_strategy.yaml
    ```
    
    Edit `configs/my_strategy.yaml` with your desired parameters. **Note:** `configs/*.yaml` files (excluding templates) are git-ignored to prevent accidental commits of production settings.

4.  **Simulation Config** (Optional):
    To customize dry-run/simulation behavior (balances, fees, tick usage):
    
    ```bash
    cp simulation_config.template.json simulation_config.json
    ```
    Edit `simulation_config.json` to override balances or set execution modes. You can also specify a custom path using `LIGHTER_SIMULATION_CONFIG_FILE`.

## Usage

**Run Manually (Deployment):**
```bash
./deployment/start.sh --config configs/my_strategy.yaml
```

**Run Manually (Dev):**
```bash
uv run python main.py --config configs/my_strategy.yaml
```

**Run Dry Run (Simulation):**
Visualize the grid strategy and required assets without placing orders. Uses `simulation_config.json` if present.
```bash
uv run python main.py --dry-run --config configs/my_strategy.yaml
```

**Run Tests:**
```bash
uv run pytest tests
```

**Run Visual TUI (Interactive):**
Launch the terminal user interface to configure strategies and run visual simulations:
```bash
uv run python -m src.ui.app
```

## SDK Management

The bot uses the `lighter-python` SDK directly from GitHub via `uv`.

**To Update the SDK:**
```bash
uv lock --upgrade-package lighter-sdk
uv sync
```

## Documentation

- [Deployment Guide](DEPLOYMENT.md): Instructions for running the bot 24/7 using tmux.

## Broadcast Server Verification

The bot runs a WebSocket server on port **9001** (default) that broadcasts real-time status, order updates, and strategy summaries.

**To verify it is working from the command line:**

1.  **Using Python (Recommended)**:
    Since `websockets` is already installed, you can use its interactive client:
    ```bash
    uv run python -m websockets ws://localhost:9001
    ```
    You should see JSON messages streaming in immediately (Config, System Info, Summary).

2.  **Using `wscat`** (if installed):
    ```bash
    wscat -c ws://localhost:9001
    ```
