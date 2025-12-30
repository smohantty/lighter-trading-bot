#!/bin/bash
set -e

SESSION_NAME="lighter-bot"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$SCRIPT_DIR/.."

# Default config path
CONFIG_PATH="spot_LIT.yaml"

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --config) CONFIG_PATH="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

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

echo "Starting new tmux session '$SESSION_NAME' with config: $CONFIG_PATH"
# Create a new detached session
tmux new-session -d -s "$SESSION_NAME"

# Run the bot in the session
# We use 'exec' or just run it. Using uv run.
tmux send-keys -t "$SESSION_NAME" "uv run python main.py --config $CONFIG_PATH" C-m

echo "Bot started in background."
echo "View logs/process with: tmux attach -t $SESSION_NAME"
echo "To detach again, press: Ctrl+b, then d"
