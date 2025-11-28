#!/usr/bin/env python3
"""
Admin Mappings routes blueprint.

Handles user-to-VM mappings administration.
"""

import logging

from flask import Blueprint, render_template, redirect, url_for, request, jsonify, current_app

from app.utils.decorators import login_required, admin_required

# Import path setup (adds rdp-gen to sys.path)
import app.utils.paths

from app.services.proxmox_client import (
    get_all_vms,
    get_user_vm_map,
    set_user_vm_mapping,
    get_admin_group_members,
    add_user_to_admin_group,
    remove_user_from_admin_group,
)
from app.config import ADMIN_GROUP, ADMIN_USERS

logger = logging.getLogger(__name__)

admin_mappings_bp = Blueprint('admin_mappings', __name__, url_prefix='/admin/mappings')


@admin_mappings_bp.route("", methods=["GET", "POST"])
@admin_required
def admin_mappings():
    """Admin mappings view."""
    from app.services.user_manager import require_user, get_pve_users
    
    user = require_user()
    message = request.args.get('message')
    error = request.args.get('error')
    selected_user = None

    if request.method == "POST":
        # Just selecting a user to view/edit
        selected_user = (request.form.get("user") or "").strip()

    # Progressive loading - return minimal data on GET
    if request.method == "GET" and not request.args.get('user'):
        return render_template(
            "mappings.html",
            user=user,
            progressive_load=True,
            message=message,
            error=error,
        )
    
    # If user is selected via query param (from redirect), use that
    if request.args.get('user'):
        selected_user = request.args.get('user')
    
    # POST request or user selected - load full data
    mapping = get_user_vm_map()
    users = get_pve_users()
    all_vms = get_all_vms()
    
    # Get admin group info
    current_admins = list(set(ADMIN_USERS + get_admin_group_members()))
    protected_admins = ADMIN_USERS  # Users in ADMIN_USERS config can't be removed via UI
    
    # Create vmid to name mapping for display
    vm_id_to_name = {v["vmid"]: v.get("name", f"vm-{v['vmid']}") for v in all_vms}
    all_vm_ids = sorted({v["vmid"] for v in all_vms})

    return render_template(
        "mappings.html",
        user=user,
        progressive_load=False,
        mapping=mapping,
        users=users,
        all_vm_ids=all_vm_ids,
        vm_id_to_name=vm_id_to_name,
        message=message,
        error=error,
        selected_user=selected_user,
        admin_group=ADMIN_GROUP,
        current_admins=current_admins,
        protected_admins=protected_admins,
    )


@admin_mappings_bp.route("/update-vms", methods=["POST"])
@admin_required
def admin_mappings_update_vms():
    """Update VM assignments for selected user."""
    from app.services.user_manager import require_user
    
    user = require_user()
    selected_user = (request.form.get("user") or "").strip()
    vmids_str = (request.form.get("vmids") or "").strip()
    
    if not selected_user:
        return redirect(url_for("admin_mappings.admin_mappings", error="User is required"))
    
    try:
        if vmids_str:
            # Split by comma or whitespace
            parts = [p.strip() for p in vmids_str.replace(',', ' ').split() if p.strip()]
            vmids = [int(p) for p in parts]
        else:
            vmids = []
        set_user_vm_mapping(selected_user, vmids)
        return redirect(url_for("admin_mappings.admin_mappings", user=selected_user, message=f"Updated VM assignments for {selected_user}"))
    except ValueError:
        return redirect(url_for("admin_mappings.admin_mappings", user=selected_user, error="VM IDs must be integers"))
    except Exception as e:
        logger.exception("Failed to update VM mappings for %s", selected_user)
        return redirect(url_for("admin_mappings.admin_mappings", user=selected_user, error=f"Failed to update: {str(e)}"))


@admin_mappings_bp.route("/unassign-form", methods=["POST"])
@admin_required
def admin_mappings_unassign_form():
    """Unassign a specific VM from selected user (form-based)."""
    from app.services.user_manager import require_user
    
    user = require_user()
    selected_user = (request.form.get("user") or "").strip()
    vmid_str = (request.form.get("vmid") or "").strip()
    
    if not selected_user or not vmid_str:
        return redirect(url_for("admin_mappings.admin_mappings", user=selected_user, error="User and VM ID are required"))
    
    try:
        vmid = int(vmid_str)
        mapping = get_user_vm_map()
        current_vms = mapping.get(selected_user, [])
        
        if vmid in current_vms:
            current_vms.remove(vmid)
            set_user_vm_mapping(selected_user, current_vms)
            return redirect(url_for("admin_mappings.admin_mappings", user=selected_user, message=f"Removed VM {vmid} from {selected_user}"))
        else:
            return redirect(url_for("admin_mappings.admin_mappings", user=selected_user, error=f"VM {vmid} not assigned to {selected_user}"))
    except ValueError:
        return redirect(url_for("admin_mappings.admin_mappings", user=selected_user, error="Invalid VM ID"))
    except Exception as e:
        logger.exception("Failed to unassign VM %s from %s", vmid_str, selected_user)
        return redirect(url_for("admin_mappings.admin_mappings", user=selected_user, error=f"Failed to unassign: {str(e)}"))


@admin_mappings_bp.route("/grant-admin-form", methods=["POST"])
@admin_required
def admin_mappings_grant_admin_form():
    """Grant admin privileges to selected user (form-based)."""
    from app.services.user_manager import require_user
    
    user = require_user()
    selected_user = (request.form.get("user") or "").strip()
    
    if not selected_user:
        return redirect(url_for("admin_mappings.admin_mappings", error="User is required"))
    
    try:
        if not ADMIN_GROUP:
            return redirect(url_for("admin_mappings.admin_mappings", user=selected_user, error="ADMIN_GROUP not configured"))
        
        add_user_to_admin_group(selected_user)
        return redirect(url_for("admin_mappings.admin_mappings", user=selected_user, message=f"Added {selected_user} to admin group"))
    except Exception as e:
        logger.exception("Failed to add %s to admin group", selected_user)
        return redirect(url_for("admin_mappings.admin_mappings", user=selected_user, error=f"Failed to grant admin: {str(e)}"))


@admin_mappings_bp.route("/remove-admin-form", methods=["POST"])
@admin_required
def admin_mappings_remove_admin_form():
    """Remove admin privileges from selected user (form-based)."""
    from app.services.user_manager import require_user
    
    user = require_user()
    selected_user = (request.form.get("user") or "").strip()
    
    if not selected_user:
        return redirect(url_for("admin_mappings.admin_mappings", error="User is required"))
    
    try:
        # Check if user is in protected list
        if selected_user in ADMIN_USERS:
            return redirect(url_for("admin_mappings.admin_mappings", user=selected_user, error=f"{selected_user} is a protected admin (in ADMIN_USERS config)"))
        
        if not ADMIN_GROUP:
            return redirect(url_for("admin_mappings.admin_mappings", user=selected_user, error="ADMIN_GROUP not configured"))
        
        remove_user_from_admin_group(selected_user)
        return redirect(url_for("admin_mappings.admin_mappings", user=selected_user, message=f"Removed {selected_user} from admin group"))
    except Exception as e:
        logger.exception("Failed to remove %s from admin group", selected_user)
        return redirect(url_for("admin_mappings.admin_mappings", user=selected_user, error=f"Failed to remove admin: {str(e)}"))


@admin_mappings_bp.route("/unassign", methods=["POST"])
@admin_required
def api_unassign_vm():
    """Remove a single VM from a user's mapping (JSON API)."""
    from app.services.user_manager import require_user
    
    try:
        data = request.get_json()
        user = data.get("user")
        vmid = int(data.get("vmid"))
        
        if not user or not vmid:
            return jsonify({"ok": False, "error": "Missing user or vmid"}), 400
        
        # Get current mapping
        mapping = get_user_vm_map()
        
        if user not in mapping:
            return jsonify({"ok": False, "error": "User has no mappings"}), 404
        
        # Remove the vmid from user's list
        if vmid in mapping[user]:
            mapping[user].remove(vmid)
            
            # If user has no more VMs, remove the user entry entirely
            if not mapping[user]:
                del mapping[user]
            
            # Save updated mapping
            set_user_vm_mapping(user, mapping.get(user, []))
            
            logger.info("Admin %s removed VM %d from user %s", require_user(), vmid, user)
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": "VM not in user's mappings"}), 404
            
    except Exception as e:
        logger.exception("Failed to unassign VM")
        return jsonify({"ok": False, "error": str(e)}), 500


@admin_mappings_bp.route("/add-admin", methods=["POST"])
@admin_required
def add_admin():
    """Add a user to the admin group."""
    from app.services.user_manager import require_user
    
    try:
        user_to_add = request.form.get("user", "").strip()
        
        if not user_to_add:
            return redirect(url_for("admin_mappings.admin_mappings"))
        
        success = add_user_to_admin_group(user_to_add)
        
        if success:
            logger.info("Admin %s added %s to admin group", require_user(), user_to_add)
        else:
            logger.error("Failed to add %s to admin group", user_to_add)
        
        return redirect(url_for("admin_mappings.admin_mappings"))
    except Exception as e:
        logger.exception("Failed to add admin")
        return redirect(url_for("admin_mappings.admin_mappings"))


@admin_mappings_bp.route("/remove-admin", methods=["POST"])
@admin_required
def remove_admin():
    """Remove a user from the admin group (JSON API)."""
    from app.services.user_manager import require_user
    
    try:
        data = request.get_json()
        user_to_remove = data.get("user", "").strip()
        
        if not user_to_remove:
            return jsonify({"ok": False, "error": "Missing user"}), 400
        
        # Don't allow removing users from ADMIN_USERS config
        if user_to_remove in ADMIN_USERS:
            return jsonify({"ok": False, "error": "Cannot remove users defined in ADMIN_USERS config"}), 400
        
        success = remove_user_from_admin_group(user_to_remove)
        
        if success:
            logger.info("Admin %s removed %s from admin group", require_user(), user_to_remove)
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": "Failed to remove user from admin group"}), 500
            
    except Exception as e:
        logger.exception("Failed to remove admin")
        return jsonify({"ok": False, "error": str(e)}), 500
