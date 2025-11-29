#!/usr/bin/env python3
"""
Path utilities for the application.

Ensures that legacy rdp-gen directory is added to sys.path for imports.
This module is imported by various parts of the application to ensure
the rdp-gen services are available.
"""

import os
import sys


def setup_rdp_gen_path():
    """
    Add rdp-gen directory to sys.path if it exists.
    
    This allows importing modules from the legacy rdp-gen directory
    while we transition to the new app structure.
    """
    # Get the project root (parent of app/)
    current_dir = os.path.dirname(os.path.abspath(__file__))  # app/utils/
    app_dir = os.path.dirname(current_dir)  # app/
    project_root = os.path.dirname(app_dir)  # project root
    
    # Add rdp-gen directory to path
    rdp_gen_dir = os.path.join(project_root, "rdp-gen")
    if os.path.exists(rdp_gen_dir) and rdp_gen_dir not in sys.path:
        sys.path.insert(0, rdp_gen_dir)


# Execute path setup on import
setup_rdp_gen_path()
