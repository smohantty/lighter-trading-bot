# Deployment Guide

This guide describes how to deploy the Lighter Trading Bot using `tmux`. This method ensures the bot keeps running after you disconnect from the server/terminal, but requires manual startup (it will **not** auto-start on boot).

## Prerequisites
- **tmux**: Must be installed (`sudo apt install tmux` on Ubuntu/Debian, or `brew install tmux` on macOS).
- **uv**: The Python package manager used by this project.

## Quick Start

### 1. Start the Bot
Run the start script. By default, it uses `spot_LIT.yaml`.

```bash
./deployment/start.sh
```

**Options:**
- Specify a custom config:
  ```bash
  ./deployment/start.sh --config path/to/my_config.yaml
  ```

### 2. View the Bot
To see what the bot is doing (logs, status):

```bash
tmux attach -t lighter-bot
```

**To Detach (Exit view without stopping bot):**
Press `Ctrl+b`, then press `d`.

### 3. Stop the Bot
Gracefully stop the bot and close the session:

```bash
./deployment/stop.sh
```

## Troubleshooting

- **Session not found**: The bot might have crashed immediately. Try running without tmux to debug:
  ```bash
  uv run python main.py --config spot_LIT.yaml
  ```
- **"Address already in use"**: Ensure no other instance is running. The `stop.sh` script attempts to kill the session, but check manually with `ps aux | grep main.py` or similar if issues persist.
