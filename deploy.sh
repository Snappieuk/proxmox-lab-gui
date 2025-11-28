#!/bin/bash
set -e

echo "=== Deploying Proxmox Lab GUI ==="

# Navigate to repository directory
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

# Pull latest changes
echo "Pulling latest changes from git..."
git pull

# Kill any existing Flask processes and anything on port 8080
echo "Stopping any running Flask processes..."
pkill -9 -f "app.py" 2>/dev/null || true
pkill -9 -f "python.*app" 2>/dev/null || true
lsof -ti:8080 | xargs kill -9 2>/dev/null || true
sleep 3

echo "Starting Flask app..."
cd "$REPO_DIR/rdp-gen"
nohup python3 app.py > /tmp/flask.log 2>&1 &
FLASK_PID=$!
echo "Started Flask with PID: $FLASK_PID"
sleep 3

# Verify it's running
if pgrep -f "app.py" > /dev/null; then
    echo "✓ Flask app is running"
    echo "Logs: tail -f /tmp/flask.log"
    exit 0
else
    echo "ERROR: Flask app failed to start!"
    echo "Last 30 lines of logs:"
    tail -30 /tmp/flask.log
    exit 1
fi

echo "=== Deployment complete ==="
