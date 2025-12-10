#!/usr/bin/env python3
"""
Class Template Management API endpoints.

Handles template operations: reimage, save, push.
"""

import logging
from datetime import datetime

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
    """Verify user is teacher, co-owner of class, or admin."""
    username = session.get('user', '').split('@')[0]
    user = get_user_by_username(username)
    
    if not user:
        return None, jsonify({'ok': False, 'error': 'User not found'}), 404
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return None, jsonify({'ok': False, 'error': 'Class not found'}), 404
    
    # Check if user is admin, primary teacher, or co-owner
    if not user.is_adminer and not class_.is_owner(user):
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
        
        # ALWAYS query actual node from Proxmox (VM may have been migrated or database could be stale)
        logger.info(f"Querying actual node for VMID {vmid}...")
        try:
            # Get all VMs from cluster to find the actual node
            resources = proxmox.cluster.resources.get(type="vm")
            actual_node = None
            for r in resources:
                if int(r.get('vmid', -1)) == int(vmid):
                    actual_node = r.get('node')
                    if actual_node != node:
                        logger.info(f"VM {vmid} migrated from {node} to {actual_node}, using actual node")
                    else:
                        logger.info(f"VM {vmid} confirmed on node {actual_node}")
                    node = actual_node
                    # Update database with correct node
                    if teacher_vm.node != actual_node:
                        teacher_vm.node = actual_node
                        db.session.commit()
                    break
            
            if not actual_node:
                logger.error(f"VM {vmid} not found in cluster resources")
                return jsonify({
                    "ok": False, 
                    "error": f"Teacher VM {vmid} not found in Proxmox. It may have been deleted."
                }), 404
        except Exception as e:
            logger.error(f"Failed to query cluster resources for VM {vmid}: {e}")
            return jsonify({
                "ok": False, 
                "error": f"Cannot query Proxmox cluster: {e}"
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
        
        # First: Check database cache
        if teacher_vm.cached_ip:
            ip_address = teacher_vm.cached_ip
            logger.info(f"Using cached IP for VM {vmid}: {ip_address}")
        
        # Second: Check VMInventory cache
        if not ip_address:
            from app.models import VMInventory
            from app.config import CLUSTERS
            cluster_id = None
            for c in CLUSTERS:
                if c['host'] == cluster_ip:
                    cluster_id = c['id']
                    break
            if cluster_id:
                inventory = VMInventory.query.filter_by(cluster_id=cluster_id, vmid=vmid).first()
                if inventory and inventory.ip:
                    ip_address = inventory.ip
                    logger.info(f"Using VMInventory cached IP for VM {vmid}: {ip_address}")
        
        try:
            # Third: For running VMs, try guest agent if no cached IP
            if not ip_address and vm_status == 'running':
                try:
                    logger.info(f"Attempting guest agent network lookup for VM {vmid} on node {node}")
                    # Try to get network interfaces from QEMU guest agent
                    result = proxmox.nodes(node).qemu(vmid).agent('network-get-interfaces').get()
                    logger.debug(f"Guest agent response for VM {vmid}: {result}")
                    
                    if result and 'result' in result:
                        interfaces = result['result']
                        logger.info(f"Found {len(interfaces)} network interfaces for VM {vmid}")
                        # Look for first non-loopback IPv4 address
                        for iface in interfaces:
                            iface_name = iface.get('name', 'unknown')
                            if iface_name in ('lo', 'loopback'):
                                continue
                            ip_addresses = iface.get('ip-addresses', [])
                            logger.debug(f"Interface {iface_name}: {len(ip_addresses)} addresses")
                            for addr in ip_addresses:
                                if addr.get('ip-address-type') == 'ipv4':
                                    found_ip = addr.get('ip-address')
                                    if found_ip and not found_ip.startswith('127.'):
                                        ip_address = found_ip
                                        logger.info(f"Found IP via guest agent for VM {vmid}: {ip_address}")
                                        break
                            if ip_address:
                                break
                    else:
                        logger.warning(f"Guest agent returned no result for VM {vmid}")
                except Exception as e:
                    logger.warning(f"Guest agent network lookup failed for VM {vmid}: {e}")
                
                # Fallback: ARP scan if guest agent failed
                if not ip_address and mac_address:
                    logger.info(f"Attempting ARP scan for VM {vmid} with MAC {mac_address}")
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
                            logger.info(f"Found IP via ARP scan for VM {vmid}: {ip_address}")
                        else:
                            logger.warning(f"ARP scan found no IP for VM {vmid}")
                    else:
                        logger.warning(f"Could not find cluster_id for cluster_ip {cluster_ip}")
                else:
                    if not mac_address:
                        logger.warning(f"No MAC address available for VM {vmid}, cannot perform ARP scan")
                
                # Update cache if we found an IP
                if ip_address:
                    teacher_vm.cached_ip = ip_address
                    teacher_vm.ip_updated_at = datetime.utcnow()
                    db.session.commit()
                    logger.info(f"Cached IP {ip_address} for VM {vmid}")
                
                # RDP availability: always true if we have an IP (teacher VMs need RDP access)
                rdp_available = bool(ip_address)
                logger.info(f"RDP available for teacher VM {vmid}: {rdp_available} (IP: {ip_address})")
        except Exception as ip_err:
            logger.warning(f"Teacher VM IP discovery failed: {ip_err}")

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
    """Delete teacher VM and recreate fresh copy from class base QCOW2.
    
    This resets the teacher's working VM to match the original class template,
    discarding any changes the teacher has made.
    """
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    try:
        from app.models import VMAssignment, db
        from app.services.proxmox_operations import delete_vm
        
        # Find the teacher VM
        teacher_vm = VMAssignment.query.filter_by(
            class_id=class_id,
            is_teacher_vm=True
        ).first()
        
        if not teacher_vm:
            # Fallback: find VM assigned to teacher
            teacher_vm = VMAssignment.query.filter_by(
                class_id=class_id,
                assigned_user_id=class_.teacher_id,
                is_template_vm=False
            ).first()
        
        if not teacher_vm:
            return jsonify({'ok': False, 'error': 'No teacher VM found for this class'}), 404
        
        old_vmid = teacher_vm.proxmox_vmid
        old_node = teacher_vm.node
        cluster_ip = class_.template.cluster_ip if class_.template else None
        
        if not cluster_ip:
            from app.config import CLUSTERS
            if CLUSTERS:
                cluster_ip = CLUSTERS[0]["host"]
            else:
                return jsonify({'ok': False, 'error': 'No clusters configured'}), 500
        
        logger.info(f"Reimaging teacher VM {old_vmid} for class {class_id} (will recreate from class base)")
        
        # Delete old teacher VM
        delete_success, delete_msg = delete_vm(old_vmid, old_node, cluster_ip)
        
        if not delete_success:
            logger.warning(f"Failed to delete teacher VM {old_vmid}: {delete_msg}")
            # Continue anyway - may already be deleted
        
        # Delete VMAssignment record
        db.session.delete(teacher_vm)
        db.session.commit()
        
        # Recreate teacher VM from class base using same overlay approach
        from app.services.class_vm_service import recreate_teacher_vm_from_base
        
        success, message, new_vmid = recreate_teacher_vm_from_base(
            class_id=class_id,
            class_name=class_.name
        )
        
        if success:
            logger.info(f"Teacher VM reimaged: deleted {old_vmid}, created {new_vmid}")
            return jsonify({
                'ok': True,
                'message': f'Teacher VM reimaged successfully',
                'old_vmid': old_vmid,
                'new_vmid': new_vmid
            })
        else:
            return jsonify({'ok': False, 'error': f'Failed to recreate teacher VM: {message}'}), 500
            
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Failed to reimage teacher VM: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_class_template_bp.route("/<int:class_id>/template/save", methods=['POST'])
@login_required
def save_template(class_id: int):
    """Export teacher VM disk to update the class base QCOW2.
    
    This updates the class template with changes from the teacher's working VM.
    Student VMs are NOT affected until 'Push' is clicked.
    """
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    try:
        from app.models import VMAssignment
        from app.services.proxmox_operations import stop_class_vm
        from app.services.class_vm_service import save_teacher_vm_to_base
        
        # Find the teacher VM
        teacher_vm = VMAssignment.query.filter_by(
            class_id=class_id,
            is_teacher_vm=True
        ).first()
        
        if not teacher_vm:
            # Fallback: find VM assigned to teacher
            teacher_vm = VMAssignment.query.filter_by(
                class_id=class_id,
                assigned_user_id=class_.teacher_id,
                is_template_vm=False
            ).first()
        
        if not teacher_vm:
            return jsonify({'ok': False, 'error': 'No teacher VM found for this class'}), 404
        
        vmid = teacher_vm.proxmox_vmid
        node = teacher_vm.node
        cluster_ip = class_.template.cluster_ip if class_.template else None
        
        if not cluster_ip:
            from app.config import CLUSTERS
            if CLUSTERS:
                cluster_ip = CLUSTERS[0]["host"]
            else:
                return jsonify({'ok': False, 'error': 'No clusters configured'}), 500
        
        logger.info(f"Saving teacher VM {vmid} changes to class base for class {class_id}")
        
        # Stop the VM first (required for clean disk export)
        logger.info(f"Stopping teacher VM {vmid} before exporting disk")
        stop_success, stop_msg = stop_class_vm(vmid, node, cluster_ip)
        
        if not stop_success:
            logger.warning(f"Failed to stop teacher VM {vmid}: {stop_msg} (continuing anyway)")
        
        # Wait for shutdown to complete
        import time
        time.sleep(5)
        
        # Export teacher VM disk to update class base QCOW2
        success, message = save_teacher_vm_to_base(
            class_id=class_id,
            teacher_vmid=vmid,
            teacher_node=node,
            class_name=class_.name
        )
        
        if success:
            logger.info(f"Teacher VM {vmid} changes saved to class base QCOW2")
            return jsonify({
                'ok': True,
                'message': 'Template updated successfully. Click "Push" to propagate changes to student VMs.'
            })
        else:
            return jsonify({'ok': False, 'error': f'Failed to save template: {message}'}), 500
            
    except Exception as e:
        logger.exception(f"Failed to save template: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_class_template_bp.route("/<int:class_id>/template/push", methods=['POST'])
@login_required
def push_template(class_id: int):
    """Push staging template to student VMs by rebasing overlays (FAST!).
    
    This rebases student VM overlays to the new staging template without
    recreating VMs. Much faster than delete/recreate approach.
    """
    class_, error_response, status_code = require_teacher_or_admin(class_id)
    if error_response:
        return error_response, status_code
    
    try:
        from app.services.class_vm_service import push_staging_to_students
        
        logger.info(f"Pushing staging template to students for class {class_id}")
        
        # Use fast overlay rebase approach
        success, message, updated_count = push_staging_to_students(
            class_id=class_id,
            class_name=class_.name
        )
        
        if success:
            return jsonify({
                'ok': True,
                'message': message,
                'updated': updated_count
            })
        else:
            return jsonify({'ok': False, 'error': message}), 500
            
    except Exception as e:
        logger.exception(f"Failed to push template: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500
