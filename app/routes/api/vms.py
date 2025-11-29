#!/usr/bin/env python3
"""
VM API routes blueprint.

Handles VM list, IP lookup, and power control operations.
Database-first architecture: All queries read from VMInventory table.
Background sync keeps database current - NEVER makes direct Proxmox API calls for reads.
"""

import logging

from flask import Blueprint, jsonify, request, current_app, session

from app.utils.decorators import login_required
from app.models import User, VMAssignment
from app.config import MAPPINGS_FILE, CLUSTERS

logger = logging.getLogger(__name__)

api_vms_bp = Blueprint('api_vms', __name__, url_prefix='/api')


@api_vms_bp.route("/vms")
@login_required
def api_vms():
    """Get VMs for current user from VMInventory database.

    Query parameters:
    - search: Name/IP/VMID substring filter.
    - cluster: Limit to a specific cluster_id.
    - force_refresh=true: Trigger background sync (doesn't block).
    
    This endpoint ONLY queries the database - never makes direct Proxmox API calls.
    Background sync keeps VMInventory current.
    """
    from app.services.user_manager import require_user, is_admin_user
    from app.services.inventory_service import fetch_vm_inventory

    username = require_user()
    force_refresh = request.args.get('force_refresh', 'false').lower() == 'true'
    search = request.args.get('search', '').strip() or None
    cluster_filter = request.args.get('cluster', '').strip() or None

    try:
        # Determine owned VMIDs for non-admin users
        if is_admin_user(username):
            owned_vmids = None  # Admins see everything
        else:
            owned_vmids = _resolve_user_owned_vmids(username)
        
        # Query database (fast, <100ms)
        vms = fetch_vm_inventory(
            cluster_id=cluster_filter,
            search=search,
            vmids=owned_vmids
        )
        
        # Augment with RDP availability
        _augment_rdp_availability(vms)
        
        # Trigger background refresh if requested
        if force_refresh:
            from app.services.background_sync import trigger_immediate_sync
            trigger_immediate_sync()
        
        return jsonify(vms)
        
    except Exception as e:
        logger.exception("Failed to fetch VMs for user %s", username)
        return jsonify({"error": True, "message": f"Failed to fetch VMs: {e}"}), 500


@api_vms_bp.route("/vm/<int:vmid>/ip")
@login_required
def api_vm_ip(vmid: int):
    """Get IP address for a specific VM from database (lazy loading)."""
    from app.services.user_manager import require_user, is_admin_user
    from app.services.inventory_service import get_vm_from_inventory
    from flask import session
    from app.config import CLUSTERS
    
    user = require_user()
    cluster_id = request.args.get('cluster', session.get("cluster_id", CLUSTERS[0]["id"]))
    
    try:
        # Query from database
        vm = get_vm_from_inventory(cluster_id, vmid)
        
        if not vm:
            return jsonify({"error": "VM not found"}), 404
        
        # Check access permissions
        if not is_admin_user(user):
            owned_vmids = _resolve_user_owned_vmids(user)
            if vmid not in owned_vmids:
                return jsonify({"error": "VM not accessible"}), 403
        
        # Return IP from database
        ip = vm.get('ip')
        
        if ip and ip not in ('N/A', 'Fetching...', '', None):
            return jsonify({
                "vmid": vmid,
                "ip": ip,
                "status": "complete"
            })
        
        # No IP found
        return jsonify({
            "vmid": vmid,
            "ip": None,
            "status": "not_found"
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


def _augment_rdp_availability(vms: list):
    """Add rdp_available field to each VM dict in-place based on stored flags."""
    for vm in vms:
        # RDP availability is stored in database from background sync
        # Windows VMs typically have RDP available when running
        is_windows = vm.get('category') == 'windows' or 'win' in vm.get('name', '').lower()
        has_ip = vm.get('ip') and vm['ip'] not in ('N/A', 'Fetching...', '', None)
        is_running = vm.get('status') == 'running'
        
        # Use database flag if available, otherwise infer from metadata
        vm['rdp_available'] = vm.get('rdp_available', False) or (is_windows and has_ip and is_running)


@api_vms_bp.route("/vm/<int:vmid>/status")
@login_required
def api_vm_status(vmid: int):
    """
    Get status for a single VM from database (optimized for post-action refresh).
    
    Query parameters:
    - cluster: Cluster ID (optional, will try to find VM in any cluster if omitted)
    """
    from app.services.user_manager import require_user, is_admin_user
    from app.services.inventory_service import get_vm_from_inventory
    from flask import session
    from app.config import CLUSTERS
    
    user = require_user()
    cluster_id = request.args.get('cluster', session.get("cluster_id", CLUSTERS[0]["id"]))
    
    try:
        # Query from database
        vm = get_vm_from_inventory(cluster_id, vmid)
        
        if not vm:
            return jsonify({"error": "VM not found"}), 404
        
        # Check access permissions (admin sees all, users see only their VMs)
        if not is_admin_user(user):
            owned_vmids = _resolve_user_owned_vmids(user)
            if vmid not in owned_vmids:
                return jsonify({"error": "VM not accessible"}), 403
        
        # Augment with RDP availability
        _augment_rdp_availability([vm])
        
        return jsonify(vm)
        
    except Exception as e:
        logger.exception("Failed to get status for VM %d", vmid)
        return jsonify({"error": str(e)}), 500


@api_vms_bp.route("/vm/<int:vmid>/start", methods=["POST"])
@login_required
def api_vm_start(vmid: int):
    """Start a VM and immediately update database.
    
    Control operations (start/stop) MUST go through Proxmox API.
    Database is updated immediately after for fast UI refresh.
    """
    from app.services.user_manager import require_user, is_admin_user
    from app.services.inventory_service import get_vm_from_inventory, update_vm_status
    from app.services.proxmox_service import get_proxmox_admin
    
    user = require_user()
    cluster_id = request.json.get('cluster_id') if request.json else session.get("cluster_id", CLUSTERS[0]["id"])
    
    try:
        # Get VM from database for validation
        vm = get_vm_from_inventory(cluster_id, vmid)
        if not vm:
            return jsonify({"ok": False, "error": "VM not found"}), 404
        
        # Check access permissions
        if not is_admin_user(user):
            owned_vmids = _resolve_user_owned_vmids(user)
            if vmid not in owned_vmids:
                return jsonify({"ok": False, "error": "VM not accessible"}), 403
        
        # Execute start via Proxmox API
        proxmox = get_proxmox_admin()
        node = vm['node']
        vm_type = vm['type']
        
        if vm_type == 'qemu':
            proxmox.nodes(node).qemu(vmid).status.start.post()
        else:  # lxc
            proxmox.nodes(node).lxc(vmid).status.start.post()
        
        # Immediately update database
        update_vm_status(cluster_id, vmid, 'running')
        
        logger.info(f"Started VM {vmid} on node {node}")
        return jsonify({"ok": True})
        
    except Exception as e:
        logger.exception(f"Failed to start VM {vmid}")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_vms_bp.route("/vm/<int:vmid>/stop", methods=["POST"])
@login_required
def api_vm_stop(vmid: int):
    """Stop a VM and immediately update database.
    
    Control operations (start/stop) MUST go through Proxmox API.
    Database is updated immediately after for fast UI refresh.
    """
    from app.services.user_manager import require_user, is_admin_user
    from app.services.inventory_service import get_vm_from_inventory, update_vm_status
    from app.services.proxmox_service import get_proxmox_admin
    
    user = require_user()
    cluster_id = request.json.get('cluster_id') if request.json else session.get("cluster_id", CLUSTERS[0]["id"])
    
    try:
        # Get VM from database for validation
        vm = get_vm_from_inventory(cluster_id, vmid)
        if not vm:
            return jsonify({"ok": False, "error": "VM not found"}), 404
        
        # Check access permissions
        if not is_admin_user(user):
            owned_vmids = _resolve_user_owned_vmids(user)
            if vmid not in owned_vmids:
                return jsonify({"ok": False, "error": "VM not accessible"}), 403
        
        # Execute stop via Proxmox API
        proxmox = get_proxmox_admin()
        node = vm['node']
        vm_type = vm['type']
        
        if vm_type == 'qemu':
            proxmox.nodes(node).qemu(vmid).status.shutdown.post()
        else:  # lxc
            proxmox.nodes(node).lxc(vmid).status.shutdown.post()
        
        # Immediately update database
        update_vm_status(cluster_id, vmid, 'stopped')
        
        logger.info(f"Stopped VM {vmid} on node {node}")
        return jsonify({"ok": True})
        
    except Exception as e:
        logger.exception(f"Failed to stop VM {vmid}")
        return jsonify({"ok": False, "error": str(e)}), 500
