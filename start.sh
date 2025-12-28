#!/bin/bash
set -e

# Ensure venv exists
if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Please run ./setup.sh first."
    exit 1
fi

echo "Starting Lighter Trading Bot..."
echo "Using Wallet Config: $(grep LIGHTER_WALLET_CONFIG_FILE .env | cut -d= -f2)"

# Run
./venv/bin/python main.py
