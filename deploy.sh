#!/bin/bash
set -e

echo "=== Deploying Proxmox Lab GUI ==="

# Navigate to repository directory
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"
echo "Repository directory: $REPO_DIR"

# Pull latest changes
echo "Pulling latest changes from git..."
git pull

# Install/update dependencies
if [ -d "venv" ]; then
    echo "Installing/updating dependencies..."
    source venv/bin/activate
    pip install -q -r requirements.txt
fi

# Check if systemd service exists
if systemctl list-unit-files | grep -q "proxmox-gui.service"; then
    echo "Restarting systemd service..."
    sudo systemctl restart proxmox-gui
    sudo systemctl status proxmox-gui --no-pager
else
    # Kill any existing Flask processes and anything on port 8080
    echo "Stopping any running Flask processes..."
    pkill -9 -f "run.py" 2>/dev/null || true
    pkill -9 -f "python.*run" 2>/dev/null || true
    lsof -ti:8080 | xargs kill -9 2>/dev/null || true
    sleep 2

    echo "Starting Flask app..."
    cd "$REPO_DIR"
    nohup python3 run.py > /tmp/flask.log 2>&1 &
    FLASK_PID=$!
    echo "Started Flask with PID: $FLASK_PID"
    sleep 3

    # Verify it's running
    if pgrep -f "run.py" > /dev/null; then
        echo "✓ Flask app is running"
        echo "Logs: tail -f /tmp/flask.log"
    else
        echo "ERROR: Flask app failed to start!"
        echo "Last 30 lines of logs:"
        tail -30 /tmp/flask.log
        exit 1
    fi
fi

echo "=== Deployment complete ==="
