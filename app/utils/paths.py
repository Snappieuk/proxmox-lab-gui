#!/usr/bin/env python3
"""
Path configuration for legacy module imports.

This module sets up the Python path to allow importing from the rdp-gen directory
which contains the legacy Proxmox client and related modules.
"""

import os
import sys

# Add rdp-gen to path for legacy imports (proxmox_client, config, etc.)
_rdp_gen_path = os.path.join(os.path.dirname(__file__), '..', '..', 'rdp-gen')
if _rdp_gen_path not in sys.path:
    sys.path.insert(0, _rdp_gen_path)
