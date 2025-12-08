#!/usr/bin/env python3
"""
API endpoints for managing manual VM:Class mappings.

Allows admins/teachers to add existing VMs to classes without auto-assignment.
"""

import logging

from flask import Blueprint, jsonify, request, session

from app.models import VMAssignment, db
from app.services.class_service import get_class_by_id, get_user_by_username
from app.services.user_manager import is_admin_user
from app.utils.decorators import login_required

logger = logging.getLogger(__name__)

api_vm_class_mappings_bp = Blueprint('api_vm_class_mappings', __name__, url_prefix='/api')


@api_vm_class_mappings_bp.route("/vm-class-mappings", methods=["GET"])
@login_required
def list_vm_class_mappings():
    """Get all VM:Class mappings (manually added VMs only).
    
    Admins see all mappings. Teachers see mappings for classes they own/co-own.
    """
    user = session.get("user")
    user_obj = get_user_by_username(user)
    
    if not user_obj:
        return jsonify({"ok": False, "error": "User not found"}), 404
    
    # Get all manually added VM assignments
    assignments = VMAssignment.query.filter_by(manually_added=True).all()
    
    # Filter based on permissions
    if not is_admin_user(user):
        # Teachers only see mappings for classes they own or co-own
        assignments = [a for a in assignments if a.class_ and a.class_.is_owner(user_obj)]
    
    mappings = []
    for assignment in assignments:
        mapping = {
            'id': assignment.id,
            'vmid': assignment.proxmox_vmid,
            'node': assignment.node,
            'class_id': assignment.class_id,
            'class_name': assignment.class_.name if assignment.class_ else 'Unknown',
            'assigned_user_id': assignment.assigned_user_id,
            'assigned_user_name': assignment.assigned_user.username if assignment.assigned_user else None,
            'status': assignment.status,
            'created_at': assignment.created_at.isoformat() if assignment.created_at else None,
        }
        mappings.append(mapping)
    
    return jsonify({"ok": True, "mappings": mappings})


@api_vm_class_mappings_bp.route("/vm-class-mappings", methods=["POST"])
@login_required
def add_vm_to_class():
    """Manually add a VM to a class (won't be auto-assigned).
    
    Admins and class owners/co-owners can add VMs.
    """
    user = session.get("user")
    user_obj = get_user_by_username(user)
    
    if not user_obj:
        return jsonify({"ok": False, "error": "User not found"}), 404
    
    data = request.get_json()
    vmid = data.get('vmid')
    node = data.get('node')
    class_id = data.get('class_id')
    
    if not vmid or not class_id:
        return jsonify({"ok": False, "error": "vmid and class_id are required"}), 400
    
    # Verify class exists
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check permissions: admin or class owner/co-owner
    if not is_admin_user(user) and not class_.is_owner(user_obj):
        return jsonify({"ok": False, "error": "Access denied - only class owners can add VMs"}), 403
    
    if not vmid or not class_id:
        return jsonify({"ok": False, "error": "vmid and class_id are required"}), 400
    
    # Verify class exists
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check if VM is already assigned to this class
    existing = VMAssignment.query.filter_by(
        class_id=class_id,
        proxmox_vmid=vmid
    ).first()
    
    if existing:
        return jsonify({"ok": False, "error": "VM already assigned to this class"}), 409
    
    # Create assignment with manually_added flag
    assignment = VMAssignment(
        class_id=class_id,
        proxmox_vmid=vmid,
        node=node,
        status='available',
        manually_added=True,
        is_template_vm=False
    )
    
    try:
        db.session.add(assignment)
        db.session.commit()
        logger.info(f"Manually added VM {vmid} to class {class_id} ({class_.name})")
        
        return jsonify({
            "ok": True,
            "message": f"VM {vmid} added to class {class_.name}",
            "assignment": {
                'id': assignment.id,
                'vmid': assignment.proxmox_vmid,
                'node': assignment.node,
                'class_id': assignment.class_id,
                'class_name': class_.name,
                'manually_added': True,
                'status': assignment.status
            }
        })
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Failed to add VM {vmid} to class {class_id}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_vm_class_mappings_bp.route("/vm-class-mappings/<int:mapping_id>", methods=["DELETE"])
@login_required
def remove_vm_from_class(mapping_id: int):
    """Remove a manually added VM from a class.
    
    Admins and class owners/co-owners can remove VMs.
    """
    user = session.get("user")
    user_obj = get_user_by_username(user)
    
    if not user_obj:
        return jsonify({"ok": False, "error": "User not found"}), 404
    
    assignment = VMAssignment.query.get(mapping_id)
    if not assignment:
        return jsonify({"ok": False, "error": "Mapping not found"}), 404
    
    if not assignment.manually_added:
        return jsonify({"ok": False, "error": "Can only remove manually added VMs"}), 400
    
    # Check permissions: admin or class owner/co-owner
    if not is_admin_user(user) and (not assignment.class_ or not assignment.class_.is_owner(user_obj)):
        return jsonify({"ok": False, "error": "Access denied - only class owners can remove VMs"}), 403
    
    vmid = assignment.proxmox_vmid
    class_name = assignment.class_.name if assignment.class_ else 'Unknown'
    
    try:
        db.session.delete(assignment)
        db.session.commit()
        logger.info(f"Removed VM {vmid} from class {class_name}")
        
        return jsonify({
            "ok": True,
            "message": f"VM {vmid} removed from class {class_name}"
        })
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Failed to remove mapping {mapping_id}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_vm_class_mappings_bp.route("/classes/<int:class_id>/available-vms", methods=["GET"])
@login_required
def get_available_vms_for_class(class_id: int):
    """Get VMs from VMInventory that could be added to this class.
    
    Admins see all VMs. Teachers see only their personally owned VMs (from mappings.json).
    """
    user = session.get("user")
    user_obj = get_user_by_username(user)
    
    # Get class
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check if user is admin or class owner
    if not is_admin_user(user) and not class_.is_owner(user_obj):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    # Get all VMs from VMInventory
    from app.services.inventory_service import fetch_vm_inventory
    all_vms = fetch_vm_inventory()
    
    # Get VMIDs already assigned to this class
    assigned_vmids = set(
        a.proxmox_vmid for a in VMAssignment.query.filter_by(class_id=class_id).all()
    )
    
    # For teachers (non-admins), filter to only VMs they personally own
    if not is_admin_user(user):
        from app.routes.api.vms import _resolve_user_owned_vmids
        owned_vmids = _resolve_user_owned_vmids(user)
        # Filter to VMs the teacher owns that aren't already in the class and aren't templates
        available_vms = [
            vm for vm in all_vms 
            if vm['vmid'] in owned_vmids
            and vm['vmid'] not in assigned_vmids 
            and not vm.get('is_template', False)
        ]
    else:
        # Admins see all unassigned VMs
        available_vms = [
            vm for vm in all_vms 
            if vm['vmid'] not in assigned_vmids and not vm.get('is_template', False)
        ]
    
    return jsonify({
        "ok": True,
        "vms": available_vms,
        "class": {
            'id': class_.id,
            'name': class_.name
        }
    })
