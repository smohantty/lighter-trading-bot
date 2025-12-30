#!/bin/bash
set -e

echo "Starting Lighter Trading Bot..."
# uv automatically manages the environment
# We parse the wallet config for display purposes only
if [ -f .env ]; then
    echo "Using Wallet Config: $(grep LIGHTER_WALLET_CONFIG_FILE .env | cut -d= -f2)"
fi

# Run via uv
uv run python main.py "$@"
