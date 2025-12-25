# Deployment Guide

This guide describes how to deploy the `lighter-trading-bot` on a Linux VPS (Ubuntu/Debian) for continuous 24/7 operation.

## Recommended Method: Systemd Service

We recommend using `systemd` to manage the bot process. It provides automatic restarts on failure, logging, and startup management.

### 1. Prepare the Environment

Ensure you have installed the bot and dependencies as per the `README.md`.
Assume the bot is located at `/home/user/lighter-trading-bot`.

Verify it runs manually first:
```bash
cd /home/user/lighter-trading-bot
./venv/bin/python main.py
```
*(Press Ctrl+C to stop)*

### 2. Create Service File

Create a file named `lighter-bot.service` in `/etc/systemd/system/`. You will need `sudo` privileges.

```bash
sudo nano /etc/systemd/system/lighter-bot.service
```

Paste the following content (adjust paths and user as necessary):

```ini
[Unit]
Description=Lighter Trading Bot
After=network.target

[Service]
# User running the bot (e.g., 'ubuntu' or 'root')
User=ubuntu
Group=ubuntu

# Working Directory is essential for loading .env, config.yaml, and wallet_config.json
WorkingDirectory=/home/ubuntu/lighter-trading-bot

# Command to execute
# Use the python binary inside the venv
ExecStart=/home/ubuntu/lighter-trading-bot/venv/bin/python main.py

# Restart policy: Always restart if it crashes
Restart=always
RestartSec=10

# Environment variables (Optional: can also rely on .env file)
# Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### 3. Enable and Start

Reload systemd to recognize the new service:
```bash
sudo systemctl daemon-reload
```

Enable the service to start on boot:
```bash
sudo systemctl enable lighter-bot
```

Start the bot immediately:
```bash
sudo systemctl start lighter-bot
```

### 4. Monitoring and Logs

View the status of the bot:
```bash
sudo systemctl status lighter-bot
```

Stream the logs in real-time:
```bash
journalctl -u lighter-bot -f
```

### 5. Updates and Maintenance

To update the bot code or configuration:
1.  Pull changes via `git pull`.
2.  Edit `config.yaml` if needed.
3.  Restart the service:
    ```bash
    sudo systemctl restart lighter-bot
    ```

## Alternative: Docker (Optional)

If you prefer Docker, you can use the provided `Dockerfile` (if available) or create a simple one:

```dockerfile
FROM python:3.10-slim
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
CMD ["python", "main.py"]
```
