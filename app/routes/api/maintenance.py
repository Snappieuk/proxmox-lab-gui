#!/usr/bin/env python3
"""Admin maintenance API endpoints for cleanup operations."""

import logging
from flask import Blueprint, jsonify, request
from app.models import db, VMAssignment, Class
from app.utils.decorators import admin_required
from sqlalchemy import func

logger = logging.getLogger(__name__)

maintenance_bp = Blueprint('maintenance', __name__, url_prefix='/api/admin/maintenance')


@maintenance_bp.route("/assignments/stats", methods=["GET"])
@admin_required
def get_assignment_stats():
    """Get statistics about VM assignments."""
    try:
        total = VMAssignment.query.count()
        unassigned = VMAssignment.query.filter_by(assigned_user_id=None).count()
        assigned = assigned_count = VMAssignment.query.filter(
            VMAssignment.assigned_user_id.isnot(None)
        ).count()
        
        # Find duplicates
        duplicates = db.session.query(
            VMAssignment.proxmox_vmid,
            VMAssignment.class_id,
            func.count(VMAssignment.id).label('count')
        ).group_by(
            VMAssignment.proxmox_vmid,
            VMAssignment.class_id
        ).having(func.count(VMAssignment.id) > 1).count()
        
        return jsonify({
            "ok": True,
            "stats": {
                "total": total,
                "assigned": assigned_count,
                "unassigned": unassigned,
                "duplicate_groups": duplicates
            }
        })
    except Exception as e:
        logger.exception("Failed to get assignment stats")
        return jsonify({"ok": False, "error": str(e)}), 500


@maintenance_bp.route("/assignments/unassigned", methods=["GET"])
@admin_required
def get_unassigned_assignments():
    """List all unassigned assignments."""
    try:
        unassigned = VMAssignment.query.filter_by(assigned_user_id=None).all()
        
        result = []
        for assign in unassigned:
            cls_name = None
            if assign.class_id:
                cls = Class.query.get(assign.class_id)
                cls_name = cls.name if cls else "UNKNOWN"
            
            result.append({
                'id': assign.id,
                'vmid': assign.proxmox_vmid,
                'class_id': assign.class_id,
                'class_name': cls_name or "DIRECT",
                'status': assign.status,
                'assigned_user_id': assign.assigned_user_id
            })
        
        return jsonify({
            "ok": True,
            "unassigned": result,
            "count": len(result)
        })
    except Exception as e:
        logger.exception("Failed to get unassigned assignments")
        return jsonify({"ok": False, "error": str(e)}), 500


@maintenance_bp.route("/assignments/duplicates", methods=["GET"])
@admin_required
def get_duplicate_assignments():
    """List duplicate assignments (same VM in same class)."""
    try:
        duplicates_query = db.session.query(
            VMAssignment.proxmox_vmid,
            VMAssignment.class_id,
            func.count(VMAssignment.id).label('count')
        ).group_by(
            VMAssignment.proxmox_vmid,
            VMAssignment.class_id
        ).having(func.count(VMAssignment.id) > 1).all()
        
        result = []
        for vmid, class_id, count in duplicates_query:
            assignments = VMAssignment.query.filter_by(
                proxmox_vmid=vmid,
                class_id=class_id
            ).order_by(VMAssignment.id).all()
            
            cls_name = None
            if class_id:
                cls = Class.query.get(class_id)
                cls_name = cls.name if cls else "UNKNOWN"
            
            assignment_list = []
            for assign in assignments:
                user_name = assign.assigned_user.username if assign.assigned_user else None
                assignment_list.append({
                    'id': assign.id,
                    'user_id': assign.assigned_user_id,
                    'username': user_name or "UNASSIGNED",
                    'status': assign.status
                })
            
            result.append({
                'vmid': vmid,
                'class_id': class_id,
                'class_name': cls_name or "DIRECT",
                'count': count,
                'assignments': assignment_list
            })
        
        return jsonify({
            "ok": True,
            "duplicates": result,
            "count": len(result)
        })
    except Exception as e:
        logger.exception("Failed to get duplicate assignments")
        return jsonify({"ok": False, "error": str(e)}), 500


@maintenance_bp.route("/assignments/delete-unassigned", methods=["POST"])
@admin_required
def delete_unassigned_assignments():
    """Delete all unassigned assignments."""
    try:
        unassigned = VMAssignment.query.filter_by(assigned_user_id=None).all()
        count = len(unassigned)
        
        for assign in unassigned:
            db.session.delete(assign)
        
        db.session.commit()
        logger.info(f"Deleted {count} unassigned assignments")
        
        return jsonify({
            "ok": True,
            "message": f"Deleted {count} unassigned assignments",
            "deleted": count
        })
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to delete unassigned assignments")
        return jsonify({"ok": False, "error": str(e)}), 500


@maintenance_bp.route("/assignments/delete/<int:assignment_id>", methods=["DELETE"])
@admin_required
def delete_assignment(assignment_id: int):
    """Delete a specific assignment by ID."""
    try:
        assign = VMAssignment.query.get(assignment_id)
        if not assign:
            return jsonify({"ok": False, "error": "Assignment not found"}), 404
        
        vmid = assign.proxmox_vmid
        cls_id = assign.class_id
        user_name = assign.assigned_user.username if assign.assigned_user else "UNASSIGNED"
        
        db.session.delete(assign)
        db.session.commit()
        
        logger.info(f"Deleted assignment {assignment_id}: VM {vmid} from user {user_name}")
        
        return jsonify({
            "ok": True,
            "message": f"Deleted assignment {assignment_id}",
            "deleted": {
                'id': assignment_id,
                'vmid': vmid,
                'class_id': cls_id,
                'user': user_name
            }
        })
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Failed to delete assignment {assignment_id}")
        return jsonify({"ok": False, "error": str(e)}), 500
