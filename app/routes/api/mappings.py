#!/usr/bin/env python3
"""
Mappings API routes blueprint.

Provides JSON endpoints for managing user-to-VM mappings (legacy system).
This version is database-first friendly: it avoids heavy Proxmox lookups
and relies on cached VM id/name lists where possible.
"""

import logging
from typing import List, Dict, Any

from flask import Blueprint, jsonify, request

from app.utils.decorators import admin_required


from app.services.proxmox_client import (
    get_user_vm_map,
    save_user_vm_map,
    get_all_vm_ids_and_names,
)
from app.services.user_manager import get_pve_users, require_user

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
        mapping = get_user_vm_map()
        users = get_pve_users()
        vm_catalog = _serialize_vm_list(get_all_vm_ids_and_names())
        return jsonify({
            "ok": True,
            "mapping": mapping,
            "users": users,
            "vms": vm_catalog,
        })
    except Exception as e:
        logger.exception("Failed to load mappings data")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_mappings_bp.route("/mappings/update", methods=["POST"])
@admin_required
def update_mappings():
    """Update mapping for a single user. Body: {user: str, vmids: [int]}."""
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

        mapping = get_user_vm_map()

        if vmids:
            mapping[user] = vmids
        else:
            mapping.pop(user, None)

        save_user_vm_map(mapping)
        acting = require_user()
        logger.info("User %s updated mappings for %s -> %s", acting, user, vmids)

        return jsonify({"ok": True, "mapping": mapping.get(user, [])})
    except Exception as e:
        logger.exception("Failed to update mappings")
        return jsonify({"ok": False, "error": str(e)}), 500
