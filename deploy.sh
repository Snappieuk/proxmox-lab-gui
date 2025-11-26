#!/bin/bash
set -e

echo "=== Deploying Proxmox Lab GUI ==="

# Check if running as systemd service and restart it
if systemctl is-active --quiet proxmox-gui 2>/dev/null; then
    echo "Restarting systemd service..."
    sudo systemctl restart proxmox-gui
    echo "✓ Service restarted successfully"
    
elif pgrep -f "gunicorn.*app:app" > /dev/null; then
    # Gunicorn is running but not as a service - send graceful reload
    echo "Sending HUP signal to Gunicorn (graceful reload)..."
    pkill -HUP -f "gunicorn.*app:app"
    echo "✓ Gunicorn reloaded successfully"
    
else
    echo "ERROR: No running instance found!"
    echo "Start the service with: sudo systemctl start proxmox-gui"
    exit 1
fi

echo "=== Deployment complete ==="
