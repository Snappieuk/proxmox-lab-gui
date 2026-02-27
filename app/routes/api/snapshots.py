#!/usr/bin/env python3
"""
Admin endpoints for snapshot management and migration.
"""

import logging
from flask import Blueprint, jsonify, request
from app.utils.decorators import admin_required
from app.services.snapshot_migration import (
    create_baseline_snapshots_for_class,
    create_baseline_snapshots_for_all_linked_clones
)

logger = logging.getLogger(__name__)

api_snapshots_bp = Blueprint('api_snapshots', __name__)


@api_snapshots_bp.route("/api/admin/snapshots/migrate-class/<int:class_id>", methods=["POST"])
@admin_required
def migrate_class_snapshots(class_id: int):
    """
    Retroactively create baseline snapshots for all VMs in a specific linked clone class.
    
    This is useful for existing classes that were created before the automatic
    snapshot feature was implemented.
    
    Query params:
        - class_id: The Class ID to migrate
        
    Returns:
        {
            "ok": True/False,
            "message": "Human-readable status message",
            "stats": {
                "created": int,  # New snapshots created
                "failed": int,   # Failed attempts
                "skipped": int   # Already had snapshots
            }
        }
    """
    try:
        logger.info(f"Starting snapshot migration for class {class_id}")
        
        success, message, stats = create_baseline_snapshots_for_class(class_id)
        
        return jsonify({
            "ok": success,
            "message": message,
            "stats": stats
        }), 200 if success else 400
        
    except Exception as e:
        logger.exception(f"Error in snapshot migration endpoint: {e}")
        return jsonify({
            "ok": False,
            "message": f"Migration failed: {str(e)}",
            "stats": {}
        }), 500


@api_snapshots_bp.route("/api/admin/snapshots/migrate-all", methods=["POST"])
@admin_required
def migrate_all_snapshots():
    """
    Retroactively create baseline snapshots for ALL linked clone classes.
    
    WARNING: This processes all linked clone classes. Use only if you know
    what you're doing or after backing up critical data.
    
    Returns:
        {
            "ok": True/False,
            "message": "Human-readable status message with detailed stats",
            "stats": {
                "classes": int,       # Total classes processed
                "vms_created": int,   # Total new snapshots created
                "vms_failed": int,    # Total failed attempts
                "vms_skipped": int    # Total already had snapshots
            }
        }
    """
    try:
        # Optional safeguard: require explicit confirmation header
        confirm = request.headers.get("X-Confirm-Migration", "").lower() == "true"
        if not confirm:
            return jsonify({
                "ok": False,
                "message": "Please add header 'X-Confirm-Migration: true' to confirm retroactive snapshot migration for all linked clone classes"
            }), 400
        
        logger.warning("Starting snapshot migration for ALL linked clone classes")
        
        success, message, stats = create_baseline_snapshots_for_all_linked_clones()
        
        return jsonify({
            "ok": success,
            "message": message,
            "stats": stats
        }), 200 if success else 400
        
    except Exception as e:
        logger.exception(f"Error in batch snapshot migration endpoint: {e}")
        return jsonify({
            "ok": False,
            "message": f"Batch migration failed: {str(e)}",
            "stats": {}
        }), 500


@api_snapshots_bp.route("/api/admin/snapshots/status", methods=["GET"])
@admin_required
def snapshot_migration_status():
    """
    Get snapshot migration status for a class.
    
    Query params:
        - class_id (required): The Class ID to check
        
    Returns:
        {
            "ok": True/False,
            "class_name": "Class Name",
            "deployment_method": "linked_clone|config_clone",
            "vms": [
                {
                    "vmid": int,
                    "name": str,
                    "has_baseline": True/False,
                    "snapshot_count": int
                },
                ...
            ]
        }
    """
    try:
        class_id = request.args.get("class_id", type=int)
        if not class_id:
            return jsonify({"ok": False, "error": "class_id parameter required"}), 400
        
        from app.models import Class, VMAssignment
        
        class_ = Class.query.get(class_id)
        if not class_:
            return jsonify({"ok": False, "error": f"Class {class_id} not found"}), 404
        
        # Get all VMs in the class
        vms = VMAssignment.query.filter_by(class_id=class_id, is_template_vm=False).all()
        
        vm_list = []
        for vm in vms:
            vm_list.append({
                "vmid": vm.proxmox_vmid,
                "name": vm.vm_name,
                "has_baseline": "unknown",  # Would need SSH to determine accurately
                "status": vm.status
            })
        
        return jsonify({
            "ok": True,
            "class_name": class_.name,
            "deployment_method": getattr(class_, 'deployment_method', 'config_clone'),
            "vm_count": len(vm_list),
            "vms": vm_list
        }), 200
        
    except Exception as e:
        logger.exception(f"Error getting snapshot status: {e}")
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500
