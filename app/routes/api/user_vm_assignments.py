#!/usr/bin/env python3
"""
User VM Assignment API - Database-first architecture.

Allows admins to directly assign VMs to users outside of class context.
Replaces the legacy mappings.json system.
"""

import logging
from flask import Blueprint, jsonify, request

from app.utils.decorators import admin_required
from app.models import User, VMAssignment, db

logger = logging.getLogger(__name__)

api_user_vm_assignments_bp = Blueprint('api_user_vm_assignments', __name__, url_prefix='/api')


@api_user_vm_assignments_bp.route("/user-vm-assignments", methods=["GET"])
@admin_required
def list_user_vm_assignments():
    """Get all direct VM assignments (not through classes)."""
    try:
        # Get all assignments where class_id is NULL (direct user assignments)
        assignments = VMAssignment.query.filter_by(class_id=None).all()
        
        return jsonify({
            "ok": True,
            "assignments": [
                {
                    "id": a.id,
                    "vmid": a.proxmox_vmid,
                    "user_id": a.assigned_user_id,
                    "username": a.assigned_user.username if a.assigned_user else None,
                    "node": a.node,
                    "status": a.status,
                    "created_at": a.created_at.isoformat() if a.created_at else None
                }
                for a in assignments
            ]
        })
    except Exception as e:
        logger.exception("Failed to list user VM assignments")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_user_vm_assignments_bp.route("/user-vm-assignments", methods=["POST"])
@admin_required
def create_user_vm_assignment():
    """
    Assign a VM directly to a user (not through a class).
    
    Body: {
        "username": "student1",
        "vmid": 123,
        "node": "pve1"  (optional)
    }
    """
    try:
        data = request.get_json() or {}
        username = data.get("username", "").strip()
        vmid = data.get("vmid")
        node = data.get("node", "").strip() or None
        
        if not username:
            return jsonify({"ok": False, "error": "Username is required"}), 400
        
        if not vmid:
            return jsonify({"ok": False, "error": "VMID is required"}), 400
        
        try:
            vmid = int(vmid)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "Invalid VMID"}), 400
        
        # Find user
        user = User.query.filter_by(username=username).first()
        if not user:
            return jsonify({"ok": False, "error": f"User '{username}' not found"}), 404
        
        # Check if assignment already exists
        existing = VMAssignment.query.filter_by(
            proxmox_vmid=vmid,
            class_id=None
        ).first()
        
        if existing:
            return jsonify({
                "ok": False,
                "error": f"VM {vmid} is already assigned to {existing.assigned_user.username if existing.assigned_user else 'someone'}"
            }), 409
        
        # Create assignment
        assignment = VMAssignment(
            class_id=None,  # Direct assignment (not through class)
            proxmox_vmid=vmid,
            node=node,
            assigned_user_id=user.id,
            status='assigned',
            manually_added=True
        )
        
        db.session.add(assignment)
        db.session.commit()
        
        logger.info(f"Admin assigned VM {vmid} to user {username}")
        
        return jsonify({
            "ok": True,
            "assignment": {
                "id": assignment.id,
                "vmid": assignment.proxmox_vmid,
                "username": user.username,
                "node": assignment.node
            }
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to create user VM assignment")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_user_vm_assignments_bp.route("/user-vm-assignments/<int:assignment_id>", methods=["DELETE"])
@admin_required
def delete_user_vm_assignment(assignment_id: int):
    """Remove a direct VM assignment."""
    try:
        assignment = VMAssignment.query.get(assignment_id)
        
        if not assignment:
            return jsonify({"ok": False, "error": "Assignment not found"}), 404
        
        if assignment.class_id is not None:
            return jsonify({
                "ok": False,
                "error": "Cannot delete class-based assignment through this endpoint"
            }), 400
        
        vmid = assignment.proxmox_vmid
        username = assignment.assigned_user.username if assignment.assigned_user else "unknown"
        
        db.session.delete(assignment)
        db.session.commit()
        
        logger.info(f"Admin removed VM {vmid} assignment from user {username}")
        
        return jsonify({"ok": True})
        
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to delete user VM assignment")
        return jsonify({"ok": False, "error": str(e)}), 500
