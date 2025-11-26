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

# Ensure virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Install/update dependencies in venv
echo "Installing dependencies..."
pip install -q --upgrade pip
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
