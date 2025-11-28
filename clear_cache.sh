#!/bin/bash
# Clear Python cache and restart Flask app
# Run this after git pull to ensure new code is loaded

echo "Clearing Python cache..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true

echo "Killing existing Flask processes..."
pkill -f "python.*run.py" || true
sleep 2

echo "Cache cleared! Now restart your app with: python3 run.py"
