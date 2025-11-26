#!/bin/bash
# Start script for Proxmox Lab GUI with Flask built-in server

cd "$(dirname "$0")"

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Load environment variables from .env if it exists
if [ -f ".env" ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

# Start Flask directly (multithreaded, production-ready enough for lab use)
cd rdp-gen
exec python3 -c "
from app import app
app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)
"
