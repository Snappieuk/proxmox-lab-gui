#!/bin/bash
set -e

# Configuration - adjust these paths for your server
APP_DIR="$HOME/rdp-gen"
PROJECT_ROOT="$(dirname "$APP_DIR")"  # Parent of rdp-gen

echo "=== Deploying Proxmox Lab GUI ==="

# Navigate to project root
cd "$PROJECT_ROOT"

# Pull latest changes
echo "Pulling latest code..."
git pull

# Activate virtual environment
if [ -d "venv" ]; then
    echo "Activating virtual environment..."
    source venv/bin/activate
else
    echo "WARNING: Virtual environment not found"
fi

# Install/update dependencies
echo "Installing dependencies..."
pip install -q -r requirements.txt

# Check if running as systemd service
if systemctl is-active --quiet proxmox-gui 2>/dev/null; then
    echo "Restarting systemd service..."
    sudo systemctl restart proxmox-gui
    echo "✓ Service restarted successfully"
    
elif pgrep -f "gunicorn.*app:app" > /dev/null; then
    # Gunicorn is running but not as a service
    echo "Sending HUP signal to Gunicorn (graceful reload)..."
    pkill -HUP -f "gunicorn.*app:app"
    echo "✓ Gunicorn reloaded successfully"
    
else
    echo "WARNING: No running instance found!"
    echo "Start the service with: sudo systemctl start proxmox-gui"
    echo "Or manually with: ./start.sh"
    exit 1
fi

echo "=== Deployment complete ==="
