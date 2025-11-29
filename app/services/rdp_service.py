#!/usr/bin/env python3
"""
RDP Service - RDP file generation.

This module handles generating .rdp files for Windows VMs.
"""

import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


def build_rdp(vm: Dict[str, Any]) -> str:
    """
    Build a minimal .rdp file content for a Windows VM.
    Always returns an RDP file, even if IP is unknown.
    If IP is missing, falls back to VM name/hostname as the address.
    """
    if not vm:
        raise ValueError("VM dict is None or empty")

    vmid = vm.get("vmid")
    if not vmid:
        raise ValueError("VM missing vmid field")

    ip = vm.get("ip")
    name = vm.get("hostname") or vm.get("name") or f"vm-{vmid}"

    # Prefer IP if available; otherwise use name/hostname so download always works
    address = ip if (ip and ip != "<ip>") else name

    # Minimal valid RDP file format that Windows Remote Desktop will accept
    return (
        f"full address:s:{address}:3389\r\n"
        f"prompt for credentials:i:1\r\n"
        f"administrative session:i:0\r\n"
        f"authentication level:i:2\r\n"
        f"screen mode id:i:2\r\n"
        f"desktopwidth:i:1920\r\n"
        f"desktopheight:i:1080\r\n"
        f"session bpp:i:32\r\n"
        f"compression:i:1\r\n"
        f"keyboardhook:i:2\r\n"
        f"audiocapturemode:i:0\r\n"
        f"videoplaybackmode:i:1\r\n"
        f"connection type:i:7\r\n"
        f"networkautodetect:i:1\r\n"
        f"bandwidthautodetect:i:1\r\n"
        f"displayconnectionbar:i:1\r\n"
        f"enableworkspacereconnect:i:0\r\n"
        f"disable wallpaper:i:0\r\n"
        f"allow font smoothing:i:0\r\n"
        f"allow desktop composition:i:0\r\n"
        f"disable full window drag:i:1\r\n"
        f"disable menu anims:i:1\r\n"
        f"disable themes:i:0\r\n"
        f"disable cursor setting:i:0\r\n"
        f"bitmapcachepersistenable:i:1\r\n"
        f"audiomode:i:0\r\n"
        f"redirectprinters:i:1\r\n"
        f"redirectcomports:i:0\r\n"
        f"redirectsmartcards:i:1\r\n"
        f"redirectclipboard:i:1\r\n"
        f"redirectposdevices:i:0\r\n"
        f"autoreconnection enabled:i:1\r\n"
        f"negotiate security layer:i:1\r\n"
        f"remoteapplicationmode:i:0\r\n"
        f"alternate shell:s:\r\n"
        f"shell working directory:s:\r\n"
        f"gatewayhostname:s:\r\n"
        f"gatewayusagemethod:i:4\r\n"
        f"gatewaycredentialssource:i:4\r\n"
        f"gatewayprofileusagemethod:i:0\r\n"
        f"promptcredentialonce:i:0\r\n"
        f"gatewaybrokeringtype:i:0\r\n"
        f"use redirection server name:i:0\r\n"
        f"rdgiskdcproxy:i:0\r\n"
        f"kdcproxyname:s:\r\n"
    )
