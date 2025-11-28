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

# Start Flask using the new modular entry point
exec python3 run.py
