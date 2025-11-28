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
    
    echo "Starting Flask app..."
    cd "$REPO_DIR/rdp-gen"
    nohup python3 app.py > /tmp/flask.log 2>&1 &
    sleep 2
    
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
    echo "No running instance found, starting with start.sh..."
    cd "$REPO_DIR"
    bash start.sh
fi

echo "=== Deployment complete ==="
