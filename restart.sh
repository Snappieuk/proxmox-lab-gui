#!/bin/bash
# Full restart script - clears cache and restarts app

echo "=== Proxmox Lab GUI Restart ==="
echo ""

# Clear Python cache
echo "1. Clearing Python cache..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true

# Kill existing processes
echo "2. Stopping existing processes..."
pkill -f "python.*run.py" || true
sleep 2

# Start app
echo "3. Starting Flask app..."
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# Load environment
if [ -f ".env" ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

# Run in background
nohup python3 run.py > app.log 2>&1 &
PID=$!

sleep 3

# Check if running
if ps -p $PID > /dev/null; then
    echo ""
    echo "✓ App started successfully (PID: $PID)"
    echo "  View logs: tail -f app.log"
    echo "  Stop app: kill $PID"
else
    echo ""
    echo "✗ App failed to start. Check app.log for errors:"
    tail -20 app.log
fi
