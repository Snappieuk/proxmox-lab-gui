#!/usr/bin/env python3
"""
Class management API routes blueprint.

Handles class CRUD, template listing, VM pool management, and invite links.
"""

import logging
import re
import threading
import uuid

from flask import Blueprint, jsonify, request, session

from app.utils.decorators import login_required
from app.models import User, VMAssignment, Class, db

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
    auto_assign_vms_to_waiting_students,
    # Template management
    create_template,
    get_available_templates,
    get_global_templates,
    # VM assignment
    create_vm_assignment,
    assign_vm_to_user,
    unassign_vm,
    delete_vm_assignment,
    get_vm_assignments_for_class,
    get_user_vm_in_class,
    get_unassigned_vms_in_class,
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
    delete_vm,
    get_vm_status,
    get_vm_status_from_inventory,
    CLASS_CLUSTER_IP
)
from app.config import CLUSTERS

from app.services.clone_progress import start_clone_progress, get_clone_progress, update_clone_progress
from app.utils.auth_helpers import get_current_user

logger = logging.getLogger(__name__)

api_classes_bp = Blueprint('api_classes', __name__, url_prefix='/api/classes')


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
    """Create a new class and automatically deploy VMs (teacher or adminer only)."""
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    data = request.get_json()
    name = data.get('name', '').strip()
    description = data.get('description', '').strip()
    template_id = data.get('template_id')
    pool_size = data.get('pool_size', 0)
    cpu_cores = data.get('cpu_cores', 2)
    memory_mb = data.get('memory_mb', 2048)
    disk_size_gb = data.get('disk_size_gb', 32)
    
    if not name:
        return jsonify({"ok": False, "error": "Class name is required"}), 400
    
    # Template is now optional - classes can be created without templates
    # Pool size can be 0 - VMs can be added manually later
    
    # Create class in database
    class_, msg = create_class(
        name=name,
        teacher_id=user.id,
        description=description,
        template_id=template_id,
        pool_size=pool_size,
        cpu_cores=cpu_cores,
        memory_mb=memory_mb,
        disk_size_gb=disk_size_gb
    )
    
    if not class_:
        return jsonify({"ok": False, "error": msg}), 400
    
    # Always create VM shells (template + teacher + students) - even if pool_size=0
    # Get source node from template, or find first available node in cluster
    source_node = None
    if template_id:
        source_template = class_.template
        if source_template:
            source_node = source_template.node
    
    if not source_node:
        # No template or no node from template - get first available node from cluster
        try:
            from app.services.proxmox_service import get_proxmox_admin_for_cluster
            from app.config import CLUSTERS
            
            # Find cluster by IP
            cluster_id = None
            for cluster in CLUSTERS:
                if cluster["host"] == "10.220.15.249":
                    cluster_id = cluster["id"]
                    break
            
            if cluster_id:
                proxmox = get_proxmox_admin_for_cluster(cluster_id)
                nodes = proxmox.nodes.get()
                source_node = nodes[0]['node'] if nodes else None
            else:
                source_node = None
            
            if not source_node:
                logger.error("No nodes available in cluster")
                return jsonify({"ok": False, "error": "No nodes available in cluster"}), 500
                
        except Exception as e:
            logger.exception(f"Failed to get cluster nodes: {e}")
            return jsonify({"ok": False, "error": f"Failed to get cluster nodes: {str(e)}"}), 500
    
    # Create VMs in background thread using SSH + QCOW2 overlay cloning
    import threading
    from flask import current_app
    app = current_app._get_current_object()
    
    def _create_class_vms():
        try:
            app_ctx = app.app_context()
            app_ctx.push()
            
            from app.services.class_vm_service import deploy_class_vms
            from app.models import Class
            
            # Re-fetch class to get template info
            class_obj = Class.query.get(class_.id)
            if not class_obj:
                logger.error(f"Class {class_.id} not found in background thread")
                return
            
            # Deploy VMs using QCOW2 overlay cloning (fast)
            logger.info(f"Deploying VMs for class '{class_obj.name}' (template={class_obj.template_id}, students={class_obj.pool_size})...")
            
            success, message, vm_info = deploy_class_vms(
                class_id=class_obj.id,
                num_students=class_obj.pool_size
            )
            
            if success:
                logger.info(f"Class {class_obj.id} VM deployment succeeded: {message}")
                logger.info(f"Created: {vm_info.get('teacher_vm_count', 0)} teacher VM, "
                           f"{vm_info.get('template_vm_count', 0)} template VM, "
                           f"{vm_info.get('student_vm_count', 0)} student VMs")
            else:
                logger.error(f"Class {class_obj.id} VM deployment failed: {message}")
                
        except Exception as e:
            logger.exception(f"Failed to deploy VMs for class {class_.id}: {e}")
        finally:
            try:
                app_ctx.pop()
            except:
                pass
    
    # Start the background thread to create VMs
    threading.Thread(target=_create_class_vms, daemon=True).start()
    
    # Return immediately
    message = f"Class created successfully"
    if pool_size > 0:
        total_vms = pool_size + 2  # pool_size students + 1 teacher + 1 template
        message += f" - deploying {total_vms} VMs ({pool_size} student + 1 teacher + 1 template) in background using QCOW2 overlay cloning"
    else:
        message += " - deploying 2 VMs in background (1 template + 1 teacher, no students)"
    
    logger.info(f"Class {class_.id} '{class_.name}' created, VMs deploying in background")
    return jsonify({
        "ok": True,
        "message": message,
        "class": class_.to_dict()
    })


# ---------------------------------------------------------------------------
# Get Class Details
# ---------------------------------------------------------------------------

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
        if user.is_teacher and not class_.is_owner(user):
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
    if not user.is_adminer and not class_.is_owner(user):
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
    """Delete a class and its VMs (teacher owner or adminer only).
    
    Query param 'force=true' will delete class from DB without deleting VMs from Proxmox.
    """
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
    if not user.is_adminer and not class_.is_owner(user):
        logger.error(f"User {user.username} not authorized to delete class {class_id}")
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    # Capture template info BEFORE deletion so we can delete the class's base template VM
    template_info = None
    if class_.template:
        tpl = class_.template
        # Always attempt to delete the class's base template VM to avoid orphaned resources
        template_info = {
            'vmid': tpl.proxmox_vmid,
            'node': tpl.node,
            'cluster_ip': tpl.cluster_ip,
            'db_id': tpl.id,
        }
        logger.info(f"Class {class_id}: will delete base template VM {tpl.proxmox_vmid} (template_id={tpl.id})")

    # Check if force delete (skip VM deletion from Proxmox including template)
    force_delete = request.args.get('force', 'false').lower() == 'true'
    
    if force_delete:
        logger.warning(f"FORCE DELETE: Deleting class {class_id} ({class_.name}) WITHOUT deleting VMs from Proxmox")
        # Just delete from database, don't touch Proxmox
        success, msg, vmids = delete_class(class_id)
        
        if not success:
            logger.error(f"Failed to force delete class {class_id} from database: {msg}")
            return jsonify({"ok": False, "error": msg}), 400
        
        logger.info(f"Class {class_id} force deleted successfully (skipped {len(vmids)} VMs)")
        return jsonify({
            "ok": True,
            "message": "Class deleted (VMs left intact in Proxmox)",
            "deleted_vms": [],
            "failed_vms": [],
            "skipped_vms": vmids
        })
    
    # Normal delete: delete VMs from Proxmox (plus class template VM if applicable)
    logger.info(f"Deleting class {class_id} ({class_.name}) with {len(class_.vm_assignments)} VMs")
    
    # Get VMs to delete BEFORE deleting the class
    # SAFETY CHECK: Only collect VMs that explicitly belong to THIS class
    vm_assignments_to_delete = []
    for assignment in class_.vm_assignments:
        # Verify assignment belongs to this class (should always be true, but check anyway)
        if assignment.class_id != class_id:
            logger.error(f"SAFETY VIOLATION: Assignment {assignment.id} (VMID {assignment.proxmox_vmid}) has class_id={assignment.class_id} but we're deleting class {class_id}!")
            continue
        
        vm_assignments_to_delete.append({
            'vmid': assignment.proxmox_vmid,
            'node': assignment.node,
            'assignment_id': assignment.id,
            'class_id': assignment.class_id
        })
        logger.info(f"Queued VM {assignment.proxmox_vmid} for deletion (assignment_id={assignment.id}, class_id={assignment.class_id})")
    
    logger.info(f"Class {class_id} has {len(vm_assignments_to_delete)} VMs to delete from Proxmox")
    
    # Get cluster_ip from template
    cluster_ip = None
    if class_.template and class_.template.cluster_ip:
        cluster_ip = class_.template.cluster_ip
    
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
        logger.info(f"Deleting VM {vm_info['vmid']} on node {vm_info['node']} (cluster: {cluster_ip}, was in class {vm_info['class_id']}, assignment {vm_info['assignment_id']})")
        vm_success, vm_msg = delete_vm(vm_info['vmid'], vm_info['node'], cluster_ip)
        if vm_success:
            deleted_vms.append(vm_info['vmid'])
            logger.info(f"Successfully deleted VM {vm_info['vmid']} that belonged to class {class_id}")
        else:
            failed_vms.append({'vmid': vm_info['vmid'], 'error': vm_msg})
            logger.warning(f"Failed to delete VM {vm_info['vmid']} from class {class_id}: {vm_msg}")
    
    # Delete template VM after class deletion (foreign key broken) if we captured info and not force deleted
    template_deleted = False
    template_delete_error = None
    if template_info and not force_delete:
        from app.models import Template, db
        try:
            logger.info(f"Deleting class template VM {template_info['vmid']} (template_id={template_info['db_id']})")
            t_success, t_msg = delete_vm(template_info['vmid'], template_info['node'], template_info['cluster_ip'])
            if t_success:
                template_deleted = True
                logger.info(f"Deleted class template VM {template_info['vmid']}")
            else:
                template_delete_error = t_msg
                logger.warning(f"Failed to delete class template VM {template_info['vmid']}: {t_msg}")
            # Remove template DB record if still present
            tpl_row = Template.query.get(template_info['db_id'])
            if tpl_row:
                db.session.delete(tpl_row)
                db.session.commit()
                logger.info(f"Deleted template DB record id={template_info['db_id']}")
        except Exception as e:
            template_delete_error = str(e)
            logger.exception(f"Error deleting class template VM {template_info['vmid']}: {e}")

    logger.info(f"Class deletion complete: {len(deleted_vms)} VMs deleted, {len(failed_vms)} failed; template_deleted={template_deleted}")

    return jsonify({
        "ok": True,
        "message": msg,
        "deleted_vms": deleted_vms,
        "failed_vms": failed_vms,
        "template_deleted": template_deleted,
        "template_vmid": template_info['vmid'] if template_info else None,
        "template_delete_error": template_delete_error
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
    
    if not user.is_adminer and not class_.is_owner(user):
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
    
    if not user.is_adminer and not class_.is_owner(user):
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
    Query params:
    - poll: If 'true', continuously poll until templates are available (max 60s)
    """
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    poll_mode = request.args.get('poll', 'false').lower() == 'true'
    
    # Get templates from Proxmox
    proxmox_templates = list_proxmox_templates(CLASS_CLUSTER_IP)
    
    # If in poll mode and no templates, wait and retry
    if poll_mode and len(proxmox_templates) == 0:
        import time
        max_wait = 60  # seconds
        poll_interval = 2  # seconds
        start_time = time.time()
        
        while time.time() - start_time < max_wait:
            time.sleep(poll_interval)
            proxmox_templates = list_proxmox_templates(CLASS_CLUSTER_IP)
            if len(proxmox_templates) > 0:
                logger.info(f"Templates found after {time.time() - start_time:.1f}s of polling")
                break
    
    # Also get registered templates from database
    class_id = request.args.get('class_id', type=int)
    db_templates = get_available_templates(class_id)
    
    return jsonify({
        "ok": True,
        "proxmox_templates": proxmox_templates,
        "registered_templates": [t.to_dict() for t in db_templates],
        "template_count": len(proxmox_templates)
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


@api_classes_bp.route("/templates/replicate", methods=["POST"])
@login_required
def trigger_template_replication():
    """Trigger template replication to all nodes in background.
    
    Returns immediately with a task ID that can be used to check status.
    """
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    # Start replication in background thread
    from app.services.proxmox_operations import replicate_templates_to_all_nodes
    
    task_id = str(uuid.uuid4())
    
    def run_replication():
        try:
            logger.info(f"[Task {task_id}] Starting template replication")
            replicate_templates_to_all_nodes(CLASS_CLUSTER_IP)
            logger.info(f"[Task {task_id}] Template replication completed")
        except Exception as e:
            logger.exception(f"[Task {task_id}] Template replication failed: {e}")
    
    thread = threading.Thread(target=run_replication, daemon=True)
    thread.start()
    
    return jsonify({
        "ok": True,
        "message": "Template replication started in background",
        "task_id": task_id
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
        
        # Get status from VMInventory (database-first, no Proxmox API call)
        status = get_vm_status_from_inventory(assignment.proxmox_vmid, class_.cluster_ip)
        vm_data = assignment.to_dict()
        vm_data.update(status)
        
        return jsonify({
            "ok": True,
            "vms": [vm_data]
        })
    
    # Teacher/adminer sees all VMs
    if not user.is_adminer and not class_.is_owner(user):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    assignments = get_vm_assignments_for_class(class_id)
    vms = []
    
    for assignment in assignments:
        # Skip teacher VMs (they shouldn't appear in the VM list for students)
        # Check both is_teacher_vm flag AND vm_name pattern for backwards compatibility
        if assignment.is_teacher_vm or (assignment.vm_name and '-teacher' in assignment.vm_name.lower()):
            continue
            
        vm_data = assignment.to_dict()
        # Get status from VMInventory (database-first, no Proxmox API call)
        status = get_vm_status_from_inventory(assignment.proxmox_vmid, class_.cluster_ip)
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
    """Create VM pool for a class using Terraform (much simpler than Python cloning)."""
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    if not user.is_adminer and not class_.is_owner(user):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    data = request.get_json()
    count = data.get('count', 1)
    template_name = data.get('template_name')
    target_node = data.get('target_node')  # Optional: force specific node
    
    if not template_name:
        # Use class template if no specific one provided
        if not class_.template:
            return jsonify({"ok": False, "error": "No template specified and class has no default template"}), 400
        template_name = class_.template.name
        logger.info(f"Using class template '{template_name}' (from class {class_.name})")
    else:
        logger.info(f"Using override template '{template_name}' (from API request)")
    
    # Generate student usernames
    students = [f"student{i}" for i in range(1, count + 1)]
    
    # Capture variables for thread context
    class_id_val = class_.id
    class_name = class_.name
    teacher_id = user.id
    pool_size_val = count
    template_name_captured = template_name
    
    # Generate task ID for progress tracking
    import uuid
    task_id = str(uuid.uuid4())
    start_clone_progress(task_id, count + 1)  # Students + teacher
    
    # Capture Flask app context BEFORE starting thread
    from flask import current_app
    app = current_app._get_current_object()
    
    # Deploy VMs using Terraform in background thread
    import threading
    def _background_terraform_deploy():
        try:
            # Push application context in background thread
            app_ctx = app.app_context()
            app_ctx.push()
            from app.services.proxmox_service import get_proxmox_admin
            from app.models import db, Class
            
            # Re-fetch class in thread context
            class_obj = Class.query.get(class_id_val)
            if not class_obj:
                update_clone_progress(task_id, status="failed", error="Class not found")
                return
            
            # Get template info
            if not class_obj.template:
                update_clone_progress(task_id, status="failed", error="No template assigned to class")
                return
            
            template_obj = class_obj.template
            
            # Step 1: Clone the source template exclusively for this class
            update_clone_progress(task_id, completed=0, message=f"Cloning template '{template_obj.name}' (VMID {template_obj.proxmox_vmid}) for exclusive class use...")
            
            # Find next available VMID
            proxmox = get_proxmox_admin()
            used_vmids = set(r.get('vmid') for r in proxmox.cluster.resources.get(type="vm"))
            class_template_vmid = max(used_vmids) + 1 if used_vmids else 200
            
            # Sanitize class name for Proxmox (alphanumeric and hyphens only)
            import re
            class_prefix = re.sub(r'[^a-z0-9-]', '', class_name.lower().replace(' ', '-').replace('_', '-'))
            if not class_prefix:
                class_prefix = f"class-{class_id_val}"
            
            class_template_name = f"{class_prefix}-template"
            
            # Clone the source template to create class-specific template
            clone_success, clone_msg = clone_vm_from_template(
                template_vmid=template_obj.proxmox_vmid,
                new_vmid=class_template_vmid,
                name=class_template_name,
                node=template_obj.node,
                cluster_ip=CLASS_CLUSTER_IP,
                full_clone=True,  # Full clone to isolate from source
                create_baseline=False,  # No baseline snapshot needed for template
                wait_until_complete=True
            )
            
            if not clone_success:
                update_clone_progress(task_id, status="failed", error=f"Failed to clone template: {clone_msg}")
                return
            
            update_clone_progress(task_id, completed=1, message=f"Class template created: VMID {class_template_vmid}")
            
            # Convert the cloned VM to a template
            convert_success, convert_msg = convert_vm_to_template(
                vmid=class_template_vmid,
                node=template_obj.node,
                cluster_ip=CLASS_CLUSTER_IP
            )
            
            if not convert_success:
                update_clone_progress(task_id, status="failed", error=f"Failed to convert to template: {convert_msg}")
                # Clean up the clone
                delete_vm(class_template_vmid, template_obj.node, CLASS_CLUSTER_IP)
                return
            
            # Register the new class-specific template in database
            # create_template now handles race conditions internally
            class_specific_template, tpl_msg = create_template(
                name=class_template_name,
                proxmox_vmid=class_template_vmid,
                cluster_ip=CLASS_CLUSTER_IP,
                node=template_obj.node,
                created_by_id=teacher_id,
                is_class_template=True,
                class_id=class_id_val,
                original_template_id=template_obj.id
            )
            
            if not class_specific_template:
                logger.error(f"Failed to register class template in DB: {tpl_msg}")
                # Continue anyway - template is created in Proxmox
            
            # Update class to use the new class-specific template
            class_obj.template_id = class_specific_template.id if class_specific_template else None
            db.session.commit()
            
            logger.info(f"Class {class_id_val} now has dedicated template VMID {class_template_vmid}")
            
            # Step 2: Deploy student and teacher VMs from the class-specific template
            update_clone_progress(task_id, completed=1, message=f"Deploying {pool_size_val} student VMs + 2 teacher VMs from class template...")
            
            # Generate student usernames
            students = [f"student{i}" for i in range(1, pool_size_val + 1)]
            
            # DEPRECATED: Terraform deployment removed
            # This code path is no longer functional - use standard class creation instead
            update_clone_progress(task_id, status="failed", 
                                error="Terraform deployment has been removed. Please use standard class creation.")
            return
            
            # Register VMs in database
            student_vms = result.get('student_vms', {})
            teacher_vm = result.get('teacher_vm')
            
            # Register teacher VM
            if teacher_vm:
                assignment, _ = create_vm_assignment(
                    class_id=class_id_val,
                    proxmox_vmid=teacher_vm['id'],
                    node=teacher_vm['node'],
                    is_template_vm=True
                )
                update_clone_progress(task_id, completed=1, message=f"Teacher VM created: {teacher_vm['name']}")
            
            # Register student VMs (unassigned, ready for students to claim)
            registered_count = 0
            for student_name, vm_info in student_vms.items():
                assignment, _ = create_vm_assignment(
                    class_id=class_id_val,
                    proxmox_vmid=vm_info['id'],
                    node=vm_info['node'],
                    is_template_vm=False
                )
                registered_count += 1
                update_clone_progress(
                    task_id,
                    completed=registered_count + 1,
                    message=f"Registered {registered_count}/{len(student_vms)} student VMs"
                )
            
            update_clone_progress(
                task_id,
                status="completed",
                message=f"Successfully deployed {len(student_vms)} student VMs + 1 teacher VM"
            )
            
        except Exception as e:
            logger.exception(f"Terraform deployment failed for class {class_id}: {e}")
            update_clone_progress(task_id, status="failed", error=str(e))
        finally:
            try:
                app_ctx.pop()
            except Exception:
                pass
    
    threading.Thread(target=_background_terraform_deploy, daemon=True).start()
    
    return jsonify({
        "ok": True,
        "message": f"Terraform is deploying {count} student VMs + 1 teacher VM in parallel (much faster than sequential Python cloning).",
        "task_id": task_id,
        "info": "Terraform handles node selection, storage compatibility, VMID allocation, and error handling automatically."
    })


@api_classes_bp.route("/clone/progress/<task_id>", methods=["GET"])
@login_required
def clone_progress_route(task_id: str):
    """Check progress of a VM cloning task."""
    progress = get_clone_progress(task_id)
    return jsonify(progress)

@api_classes_bp.route("/<int:class_id>/promote-editable", methods=["POST"])
@login_required
def promote_editable_to_base_template(class_id: int):
    """Promote the Teacher Editable VM to become the new Class Base Template.

    Steps:
    1) Clone Teacher Editable VM -> convert to template (new base template)
    2) Update class.template_id to new template
    3) Recreate Teacher Template VM from new base template (linked clone -> convert to template)
    4) Update assignments: remove old teacher template assignment, add new one
    """
    from datetime import datetime
    
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]

    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404

    # Ownership check
    if not user.is_adminer and not class_.is_owner(user):
        return jsonify({"ok": False, "error": "Access denied"}), 403

    # Find Teacher Editable VM (is_template_vm=False, preferably assigned to teacher)
    teacher_vm = None
    # First try: find VM assigned to teacher
    for assignment in class_.vm_assignments:
        if not assignment.is_template_vm and assignment.assigned_user_id == class_.teacher_id and assignment.proxmox_vmid:
            teacher_vm = assignment
            break
    
    # Fallback: find any unassigned non-template VM (earliest created)
    if not teacher_vm:
        unassigned = [a for a in class_.vm_assignments if not a.is_template_vm and a.assigned_user_id is None and a.proxmox_vmid]
        if unassigned:
            teacher_vm = sorted(unassigned, key=lambda a: a.created_at or datetime.min)[0]
            logger.info(f"Using unassigned VM {teacher_vm.proxmox_vmid} as teacher editable (not assigned to teacher)")
    
    if not teacher_vm:
        return jsonify({"ok": False, "error": "Teacher Editable VM not found"}), 404

    # Get current class base template (from class_.template)
    current_base_template = class_.template
    if not current_base_template:
        return jsonify({"ok": False, "error": "Class base template not registered"}), 400

    # Capture variables for thread context
    teacher_vm_vmid = teacher_vm.proxmox_vmid
    teacher_vm_node = teacher_vm.node
    user_id = user.id
    current_base_id = current_base_template.id if current_base_template else None
    current_base_vmid = current_base_template.proxmox_vmid if current_base_template else None
    current_base_node = current_base_template.node if current_base_template else None

    # Prepare progress tracking
    import uuid
    task_id = str(uuid.uuid4())
    start_clone_progress(task_id, 2)  # promote -> optional cleanup

    # Capture Flask app context BEFORE starting thread
    from flask import current_app
    app = current_app._get_current_object()

    import threading
    def _background_promote():
        try:
            # Push application context in background thread
            app_ctx = app.app_context()
            app_ctx.push()
            from app.services.proxmox_service import get_proxmox_admin
            from app.models import db, Class, Template, VMAssignment

            class_obj = Class.query.get(class_id)
            if not class_obj:
                update_clone_progress(task_id, status="failed", error="Class not found in DB")
                return

            # Step 1: Clone teacher editable VM to new base template
            update_clone_progress(task_id, completed=0, message="Cloning Teacher Editable VM to new base template...")
            proxmox = get_proxmox_admin()
            used_vmids = set(r.get('vmid') for r in proxmox.cluster.resources.get(type="vm"))
            new_base_vmid = max(used_vmids) + 1 if used_vmids else 600

            # Name conventions
            import re
            class_prefix = re.sub(r'[^a-z0-9-]', '', class_obj.name.lower().replace(' ', '-').replace('_', '-')) or f"class-{class_obj.id}"
            new_base_name = f"{class_prefix}-base-template-v2"

            from app.services.proxmox_operations import clone_vm_from_template, convert_vm_to_template, delete_vm
            from app.services.proxmox_operations import CLASS_CLUSTER_IP

            clone_success, clone_msg = clone_vm_from_template(
                template_vmid=teacher_vm_vmid,
                new_vmid=new_base_vmid,
                name=new_base_name,
                node=teacher_vm_node,
                cluster_ip=CLASS_CLUSTER_IP,
                full_clone=True,
                create_baseline=False,
                wait_until_complete=True
            )

            if not clone_success:
                update_clone_progress(task_id, status="failed", error=f"Clone failed: {clone_msg}")
                return

            convert_success, convert_msg = convert_vm_to_template(
                vmid=new_base_vmid,
                node=teacher_vm_node,
                cluster_ip=CLASS_CLUSTER_IP
            )

            if not convert_success:
                update_clone_progress(task_id, status="failed", error=f"Convert to template failed: {convert_msg}")
                # cleanup clone
                delete_vm(new_base_vmid, teacher_vm_node, CLASS_CLUSTER_IP)
                return

            # Register new base template in DB and update class
            new_tpl, _msg = create_template(
                name=new_base_name,
                proxmox_vmid=new_base_vmid,
                cluster_ip=CLASS_CLUSTER_IP,
                node=teacher_vm_node,
                created_by_id=user_id,
                is_class_template=True,
                class_id=class_id,
                original_template_id=current_base_id
            )

            if new_tpl:
                class_obj.template_id = new_tpl.id
                db.session.commit()
                update_clone_progress(task_id, completed=1, message=f"Promoted editable VM to new base template (VMID {new_base_vmid})")
            else:
                update_clone_progress(task_id, status="failed", error="Failed to register new base template in DB")
                return

            # Step 2: (Simplified) No teacher template recreation – students will clone from the new base
            update_clone_progress(task_id, completed=1, message="New Class Base Template is active. Students should clone from this base.")

            # Step 3: Optional cleanup of old base template VM in Proxmox
            # Best-effort: delete old base template VM to avoid confusion
            try:
                if current_base_vmid and current_base_node:
                    delete_vm(current_base_vmid, current_base_node, CLASS_CLUSTER_IP)
            except Exception:
                pass

            update_clone_progress(task_id, status="completed", message="Promotion complete: new Class Base Template is active.")

        except Exception as e:
            logger.exception(f"Promote editable failed for class {class_id}: {e}")
            update_clone_progress(task_id, status="failed", error=str(e))
        finally:
            try:
                app_ctx.pop()
            except Exception:
                pass

    threading.Thread(target=_background_promote, daemon=True).start()

    return jsonify({
        "ok": True,
        "message": "Promotion started in background. Track progress via task_id.",
        "task_id": task_id
    })


@api_classes_bp.route("/<int:class_id>/redeploy-student-vms", methods=["POST"])
@login_required
def redeploy_student_vms(class_id: int):
    """Redeploy all student VMs from the current class base template.
    
    Deletes all existing student VMs and recreates them from the class base template.
    """
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]

    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404

    # Ownership check
    if not user.is_adminer and not class_.is_owner(user):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    # Get class base template
    if not class_.template:
        return jsonify({"ok": False, "error": "Class has no base template"}), 400
    
    class_base_vmid = class_.template.proxmox_vmid
    class_base_node = class_.template.node
    pool_size = request.get_json().get('pool_size', class_.vm_pool_size or 20)
    
    # Prepare progress tracking
    task_id = str(uuid.uuid4())
    start_clone_progress(task_id, pool_size + 1)  # delete + create
    
    # Capture Flask app context
    from flask import current_app
    app = current_app._get_current_object()
    
    def _background_redeploy():
        try:
            app_ctx = app.app_context()
            app_ctx.push()
            from app.services.proxmox_service import get_proxmox_admin
            import time
            
            class_obj = Class.query.get(class_id)
            if not class_obj:
                update_clone_progress(task_id, status="failed", error="Class not found")
                return
            
            proxmox = get_proxmox_admin()
            
            # Step 1: Delete all existing student VMs (non-template VMs not assigned to teacher)
            update_clone_progress(task_id, completed=0, message="Deleting existing student VMs...")
            student_assignments = [a for a in class_obj.vm_assignments 
                                 if not a.is_template_vm 
                                 and a.assigned_user_id != class_obj.teacher_id
                                 and a.proxmox_vmid]
            
            for assignment in student_assignments:
                try:
                    delete_vm(assignment.proxmox_vmid, assignment.node, CLASS_CLUSTER_IP)
                    logger.info(f"Deleted student VM {assignment.proxmox_vmid}")
                except Exception as e:
                    logger.warning(f"Failed to delete VM {assignment.proxmox_vmid}: {e}")
                
                # Remove assignment from database
                db.session.delete(assignment)
            
            db.session.commit()
            
            # Step 2: Create new student VMs from class base template
            update_clone_progress(task_id, completed=1, message=f"Creating {pool_size} new student VMs...")
            
            import re
            class_prefix = re.sub(r'[^a-z0-9-]', '', class_obj.name.lower().replace(' ', '-').replace('_', '-')) or f"class-{class_obj.id}"
            
            for i in range(1, pool_size + 1):
                used_vmids = set(r.get('vmid') for r in proxmox.cluster.resources.get(type="vm"))
                student_vmid = max(used_vmids) + 1 if used_vmids else (400 + i)
                student_name = f"{class_prefix}-student{i}"
                
                clone_success, clone_msg = clone_vm_from_template(
                    template_vmid=class_base_vmid,
                    new_vmid=student_vmid,
                    name=student_name,
                    node=class_base_node,
                    cluster_ip=CLASS_CLUSTER_IP,
                    full_clone=True,
                    storage='local-lvm',
                    create_baseline=False,
                    wait_until_complete=True
                )
                
                if clone_success:
                    logger.info(f"Student VM {i}/{pool_size} created: {student_name} (VMID {student_vmid})")
                    
                    # Create VM assignment
                    assignment, _ = create_vm_assignment(
                        class_id=class_id,
                        proxmox_vmid=student_vmid,
                        node=class_base_node,
                        is_template_vm=False
                    )
                    
                    update_clone_progress(
                        task_id,
                        completed=1 + i,
                        message=f"Created student VM {i}/{pool_size}: {student_name}"
                    )
                else:
                    logger.warning(f"Failed to create student VM {i}: {clone_msg}")
                    update_clone_progress(
                        task_id,
                        completed=1 + i,
                        message=f"Failed student VM {i}/{pool_size}: {clone_msg}"
                    )
            
            # Auto-assign VMs to waiting students
            assigned_count = auto_assign_vms_to_waiting_students(class_id)
            if assigned_count > 0:
                logger.info(f"Auto-assigned {assigned_count} VMs to waiting students")
            
            update_clone_progress(task_id, status="completed", message=f"Redeployed {pool_size} student VMs successfully")
            
        except Exception as e:
            logger.exception(f"Redeploy student VMs failed for class {class_id}: {e}")
            update_clone_progress(task_id, status="failed", error=str(e))
        finally:
            try:
                app_ctx.pop()
            except Exception:
                pass
    
    threading.Thread(target=_background_redeploy, daemon=True).start()
    
    return jsonify({
        "ok": True,
        "message": "Student VM redeployment started in background",
        "task_id": task_id
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
    
    if not user.is_adminer and not class_.is_owner(user):
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
    
    if not user.is_adminer and not class_.is_owner(user):
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
    
    if not user.is_adminer and not class_.is_owner(user):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    # Get assignment to find Proxmox VMID
    assignment = VMAssignment.query.get(assignment_id)
    
    if not assignment:
        return jsonify({"ok": False, "error": "VM assignment not found"}), 404
    
    # SAFETY CHECK: Verify this assignment belongs to the specified class
    if assignment.class_id != class_id:
        logger.error(f"SAFETY VIOLATION: Attempted to delete assignment {assignment_id} (VMID {assignment.proxmox_vmid}) with class_id={assignment.class_id} from class {class_id}")
        return jsonify({"ok": False, "error": "VM does not belong to this class"}), 403
    
    vmid = assignment.proxmox_vmid
    node = assignment.node
    
    logger.info(f"Deleting VM {vmid} (assignment {assignment_id}) from class {class_id}")
    
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


@api_classes_bp.route("/<int:class_id>/vms/auto-assign", methods=["POST"])
@login_required
def auto_assign_vms_route(class_id: int):
    """Auto-assign available VMs to enrolled students without VMs.
    
    Useful after:
    - Creating new VMs (called automatically in background)
    - Destroying and recreating VMs
    - When students join before VMs are ready
    """
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    if not user.is_adminer and not class_.is_owner(user):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    assigned_count = auto_assign_vms_to_waiting_students(class_id)
    
    return jsonify({
        "ok": True,
        "message": f"Auto-assigned {assigned_count} VMs to waiting students",
        "assigned_count": assigned_count
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
    
    # Get class to extract cluster_ip
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    vm_data = assignment.to_dict()
    # Get status from VMInventory (database-first, no Proxmox API call)
    status = get_vm_status_from_inventory(assignment.proxmox_vmid, class_.cluster_ip)
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
    
    # Prevent starting template VMs
    if assignment.is_template_vm:
        return jsonify({"ok": False, "error": "Cannot start a template VM. Templates must be cloned to regular VMs before they can be started."}), 400
    
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
    
    # Revert to the baseline snapshot created when VM was cloned
    success, msg = revert_vm_to_snapshot(
        vmid=assignment.proxmox_vmid,
        node=assignment.node,
        snapshot_name='baseline'  # Match the snapshot name from clone_vm_from_template
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
    """Push teacher's VM updates to class template.
    
    Clones Teacher VM 1 (editable), converts clone to template,
    and replaces the old Teacher VM 2 (class template).
    """
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    if not user.is_adminer and not class_.is_owner(user):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    # Find Teacher VM 1 (editable, is_template_vm=False)
    teacher_vm = VMAssignment.query.filter_by(
        class_id=class_id,
        is_template_vm=False
    ).filter(
        VMAssignment.proxmox_vmid != None  # Has a VMID (not unassigned student slot)
    ).first()
    
    # Also look for VMs that might be tagged as teacher
    if not teacher_vm:
        # Try to find by checking all VMs and filtering by name pattern
        all_assignments = get_vm_assignments_for_class(class_id)
        for assignment in all_assignments:
            if assignment.user_id is None and not assignment.is_template_vm:
                # This might be teacher VM if it's unassigned and not template
                teacher_vm = assignment
                break
    
    if not teacher_vm:
        return jsonify({"ok": False, "error": "Teacher VM (editable) not found. Cannot push updates."}), 404
    
    # Find old Teacher VM 2 (template, is_template_vm=True)
    old_template_vm = VMAssignment.query.filter_by(
        class_id=class_id,
        is_template_vm=True
    ).first()
    
    if not old_template_vm:
        return jsonify({"ok": False, "error": "Class template VM not found. Cannot replace template."}), 404
    
    logger.info(f"Pushing updates: Cloning Teacher VM {teacher_vm.proxmox_vmid} to replace template VM {old_template_vm.proxmox_vmid}")
    
    # Execute the update process in background
    import threading
    import uuid
    task_id = str(uuid.uuid4())
    start_clone_progress(task_id, 3)  # 3 steps: clone, convert, delete old
    
    def _background_push_updates():
        try:
            from app.services.proxmox_operations import clone_vm_from_template, convert_vm_to_template, delete_vm
            from app.services.proxmox_service import get_proxmox_admin
            
            update_clone_progress(task_id, completed=0, message="Cloning Teacher VM 1...")
            
            # Get next available VMID
            proxmox = get_proxmox_admin()
            used_vmids = set(r.get('vmid') for r in proxmox.cluster.resources.get(type="vm"))
            next_vmid = max(used_vmids) + 1 if used_vmids else 200
            
            # Clone Teacher VM 1 (full clone)
            class_prefix = class_.name.lower().replace(' ', '-')
            new_template_name = f"{class_prefix}-template-new"
            
            success, msg = clone_vm_from_template(
                template_vmid=teacher_vm.proxmox_vmid,
                new_vmid=next_vmid,
                name=new_template_name,
                node=teacher_vm.node,
                cluster_ip=CLASS_CLUSTER_IP,
                full_clone=True,
                create_baseline=False
            )
            
            if not success:
                update_clone_progress(task_id, status="failed", error=f"Clone failed: {msg}")
                return
            
            update_clone_progress(task_id, completed=1, message=f"Teacher VM cloned to VMID {next_vmid}")
            
            # Convert new clone to template
            update_clone_progress(task_id, completed=1, message="Converting clone to template...")
            
            success, msg = convert_vm_to_template(
                vmid=next_vmid,
                node=teacher_vm.node,
                cluster_ip=CLASS_CLUSTER_IP
            )
            
            if not success:
                update_clone_progress(task_id, status="failed", error=f"Template conversion failed: {msg}")
                # Clean up the failed clone
                delete_vm(next_vmid, teacher_vm.node)
                return
            
            update_clone_progress(task_id, completed=2, message=f"New template created (VMID {next_vmid})")
            
            # Delete old template VM from Proxmox
            update_clone_progress(task_id, completed=2, message="Deleting old template...")
            
            old_vmid = old_template_vm.proxmox_vmid
            old_node = old_template_vm.node
            
            delete_success, delete_msg = delete_vm(old_vmid, old_node)
            if not delete_success:
                logger.warning(f"Failed to delete old template VM {old_vmid}: {delete_msg}")
            
            # Update database: Point old template assignment to new VMID
            old_template_vm.proxmox_vmid = next_vmid
            old_template_vm.node = teacher_vm.node
            from app.models import db
            db.session.commit()
            
            update_clone_progress(
                task_id,
                completed=3,
                status="completed",
                message=f"Successfully pushed updates! New template is VMID {next_vmid}. Old template VMID {old_vmid} deleted. Students will now clone from the updated template."
            )
            
        except Exception as e:
            logger.exception(f"Push updates failed for class {class_id}: {e}")
            update_clone_progress(task_id, status="failed", error=str(e))
    
    threading.Thread(target=_background_push_updates, daemon=True).start()
    
    return jsonify({
        "ok": True,
        "message": "Pushing updates in background. Teacher VM 1 will be cloned and converted to replace the class template.",
        "task_id": task_id,
        "teacher_vm": teacher_vm.proxmox_vmid,
        "old_template_vm": old_template_vm.proxmox_vmid
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


# ===========================================================================
# QCOW2-Based VM Deployment (New Overlay-Based Approach)
# ===========================================================================

@api_classes_bp.route("/<int:class_id>/qcow2-deploy", methods=["POST"])
@login_required
def qcow2_deploy_class_vms(class_id: int):
    """
    Deploy VMs for a class using QCOW2 overlay approach.
    
    This is the new, faster VM deployment method that:
    1. Creates teacher VM as a full clone
    2. Creates student VMs as QCOW2 overlays from a class base
    3. Creates VMAssignment records in the database
    
    Request body:
    - pool_size: Number of student VMs to create (default: class.pool_size)
    - memory: Memory per VM in MB (default: 4096)
    - cores: CPU cores per VM (default: 2)
    - auto_start: Whether to start VMs (default: false)
    """
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Ownership check
    if not user.is_adminer and not class_.is_owner(user):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    # Get template info
    if not class_.template:
        return jsonify({"ok": False, "error": "Class has no template assigned"}), 400
    
    template = class_.template
    
    data = request.get_json() or {}
    pool_size = data.get('pool_size', class_.pool_size or 10)
    memory = data.get('memory', 4096)
    cores = data.get('cores', 2)
    auto_start = data.get('auto_start', False)
    
    # Start deployment in background
    task_id = str(uuid.uuid4())
    start_clone_progress(task_id, pool_size + 2)  # teacher + base + students
    
    # Capture Flask app context
    from flask import current_app
    app = current_app._get_current_object()
    
    def _background_qcow2_deploy():
        try:
            app_ctx = app.app_context()
            app_ctx.push()
            
            from app.services.class_vm_service import create_class_vms
            
            update_clone_progress(task_id, completed=0, message="Starting QCOW2-based deployment...")
            
            result = create_class_vms(
                class_id=class_id,
                template_vmid=template.proxmox_vmid,
                template_node=template.node,
                pool_size=pool_size,
                class_name=class_.name,
                teacher_id=class_.teacher_id,
                memory=memory,
                cores=cores,
                auto_start=auto_start,
            )
            
            if result.error:
                update_clone_progress(task_id, status="failed", error=result.error)
            else:
                update_clone_progress(
                    task_id, 
                    status="completed", 
                    message=f"Created {result.successful} student VMs + teacher VM {result.teacher_vmid}"
                )
            
        except Exception as e:
            logger.exception(f"QCOW2 deployment failed for class {class_id}: {e}")
            update_clone_progress(task_id, status="failed", error=str(e))
        finally:
            try:
                app_ctx.pop()
            except Exception:
                pass
    
    threading.Thread(target=_background_qcow2_deploy, daemon=True).start()
    
    return jsonify({
        "ok": True,
        "message": f"QCOW2 deployment started for {pool_size} student VMs + teacher VM",
        "task_id": task_id
    })


@api_classes_bp.route("/<int:class_id>/save-and-deploy", methods=["POST"])
@login_required
def save_and_deploy(class_id: int):
    """
    Save teacher VM changes and deploy to all student VMs.
    
    This uses the QCOW2 overlay approach to:
    1. Stop teacher VM
    2. Export teacher VM to new class base QCOW2
    3. Recreate all student overlays pointing to new base
    
    Student VMs will be reset to the new base state.
    """
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Ownership check
    if not user.is_adminer and not class_.is_owner(user):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    # Find teacher VM
    teacher_assignment = None
    for assignment in class_.vm_assignments:
        if not assignment.is_template_vm and assignment.assigned_user_id == class_.teacher_id:
            teacher_assignment = assignment
            break
    
    if not teacher_assignment:
        return jsonify({"ok": False, "error": "Teacher VM not found"}), 404
    
    # Start save-and-deploy in background
    task_id = str(uuid.uuid4())
    student_count = len([a for a in class_.vm_assignments 
                        if not a.is_template_vm and a.assigned_user_id != class_.teacher_id])
    start_clone_progress(task_id, student_count + 2)
    
    # Capture Flask app context
    from flask import current_app
    app = current_app._get_current_object()
    
    teacher_vmid = teacher_assignment.proxmox_vmid
    teacher_node = teacher_assignment.node
    
    def _background_save_deploy():
        try:
            app_ctx = app.app_context()
            app_ctx.push()
            
            from app.services.class_vm_service import save_and_deploy_teacher_vm
            
            update_clone_progress(task_id, completed=0, message="Exporting teacher VM to new class base...")
            
            success, message, updated_count = save_and_deploy_teacher_vm(
                class_id=class_id,
                teacher_vmid=teacher_vmid,
                node=teacher_node,
            )
            
            if success:
                update_clone_progress(
                    task_id, 
                    status="completed", 
                    message=f"Updated {updated_count} student VMs from teacher VM"
                )
            else:
                update_clone_progress(task_id, status="failed", error=message)
            
        except Exception as e:
            logger.exception(f"Save and deploy failed for class {class_id}: {e}")
            update_clone_progress(task_id, status="failed", error=str(e))
        finally:
            try:
                app_ctx.pop()
            except Exception:
                pass
    
    threading.Thread(target=_background_save_deploy, daemon=True).start()
    
    return jsonify({
        "ok": True,
        "message": f"Save and deploy started - will update {student_count} student VMs",
        "task_id": task_id
    })
