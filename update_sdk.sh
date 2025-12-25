#!/bin/bash
set -e

echo "Updating lighter-python SDK from remote..."

# Navigate to submodule
cd vendor/lighter-python

# Fetch latest from origin (your fork)
echo "Fetching changes from origin..."
git fetch origin

# Checkout main (or specific branch) and pull
echo "Updating to latest main..."
git checkout main
git pull origin main

# Return to root
cd ../..

echo "Reinstalling SDK to dependencies..."
./venv/bin/pip install --force-reinstall --no-deps ./vendor/lighter-python

# Commit the version bump in the main repo (optional, user might want to verify first)
echo "SDK Updated."
echo "If you want to lock this version, run: git add vendor/lighter-python && git commit -m 'Update SDK'"
