#!/bin/bash
set -e

echo "Running Ruff Format..."
uv run ruff format .

echo "Running Ruff Check..."
uv run ruff check --fix .

echo "Running Mypy..."
./run_mypy.sh

echo "All checks passed!"
