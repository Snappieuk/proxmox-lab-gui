#!/usr/bin/env python3
"""
Mappings API routes blueprint.

JSON API for user-VM mappings.
"""

import logging

from flask import Blueprint, jsonify, request, current_app

from app.utils.decorators import admin_required

# Import from legacy module
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'rdp-gen'))

from proxmox_client import (
    get_user_vm_map,
    get_all_vm_ids_and_names,
    save_user_vm_map,
)

logger = logging.getLogger(__name__)

api_mappings_bp = Blueprint('api_mappings', __name__, url_prefix='/api')


@api_mappings_bp.route("/mappings")
@admin_required
def api_mappings():
    """Return mappings data for progressive loading (lightweight - no IP lookups)."""
    from app.services.user_manager import get_pve_users
    
    try:
        mapping = get_user_vm_map()
        users = get_pve_users()
        # Get only VM IDs and names (no IP lookups - much faster)
        vm_list = get_all_vm_ids_and_names()
        
        return jsonify({
            "mapping": mapping,
            "users": users,
            "vms": vm_list,  # List of {vmid, name, cluster_id, node}
        })
    except Exception as e:
        logger.exception("Failed to get mappings data")
        return jsonify({
            "error": True,
            "message": f"Failed to load mappings: {str(e)}"
        }), 500


@api_mappings_bp.route("/mappings/update", methods=["POST"])
@admin_required
def api_mappings_update():
    """Update VM mappings for a user (JSON API - no page reload)."""
    from app.services.user_manager import require_user
    
    try:
        data = request.get_json()
        user = data.get("user", "").strip()
        vmids = data.get("vmids", [])  # List of integers
        
        if not user:
            return jsonify({
                "ok": False,
                "error": "User is required"
            }), 400
        
        # Validate vmids are integers
        try:
            vmids = [int(v) for v in vmids]
        except (ValueError, TypeError):
            return jsonify({
                "ok": False,
                "error": "Invalid VM IDs"
            }), 400
        
        # Update mappings
        mapping = get_user_vm_map()
        
        if vmids:
            mapping[user] = vmids
        else:
            # Remove user if no VMs assigned
            mapping.pop(user, None)
        
        save_user_vm_map(mapping)
        logger.info("Admin %s updated mappings for %s: %s", require_user(), user, vmids)
        
        return jsonify({
            "ok": True,
            "mapping": mapping.get(user, [])
        })
    except Exception as e:
        logger.exception("Failed to update mappings")
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500
