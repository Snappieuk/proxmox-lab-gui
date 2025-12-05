#!/usr/bin/env python3
"""
Class management API routes blueprint.

Handles class CRUD, template listing, VM pool management, and invite links.
"""

import logging
import threading
import uuid

from flask import Blueprint, jsonify, request

from app.models import VMAssignment
from app.services.class_service import (  # Class management; Invite management; Template management
    assign_vm_to_user,
    auto_assign_vms_to_waiting_students,
    create_class,
    create_template,
    delete_class,
    delete_vm_assignment,
    generate_class_invite,
    get_available_templates,
    get_class_by_id,
    get_classes_for_student,
    get_classes_for_teacher,
    get_user_vm_in_class,
    get_vm_assignments_for_class,
    invalidate_class_invite,
    join_class_via_token,
    list_all_classes,
    list_all_users,
    unassign_vm,
    update_class,
    update_user_role,
)
from app.services.clone_progress import (
    get_clone_progress,
    start_clone_progress,
    update_clone_progress,
)
from app.services.proxmox_operations import (
    CLASS_CLUSTER_IP,
    delete_vm,
    get_vm_status_from_inventory,
    list_proxmox_templates,
    revert_vm_to_snapshot,
    start_class_vm,
    stop_class_vm,
)
from app.utils.auth_helpers import get_current_user
from app.utils.decorators import login_required

# NOTE: clone_vm_from_template and convert_vm_to_template are DEPRECATED
# They use qm clone which locks templates. Use disk export workflow instead.


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
    name = (data.get('name') or '').strip()
    description = (data.get('description') or '').strip()
    template_id = data.get('template_id')
    pool_size = data.get('pool_size', 0)
    cpu_cores = data.get('cpu_cores', 2)
    memory_mb = data.get('memory_mb', 2048)
    disk_size_gb = data.get('disk_size_gb', 32)
    deployment_node = (data.get('deployment_node') or '').strip() or None  # Optional: single-node deployment
    deployment_cluster = (data.get('deployment_cluster') or '').strip() or None  # Optional: target cluster
    
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
        disk_size_gb=disk_size_gb,
        deployment_node=deployment_node,
        deployment_cluster=deployment_cluster
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
            from app.config import CLUSTERS
            from app.services.proxmox_service import get_proxmox_admin_for_cluster

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
        # Capture class_id for error handling (class_ from outer scope)
        class_id_for_error = class_.id
        app_ctx = None
        try:
            # Create and push application context for background thread
            app_ctx = app.app_context()
            app_ctx.push()
            
            from app.models import Class, db
            from app.services.class_vm_service import deploy_class_vms

            # Re-fetch class within the app context to get fresh database session
            with app.app_context():
                class_obj = Class.query.get(class_id_for_error)
                if not class_obj:
                    logger.error(f"Class {class_id_for_error} not found in background thread")
                    return
                
                # Store values we need before session closes
                class_name = class_obj.name
                template_id = class_obj.template_id
                pool_size = class_obj.pool_size
            
            # Deploy VMs using QCOW2 overlay cloning (fast)
            logger.info(f"Deploying VMs for class '{class_name}' (template={template_id}, students={pool_size})...")
            
            success, message, vm_info = deploy_class_vms(
                class_id=class_id_for_error,
                num_students=pool_size
            )
            
            if success:
                logger.info(f"Class {class_id_for_error} VM deployment succeeded: {message}")
                logger.info(f"Created: {vm_info.get('teacher_vm_count', 0)} teacher VM, "
                           f"{vm_info.get('template_vm_count', 0)} template VM, "
                           f"{vm_info.get('student_vm_count', 0)} student VMs")
            else:
                logger.error(f"Class {class_id_for_error} VM deployment failed: {message}")
                
        except Exception as e:
            logger.exception(f"Failed to deploy VMs for class {class_id_for_error}: {e}")
        finally:
            # Clean up app context
            if app_ctx:
                try:
                    app_ctx.pop()
                except Exception as ctx_error:
                    logger.error(f"Error popping app context: {ctx_error}")
    
    # Start the background thread to create VMs
    threading.Thread(target=_create_class_vms, daemon=True).start()
    
    # Return immediately
    message = "Class created successfully"
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
    
    # CRITICAL: Do NOT delete the original template VM (class_.template)
    # The template is shared across classes and should never be touched during class deletion
    # Only delete VMs that are explicitly in VMAssignment for THIS class
    
    # Check if force delete (skip VM deletion from Proxmox)
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
        
        # CRITICAL SAFETY CHECK: NEVER delete the original source template
        # Check if this VMID matches the class's source template
        if class_.template and assignment.proxmox_vmid == class_.template.proxmox_vmid:
            logger.error(f"SAFETY VIOLATION: Refusing to delete original template VM {assignment.proxmox_vmid}! This should NEVER be in VMAssignment for a class!")
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
    
    # First, stop all running VMs in parallel (faster than sequential)
    logger.info(f"Stopping all running VMs for class {class_id}...")
    from app.services.proxmox_service import get_proxmox_admin_for_cluster
    from app.config import CLUSTERS
    
    # Get cluster connection
    cluster_id = None
    for cluster in CLUSTERS:
        if cluster["host"] == (cluster_ip or "10.220.15.249"):
            cluster_id = cluster["id"]
            break
    
    if cluster_id:
        try:
            proxmox = get_proxmox_admin_for_cluster(cluster_id)
            
            # FIRST: Query actual nodes for all VMs (they may have been migrated)
            logger.info(f"Querying actual nodes for {len(vm_assignments_to_delete)} VMs...")
            try:
                resources = proxmox.cluster.resources.get(type="vm")
                for vm_info in vm_assignments_to_delete:
                    for r in resources:
                        if int(r.get('vmid', -1)) == int(vm_info['vmid']):
                            actual_node = r.get('node')
                            if actual_node != vm_info['node']:
                                logger.info(f"VM {vm_info['vmid']} migrated from {vm_info['node']} to {actual_node}, updating...")
                                vm_info['node'] = actual_node
                            break
            except Exception as e:
                logger.warning(f"Failed to query cluster resources for node validation: {e}")
            
            # SECOND: Stop all running VMs and wait for them to stop
            logger.info(f"Stopping and waiting for {len(vm_assignments_to_delete)} VMs...")
            for vm_info in vm_assignments_to_delete:
                try:
                    vmid = vm_info['vmid']
                    node = vm_info['node']
                    
                    # Check if VM is running
                    try:
                        status = proxmox.nodes(node).qemu(vmid).status.current.get()
                        if status.get('status') == 'running':
                            logger.info(f"Stopping VM {vmid} on node {node}...")
                            proxmox.nodes(node).qemu(vmid).status.stop.post()
                            
                            # Wait for VM to stop (poll until stopped or timeout)
                            from app.services.vm_core import wait_for_vm_stopped
                            from app.services.ssh_executor import get_ssh_executor_from_config
                            
                            ssh_executor = get_ssh_executor_from_config()
                            stopped = wait_for_vm_stopped(ssh_executor, vmid, timeout=60, poll_interval=2)
                            if stopped:
                                logger.info(f"VM {vmid} stopped successfully")
                            else:
                                logger.warning(f"VM {vmid} did not stop within timeout, proceeding with deletion anyway")
                        else:
                            logger.info(f"VM {vmid} already stopped (status: {status.get('status')})")
                    except Exception as e:
                        logger.warning(f"Failed to stop VM {vmid}: {e}")
                        
                except Exception as e:
                    logger.warning(f"Error during VM {vm_info['vmid']} stop: {e}")
                    
        except Exception as e:
            logger.warning(f"Error during VM stop phase: {e}")
    
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
    
    # CRITICAL: We ONLY delete VMs that are in VMAssignment for this class
    # - Teacher VM (is_teacher_vm=True)
    # - Class-base VM (is_template_vm=True) - the class-specific overlay base
    # - Student VMs (regular assignments)
    # We NEVER delete the original source template (class_.template) which is shared across classes

    logger.info(f"Class deletion complete: {len(deleted_vms)} VMs deleted, {len(failed_vms)} failed")

    return jsonify({
        "ok": True,
        "message": msg,
        "deleted_vms": deleted_vms,
        "failed_vms": failed_vms,
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
    """List available templates for class creation from database.
    
    Database-first architecture: reads from Template table (synced by background daemon).
    
    Query params:
    - cluster_id: Filter templates by cluster ID
    - poll: (deprecated - kept for backwards compatibility, ignored)
    """
    user, error = require_teacher_or_adminer()
    if error:
        return jsonify(error[0]), error[1]
    
    # Get cluster filter from query params
    cluster_id_param = request.args.get('cluster_id')
    cluster_ip = None
    
    if cluster_id_param:
        # Convert cluster_id to cluster_ip
        from app.config import CLUSTERS
        for cluster in CLUSTERS:
            if cluster['id'] == cluster_id_param:
                cluster_ip = cluster['host']
                break
    
    # Query templates from database (database-first architecture)
    from app.models import Template
    query = Template.query.filter_by(is_class_template=False)
    
    if cluster_ip:
        query = query.filter_by(cluster_ip=cluster_ip)
    
    db_templates = query.all()
    
    # Build cluster_ip to cluster_id lookup map
    from app.config import CLUSTERS
    cluster_map = {c['host']: c['id'] for c in CLUSTERS}
    
    # Convert to proxmox_templates format for backwards compatibility
    proxmox_templates = []
    for t in db_templates:
        proxmox_templates.append({
            'vmid': t.proxmox_vmid,
            'name': t.name,
            'node': t.node,
            'cluster_ip': t.cluster_ip,
            'cluster_id': cluster_map.get(t.cluster_ip),  # Look up cluster_id from template's cluster_ip
            'cpu_cores': t.cpu_cores,
            'cpu_sockets': t.cpu_sockets,
            'memory_mb': t.memory_mb,
            'disk_size_gb': t.disk_size_gb,
            'os_type': t.os_type,
            'last_verified_at': t.last_verified_at.isoformat() if t.last_verified_at else None,
        })
    
    # Also get registered templates (legacy compatibility)
    class_id = request.args.get('class_id', type=int)
    registered_templates = get_available_templates(class_id)
    
    return jsonify({
        "ok": True,
        "proxmox_templates": proxmox_templates,
        "registered_templates": [t.to_dict() for t in registered_templates],
        "template_count": len(proxmox_templates),
        "source": "database"  # Indicate data comes from database cache
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
    
    # Get cluster_ip from class template (used by both students and teachers)
    cluster_ip = None
    if class_.template and class_.template.cluster_ip:
        cluster_ip = class_.template.cluster_ip
    else:
        cluster_ip = "10.220.15.249"  # Default fallback
    
    # Check access
    if not user.is_adminer and not user.is_teacher:
        # Student can only see their own VM
        assignment = get_user_vm_in_class(user.id, class_id)
        if not assignment:
            return jsonify({"ok": False, "error": "Access denied"}), 403
        
        # Get status from VMInventory (database-first, no Proxmox API call)
        status = get_vm_status_from_inventory(assignment.proxmox_vmid, cluster_ip)
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
        status = get_vm_status_from_inventory(assignment.proxmox_vmid, cluster_ip)
        vm_data.update(status)
        
        # Debug logging to trace MAC address data flow
        logger.info(f"VM {assignment.proxmox_vmid}: assignment.mac={assignment.mac_address}, status.mac={status.get('mac')}, vm_data.mac={vm_data.get('mac')}")
        
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
    
    # Use the modern disk export + overlay deployment method
    # This creates VM shells via SSH and uses QCOW2 overlays for efficient disk cloning
    from app.services.class_vm_service import deploy_class_vms
    
    logger.info(f"Deploying {count} student VMs for class {class_.name} using disk export + overlay method")
    
    # Deploy VMs (creates teacher VM, class-base template, and student overlays)
    success, message, vm_info = deploy_class_vms(
        class_id=class_.id,
        num_students=count
    )
    
    if not success:
        logger.error(f"Failed to deploy VMs for class {class_.id}: {message}")
        return jsonify({"ok": False, "error": message}), 500
    
    logger.info(f"Successfully deployed VMs for class {class_.id}: {message}")
    
    return jsonify({
        "ok": True,
        "message": message,
        "vm_info": vm_info
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
    """DEPRECATED: This endpoint uses qm clone which locks templates and is no longer supported.
    
    Use the new disk-export workflow instead:
    - /api/classes/<id>/publish-template - Push teacher VM changes to all students
    """
    return jsonify({
        "ok": False,
        "error": f"This endpoint is deprecated and uses qm clone (template locking). Use /api/classes/{class_id}/publish-template instead.",
        "deprecated": True,
        "replacement_endpoint": f"/api/classes/{class_id}/publish-template"
    }), 410


@api_classes_bp.route("/<int:class_id>/redeploy-student-vms", methods=["POST"])
@login_required
def redeploy_student_vms(class_id: int):
    """DEPRECATED: This endpoint uses qm clone which locks templates and is no longer supported.
    
    Use the new disk-export workflow instead:
    - /api/classes/<id>/publish-template - Recreates all student VMs from teacher VM
    """
    return jsonify({
        "ok": False,
        "error": f"This endpoint is deprecated and uses qm clone (template locking). Use /api/classes/{class_id}/publish-template instead.",
        "deprecated": True,
        "replacement_endpoint": f"/api/classes/{class_id}/publish-template"
    }), 410


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
    
    # Get cluster_ip from class template
    cluster_ip = None
    if class_.template and class_.template.cluster_ip:
        cluster_ip = class_.template.cluster_ip
    else:
        cluster_ip = "10.220.15.249"  # Default fallback
    
    vm_data = assignment.to_dict()
    # Get status from VMInventory (database-first, no Proxmox API call)
    status = get_vm_status_from_inventory(assignment.proxmox_vmid, cluster_ip)
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
    """DEPRECATED: This endpoint uses qm clone which locks templates and is no longer supported.
    
    Use the new disk-export workflow instead:
    - /api/classes/<id>/publish-template - Push teacher VM changes using disk copy
    """
    return jsonify({
        "ok": False,
        "error": f"This endpoint is deprecated and uses qm clone (template locking). Use /api/classes/{class_id}/publish-template instead.",
        "deprecated": True,
        "replacement_endpoint": f"/api/classes/{class_id}/publish-template"
    }), 410


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
