# Lighter Trading Bot

A Python-based algorithmic trading bot for [Lighter.xyz](https://lighter.xyz) Perpetual DEX.
This project uses the `lighter-python` SDK to provide automated grid trading strategies.

## Features

- **Perp Grid Strategy**: Automated grid trading logic (Long/Short bias) running on Lighter's order book.
- **Robustness**: Includes auto-reconnection logic for WebSockets to ensure 24/7 uptime.
- **Safety**: 
  - Validates startup configuration (margin requirements, grid range).
  - Uses local virtual environments for isolation.
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
                "42": "0xYourEd25519ApiKey"
            }
        },
        "testnet": {
            "baseUrl": "https://testnet.zklighter.elliot.ai",
            "agentApiKeys": {
                "42": "0xYourEd25519ApiKey"
            }
        }
    }
    ```
    ### How to obtain credentials:
    1.  **Account Index**:
        - Go to [Lighter Portfolio](https://app.lighter.xyz/portfolio).
        - Connect your wallet.
        - Your **Account Index** is displayed in the account details.
        - *Alternatively*: Use the [Accounts by Address API](https://apidocs.lighter.xyz/reference/accountsbyl1address) with your Master Account (Wallet) Address.

    2.  **API Keys**:
        - Go to [Lighter API Keys](https://app.lighter.xyz/apikeys).
        - Click "Create New API Key".
        - Sign the transaction to generate a new key pair.(index , key )
        - Copy the **Private Key** (starts with `0x...`) into `agentApiKeys` -> "index":"key"

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

**Run Individual Test Script:**
```bash
uv run python tests/analyze_orders_api.py
```

## Code Quality

This project uses **Ruff** for linting and formatting, and **Mypy** for type checking.

**Run All Checks (Format, Lint, Type Check):**
```bash
./check_code.sh
```

**Run Ruff Format Only:**
```bash
uv run ruff format .
```

**Run Ruff Lint Only:**
```bash
uv run ruff check .
```

**Run Ruff Lint with Auto-fix:**
```bash
uv run ruff check --fix .
```

**Run Mypy Only:**
```bash
./run_mypy.sh
```

> **Note:** Always run `./check_code.sh` before committing changes to ensure code quality.

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
