#!/usr/bin/env python3
"""
Admin Diagnostics routes blueprint.

Handles admin view and diagnostics/probe endpoints.
"""

import logging

from flask import Blueprint, jsonify, render_template

from app.services.arp_scanner import get_arp_table
from app.services.proxmox_service import (
    _get_vm_mac,
    get_all_vms,
)
from app.services.user_manager import probe_proxmox
from app.utils.decorators import admin_required

logger = logging.getLogger(__name__)

admin_diagnostics_bp = Blueprint('admin_diagnostics', __name__, url_prefix='/admin')


@admin_diagnostics_bp.route("")
@admin_required
def admin_view():
    """Admin view of all VMs."""
    from app.services.user_manager import require_user
    
    user = require_user()
    
    # Fast path: render empty page immediately for progressive loading
    # VMs will be loaded via /api/vms after page renders
    probe = probe_proxmox()
    return render_template(
        "index_improved.html",
        progressive_load=True,
        windows_vms=[],
        linux_vms=[],
        lxc_containers=[],
        other_vms=[],
        user=user,
        message=None,
        probe=probe,
    )


@admin_diagnostics_bp.route("/probe")
@admin_required
def admin_probe():
    """Return Proxmox diagnostics as JSON (admin-only)."""
    info = probe_proxmox()
    return jsonify(info)


@admin_diagnostics_bp.route("/debug/arp")
@admin_required
def api_debug_arp():
    """Debug endpoint to show ARP scan details for troubleshooting."""
    
    try:
        # Get all VMs
        vms = get_all_vms(skip_ips=True)
        
        # Get ARP table
        arp_table = get_arp_table()
        
        # Build debug info
        debug_info = {
            "arp_table_size": len(arp_table),
            "arp_table_sample": list(arp_table.items())[:20],
            "vms": []
        }
        
        # For each running VM, show MAC extraction and ARP lookup
        for vm in vms:
            if vm.get("status") != "running":
                continue
                
            vmid = vm["vmid"]
            node = vm["node"]
            vmtype = vm.get("type", "qemu")
            
            # Try to get MAC
            mac = _get_vm_mac(node, vmid, vmtype)
            
            # Check if MAC is in ARP table
            ip_from_arp = arp_table.get(mac) if mac else None
            
            vm_debug = {
                "vmid": vmid,
                "name": vm.get("name"),
                "type": vmtype,
                "node": node,
                "status": vm.get("status"),
                "extracted_mac": mac,
                "ip_from_arp": ip_from_arp,
                "cached_ip": vm.get("ip"),
                "in_arp_table": mac in arp_table if mac else False
            }
            
            # Check for partial MAC matches
            if mac and mac not in arp_table:
                partial = [k for k in arp_table.keys() if mac[:8] in k or k[:8] in mac]
                if partial:
                    vm_debug["partial_mac_matches"] = partial
            
            debug_info["vms"].append(vm_debug)
        
        return jsonify(debug_info)
        
    except Exception as e:
        logger.exception("Debug ARP endpoint failed")
        return jsonify({"error": str(e)}), 500


@admin_diagnostics_bp.route("/ssh-pool")
@admin_required
def ssh_pool_status():
    """Get SSH connection pool status."""
    try:
        from app.services.ssh_executor import _connection_pool
        import time
        
        pool_status = {
            "active_connections": len(_connection_pool._connections),
            "connections": []
        }
        
        current_time = time.time()
        with _connection_pool._lock:
            for key, executor in _connection_pool._connections.items():
                host, username = key
                last_used = _connection_pool._last_used.get(key, 0)
                idle_seconds = int(current_time - last_used)
                
                pool_status["connections"].append({
                    "host": host,
                    "username": username,
                    "connected": executor.is_connected(),
                    "idle_seconds": idle_seconds,
                    "idle_minutes": round(idle_seconds / 60, 1)
                })
        
        return jsonify(pool_status)
        
    except Exception as e:
        logger.exception("SSH pool status endpoint failed")
        return jsonify({"error": str(e)}), 500
