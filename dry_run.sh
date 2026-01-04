#!/bin/bash
# Dry Run Simulation
#
# Usage: ./dry_run.sh [config_file]
#        ./dry_run.sh --config path/to/config.yaml
#
# Examples:
#   ./dry_run.sh                           # Uses default strategy config
#   ./dry_run.sh configs/spot_LIT.yaml     # Positional config
#   ./dry_run.sh --config configs/spot_LIT.yaml  # Named config
#
# Simulation configuration is read from LIGHTER_SIMULATION_CONFIG_FILE in .env
# (default: simulation_config.json)

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo "=========================================="
echo " SIMULATION MODE"
echo "=========================================="

# If no args provided, use default config
if [ $# -eq 0 ]; then
    echo "Strategy: configs/spot_LIT.yaml (default)"
    echo ""
    exec uv run python main.py --dry-run configs/spot_LIT.yaml
else
    echo ""
    exec uv run python main.py --dry-run "$@"
fi
