#!/bin/bash
set -e

SESSION_NAME="lighter-bot"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$SCRIPT_DIR/.."

# Default config path (Must be provided)
CONFIG_PATH=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --config) CONFIG_PATH="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [ -z "$CONFIG_PATH" ]; then
    echo "Error: You must provide a configuration file using --config"
    echo "Usage: ./deployment/start.sh --config configs/my_strategy.yaml"
    exit 1
fi

# Check if tmux is installed
if ! command -v tmux &> /dev/null; then
    echo "Error: tmux is not installed. Please install it first."
    exit 1
fi

cd "$PROJECT_ROOT"

# Check if session exists
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "Session '$SESSION_NAME' already exists."
    echo "Attach with: tmux attach -t $SESSION_NAME"
    exit 0
fi

# Run Dry Run
echo "Running Dry Run Simulation..."
uv run python main.py --dry-run --config "$CONFIG_PATH"
DRY_RUN_EXIT_CODE=$?

if [ $DRY_RUN_EXIT_CODE -ne 0 ]; then
    echo "Dry Run Failed or Validated with Errors. Aborting."
    exit 1
fi

echo ""
read -p "Do you want to proceed with live deployment? (y/N) " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Deployment aborted."
    exit 0
fi

echo "Starting new tmux session '$SESSION_NAME' with config: $CONFIG_PATH"
# Create a new detached session
tmux new-session -d -s "$SESSION_NAME"

# Run the bot in the session
# We use 'exec' or just run it. Using uv run.
tmux send-keys -t "$SESSION_NAME" "uv run python main.py --config $CONFIG_PATH" C-m

echo "Bot started in background."
echo "View logs/process with: tmux attach -t $SESSION_NAME"
echo "To detach again, press: Ctrl+b, then d"
