#!/bin/bash
set -e

echo "=== Deploying Proxmox Lab GUI ==="

# Navigate to repository directory
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

# Pull latest changes
echo "Pulling latest changes from git..."
git pull

# Check if Flask app is running and restart it
if pgrep -f "python.*app.py" > /dev/null; then
    echo "Found running Flask app, stopping it..."
    pkill -f "python.*app.py"
    sleep 3
    
    # Verify it stopped
    if pgrep -f "python.*app.py" > /dev/null; then
        echo "Process still running, force killing..."
        pkill -9 -f "python.*app.py"
        sleep 2
    fi
    
    # Also kill anything on port 8080
    echo "Ensuring port 8080 is free..."
    lsof -ti:8080 | xargs kill -9 2>/dev/null || true
    sleep 2
    
    echo "Starting Flask app..."
    cd "$REPO_DIR/rdp-gen"
    nohup python3 app.py > /tmp/flask.log 2>&1 &
    sleep 3
    
    # Verify it started
    if pgrep -f "python.*app.py" > /dev/null; then
        echo "✓ Flask app restarted successfully"
        echo "Logs: tail -f /tmp/flask.log"
    else
        echo "ERROR: Failed to start Flask app!"
        echo "Check logs: cat /tmp/flask.log"
        exit 1
    fi
    
else
    echo "No running instance found, starting fresh..."
    # Kill anything on port 8080 just in case
    lsof -ti:8080 | xargs kill -9 2>/dev/null || true
    sleep 2
    
    cd "$REPO_DIR/rdp-gen"
    nohup python3 app.py > /tmp/flask.log 2>&1 &
    sleep 3
fi

# Verify it's running
if pgrep -f "python.*app.py" > /dev/null; then
    echo "✓ Flask app is running"
    echo "Logs: tail -f /tmp/flask.log"
else
    echo "ERROR: Flask app failed to start!"
    echo "Check logs: cat /tmp/flask.log"
    exit 1
fi

echo "=== Deployment complete ==="
