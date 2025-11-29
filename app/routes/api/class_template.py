#!/usr/bin/env python3
"""
Class Template Management API endpoints.

Handles template operations: reimage, save, push.
"""

import logging

from flask import Blueprint, jsonify, request, session

from app.utils.decorators import login_required
from app.services.class_service import get_class_by_id, get_user_by_username
from app.services.proxmox_operations import (
    revert_vm_to_snapshot,
    create_vm_snapshot,
    clone_vm_from_template,
    delete_vm,
    get_vm_status,
    start_class_vm,
    stop_class_vm
)
from app.services.proxmox_service import get_proxmox_admin_for_cluster
from app.models import db, VMAssignment, Template

logger = logging.getLogger(__name__)

api_class_template_bp = Blueprint('api_class_template', __name__, url_prefix='/api/class')


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
    """Get template VM status and info."""
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    if not class_.template:
        return jsonify({'ok': False, 'error': 'No template assigned to class'}), 404
    
    template = class_.template
    
    try:
        # Get node from Proxmox if not set in template
        node = template.node
        logger.info(f"Fetching VM config for template_id={template.id}, vmid={template.proxmox_vmid}, node={node}")
        
        proxmox = get_proxmox_admin_for_cluster(template.cluster_ip)
        
        # If node is missing or invalid, fetch it from Proxmox
        if not node or node == "qemu" or node == "None":
            logger.warning(f"Template {template.id} has invalid/missing node: '{node}'. Fetching from Proxmox...")
            try:
                # Get all VMs from cluster to find the node
                resources = proxmox.cluster.resources.get(type="vm")
                for r in resources:
                    if r.get('vmid') == template.proxmox_vmid:
                        node = r.get('node')
                        logger.info(f"Found node '{node}' for VMID {template.proxmox_vmid}")
                        # Update template with correct node
                        template.node = node
                        db.session.commit()
                        break
                
                if not node or node == "qemu" or node == "None":
                    logger.error(f"Could not find valid node for VMID {template.proxmox_vmid}")
                    return jsonify({
                        "ok": False, 
                        "error": f"Template VM {template.proxmox_vmid} not found in Proxmox. It may have been deleted."
                    }), 404
            except Exception as e:
                logger.error(f"Failed to fetch node from Proxmox: {e}")
                return jsonify({
                    "ok": False, 
                    "error": f"Cannot access template VM. It may have been deleted or the cluster is unreachable."
                }), 500
        
        vm_config = proxmox.nodes(node).qemu(template.proxmox_vmid).config.get()

        # Get VM status from Proxmox (after node is validated)
        vm_status = get_vm_status(template.proxmox_vmid, node, template.cluster_ip)

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

        # Attempt fast IP discovery for template VM
        ip_address = None
        rdp_available = False
        try:
            from app.services import proxmox_client as pc
            # Use existing internal lookup (guest agent if running)
            if vm_status == 'running':
                ip_address = pc._lookup_vm_ip(node, template.proxmox_vmid, 'qemu')  # private helper
                if not ip_address and mac_address:
                    # Fallback: synchronous ARP scan for this single MAC
                    from app.services.arp_scanner import discover_ips_via_arp
                    from app.config import ARP_SUBNETS, CLUSTERS
                    cluster_id = None
                    for c in CLUSTERS:
                        if c['host'] == template.cluster_ip:
                            cluster_id = c['id']
                            break
                    if cluster_id:
                        composite_key = f"{cluster_id}:{template.proxmox_vmid}"
                        discovered = discover_ips_via_arp({composite_key: mac_address}, subnets=ARP_SUBNETS, background=False)
                        ip_address = discovered.get(composite_key)
                if ip_address:
                    # Basic RDP availability heuristic: Windows category or port open
                    # We don't have ostype here; attempt port check
                    rdp_available = pc.has_rdp_port_open(ip_address)
        except Exception as ip_err:
            logger.debug(f"Template IP discovery failed: {ip_err}")

        return jsonify({
            'ok': True,
            'template': {
                **template.to_dict(),
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
    """Start the template VM."""
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    if not class_.template:
        return jsonify({'ok': False, 'error': 'No template assigned'}), 404
    
    try:
        template = class_.template
        success = start_class_vm(template.proxmox_vmid, template.cluster_ip)
        
        if success:
            return jsonify({'ok': True, 'message': 'Template VM started'})
        else:
            return jsonify({'ok': False, 'error': 'Failed to start template VM'}), 500
    except Exception as e:
        logger.exception(f"Failed to start template: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_class_template_bp.route("/<int:class_id>/template/stop", methods=['POST'])
@login_required
def stop_template(class_id: int):
    """Stop the template VM."""
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    if not class_.template:
        return jsonify({'ok': False, 'error': 'No template assigned'}), 404
    
    try:
        template = class_.template
        success = stop_class_vm(template.proxmox_vmid, template.cluster_ip)
        
        if success:
            return jsonify({'ok': True, 'message': 'Template VM stopped'})
        else:
            return jsonify({'ok': False, 'error': 'Failed to stop template VM'}), 500
    except Exception as e:
        logger.exception(f"Failed to stop template: {e}")
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
        from app.services.proxmox_operations import delete_vm, clone_template_for_class
        from app.models import db
        
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
        from app.services.proxmox_operations import stop_class_vm, convert_vm_to_template
        
        template = class_.template
        
        # Stop the VM first (required for template conversion)
        logger.info(f"Stopping VM {template.proxmox_vmid} before template conversion")
        stop_success = stop_class_vm(template.proxmox_vmid, template.cluster_ip)
        
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
    """Push template to all VMs in class (delete old VMs and re-clone from new template).
    
    This deletes all existing VMs and creates fresh ones from the current template.
    Each new VM gets a baseline snapshot for the reimage functionality.
    """
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    if not class_.template:
        return jsonify({'ok': False, 'error': 'No template assigned'}), 404
    
    try:
        from app.services.proxmox_operations import sanitize_vm_name
        import time
        
        template = class_.template
        assignments = class_.vm_assignments
        
        results = []
        errors = []
        deleted_count = 0
        created_count = 0
        
        # Get cluster connection
        cluster_id = None
        from app.config import CLUSTERS
        for cluster in CLUSTERS:
            if cluster["host"] == template.cluster_ip:
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return jsonify({'ok': False, 'error': 'Cluster not found'}), 500
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # SAFETY CHECK: Verify all assignments belong to this class
        assignment_vmids = {a.proxmox_vmid for a in assignments}
        logger.info(f"Class {class_id} has {len(assignment_vmids)} VMs in database: {sorted(assignment_vmids)}")
        
        # Step 1: Delete ONLY VMs that belong to this class (via VMAssignment records)
        logger.info(f"Deleting {len(assignments)} existing VMs for class {class_.name} (class_id={class_id})")
        for assignment in assignments:
            # Double-check this assignment belongs to this class
            if assignment.class_id != class_id:
                logger.error(f"SAFETY VIOLATION: Assignment {assignment.id} (VMID {assignment.proxmox_vmid}) does not belong to class {class_id}!")
                errors.append(f"VM {assignment.proxmox_vmid} does not belong to this class (skipped)")
                continue
            
            try:
                logger.info(f"Deleting VM {assignment.proxmox_vmid} (assignment_id={assignment.id}, class_id={assignment.class_id})")
                delete_success = delete_vm(assignment.proxmox_vmid, template.cluster_ip)
                if delete_success:
                    deleted_count += 1
                    logger.info(f"Successfully deleted VM {assignment.proxmox_vmid} from class {class_id}")
                else:
                    logger.warning(f"Failed to delete VM {assignment.proxmox_vmid} from class {class_id}")
                    errors.append(f"Failed to delete VM {assignment.proxmox_vmid}")
            except Exception as e:
                logger.warning(f"Error deleting VM {assignment.proxmox_vmid} from class {class_id}: {e}")
                errors.append(f"Error deleting VM {assignment.proxmox_vmid}: {str(e)}")
        
        # Step 2: Delete all assignment records (will recreate them)
        vm_count = len(assignments)
        for assignment in assignments:
            db.session.delete(assignment)
        db.session.commit()
        
        logger.info(f"Deleted {deleted_count}/{vm_count} VMs and cleared all assignments")
        
        # Step 3: Clone new VMs from the updated template
        logger.info(f"Creating {vm_count} new VMs from template {template.proxmox_vmid}")
        
        from app.services.proxmox_operations import clone_vms_for_class
        created_vms = clone_vms_for_class(
            template_vmid=template.proxmox_vmid,
            node=template.node,
            count=vm_count,
            name_prefix=sanitize_vm_name(class_.name, fallback="classvm"),
            cluster_ip=template.cluster_ip
        )
        
        # Step 4: Create new assignments for the fresh VMs
        for vm_info in created_vms:
            assignment = VMAssignment(
                class_id=class_.id,
                proxmox_vmid=vm_info['vmid'],
                node=vm_info['node'],
                status='available'
            )
            db.session.add(assignment)
            created_count += 1
            results.append(f"Created new VM {vm_info['vmid']} on {vm_info['node']}")
        
        db.session.commit()
        
        logger.info(f"Successfully pushed template to class {class_.name}: deleted {deleted_count} VMs, created {created_count} new VMs")
        
        return jsonify({
            'ok': True,
            'message': f'Template pushed: deleted {deleted_count} old VMs, created {created_count} new VMs',
            'deleted': deleted_count,
            'created': created_count,
            'results': results,
            'errors': errors
        })
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Failed to push template: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500
