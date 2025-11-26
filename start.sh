#!/bin/bash
# Start script for Proxmox Lab GUI with Gunicorn

cd "$(dirname "$0")"

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Load environment variables from .env if it exists
if [ -f ".env" ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

# Start Gunicorn
exec gunicorn --config gunicorn.conf.py --chdir rdp-gen app:app
