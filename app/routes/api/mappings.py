#!/usr/bin/env python3
"""
Mappings API routes blueprint.

Provides JSON endpoints for managing user-to-VM mappings (legacy system).
This version is database-first friendly: it avoids heavy Proxmox lookups
and relies on cached VM id/name lists where possible.
"""

import logging
from typing import Any, Dict, List

from flask import Blueprint, jsonify, request

from app.services.proxmox_service import (
    get_all_vm_ids_and_names,
    get_user_vm_map,
    save_user_vm_map,
)
from app.services.user_manager import get_pve_users, require_user
from app.models import User, VMAssignment, db
from app.utils.decorators import admin_required

logger = logging.getLogger(__name__)

api_mappings_bp = Blueprint("api_mappings", __name__, url_prefix="/api")


def _serialize_vm_list(raw_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a minimized VM list (vmid + name + cluster + node) for UI dropdowns."""
    out = []
    for vm in raw_list:
        out.append({
            "vmid": vm.get("vmid"),
            "name": vm.get("name"),
            "cluster_id": vm.get("cluster_id"),
            "node": vm.get("node"),
        })
    return out


@api_mappings_bp.route("/mappings")
@admin_required
def list_mappings():
    """Return current mappings, users, and lightweight VM catalog."""
    try:
        from app.models import User
        
        mapping = get_user_vm_map()
        
        # Get Proxmox users
        pve_users = get_pve_users()
        for u in pve_users:
            u['type'] = 'proxmox'
        
        # Get local database users
        local_users = User.query.all()
        local_user_list = [{
            'userid': user.username,
            'type': 'local',
            'role': user.role
        } for user in local_users]
        
        # Combine both lists
        all_users = pve_users + local_user_list
        all_users.sort(key=lambda x: x['userid'])
        
        vm_catalog = _serialize_vm_list(get_all_vm_ids_and_names())
        return jsonify({
            "ok": True,
            "mapping": mapping,
            "users": all_users,
            "vms": vm_catalog,
        })
    except Exception as e:
        logger.exception("Failed to load mappings data")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_mappings_bp.route("/mappings/update", methods=["POST"])
@admin_required
def update_mappings():
    """Update mapping for a single user. Body: {user: str, vmids: [int]}.
    
    Syncs to both legacy mappings.json AND database VMAssignment table (class_id=NULL).
    """
    try:
        data = request.get_json(force=True) or {}
        user = (data.get("user") or "").strip()
        vmids = data.get("vmids") or []

        if not user:
            return jsonify({"ok": False, "error": "User is required"}), 400

        # Normalize vmids to integers
        try:
            vmids = [int(v) for v in vmids]
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "Invalid VM IDs"}), 400

        # Find user in database
        user_obj = User.query.filter_by(username=user).first()
        if not user_obj:
            return jsonify({"ok": False, "error": f"User '{user}' not found in database"}), 404

        # Update legacy mappings.json
        mapping = get_user_vm_map()
        if vmids:
            mapping[user] = vmids
        else:
            mapping.pop(user, None)
        save_user_vm_map(mapping)

        # Sync to database VMAssignment table (direct assignments, class_id=NULL)
        # Clear session cache to avoid stale data
        db.session.expire_all()
        
        # Get current direct assignments for this user
        current_assignments = VMAssignment.query.filter_by(
            assigned_user_id=user_obj.id,
            class_id=None
        ).all()
        current_vmids = {a.proxmox_vmid for a in current_assignments}
        
        # Find VMs to add and remove
        vmids_to_add = set(vmids) - current_vmids
        vmids_to_remove = current_vmids - set(vmids)
        
        # Remove assignments no longer in list
        for vmid in vmids_to_remove:
            assignment = VMAssignment.query.filter_by(
                proxmox_vmid=vmid,
                assigned_user_id=user_obj.id,
                class_id=None
            ).first()
            if assignment:
                db.session.delete(assignment)
                logger.info("Deleted direct assignment: user=%s, vmid=%d", user, vmid)
        
        # Add new assignments
        for vmid in vmids_to_add:
            # Check if VM already assigned to someone else
            existing = VMAssignment.query.filter_by(
                proxmox_vmid=vmid,
                class_id=None
            ).first()
            if existing:
                logger.warning("VM %d already assigned to %s, skipping", vmid, existing.assigned_user.username if existing.assigned_user else "unknown")
                continue
            
            assignment = VMAssignment(
                class_id=None,  # Direct assignment
                proxmox_vmid=vmid,
                assigned_user_id=user_obj.id,
                status='assigned'
            )
            db.session.add(assignment)
            logger.info("Created direct assignment: user=%s, vmid=%d", user, vmid)
        
        # Commit all changes
        db.session.commit()
        
        # Clear cache again after commit so next queries get fresh data
        db.session.expire_all()
        
        acting = require_user()
        logger.info("User %s synced mappings for %s: vmids=%s", acting, user, vmids)

        return jsonify({"ok": True, "mapping": mapping.get(user, [])})
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to update mappings")
        return jsonify({"ok": False, "error": str(e)}), 500
