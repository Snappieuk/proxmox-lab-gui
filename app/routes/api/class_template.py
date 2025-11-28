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
        # Get VM status from Proxmox
        vm_status = get_vm_status(template.proxmox_vmid, template.cluster_ip)
        
        # Get IP and other details
        proxmox = get_proxmox_admin_for_cluster(template.cluster_ip)
        vm_config = proxmox.nodes(template.node).qemu(template.proxmox_vmid).config.get()
        
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
        
        return jsonify({
            'ok': True,
            'template': {
                **template.to_dict(),
                'status': vm_status,
                'mac_address': mac_address
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
    """Revert template VM to base snapshot."""
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    if not class_.template:
        return jsonify({'ok': False, 'error': 'No template assigned'}), 404
    
    try:
        template = class_.template
        snapshot_name = 'baseline'
        
        # Revert to baseline snapshot
        success = revert_vm_to_snapshot(
            template.proxmox_vmid,
            template.cluster_ip,
            snapshot_name
        )
        
        if success:
            logger.info(f"Re-imaged template {template.proxmox_vmid} to baseline snapshot")
            return jsonify({'ok': True, 'message': 'Template reverted to baseline'})
        else:
            return jsonify({'ok': False, 'error': 'Failed to revert to baseline snapshot'}), 500
    except Exception as e:
        logger.exception(f"Failed to reimage template: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_class_template_bp.route("/<int:class_id>/template/save", methods=['POST'])
@login_required
def save_template(class_id: int):
    """Save current template VM state as new baseline."""
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    if not class_.template:
        return jsonify({'ok': False, 'error': 'No template assigned'}), 404
    
    try:
        template = class_.template
        snapshot_name = 'baseline'
        description = f"Baseline snapshot for class {class_.name}"
        
        # Create/replace baseline snapshot
        success = create_vm_snapshot(
            template.proxmox_vmid,
            template.cluster_ip,
            snapshot_name,
            description
        )
        
        if success:
            logger.info(f"Saved template {template.proxmox_vmid} as new baseline")
            return jsonify({'ok': True, 'message': 'Template baseline saved'})
        else:
            return jsonify({'ok': False, 'error': 'Failed to create snapshot'}), 500
    except Exception as e:
        logger.exception(f"Failed to save template: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_class_template_bp.route("/<int:class_id>/template/push", methods=['POST'])
@login_required
def push_template(class_id: int):
    """Push template to all VMs in class (delete and re-clone)."""
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    if not class_.template:
        return jsonify({'ok': False, 'error': 'No template assigned'}), 404
    
    try:
        template = class_.template
        assignments = class_.vm_assignments
        
        results = []
        errors = []
        
        for assignment in assignments:
            try:
                # Delete existing VM
                delete_success = delete_vm(assignment.proxmox_vmid, template.cluster_ip)
                if not delete_success:
                    errors.append(f"Failed to delete VM {assignment.proxmox_vmid}")
                    continue
                
                # Clone new VM from template
                new_vmid = clone_vm_from_template(
                    template.proxmox_vmid,
                    template.cluster_ip,
                    name=f"{class_.name}-{assignment.id}",
                    target_node=template.node
                )
                
                if new_vmid:
                    # Update assignment with new VMID
                    old_vmid = assignment.proxmox_vmid
                    assignment.proxmox_vmid = new_vmid
                    assignment.node = template.node
                    
                    # Get MAC address from new VM
                    proxmox = get_proxmox_admin_for_cluster(template.cluster_ip)
                    vm_config = proxmox.nodes(template.node).qemu(new_vmid).config.get()
                    
                    for key in vm_config.keys():
                        if key.startswith('net'):
                            net_value = vm_config[key]
                            if '=' in net_value:
                                parts = net_value.split(',')
                                for part in parts:
                                    if '=' in part:
                                        k, v = part.split('=', 1)
                                        if k.strip().lower() in ('macaddr', 'hwaddr'):
                                            assignment.mac_address = v.strip()
                                            break
                            if assignment.mac_address:
                                break
                    
                    results.append(f"Recreated VM {old_vmid} → {new_vmid}")
                else:
                    errors.append(f"Failed to clone VM for assignment {assignment.id}")
            except Exception as e:
                logger.exception(f"Failed to push to VM {assignment.proxmox_vmid}: {e}")
                errors.append(f"VM {assignment.proxmox_vmid}: {str(e)}")
        
        # Commit all changes
        db.session.commit()
        
        logger.info(f"Pushed template to {len(results)} VMs for class {class_.name}")
        
        return jsonify({
            'ok': True,
            'message': f'Template pushed to {len(results)} VMs',
            'results': results,
            'errors': errors
        })
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Failed to push template: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500
