#!/usr/bin/env python3
"""Simple CLI script to run probe_proxmox() and print results as JSON.

Usage:
    python3 rdp-gen/proxmox_probe.py
"""
import json
import logging

from proxmox_client import probe_proxmox

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    info = probe_proxmox()
    print(json.dumps(info, indent=2))
