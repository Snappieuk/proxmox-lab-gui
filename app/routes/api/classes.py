#!/usr/bin/env python3
"""
Class management API routes blueprint.

Handles class CRUD, template listing, VM pool management, and invite links.
"""

import logging

from flask import Blueprint, jsonify, request, session

from app.utils.decorators import login_required

# Import path setup is no longer needed
import app.utils.paths

from app.services.class_service import (
    # Class management
    create_class,
    get_class_by_id,
    get_class_by_token,
    get_classes_for_teacher,
    get_classes_for_student,
    list_all_classes,
    update_class,
    delete_class,
    # Invite management
    generate_class_invite,
    invalidate_class_invite,
    join_class_via_token,
    # Template management
    create_template,
    get_template_by_id,
    get_available_templates,
    get_global_templates,
    delete_template,
    # VM assignment
    create_vm_assignment,
    assign_vm_to_user,
    unassign_vm,
    delete_vm_assignment,
    get_vm_assignments_for_class,
    get_vm_assignments_for_user,
    get_user_vm_in_class,
    get_unassigned_vms_in_class,
    get_vm_assignment_by_vmid,
    # User management
    get_user_by_username,
    get_user_by_id,
    list_all_users,
    update_user_role,
)

from app.services.proxmox_operations import (
    list_proxmox_templates,
    clone_vm_from_template,
    convert_vm_to_template,
    create_vm_snapshot,
    revert_vm_to_snapshot,
    delete_vm,
    start_class_vm,
    stop_class_vm,
    get_vm_status,
    clone_vms_for_class,
    CLASS_CLUSTER_IP
)

from app.models import User, VMAssignment

logger = logging.getLogger(__name__)

api_classes_bp = Blueprint('api_classes', __name__, url_prefix='/api/classes')


def get_current_user() -> User:
    """Get current logged-in user from session."""
    username = session.get('user')
    if not username:
        return None
    # Remove @pve suffix if present for local user lookup
    if '@' in username:
        username = username.split('@')[0]
    return get_user_by_username(username)


def require_teacher_or_adminer():
    """Verify current user is teacher or adminer, return user or abort."""
    user = get_current_user()
    if not user:
        return None, ({"ok": False, "error": "Not authenticated"}, 401)
    if not user.is_teacher and not user.is_adminer:
        return None, ({"ok": False, "error": "Teacher or admin privileges required"}, 403)
    return user, None


def require_adminer():
    """Verify current user is adminer, return user or abort."""
    user = get_current_user()
    if not user:
        return None, ({"ok": False, "error": "Not authenticated"}, 401)
    if not user.is_adminer:
        return None, ({"ok": False, "error": "Admin privileges required"}, 403)
    return user, None


# ---------------------------------------------------------------------------
# Class CRUD
# ---------------------------------------------------------------------------

@api_classes_bp.route("", methods=["GET"])
@login_required
def list_classes():
    """List classes visible to current user.
    
    - Adminers see all classes
    - Teachers see their own classes
    - Students see classes they're enrolled in
    """
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    
    if user.is_adminer:
        classes = list_all_classes()
    elif user.is_teacher:
        classes = get_classes_for_teacher(user.id)
    else:
        classes = get_classes_for_student(user.id)
    
    return jsonify({
        "ok": True,
        "classes": [c.to_dict() for c in classes]
    })


@api_classes_bp.route("", methods=["POST"])
@login_required
def create_new_class():
    """Create a new class (teacher or adminer only)."""
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    data = request.get_json()
    name = data.get('name', '').strip()
    description = data.get('description', '').strip()
    template_id = data.get('template_id')
    pool_size = data.get('pool_size', 0)
    
    if not name:
        return jsonify({"ok": False, "error": "Class name is required"}), 400
    
    class_, msg = create_class(
        name=name,
        teacher_id=user.id,
        description=description,
        template_id=template_id,
        pool_size=pool_size
    )
    
    if not class_:
        return jsonify({"ok": False, "error": msg}), 400
    
    return jsonify({
        "ok": True,
        "message": msg,
        "class": class_.to_dict()
    })


@api_classes_bp.route("/<int:class_id>", methods=["GET"])
@login_required
def get_class(class_id: int):
    """Get class details."""
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check access permissions
    if not user.is_adminer:
        if user.is_teacher and class_.teacher_id != user.id:
            return jsonify({"ok": False, "error": "Access denied"}), 403
        if not user.is_teacher:
            # Student - check if they have a VM in this class
            vm = get_user_vm_in_class(user.id, class_id)
            if not vm:
                return jsonify({"ok": False, "error": "Access denied"}), 403
    
    return jsonify({
        "ok": True,
        "class": class_.to_dict()
    })


@api_classes_bp.route("/<int:class_id>", methods=["PUT"])
@login_required
def update_class_route(class_id: int):
    """Update class properties (teacher owner or adminer only)."""
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check ownership
    if not user.is_adminer and class_.teacher_id != user.id:
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    data = request.get_json()
    success, msg = update_class(
        class_id=class_id,
        name=data.get('name'),
        description=data.get('description'),
        template_id=data.get('template_id'),
        pool_size=data.get('pool_size')
    )
    
    if not success:
        return jsonify({"ok": False, "error": msg}), 400
    
    return jsonify({
        "ok": True,
        "message": msg,
        "class": get_class_by_id(class_id).to_dict()
    })


@api_classes_bp.route("/<int:class_id>", methods=["DELETE"])
@login_required
def delete_class_route(class_id: int):
    """Delete a class and its VMs (teacher owner or adminer only)."""
    logger.info(f"DELETE request for class {class_id}")
    
    user, error = require_teacher_or_adminer()
    if error:
        logger.error(f"Permission denied for class {class_id} deletion: {error}")
        return jsonify(error[0]), error[1]
    
    class_ = get_class_by_id(class_id)
    if not class_:
        logger.error(f"Class {class_id} not found")
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check ownership
    if not user.is_adminer and class_.teacher_id != user.id:
        logger.error(f"User {user.username} not authorized to delete class {class_id}")
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    logger.info(f"Deleting class {class_id} ({class_.name}) with {len(class_.vm_assignments)} VMs")
    
    # Get VMs to delete BEFORE deleting the class
    vm_assignments_to_delete = []
    for assignment in class_.vm_assignments:
        vm_assignments_to_delete.append({
            'vmid': assignment.proxmox_vmid,
            'node': assignment.node
        })
    
    # Delete class from database (cascades to VM assignments)
    success, msg, vmids = delete_class(class_id)
    
    if not success:
        logger.error(f"Failed to delete class {class_id} from database: {msg}")
        return jsonify({"ok": False, "error": msg}), 400
    
    logger.info(f"Class {class_id} deleted from database, now deleting {len(vm_assignments_to_delete)} VMs from Proxmox")
    
    # Delete VMs from Proxmox (best effort, class already deleted)
    deleted_vms = []
    failed_vms = []
    for vm_info in vm_assignments_to_delete:
        # Need to get cluster_ip for the VM
        cluster_ip = None
        if class_.template and class_.template.cluster_ip:
            cluster_ip = class_.template.cluster_ip
        
        logger.info(f"Deleting VM {vm_info['vmid']} on node {vm_info['node']} (cluster: {cluster_ip})")
        vm_success, vm_msg = delete_vm(vm_info['vmid'], vm_info['node'], cluster_ip)
        if vm_success:
            deleted_vms.append(vm_info['vmid'])
            logger.info(f"Successfully deleted VM {vm_info['vmid']}")
        else:
            failed_vms.append({'vmid': vm_info['vmid'], 'error': vm_msg})
            logger.warning(f"Failed to delete VM {vm_info['vmid']}: {vm_msg}")
    
    logger.info(f"Class deletion complete: {len(deleted_vms)} VMs deleted, {len(failed_vms)} failed")
    
    return jsonify({
        "ok": True,
        "message": msg,
        "deleted_vms": deleted_vms,
        "failed_vms": failed_vms
    })


# ---------------------------------------------------------------------------
# Invite Token Management
# ---------------------------------------------------------------------------

@api_classes_bp.route("/<int:class_id>/invite", methods=["POST", "DELETE"])
@login_required
def manage_invite(class_id: int):
    """Generate/delete invite token for a class."""
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    if not user.is_adminer and class_.teacher_id != user.id:
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    data = request.get_json() or {}
    never_expires = data.get('never_expires', False)
    
    token, msg = generate_class_invite(class_id, never_expires)
    
    if not token:
        return jsonify({"ok": False, "error": msg}), 400
    
    return jsonify({
        "ok": True,
        "message": msg,
        "token": token,
        "invite_url": f"/classes/join/{token}"
    })


@api_classes_bp.route("/<int:class_id>/invite", methods=["DELETE"])
@login_required
def disable_invite(class_id: int):
    """Disable (invalidate) the invite token for a class."""
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    if not user.is_adminer and class_.teacher_id != user.id:
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    success, msg = invalidate_class_invite(class_id)
    
    if not success:
        return jsonify({"ok": False, "error": msg}), 400
    
    return jsonify({"ok": True, "message": msg})


@api_classes_bp.route("/join/<token>", methods=["POST"])
@login_required
def join_via_token(token: str):
    """Join a class using an invite token."""
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    
    success, msg, assignment = join_class_via_token(token, user.id)
    
    if not success:
        return jsonify({"ok": False, "error": msg}), 400
    
    response = {"ok": True, "message": msg}
    if assignment:
        response["vm_assignment"] = assignment.to_dict()
    
    return jsonify(response)


# ---------------------------------------------------------------------------
# Template Management
# ---------------------------------------------------------------------------

@api_classes_bp.route("/templates", methods=["GET"])
@login_required
def list_templates():
    """List available templates for class creation.
    
    Only shows templates from cluster 10.220.15.249.
    """
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    # Get templates from Proxmox
    proxmox_templates = list_proxmox_templates(CLASS_CLUSTER_IP)
    
    # Also get registered templates from database
    class_id = request.args.get('class_id', type=int)
    db_templates = get_available_templates(class_id)
    
    return jsonify({
        "ok": True,
        "proxmox_templates": proxmox_templates,
        "registered_templates": [t.to_dict() for t in db_templates]
    })


@api_classes_bp.route("/templates", methods=["POST"])
@login_required
def register_template():
    """Register a Proxmox template in the database."""
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    data = request.get_json()
    name = data.get('name', '').strip()
    proxmox_vmid = data.get('proxmox_vmid')
    node = data.get('node')
    is_class_template = data.get('is_class_template', False)
    class_id = data.get('class_id')
    
    if not name or not proxmox_vmid:
        return jsonify({"ok": False, "error": "Name and Proxmox VMID are required"}), 400
    
    template, msg = create_template(
        name=name,
        proxmox_vmid=proxmox_vmid,
        cluster_ip=CLASS_CLUSTER_IP,
        node=node,
        created_by_id=user.id,
        is_class_template=is_class_template,
        class_id=class_id
    )
    
    if not template:
        return jsonify({"ok": False, "error": msg}), 400
    
    return jsonify({
        "ok": True,
        "message": msg,
        "template": template.to_dict()
    })


# ---------------------------------------------------------------------------
# VM Pool Management
# ---------------------------------------------------------------------------

@api_classes_bp.route("/<int:class_id>/vms", methods=["GET"])
@login_required
def list_class_vms(class_id: int):
    """List all VMs in a class with their live status."""
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check access
    if not user.is_adminer and not user.is_teacher:
        # Student can only see their own VM
        assignment = get_user_vm_in_class(user.id, class_id)
        if not assignment:
            return jsonify({"ok": False, "error": "Access denied"}), 403
        
        # Get live status
        status = get_vm_status(assignment.proxmox_vmid, assignment.node)
        vm_data = assignment.to_dict()
        vm_data.update(status)
        
        return jsonify({
            "ok": True,
            "vms": [vm_data]
        })
    
    # Teacher/adminer sees all VMs
    if not user.is_adminer and class_.teacher_id != user.id:
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    assignments = get_vm_assignments_for_class(class_id)
    vms = []
    
    for assignment in assignments:
        vm_data = assignment.to_dict()
        # Get live status from Proxmox
        status = get_vm_status(assignment.proxmox_vmid, assignment.node)
        vm_data.update(status)
        vms.append(vm_data)
    
    return jsonify({
        "ok": True,
        "vms": vms,
        "total": len(vms),
        "assigned": sum(1 for v in vms if v.get('assigned_user_id')),
        "unassigned": sum(1 for v in vms if not v.get('assigned_user_id'))
    })


@api_classes_bp.route("/<int:class_id>/vms", methods=["POST"])
@login_required
def create_class_vms(class_id: int):
    """Create VM pool for a class by cloning from template."""
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    if not user.is_adminer and class_.teacher_id != user.id:
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    data = request.get_json()
    count = data.get('count', 1)
    template_vmid = data.get('template_vmid')
    target_node = data.get('target_node')
    
    if not template_vmid:
        # Use class template if no specific one provided
        if not class_.template:
            return jsonify({"ok": False, "error": "No template specified and class has no default template"}), 400
        template_vmid = class_.template.proxmox_vmid
    
    # Clone VMs
    created_vms = clone_vms_for_class(
        template_vmid=template_vmid,
        class_name=class_.name,
        count=count,
        target_node=target_node,
        cluster_ip=CLASS_CLUSTER_IP
    )
    
    # Register created VMs in database
    registered = []
    for vmid, node in created_vms:
        assignment, msg = create_vm_assignment(class_id, vmid, node)
        if assignment:
            registered.append(assignment.to_dict())
    
    return jsonify({
        "ok": True,
        "message": f"Created {len(created_vms)} VMs",
        "vms": registered
    })


@api_classes_bp.route("/<int:class_id>/vms/<int:assignment_id>/assign", methods=["POST"])
@login_required
def assign_vm_route(class_id: int, assignment_id: int):
    """Manually assign a VM to a user."""
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    if not user.is_adminer and class_.teacher_id != user.id:
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    data = request.get_json()
    target_user_id = data.get('user_id')
    
    if not target_user_id:
        return jsonify({"ok": False, "error": "User ID is required"}), 400
    
    success, msg = assign_vm_to_user(assignment_id, target_user_id)
    
    if not success:
        return jsonify({"ok": False, "error": msg}), 400
    
    return jsonify({"ok": True, "message": msg})


@api_classes_bp.route("/<int:class_id>/vms/<int:assignment_id>/unassign", methods=["POST"])
@login_required
def unassign_vm_route(class_id: int, assignment_id: int):
    """Unassign a VM from its user."""
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    if not user.is_adminer and class_.teacher_id != user.id:
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    success, msg = unassign_vm(assignment_id)
    
    if not success:
        return jsonify({"ok": False, "error": msg}), 400
    
    return jsonify({"ok": True, "message": msg})


@api_classes_bp.route("/<int:class_id>/vms/<int:assignment_id>", methods=["DELETE"])
@login_required
def delete_vm_route(class_id: int, assignment_id: int):
    """Delete a VM from class and Proxmox."""
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    if not user.is_adminer and class_.teacher_id != user.id:
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    # Get assignment to find Proxmox VMID
    assignment = VMAssignment.query.get(assignment_id)
    
    if not assignment:
        return jsonify({"ok": False, "error": "VM assignment not found"}), 404
    
    vmid = assignment.proxmox_vmid
    node = assignment.node
    
    # Delete from database first
    success, msg, _ = delete_vm_assignment(assignment_id)
    if not success:
        return jsonify({"ok": False, "error": msg}), 400
    
    # Delete from Proxmox
    pve_success, pve_msg = delete_vm(vmid, node)
    
    return jsonify({
        "ok": True,
        "message": msg,
        "proxmox_deleted": pve_success,
        "proxmox_message": pve_msg
    })


# ---------------------------------------------------------------------------
# Student VM Operations
# ---------------------------------------------------------------------------

@api_classes_bp.route("/<int:class_id>/my-vm", methods=["GET"])
@login_required
def get_my_vm(class_id: int):
    """Get current user's VM in a class."""
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    
    assignment = get_user_vm_in_class(user.id, class_id)
    if not assignment:
        return jsonify({"ok": False, "error": "You don't have a VM in this class"}), 404
    
    vm_data = assignment.to_dict()
    status = get_vm_status(assignment.proxmox_vmid, assignment.node)
    vm_data.update(status)
    
    return jsonify({"ok": True, "vm": vm_data})


@api_classes_bp.route("/<int:class_id>/my-vm/start", methods=["POST"])
@login_required
def start_my_vm(class_id: int):
    """Start current user's VM in a class."""
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    
    assignment = get_user_vm_in_class(user.id, class_id)
    if not assignment:
        return jsonify({"ok": False, "error": "You don't have a VM in this class"}), 404
    
    success, msg = start_class_vm(assignment.proxmox_vmid, assignment.node)
    
    if not success:
        return jsonify({"ok": False, "error": msg}), 400
    
    return jsonify({"ok": True, "message": msg})


@api_classes_bp.route("/<int:class_id>/my-vm/stop", methods=["POST"])
@login_required
def stop_my_vm(class_id: int):
    """Stop current user's VM in a class."""
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    
    assignment = get_user_vm_in_class(user.id, class_id)
    if not assignment:
        return jsonify({"ok": False, "error": "You don't have a VM in this class"}), 404
    
    success, msg = stop_class_vm(assignment.proxmox_vmid, assignment.node)
    
    if not success:
        return jsonify({"ok": False, "error": msg}), 400
    
    return jsonify({"ok": True, "message": msg})


@api_classes_bp.route("/<int:class_id>/my-vm/revert", methods=["POST"])
@login_required
def revert_my_vm(class_id: int):
    """Revert current user's VM to class baseline snapshot.
    
    This is a fast operation using Proxmox's snapshot system.
    """
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    
    assignment = get_user_vm_in_class(user.id, class_id)
    if not assignment:
        return jsonify({"ok": False, "error": "You don't have a VM in this class"}), 404
    
    success, msg = revert_vm_to_snapshot(
        vmid=assignment.proxmox_vmid,
        node=assignment.node,
        snapshot_name='class_baseline'
    )
    
    if not success:
        return jsonify({"ok": False, "error": msg}), 400
    
    return jsonify({"ok": True, "message": msg})


# ---------------------------------------------------------------------------
# Teacher Operations
# ---------------------------------------------------------------------------

@api_classes_bp.route("/<int:class_id>/push-updates", methods=["POST"])
@login_required
def push_updates(class_id: int):
    """Push teacher's template updates to all class VMs.
    
    Creates/updates the baseline snapshot on all VMs.
    """
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    if not user.is_adminer and class_.teacher_id != user.id:
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    # Get all VMs in class
    assignments = get_vm_assignments_for_class(class_id)
    
    success_count = 0
    failed = []
    
    for assignment in assignments:
        success, msg = create_vm_snapshot(
            vmid=assignment.proxmox_vmid,
            node=assignment.node,
            snapshot_name='class_baseline'
        )
        if success:
            success_count += 1
        else:
            failed.append({
                'vmid': assignment.proxmox_vmid,
                'error': msg
            })
    
    return jsonify({
        "ok": True,
        "message": f"Updated {success_count} of {len(assignments)} VMs",
        "success_count": success_count,
        "total": len(assignments),
        "failed": failed
    })


# ---------------------------------------------------------------------------
# User Management (Adminer only)
# ---------------------------------------------------------------------------

@api_classes_bp.route("/users", methods=["GET"])
@login_required
def list_users():
    """List all users (adminer only)."""
    user, error = require_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    users = list_all_users()
    return jsonify({
        "ok": True,
        "users": [u.to_dict() for u in users]
    })


@api_classes_bp.route("/users/<int:user_id>/role", methods=["PUT"])
@login_required
def update_role(user_id: int):
    """Update a user's role (adminer only)."""
    user, error = require_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    data = request.get_json()
    new_role = data.get('role')
    
    if not new_role:
        return jsonify({"ok": False, "error": "Role is required"}), 400
    
    success, msg = update_user_role(user_id, new_role)
    
    if not success:
        return jsonify({"ok": False, "error": msg}), 400
    
    return jsonify({"ok": True, "message": msg})
