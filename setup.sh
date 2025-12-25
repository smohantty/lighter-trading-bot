#!/bin/bash
set -e

echo "Setting up Lighter Trading Bot Environment..."

# 1. Create Virtual Environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv --without-pip
    curl -sS https://bootstrap.pypa.io/get-pip.py | ./venv/bin/python
else
    echo "Virtual environment already exists."
fi

# 2. Install Dependencies
echo "Installing dependencies..."
./venv/bin/pip install -r requirements.txt

# 2b. Initialize Submodules
if [ -f ".gitmodules" ]; then
    echo "Initializing submodules..."
    git -c protocol.file.allow=always submodule update --init --recursive
fi

# 3. Install/Update Local SDK (from vendor)
echo "Installing lighter-python SDK from vendor..."
./venv/bin/pip install --force-reinstall --no-deps ./vendor/lighter-python

# 4. Config Initialization (Helper)
if [ ! -f ".env" ]; then
    echo "Creating .env from example..."
    cp .env.example .env
fi

if [ ! -f "wallet_config.json" ]; then
    echo "Creating wallet_config.json template..."
    # We already created a template in previous step, but good to have check
    echo '{
  "master_account_address": "0xYourMasterAddress",
  "agent_private_key": {
    "mainnet": "0xYourMainnetKey",
    "testnet": "0xYourTestnetKey"
  }
}' > wallet_config.json
fi

echo "Setup Complete!"
echo "Please edit 'wallet_config.json' and 'config.yaml' before running."
