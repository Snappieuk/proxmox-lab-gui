#!/usr/bin/env python3
"""
VM API routes blueprint.

Handles VM list, IP lookup, and power control operations.
"""

import logging

from flask import Blueprint, jsonify, request, current_app

from app.utils.decorators import login_required

# Import path setup is no longer needed
import app.utils.paths

from app.services.proxmox_client import (
    get_vms_for_user,
    get_vm_ip,
    find_vm_for_user,
    start_vm,
    shutdown_vm,
    build_rdp,
    invalidate_cluster_cache,
    verify_vm_ip,
)
from app.services.inventory_service import fetch_vm_inventory
from app.config import VM_CACHE_TTL
import threading, time
from app.services.arp_scanner import get_scan_status, has_rdp_port_open, get_rdp_cache_time

logger = logging.getLogger(__name__)

api_vms_bp = Blueprint('api_vms', __name__, url_prefix='/api')


@api_vms_bp.route("/vms")
@login_required
def api_vms():
    """
    Get VMs for current user.
    
    Query parameters:
    - skip_ips: Skip ARP scan for fast initial load (IPs will be 'N/A')
    - force_refresh: Bypass cache and fetch fresh data from Proxmox
    - search: Filter VMs by name/VMID/IP
    """
    from app.services.user_manager import require_user
    
    user = require_user()
    skip_ips = request.args.get('skip_ips', 'false').lower() == 'true'
    force_refresh = request.args.get('force_refresh', 'false').lower() == 'true'
    search = request.args.get('search', '')
    
    # Optional DB-backed inventory path (?db=true). Allows cluster filtering without live Proxmox queries.
    use_db = request.args.get('db', 'false').lower() == 'true'
    cluster_filter = request.args.get('cluster')  # cluster_id string

    try:
        if use_db:
            # Fetch persisted inventory then filter to user's accessible VMs (if mapping applies)
            inventory = fetch_vm_inventory(cluster_id=cluster_filter, search=search or None)
            user_accessible = get_vms_for_user(user, skip_ips=True, force_refresh=False)
            accessible_vmids = {vm['vmid'] for vm in user_accessible}
            vms = [inv for inv in inventory if inv['vmid'] in accessible_vmids]

            # Determine staleness (max last_updated age for rows considered)
            now_ts = time.time()
            latest_dt = None
            for row in inventory:
                lu = row.get('last_updated')
                if lu:
                    try:
                        # Parse ISO timestamp
                        from datetime import datetime
                        dt = datetime.fromisoformat(lu.replace('Z',''))
                        if latest_dt is None or dt > latest_dt:
                            latest_dt = dt
                    except Exception:
                        continue
            stale = True
            if latest_dt is not None:
                age = (datetime.utcnow() - latest_dt).total_seconds()
                stale = age > VM_CACHE_TTL

            # If stale trigger background refresh (non-blocking)
            if stale:
                def _bg_refresh():
                    import logging
                    logger = logging.getLogger(__name__)
                    try:
                        # Force refresh to update inventory persistence
                        from app.services.proxmox_client import get_all_vms
                        get_all_vms(skip_ips=False, force_refresh=True)
                    except Exception as e:
                        logger.warning(f"Background inventory refresh failed: {e}")
                threading.Thread(target=_bg_refresh, daemon=True).start()

            return jsonify({"vms": vms, "stale": stale})
        else:
            vms = get_vms_for_user(user, search=search or None, skip_ips=skip_ips, force_refresh=force_refresh)
        
        # Check which VMs can actually generate RDP files
        rdp_cache_valid = get_rdp_cache_time() > 0  # Has the port scan completed?
        
        for vm in vms:
            rdp_can_build = False
            rdp_port_available = False
            
            # Check if build_rdp would succeed (has valid IP)
            try:
                build_rdp(vm)
                rdp_can_build = True
            except Exception:
                rdp_can_build = False
            
            # Check if this is Windows OR has port 3389 open
            if vm.get('category') == 'windows':
                rdp_port_available = True  # Windows always assumed to have RDP
            elif rdp_cache_valid and vm.get('ip') and vm['ip'] not in ('N/A', 'Fetching...', ''):
                rdp_port_available = has_rdp_port_open(vm['ip'])
            
            # Only show button if BOTH conditions met
            vm['rdp_available'] = rdp_can_build and rdp_port_available
        
        return jsonify(vms)
    except Exception as e:
        logger.exception("Failed to get VMs for user %s", user)
        return jsonify({
            "error": True,
            "message": f"Failed to load VMs: {str(e)}",
            "details": "Check if Proxmox server is reachable and credentials are correct"
        }), 500


@api_vms_bp.route("/vm/<int:vmid>/ip")
@login_required
def api_vm_ip(vmid: int):
    """Get IP address for a specific VM (lazy loading)."""
    from app.services.user_manager import require_user
    
    user = require_user()
    try:
        vm = find_vm_for_user(user, vmid)
        if not vm:
            return jsonify({"error": "VM not found or not accessible"}), 404
        
        # First check if we have a scan status (from background ARP scan)
        scan_status = get_scan_status(vmid)
        
        if scan_status:
            # If status is an IP address (contains dots), return it
            if '.' in scan_status and scan_status.count('.') == 3:
                return jsonify({
                    "vmid": vmid,
                    "ip": scan_status,
                    "status": "complete"
                })
        
        # Direct IP lookup (guest agent/interfaces)
        cluster_id = vm.get("cluster_id", "cluster1")
        ip = get_vm_ip(cluster_id, vmid, vm["node"], vm["type"])
        
        if ip:
            return jsonify({
                "vmid": vmid,
                "ip": ip,
                "status": "complete"
            })
        
        # No IP found
        return jsonify({
            "vmid": vmid,
            "ip": None,
            "status": scan_status if scan_status else "not_found"
        })
    except Exception as e:
        logger.exception("Failed to get IP for VM %d", vmid)
        return jsonify({"error": str(e), "status": "error"}), 500


@api_vms_bp.route("/vm/<int:vmid>/status")
@login_required
def api_vm_status(vmid: int):
    """
    Get status for a single VM (optimized for post-action refresh).
    
    Query parameters:
    - skip_ip: Skip IP lookup for faster response (defaults to False)
    """
    from app.services.user_manager import require_user
    
    user = require_user()
    skip_ip = request.args.get('skip_ip', 'false').lower() == 'true'
    
    try:
        # Pass skip_ip to find_vm_for_user for optimization
        vm = find_vm_for_user(user, vmid, skip_ip=skip_ip)
        if not vm:
            return jsonify({"error": "VM not found or not accessible"}), 404
        
        # Check RDP availability (only if we have an IP and didn't skip lookup)
        if not skip_ip and vm.get('ip') and vm['ip'] not in ('N/A', 'Fetching...', ''):
            rdp_cache_valid = get_rdp_cache_time() > 0
            rdp_can_build = False
            rdp_port_available = False
            
            try:
                build_rdp(vm)
                rdp_can_build = True
            except Exception:
                rdp_can_build = False
            
            if vm.get('category') == 'windows':
                rdp_port_available = True
            elif rdp_cache_valid:
                rdp_port_available = has_rdp_port_open(vm['ip'])
            
            vm['rdp_available'] = rdp_can_build and rdp_port_available
        else:
            vm['rdp_available'] = False
        
        return jsonify(vm)
    except Exception as e:
        logger.exception("Failed to get status for VM %d", vmid)
        return jsonify({"error": str(e)}), 500


@api_vms_bp.route("/vm/<int:vmid>/start", methods=["POST"])
@login_required
def api_vm_start(vmid: int):
    """Start a VM."""
    from app.services.user_manager import require_user
    
    user = require_user()
    vm = find_vm_for_user(user, vmid)
    if not vm:
        return jsonify({"ok": False, "error": "VM not found or not allowed"}), 404

    try:
        start_vm(vm)
        invalidate_cluster_cache()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True})


@api_vms_bp.route("/vm/<int:vmid>/stop", methods=["POST"])
@login_required
def api_vm_stop(vmid: int):
    """Stop a VM."""
    from app.services.user_manager import require_user
    
    user = require_user()
    vm = find_vm_for_user(user, vmid)
    if not vm:
        return jsonify({"ok": False, "error": "VM not found or not allowed"}), 404

    try:
        shutdown_vm(vm)
        invalidate_cluster_cache()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True})
