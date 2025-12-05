#!/usr/bin/env python3
"""
Class Template Management API endpoints.

Handles template operations: reimage, save, push.
"""

import logging

from flask import Blueprint, jsonify, session

from app.models import Template, db
from app.services.class_service import get_class_by_id, get_user_by_username
from app.services.proxmox_operations import (
    delete_vm,
    get_vm_status,
    start_class_vm,
    stop_class_vm,
)
from app.services.proxmox_service import get_proxmox_admin_for_cluster
from app.utils.decorators import login_required

logger = logging.getLogger(__name__)

api_class_template_bp = Blueprint('api_class_template', __name__, url_prefix='/api/classes')


def require_teacher_or_admin(class_id: int):
    """Verify user is teacher of class or admin."""
    username = session.get('user', '').split('@')[0]
    user = get_user_by_username(username)
    
    if not user:
        return None, jsonify({'ok': False, 'error': 'User not found'}), 404
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return None, jsonify({'ok': False, 'error': 'Class not found'}), 404
    
    if not user.is_adminer and class_.teacher_id != user.id:
        return None, jsonify({'ok': False, 'error': 'Permission denied'}), 403
    
    return class_, None, None


@api_class_template_bp.route("/<int:class_id>/template/info")
@login_required
def get_template_info(class_id: int):
    """Get template VM status and info (returns TEACHER USABLE VM, not the Proxmox template)."""
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    # Check if class creation is still in progress
    if hasattr(class_, 'clone_task_id') and class_.clone_task_id:
        from app.services.clone_progress import get_clone_progress
        progress = get_clone_progress(class_.clone_task_id)
        if progress.get('status') == 'running':
            return jsonify({
                'ok': False,
                'error': 'Class is still being created',
                'progress': progress
            }), 202  # 202 Accepted - still processing
    
    # Find the teacher usable VM (is_template_vm=False, assigned to teacher)
    teacher_vm = None
    for assignment in class_.vm_assignments:
        if not assignment.is_template_vm and assignment.assigned_user_id == class_.teacher_id:
            teacher_vm = assignment
            break
    
    if not teacher_vm:
        # Try to find any unassigned non-template VM
        for assignment in class_.vm_assignments:
            if not assignment.is_template_vm and assignment.assigned_user_id is None:
                teacher_vm = assignment
                break
    
    if not teacher_vm or not teacher_vm.proxmox_vmid:
        return jsonify({'ok': False, 'error': 'Teacher usable VM not yet created. Class creation may still be in progress.'}), 404
    
    # Use the VMAssignment data instead of Template data
    vmid = teacher_vm.proxmox_vmid
    node = teacher_vm.node
    
    try:
        logger.info(f"Fetching VM config for teacher usable VM: class_id={class_id}, vmid={vmid}, node={node}")
        
        # Get cluster_ip from the class's template, or use default cluster
        cluster_ip = class_.template.cluster_ip if class_.template else None
        if not cluster_ip:
            # For template-less classes, use the first available cluster
            from app.config import CLUSTERS
            if CLUSTERS:
                cluster_ip = CLUSTERS[0]["host"]
                logger.info(f"Template-less class, using default cluster: {cluster_ip}")
            else:
                return jsonify({'ok': False, 'error': 'No clusters configured'}), 500
        
        proxmox = get_proxmox_admin_for_cluster(cluster_ip)
        
        # If node is missing or invalid, fetch it from Proxmox
        if not node or node == "qemu" or node == "None":
            logger.warning(f"VMAssignment {teacher_vm.id} has invalid/missing node: '{node}'. Fetching from Proxmox...")
            try:
                # Get all VMs from cluster to find the node
                resources = proxmox.cluster.resources.get(type="vm")
                for r in resources:
                    if r.get('vmid') == vmid:
                        node = r.get('node')
                        logger.info(f"Found node '{node}' for VMID {vmid}")
                        # Update assignment with correct node
                        teacher_vm.node = node
                        db.session.commit()
                        break
                
                if not node or node == "qemu" or node == "None":
                    logger.error(f"Could not find valid node for VMID {vmid}")
                    return jsonify({
                        "ok": False, 
                        "error": f"Teacher VM {vmid} not found in Proxmox. It may have been deleted."
                    }), 404
            except Exception as e:
                logger.error(f"Failed to fetch node from Proxmox: {e}")
                return jsonify({
                    "ok": False, 
                    "error": "Cannot access teacher VM. It may have been deleted or the cluster is unreachable."
                }), 500
        
        vm_config = proxmox.nodes(node).qemu(vmid).config.get()

        # Get VM status from Proxmox (after node is validated)
        vm_status = get_vm_status(vmid, node, cluster_ip)

        # Extract MAC address from network config
        mac_address = None
        for key in vm_config.keys():
            if key.startswith('net'):
                net_value = vm_config[key]
                if '=' in net_value:
                    parts = net_value.split(',')
                    for part in parts:
                        if '=' in part:
                            k, v = part.split('=', 1)
                            if k.strip().lower() in ('macaddr', 'hwaddr'):
                                mac_address = v.strip()
                                break
                if mac_address:
                    break

        # Attempt fast IP discovery for teacher usable VM
        ip_address = None
        rdp_available = False
        try:
            from app.services import proxmox_client as pc

            # Use existing internal lookup (guest agent if running)
            if vm_status == 'running':
                ip_address = pc._lookup_vm_ip(node, vmid, 'qemu')  # private helper
                if not ip_address and mac_address:
                    # Fallback: synchronous ARP scan for this single MAC
                    from app.config import ARP_SUBNETS, CLUSTERS
                    from app.services.arp_scanner import discover_ips_via_arp
                    cluster_id = None
                    for c in CLUSTERS:
                        if c['host'] == cluster_ip:
                            cluster_id = c['id']
                            break
                    if cluster_id:
                        composite_key = f"{cluster_id}:{vmid}"
                        discovered = discover_ips_via_arp({composite_key: mac_address}, subnets=ARP_SUBNETS, background=False)
                        ip_address = discovered.get(composite_key)
                if ip_address:
                    # Basic RDP availability heuristic: Windows category or port open
                    # We don't have ostype here; attempt port check
                    rdp_available = pc.has_rdp_port_open(ip_address)
        except Exception as ip_err:
            logger.debug(f"Teacher VM IP discovery failed: {ip_err}")

        return jsonify({
            'ok': True,
            'template': {
                'id': teacher_vm.id,
                'proxmox_vmid': vmid,
                'node': node,
                'cluster_ip': cluster_ip,
                'name': vm_config.get('name', f'VM-{vmid}'),
                'status': vm_status,
                'mac_address': mac_address,
                'ip': ip_address,
                'rdp_available': rdp_available
            }
        })
    except Exception as e:
        logger.exception(f"Failed to get template info: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_class_template_bp.route("/<int:class_id>/template/start", methods=['POST'])
@login_required
def start_template(class_id: int):
    """Start the teacher usable VM (not the Proxmox template)."""
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    # Find the teacher usable VM (is_template_vm=False, assigned to teacher)
    teacher_vm = None
    for assignment in class_.vm_assignments:
        if not assignment.is_template_vm and assignment.assigned_user_id == class_.teacher_id:
            teacher_vm = assignment
            break
    
    if not teacher_vm:
        # Try to find any unassigned non-template VM
        for assignment in class_.vm_assignments:
            if not assignment.is_template_vm and assignment.assigned_user_id is None:
                teacher_vm = assignment
                break
    
    if not teacher_vm or not teacher_vm.proxmox_vmid:
        return jsonify({'ok': False, 'error': 'Teacher usable VM not found'}), 404
    
    try:
        vmid = teacher_vm.proxmox_vmid
        node = teacher_vm.node
        cluster_ip = class_.template.cluster_ip if class_.template else None
        
        if not cluster_ip:
            # For template-less classes, use the first available cluster
            from app.config import CLUSTERS
            if CLUSTERS:
                cluster_ip = CLUSTERS[0]["host"]
                logger.info(f"Template-less class, using default cluster: {cluster_ip}")
            else:
                return jsonify({'ok': False, 'error': 'No clusters configured'}), 500
        
        if not node:
            # Try to find the VM's node via cluster resources
            try:
                from app.services.proxmox_service import get_proxmox_admin_for_cluster
                
                proxmox = get_proxmox_admin_for_cluster(cluster_ip)
                resources = proxmox.cluster.resources.get(type="vm")
                for r in resources:
                    if r.get('vmid') == vmid:
                        node = r.get('node')
                        break
            except Exception as e:
                logger.warning(f"Failed to auto-detect node for VM {vmid}: {e}")
        
        if not node:
            return jsonify({'ok': False, 'error': 'Could not determine VM node. Please check VM configuration.'}), 400
        
        success, msg = start_class_vm(vmid, node, cluster_ip)
        
        if success:
            return jsonify({'ok': True, 'message': 'Teacher VM started'})
        else:
            return jsonify({'ok': False, 'error': msg or 'Failed to start teacher VM'}), 500
    except Exception as e:
        logger.exception(f"Failed to start teacher VM: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_class_template_bp.route("/<int:class_id>/template/stop", methods=['POST'])
@login_required
def stop_template(class_id: int):
    """Stop the teacher usable VM (not the Proxmox template)."""
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    # Find the teacher usable VM (is_template_vm=False, assigned to teacher)
    teacher_vm = None
    for assignment in class_.vm_assignments:
        if not assignment.is_template_vm and assignment.assigned_user_id == class_.teacher_id:
            teacher_vm = assignment
            break
    
    if not teacher_vm:
        # Try to find any unassigned non-template VM
        for assignment in class_.vm_assignments:
            if not assignment.is_template_vm and assignment.assigned_user_id is None:
                teacher_vm = assignment
                break
    
    if not teacher_vm or not teacher_vm.proxmox_vmid:
        return jsonify({'ok': False, 'error': 'Teacher usable VM not found'}), 404
    
    try:
        vmid = teacher_vm.proxmox_vmid
        node = teacher_vm.node
        cluster_ip = class_.template.cluster_ip if class_.template else None
        
        if not cluster_ip:
            # For template-less classes, use the first available cluster
            from app.config import CLUSTERS
            if CLUSTERS:
                cluster_ip = CLUSTERS[0]["host"]
                logger.info(f"Template-less class, using default cluster: {cluster_ip}")
            else:
                return jsonify({'ok': False, 'error': 'No clusters configured'}), 500
        
        if not node:
            # Try to find the VM's node via cluster resources
            try:
                from app.services.proxmox_service import get_proxmox_admin_for_cluster
                
                proxmox = get_proxmox_admin_for_cluster(cluster_ip)
                resources = proxmox.cluster.resources.get(type="vm")
                for r in resources:
                    if r.get('vmid') == vmid:
                        node = r.get('node')
                        break
            except Exception as e:
                logger.warning(f"Failed to auto-detect node for VM {vmid}: {e}")
        
        if not node:
            return jsonify({'ok': False, 'error': 'Could not determine VM node. Please check VM configuration.'}), 400
        
        success, msg = stop_class_vm(vmid, node, cluster_ip)
        
        if success:
            return jsonify({'ok': True, 'message': 'Teacher VM stopped'})
        else:
            return jsonify({'ok': False, 'error': msg or 'Failed to stop teacher VM'}), 500
    except Exception as e:
        logger.exception(f"Failed to stop teacher VM: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_class_template_bp.route("/<int:class_id>/template/reimage", methods=['POST'])
@login_required
def reimage_template(class_id: int):
    """Delete current template VM and recreate from original template (fresh copy)."""
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    if not class_.template:
        return jsonify({'ok': False, 'error': 'No template assigned'}), 404
    
    try:
        from app.models import db
        from app.services.proxmox_operations import clone_template_for_class, delete_vm
        
        template = class_.template
        old_vmid = template.proxmox_vmid
        
        # Get original template if this template was cloned
        original_template_id = template.original_template_id
        if not original_template_id:
            return jsonify({'ok': False, 'error': 'No original template found for reimage'}), 404
        
        original_template = Template.query.get(original_template_id)
        if not original_template:
            return jsonify({'ok': False, 'error': 'Original template not found'}), 404
        
        # SAFETY CHECK: Verify this is actually the class's template
        if template.id != class_.template_id:
            logger.error(f"SAFETY VIOLATION: Template {template.id} (VMID {template.proxmox_vmid}) is not the template for class {class_id}")
            return jsonify({'ok': False, 'error': 'Template does not belong to this class'}), 403
        
        # Delete current template VM
        logger.info(f"Deleting current template VM {old_vmid} for class {class_id} reimage (will recreate from original template {original_template.proxmox_vmid})")
        delete_success, delete_msg = delete_vm(old_vmid, template.cluster_ip)
        
        if not delete_success:
            return jsonify({'ok': False, 'error': f'Failed to delete old template: {delete_msg}'}), 500
        
        # Clone from original template again
        clone_success, new_vmid, clone_msg = clone_template_for_class(
            original_template.proxmox_vmid,
            original_template.node,
            f"{class_.name}-template-reimaged",
            original_template.cluster_ip
        )
        
        if clone_success and new_vmid:
            # Update template record with new VMID
            template.proxmox_vmid = new_vmid
            db.session.commit()
            
            logger.info(f"Re-imaged template: deleted {old_vmid}, created {new_vmid} from original")
            return jsonify({'ok': True, 'message': f'Template re-imaged successfully (new VMID: {new_vmid})'})
        else:
            return jsonify({'ok': False, 'error': f'Failed to clone new template: {clone_msg}'}), 500
    except Exception as e:
        logger.exception(f"Failed to reimage template: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_class_template_bp.route("/<int:class_id>/template/save", methods=['POST'])
@login_required
def save_template(class_id: int):
    """Convert current working VM to a template (Proxmox template conversion)."""
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    if not class_.template:
        return jsonify({'ok': False, 'error': 'No template assigned'}), 404
    
    try:
        from app.services.proxmox_operations import (
            convert_vm_to_template,
            stop_class_vm,
        )
        
        template = class_.template
        
        # Stop the VM first (required for template conversion)
        logger.info(f"Stopping VM {template.proxmox_vmid} before template conversion")
        stop_class_vm(template.proxmox_vmid, template.cluster_ip)
        
        # Wait a moment for shutdown
        import time
        time.sleep(3)
        
        # Convert to template
        success, msg = convert_vm_to_template(
            template.proxmox_vmid,
            template.node,
            template.cluster_ip
        )
        
        if success:
            logger.info(f"Converted VM {template.proxmox_vmid} to template for class {class_.name}")
            return jsonify({'ok': True, 'message': 'VM converted to template successfully'})
        else:
            return jsonify({'ok': False, 'error': f'Failed to convert to template: {msg}'}), 500
    except Exception as e:
        logger.exception(f"Failed to save template: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_class_template_bp.route("/<int:class_id>/template/push", methods=['POST'])
@login_required
def push_template(class_id: int):
    """Push template to student VMs only (preserve teacher/editable and template VMs).
    
    Deletes only student VMs and recreates them from the current class base template.
    """
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    if not class_.template:
        return jsonify({'ok': False, 'error': 'No template assigned'}), 404
    
    try:
        from app.config import CLUSTERS
        
        template = class_.template
        assignments = list(class_.vm_assignments)  # materialize
        
        results = []
        errors = []
        deleted_count = 0
        created_count = 0
        
        # Resolve cluster id
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == template.cluster_ip:
                cluster_id = cluster["id"]
                break
        if not cluster_id:
            return jsonify({'ok': False, 'error': 'Cluster not found'}), 500
        
        # Log current assignments
        assignment_vmids = {a.proxmox_vmid for a in assignments}
        logger.info(f"Class {class_id} has {len(assignment_vmids)} VMs in DB: {sorted(assignment_vmids)}")
        
        # Determine protected assignments
        protected = []
        # Always protect template reference VMs (if any are tracked as assignments)
        protected.extend([a for a in assignments if a.is_template_vm])
        
        # Protect teacher's editable VM if assigned to the teacher
        teacher_owned = [a for a in assignments if (not a.is_template_vm and a.assigned_user_id == class_.teacher_id)]
        if teacher_owned:
            protected.extend(teacher_owned)
        else:
            # Fallback: protect earliest created unassigned non-template VM as teacher editable
            unassigned_non_template = [a for a in assignments if (not a.is_template_vm and a.assigned_user_id is None)]
            if unassigned_non_template:
                keep = sorted(unassigned_non_template, key=lambda a: a.created_at or 0)[0]
                protected.append(keep)
                logger.info(f"Protecting presumed teacher editable VMID {keep.proxmox_vmid}")
        
        protected_ids = {a.id for a in protected}
        deletable = [a for a in assignments if a.id not in protected_ids]
        
        logger.info(f"Will preserve {len(protected)} VM(s): {[a.proxmox_vmid for a in protected]}")
        logger.info(f"Will recreate {len(deletable)} student VM(s): {[a.proxmox_vmid for a in deletable]}")
        
        if not deletable:
            return jsonify({
                'ok': True,
                'message': 'No student VMs to recreate; nothing changed.',
                'deleted': 0,
                'created': 0,
                'results': [],
                'errors': []
            })
        
        # Delete only deletable VMs and their assignment records
        for assignment in deletable:
            try:
                logger.info(f"Deleting student VM {assignment.proxmox_vmid} on node {assignment.node} (assignment_id={assignment.id})")
                delete_success, delete_msg = delete_vm(assignment.proxmox_vmid, assignment.node, template.cluster_ip)
                if delete_success:
                    deleted_count += 1
                    db.session.delete(assignment)
                    results.append(f"Deleted VM {assignment.proxmox_vmid}")
                else:
                    logger.warning(f"Failed to delete VM {assignment.proxmox_vmid}: {delete_msg}")
                    errors.append(f"Failed to delete VM {assignment.proxmox_vmid}: {delete_msg}")
            except Exception as e:
                logger.warning(f"Error deleting VM {assignment.proxmox_vmid}: {e}")
                errors.append(f"Error deleting VM {assignment.proxmox_vmid}: {str(e)}")
        
        db.session.commit()
        
        # Recreate student VMs using disk copy approach (same as initial class creation)
        from app.services.class_vm_service import recreate_student_vms_from_template
        clone_count = len(deletable)
        logger.info(f"Creating {clone_count} new student VM(s) from template {template.proxmox_vmid}")
        
        success, message, new_vmids = recreate_student_vms_from_template(
            class_id=class_.id,
            template_vmid=template.proxmox_vmid,
            template_node=template.node,
            count=clone_count,
            class_name=class_.name
        )
        
        if not success:
            errors.append(f"Failed to recreate VMs: {message}")
        else:
            created_count = len(new_vmids)
            results.extend([f"Created new VM {vmid}" for vmid in new_vmids])
        
        db.session.commit()
        
        logger.info(f"Template push complete for class {class_.name}: deleted {deleted_count}, created {created_count}")
        return jsonify({
            'ok': True,
            'message': f'Template pushed: deleted {deleted_count} student VM(s), created {created_count} new VM(s)',
            'deleted': deleted_count,
            'created': created_count,
            'results': results,
            'errors': errors
        })
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Failed to push template: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500
