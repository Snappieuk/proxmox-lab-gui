#!/bin/bash
set -e

echo "=== Deploying Proxmox Lab GUI ==="

# Navigate to repository directory
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

# Pull latest changes
echo "Pulling latest changes from git..."
git pull

# Check if running as systemd service and restart it
if systemctl is-active --quiet proxmox-gui 2>/dev/null; then
    echo "Restarting systemd service..."
    sudo systemctl restart proxmox-gui
    sleep 2
    sudo systemctl status proxmox-gui --no-pager -l
    echo "✓ Service restarted successfully"
    
elif pgrep -f "rdp-gen/app.py" > /dev/null; then
    # Flask is running standalone - restart it
    echo "Found running Flask app, stopping it..."
    pkill -f "rdp-gen/app.py"
    sleep 3
    
    # Verify it stopped
    if pgrep -f "rdp-gen/app.py" > /dev/null; then
        echo "Process still running, force killing..."
        pkill -9 -f "rdp-gen/app.py"
        sleep 2
    fi
    
    echo "Starting Flask app..."
    cd "$REPO_DIR/rdp-gen"
    nohup python3 app.py > /tmp/flask.log 2>&1 &
    sleep 2
    
    # Verify it started
    if pgrep -f "rdp-gen/app.py" > /dev/null; then
        echo "✓ Flask app restarted successfully"
        echo "Logs: tail -f /tmp/flask.log"
    else
        echo "ERROR: Failed to start Flask app!"
        echo "Check logs: cat /tmp/flask.log"
        exit 1
    fi
    
else
    echo "ERROR: No running instance found!"
    echo ""
    echo "Checking for Flask processes:"
    ps aux | grep -i "[p]ython.*app.py" || echo "  (none found)"
    echo ""
    echo "Checking systemd service:"
    systemctl status proxmox-gui 2>/dev/null || echo "  (service not found or not running)"
    echo ""
    echo "Start the app with: ./start.sh"
    echo "Or enable systemd service: sudo systemctl enable --now proxmox-gui"
    exit 1
fi

echo "=== Deployment complete ==="
