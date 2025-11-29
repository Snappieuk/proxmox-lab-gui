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
from app.services.inventory_service import fetch_vm_inventory, persist_vm_inventory
from app.models import User, VMAssignment
from app.config import MAPPINGS_FILE
from app.config import VM_CACHE_TTL
import threading, time
from app.services.arp_scanner import get_scan_status, has_rdp_port_open, get_rdp_cache_time

logger = logging.getLogger(__name__)

# Track last background refresh to prevent spam
_last_bg_refresh = 0
_bg_refresh_cooldown = 60  # seconds

api_vms_bp = Blueprint('api_vms', __name__, url_prefix='/api')


@api_vms_bp.route("/vms")
@login_required
def api_vms():
    """DB-first VM listing endpoint.

    Always returns persisted inventory immediately (fast path) filtered by user ownership, then
    optionally triggers a background live refresh to update the DB for subsequent requests.

    Query parameters:
    - search: Name/IP/VMID substring filter applied to inventory rows.
    - cluster: Limit to a specific cluster_id.
    - force_refresh=true: Spawn background refresh thread regardless of staleness.
    - live=true: (Optional) Return live Proxmox data directly (bypasses DB-first logic).
    """
    from app.services.user_manager import require_user
    from datetime import datetime

    username = require_user()  # Session username (may include realm suffix)
    force_refresh = request.args.get('force_refresh', 'false').lower() == 'true'
    live_direct = request.args.get('live', 'false').lower() == 'true'
    search = request.args.get('search', '') or None
    cluster_filter = request.args.get('cluster') or None

    # Direct live mode OR force refresh returns raw list (legacy frontend expectation)
    if live_direct or force_refresh:
        try:
            vms_live = get_vms_for_user(username, search=search, skip_ips=False, force_refresh=True)
            _augment_rdp_availability(vms_live)
            # Persist latest snapshot (best-effort)
            try:
                persist_vm_inventory(vms_live)
            except Exception:
                logger.exception("Failed to persist inventory after live/force fetch")
            return jsonify(vms_live)
        except Exception as e:
            logger.exception("live/force fetch failed for user %s", username)
            return jsonify({"error": True, "message": f"Live/force fetch failed: {e}"}), 500

    try:
        # Fetch persisted rows (fast path)
        inventory_rows = fetch_vm_inventory(cluster_id=cluster_filter, search=search)

        # If inventory is completely empty, do initial live fetch to populate it
        if len(inventory_rows) == 0:
            logger.info(f"Inventory empty for user {username}, performing initial live fetch")
            try:
                vms_live = get_vms_for_user(username, search=search, skip_ips=False, force_refresh=True)
                _augment_rdp_availability(vms_live)
                # Persist for future requests
                try:
                    persist_vm_inventory(vms_live)
                    logger.info(f"Populated inventory with {len(vms_live)} VMs")
                except Exception:
                    logger.exception("Failed to persist inventory during initial fetch")
                return jsonify({"vms": vms_live, "stale": False, "mode": "initial"})
            except Exception as e:
                logger.exception(f"Initial live fetch failed for user {username}")
                return jsonify({"error": True, "message": f"Initial fetch failed: {e}"}), 500

        # Filter to user-owned VMIDs using local DB assignments + mappings.json without Proxmox calls
        # UNLESS user is admin, then show all VMs
        from app.services.user_manager import is_admin_user
        if is_admin_user(username):
            filtered = inventory_rows  # Admins see everything
            logger.debug(f"User {username} is admin, showing all {len(filtered)} inventory VMs")
        else:
            owned_vmids = _resolve_user_owned_vmids(username)
            filtered = [r for r in inventory_rows if r.get('vmid') in owned_vmids]
            logger.debug(f"User {username} filtered to {len(filtered)} VMs from owned set {owned_vmids}")

        # Determine staleness based on newest row age
        latest_dt = None
        for r in inventory_rows:
            lu = r.get('last_updated')
            if not lu:
                continue
            try:
                dt = datetime.fromisoformat(lu.replace('Z', ''))
                if latest_dt is None or dt > latest_dt:
                    latest_dt = dt
            except Exception:
                continue
        if latest_dt is None:
            stale = True  # Empty inventory or unparsable timestamps -> treat as stale
        else:
            age = (datetime.utcnow() - latest_dt).total_seconds()
            stale = age > VM_CACHE_TTL

        # Trigger background refresh if stale or forced
        # Background refresh only if stale (force_refresh handled in live path above)
        if stale:
            _trigger_background_refresh(username, reason="stale")

        # Augment with RDP availability (uses cached port scan data)
        _augment_rdp_availability(filtered)

        return jsonify({"vms": filtered, "stale": stale, "mode": "inventory"})
    except Exception as e:
        logger.exception("inventory fast path failed for user %s", username)
        return jsonify({"error": True, "message": f"Inventory path failed: {e}"}), 500


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


def _resolve_user_owned_vmids(username: str) -> set:
    """Resolve VMIDs the user should see without performing a live Proxmox fetch.

    Combines class-based assignments (VMAssignment) and legacy mappings.json.
    Accepts realm-suffixed usernames (e.g., student1@pve) and normalizes variants.
    """
    variants = {username}
    if '@' in username:
        base = username.split('@', 1)[0]
        variants.add(base)
    # Proxmox common realms
    variants.update({v + '@pve' for v in list(variants)})
    variants.update({v + '@pam' for v in list(variants)})

    owned = set()
    try:
        # Class-based assignments
        user_row = User.query.filter(User.username.in_(variants)).first()
        if user_row:
            for assign in user_row.vm_assignments:
                if assign.proxmox_vmid:
                    owned.add(assign.proxmox_vmid)
    except Exception:
        logger.exception("Failed to gather class assignments for %s", username)

    # Legacy mappings.json
    try:
        import os, json
        if os.path.exists(MAPPINGS_FILE):
            with open(MAPPINGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # Data may be {"user@pve": [100,101]} or similar
            for name_variant in variants:
                vm_list = data.get(name_variant)
                if isinstance(vm_list, list):
                    for vmid in vm_list:
                        try:
                            owned.add(int(vmid))
                        except Exception:
                            continue
    except Exception:
        logger.exception("Failed to parse mappings.json for %s", username)

    return owned


def _trigger_background_refresh(username: str, reason: str):
    """Spawn a background thread to perform a live Proxmox fetch and persist inventory.

    Applies cooldown to avoid repeated expensive refreshes. Uses app context safely.
    """
    global _last_bg_refresh
    now = time.time()
    if now - _last_bg_refresh < _bg_refresh_cooldown and reason != 'force':
        return  # Respect cooldown unless forced
    _last_bg_refresh = now

    app = current_app._get_current_object()

    def _worker():
        with app.app_context():
            try:
                live_vms = get_vms_for_user(username, skip_ips=False, force_refresh=True)
                persist_vm_inventory(live_vms)
                logger.info("Background refresh complete (%s) for %s: %d VMs", reason, username, len(live_vms))
            except Exception as e:
                logger.exception("Background refresh failed (%s) for %s: %s", reason, username, e)

    threading.Thread(target=_worker, daemon=True).start()


def _augment_rdp_availability(vms: list):
    """Add rdp_available field to each VM dict in-place based on IP and port scan cache."""
    rdp_cache_valid = get_rdp_cache_time() > 0
    for vm in vms:
        rdp_can_build = False
        rdp_port_available = False
        try:
            build_rdp(vm)
            rdp_can_build = True
        except Exception:
            rdp_can_build = False
        if vm.get('category') == 'windows':
            rdp_port_available = True
        elif rdp_cache_valid and vm.get('ip') and vm['ip'] not in ('N/A', 'Fetching...', ''):
            rdp_port_available = has_rdp_port_open(vm['ip'])
        vm['rdp_available'] = rdp_can_build and rdp_port_available


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
    """Start a VM and immediately update database."""
    from app.services.user_manager import require_user
    
    user = require_user()
    vm = find_vm_for_user(user, vmid)
    if not vm:
        return jsonify({"ok": False, "error": "VM not found or not allowed"}), 404

    try:
        start_vm(vm)
        invalidate_cluster_cache()
        
        # Immediately update database with new status
        _update_vm_status_in_db(vmid, vm.get("cluster_id"), "running")
        
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True})


@api_vms_bp.route("/vm/<int:vmid>/stop", methods=["POST"])
@login_required
def api_vm_stop(vmid: int):
    """Stop a VM and immediately update database."""
    from app.services.user_manager import require_user
    
    user = require_user()
    vm = find_vm_for_user(user, vmid)
    if not vm:
        return jsonify({"ok": False, "error": "VM not found or not allowed"}), 404

    try:
        shutdown_vm(vm)
        invalidate_cluster_cache()
        
        # Immediately update database with new status
        _update_vm_status_in_db(vmid, vm.get("cluster_id"), "stopped")
        
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True})


def _update_vm_status_in_db(vmid: int, cluster_id: str, new_status: str):
    """Immediately update VM status in database after state change."""
    try:
        from app.models import VMInventory, db
        from datetime import datetime
        
        vm = VMInventory.query.filter_by(
            cluster_id=cluster_id,
            vmid=vmid
        ).first()
        
        if vm:
            vm.status = new_status
            vm.last_status_check = datetime.utcnow()
            vm.last_updated = datetime.utcnow()
            
            # Clear uptime when stopped
            if new_status == 'stopped':
                vm.uptime = None
                vm.cpu_usage = None
                vm.memory_usage = None
            
            db.session.commit()
            logger.debug(f"Updated VM {vmid} status to {new_status} in database")
    except Exception as e:
        logger.warning(f"Failed to update VM {vmid} status in database: {e}")
