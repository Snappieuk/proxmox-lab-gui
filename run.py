#!/usr/bin/env python3
"""
Application entry point.

Run the Flask application with: python run.py
"""

import logging
import os
import sys

# Disable SSL warnings before any imports that might trigger them
if os.getenv("PVE_VERIFY", "False").lower() not in ("true", "1", "yes"):
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Ensure the app directory is in the Python path
# Add current directory to path first for 'app' package
_current_dir = os.path.dirname(os.path.abspath(__file__))
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)

# Import the create_app factory from our app package
from app import create_app

# Create the Flask application
app = create_app()


if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(level=logging.INFO)
    
    # Get port from environment or default to 8080
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")
    debug = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
    
    # Run the application
    app.run(host=host, port=port, threaded=True, debug=debug)
