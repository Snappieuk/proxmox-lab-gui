#!/usr/bin/env python3
"""
Unified Admin routes blueprint.

Consolidates all admin-related routes:
- Cluster management
- Diagnostics & debugging
- User management
- Legacy mappings UI
- VM-to-class mappings UI
"""

import logging
import time

from flask import Blueprint, abort, jsonify, render_template, request, session

from app.services.arp_scanner import get_arp_table
from app.services.class_service import (
    create_local_user,
    delete_user,
    get_user_by_username,
    list_all_users,
    update_user_role,
)
from app.services.proxmox_service import (
    _get_vm_mac,
    get_all_vms,
)
from app.services.user_manager import (
    add_user_to_admin_group,
    create_pve_user,
    get_pve_users,
    is_admin_user,
    probe_proxmox,
    remove_user_from_admin_group,
    require_user,
)
from app.utils.decorators import admin_required, login_required

logger = logging.getLogger(__name__)

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


# ==============================================================================
# DIAGNOSTICS & MAIN ADMIN VIEW
# ==============================================================================

@admin_bp.route("")
@admin_required
def admin_view():
    """Admin view of all VMs."""
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


@admin_bp.route("/probe")
@admin_required
def admin_probe():
    """Return Proxmox diagnostics as JSON (admin-only)."""
    info = probe_proxmox()
    return jsonify(info)


@admin_bp.route("/debug/arp")
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


@admin_bp.route("/ssh-pool")
@admin_required
def ssh_pool_status():
    """Get SSH connection pool status."""
    try:
        from app.services.ssh_executor import _connection_pool
        
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


# ==============================================================================
# CLUSTER SETTINGS
# ==============================================================================

@admin_bp.route("/settings")
@admin_required
def admin_settings():
    """Settings page for cluster configuration."""
    return render_template("settings.html")




# ==============================================================================
# LEGACY MAPPINGS UI
# ==============================================================================


@admin_bp.route("/mappings", endpoint="admin_mappings")
@admin_required
def mappings():
    """
    Render the mappings management page.
    
    This page allows admins to assign VMs to Proxmox users via mappings.json.
    The actual data is loaded via AJAX from /api/mappings endpoint.
    """
    return render_template("mappings.html")


# ==============================================================================
# VM-TO-CLASS MAPPINGS UI
# ==============================================================================

@admin_bp.route("/vm-class-mappings")
@login_required
def vm_class_mappings():
    """Admin page for managing VM to Class mappings."""
    user = session.get("user")
    if not is_admin_user(user):
        abort(403)
    
    return render_template("admin/vm_class_mappings.html")


# ==============================================================================
# USER MANAGEMENT
# ==============================================================================

@admin_bp.route("/users")
@admin_required
def users_management():
    """User management view."""
    user = require_user()
    
    return render_template("admin/users.html", current_user=user)


@admin_bp.route("/users/api/users")
@admin_required
def api_get_users():
    """Get all users (Proxmox + Local)."""
    try:
        # Get Proxmox users
        proxmox_users = get_pve_users()
        
        # Get local users
        local_users = list_all_users()
        
        # Combine and enrich
        users_data = []
        
        # Add Proxmox users
        for pve_user in proxmox_users:
            userid = pve_user['userid']
            username = userid.split('@')[0]
            is_admin = is_admin_user(userid)
            
            # Find matching local user
            local_user = next((u for u in local_users if u.username == username), None)
            
            users_data.append({
                'username': username,
                'userid': userid,
                'type': 'proxmox',
                'is_admin': is_admin,
                'local_role': local_user.role if local_user else None,
                'has_local_account': bool(local_user)
            })
        
        # Add local-only users (not in Proxmox)
        proxmox_usernames = {u['userid'].split('@')[0] for u in proxmox_users}
        for local_user in local_users:
            if local_user.username not in proxmox_usernames:
                users_data.append({
                    'username': local_user.username,
                    'userid': None,
                    'type': 'local',
                    'is_admin': False,
                    'local_role': local_user.role,
                    'has_local_account': True
                })
        
        return jsonify({
            'ok': True,
            'users': users_data
        })
        
    except Exception as e:
        logger.exception("Failed to get users")
        return jsonify({'ok': False, 'error': str(e)}), 500


@admin_bp.route("/users/api/users/create", methods=['POST'])
@admin_required
def api_create_user():
    """Create a new Proxmox user."""
    try:
        data = request.get_json()
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        user_type = data.get('type', 'proxmox')
        role = data.get('role', 'user')
        
        if not username or not password:
            return jsonify({'ok': False, 'error': 'Username and password required'}), 400
        
        if user_type == 'proxmox':
            # Create Proxmox user
            success, error = create_pve_user(username, password)
            if not success:
                return jsonify({'ok': False, 'error': error}), 400
            
            # Optionally create local account too
            if data.get('create_local'):
                create_local_user(username, password, role=role)
        else:
            # Create local-only user
            success, error = create_local_user(username, password, role=role)
            if not success:
                return jsonify({'ok': False, 'error': error}), 400
        
        return jsonify({'ok': True, 'message': f'User {username} created successfully'})
        
    except Exception as e:
        logger.exception("Failed to create user")
        return jsonify({'ok': False, 'error': str(e)}), 500


@admin_bp.route("/users/api/users/<username>/role", methods=['POST'])
@admin_required
def api_update_user_role(username):
    """Update local user role."""
    try:
        data = request.get_json()
        new_role = data.get('role')
        
        if new_role not in ('user', 'teacher', 'adminer'):
            return jsonify({'ok': False, 'error': 'Invalid role'}), 400
        
        user = get_user_by_username(username)
        if not user:
            return jsonify({'ok': False, 'error': 'User not found'}), 404
        
        update_user_role(user.id, new_role)
        
        return jsonify({'ok': True, 'message': f'Role updated to {new_role}'})
        
    except Exception as e:
        logger.exception("Failed to update user role")
        return jsonify({'ok': False, 'error': str(e)}), 500


@admin_bp.route("/users/api/users/<username>/admin", methods=['POST'])
@admin_required
def api_toggle_admin(username):
    """Add or remove user from admin group."""
    try:
        data = request.get_json()
        make_admin = data.get('is_admin', False)
        
        userid = f"{username}@pve"
        
        if make_admin:
            success = add_user_to_admin_group(userid)
        else:
            success = remove_user_from_admin_group(userid)
        
        if not success:
            return jsonify({'ok': False, 'error': 'Failed to update admin status'}), 500
        
        action = 'added to' if make_admin else 'removed from'
        return jsonify({'ok': True, 'message': f'User {action} admin group'})
        
    except Exception as e:
        logger.exception("Failed to toggle admin status")
        return jsonify({'ok': False, 'error': str(e)}), 500


@admin_bp.route("/users/api/users/<username>", methods=['DELETE'])
@admin_required
def api_delete_user(username):
    """Delete a local user account."""
    try:
        user = get_user_by_username(username)
        if not user:
            return jsonify({'ok': False, 'error': 'User not found'}), 404
        
        delete_user(user.id)
        
        return jsonify({'ok': True, 'message': f'User {username} deleted'})
        
    except Exception as e:
        logger.exception("Failed to delete user")
        return jsonify({'ok': False, 'error': str(e)}), 500
