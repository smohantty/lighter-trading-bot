#!/bin/bash

SESSION_NAME="lighter-bot"
MAX_WAIT_SECONDS="${LIGHTER_STOP_WAIT_SECONDS:-20}"

if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "No session named '$SESSION_NAME' found."
    exit 0
fi

echo "Sending SIGINT to the bot in session '$SESSION_NAME'..."
# Send Ctrl+C to the active pane
tmux send-keys -t "$SESSION_NAME" C-c

# Wait for graceful shutdown, including exchange cancel requests on exit
elapsed=0
while tmux has-session -t "$SESSION_NAME" 2>/dev/null; do
    if [ "$elapsed" -ge "$MAX_WAIT_SECONDS" ]; then
        break
    fi
    sleep 1
    elapsed=$((elapsed + 1))
done

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "Bot session still active after ${MAX_WAIT_SECONDS}s. Killing session..."
    tmux kill-session -t "$SESSION_NAME"
    echo "Session killed."
else
    echo "Bot stopped gracefully in ${elapsed}s."
fi
