#!/bin/bash

SESSION_NAME="lighter-bot"

if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "No session named '$SESSION_NAME' found."
    exit 0
fi

echo "Sending SIGINT to the bot in session '$SESSION_NAME'..."
# Send Ctrl+C to the active pane
tmux send-keys -t "$SESSION_NAME" C-c

# Wait a moment for graceful shutdown
sleep 2

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "Bot session still active. Killing session..."
    tmux kill-session -t "$SESSION_NAME"
    echo "Session killed."
else
    echo "Bot stopped gracefully."
fi
