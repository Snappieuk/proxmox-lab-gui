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
    echo "✓ Service restarted successfully"
    
elif pgrep -f "python.*rdp-gen/app.py" > /dev/null; then
    # Flask is running standalone - restart it
    echo "Stopping Flask app..."
    pkill -f "python.*rdp-gen/app.py"
    sleep 2
    echo "Starting Flask app..."
    cd "$REPO_DIR/rdp-gen"
    nohup python3 app.py > /tmp/flask.log 2>&1 &
    echo "✓ Flask app restarted successfully"
    
else
    echo "ERROR: No running instance found!"
    echo "Start the app with: ./start.sh"
    echo "Or enable systemd service: sudo systemctl start proxmox-gui"
    exit 1
fi

echo "=== Deployment complete ==="
