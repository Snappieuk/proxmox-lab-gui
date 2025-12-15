#!/usr/bin/env python3
"""
VM API routes blueprint.

Handles VM list, IP lookup, and power control operations.
Database-first architecture: All queries read from VMInventory table.
Background sync keeps database current - NEVER makes direct Proxmox API calls for reads.
"""

import logging

from flask import Blueprint, jsonify, request, session

from app.services.proxmox_service import get_clusters_from_db
from app.models import User, VMAssignment
from app.utils.decorators import login_required

logger = logging.getLogger(__name__)

api_vms_bp = Blueprint('api_vms', __name__, url_prefix='/api')


@api_vms_bp.route("/vms")
@login_required
def api_vms():
    """Get VMs for current user from VMInventory database.

    Query parameters:
    - search: Name/IP/VMID substring filter.
    - cluster: Limit to a specific cluster_id.
    - force_refresh=true: Trigger background sync (doesn't block).
    
    This endpoint ONLY queries the database - never makes direct Proxmox API calls.
    Background sync keeps VMInventory current.
    """
    from app.services.inventory_service import fetch_vm_inventory
    from app.services.user_manager import is_admin_user, require_user

    username = require_user()
    force_refresh = request.args.get('force_refresh', 'false').lower() == 'true'
    search = request.args.get('search', '').strip() or None
    cluster_filter = request.args.get('cluster', '').strip() or None

    try:
        # Determine owned VMIDs for non-admin users
        if is_admin_user(username):
            owned_vmids = None  # Admins see everything
        else:
            owned_vmids = _resolve_user_owned_vmids(username)
        
        # Query database (fast, <100ms)
        vms = fetch_vm_inventory(
            cluster_id=cluster_filter,
            search=search,
            vmids=owned_vmids
        )
        
        # Augment with RDP availability
        _augment_rdp_availability(vms)
        
        # Augment with builder VM information (for "My Template VMs" section)
        _augment_builder_vm_info(vms, username)
        
        # For admins ONLY, add mapped user information
        is_admin = is_admin_user(username)
        if is_admin:
            for vm in vms:
                vm['mapped_to'] = _get_vm_mapped_user(vm['vmid'])
        
        # Trigger background refresh if requested
        if force_refresh:
            from app.services.background_sync import trigger_immediate_sync
            trigger_immediate_sync()
        
        return jsonify({
            'vms': vms,
            'is_admin': is_admin  # Tell frontend whether to show mapped_to field
        })
        
    except Exception as e:
        logger.exception("Failed to fetch VMs for user %s", username)
        return jsonify({"error": True, "message": f"Failed to fetch VMs: {e}"}), 500


@api_vms_bp.route("/vm/<int:vmid>/ip")
@login_required
def api_vm_ip(vmid: int):
    """Get IP address for a specific VM from database (lazy loading)."""
    from flask import session

    from app.services.proxmox_service import get_clusters_from_db
    from app.services.inventory_service import get_vm_from_inventory
    from app.services.user_manager import is_admin_user, require_user
    
    user = require_user()
    cluster_id = request.args.get('cluster', session.get("cluster_id", get_clusters_from_db()[0]["id"]))
    
    try:
        # Query from database
        vm = get_vm_from_inventory(cluster_id, vmid)
        
        if not vm:
            return jsonify({"error": "VM not found"}), 404
        
        # Check access permissions
        if not is_admin_user(user):
            owned_vmids = _resolve_user_owned_vmids(user)
            if vmid not in owned_vmids:
                return jsonify({"error": "VM not accessible"}), 403
        
        # Return IP from database
        ip = vm.get('ip')
        
        if ip and ip not in ('N/A', 'Fetching...', '', None):
            return jsonify({
                "vmid": vmid,
                "ip": ip,
                "status": "complete"
            })
        
        # No IP found
        return jsonify({
            "vmid": vmid,
            "ip": None,
            "status": "not_found"
        })
        
    except Exception as e:
        logger.exception("Failed to get IP for VM %d", vmid)
        return jsonify({"error": str(e), "status": "error"}), 500


def _resolve_user_owned_vmids(username: str) -> set:
    """Resolve VMIDs the user should see from database VMAssignments.

    Database-first architecture: Only uses VMAssignment table.
    Accepts realm-suffixed usernames (e.g., student1@pve) and normalizes variants.
    Includes VMs from classes where user is owner or co-owner.
    """
    variants = {username}
    if '@' in username:
        base = username.split('@', 1)[0]
        variants.add(base)
    # Proxmox common realms
    variants.update({v + '@pve' for v in list(variants)})
    variants.update({v + '@pam' for v in list(variants)})

    owned = set()
    
    # Query database for VM assignments
    try:
        user_row = User.query.filter(User.username.in_(variants)).first()
        if user_row:
            # Direct VM assignments (assigned to this user)
            for assign in user_row.vm_assignments:
                if assign.proxmox_vmid:
                    owned.add(assign.proxmox_vmid)
            logger.debug("Found %d directly assigned VMs for %s", len(user_row.vm_assignments.all()), username)
            
            # VMs from classes where user is owner or co-owner
            from app.models import Class
            if user_row.is_teacher or user_row.is_adminer:
                # Get classes owned by this teacher
                owned_classes = Class.query.filter_by(teacher_id=user_row.id).all()
                
                # Get classes where this teacher is a co-owner
                co_owned_classes = [c for c in Class.query.all() if c.is_owner(user_row) and c.teacher_id != user_row.id]
                
                all_managed_classes = owned_classes + co_owned_classes
                
                for class_ in all_managed_classes:
                    for assign in class_.vm_assignments:
                        if assign.proxmox_vmid:
                            owned.add(assign.proxmox_vmid)
                
                logger.debug("Found %d VMs from %d managed classes for teacher %s", 
                           len([a for c in all_managed_classes for a in c.vm_assignments]), 
                           len(all_managed_classes), username)
                
    except Exception:
        logger.exception("Failed to gather VM assignments for %s", username)

    logger.info("User %s has access to %d VMs from database", username, len(owned))
    return owned


def _get_vm_mapped_user(vmid: int) -> str:
    """Get the username that owns this VM from VMAssignment table.
    
    Returns username or 'Nobody' if not assigned.
    """
    try:
        assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
        if assignment and assignment.assigned_user_id:
            user = User.query.get(assignment.assigned_user_id)
            return user.username if user else 'Nobody'
    except Exception:
        logger.exception("Failed to check VM assignment for VMID %d", vmid)
    return 'Nobody'


def _augment_rdp_availability(vms: list):
    """Add rdp_available field to each VM dict in-place based on stored flags."""
    for vm in vms:
        # RDP availability is stored in database from background sync
        # Windows VMs typically have RDP available when running
        is_windows = vm.get('category') == 'windows' or 'win' in vm.get('name', '').lower()
        has_ip = vm.get('ip') and vm['ip'] not in ('N/A', 'Fetching...', '', None)
        is_running = vm.get('status') == 'running'
        
        # Use database flag if available, otherwise infer from metadata
        vm['rdp_available'] = vm.get('rdp_available', False) or (is_windows and has_ip and is_running)


def _augment_builder_vm_info(vms: list, username: str):
    """Add is_builder_vm field to each VM dict in-place.
    
    A VM is considered a "builder VM" if:
    1. It has a VMAssignment with class_id=NULL (direct assignment, not part of a class)
    2. It's assigned to the current user
    3. It's not a template
    
    This is used by the "My Template VMs" section in the Template Builder UI.
    """
    try:
        # Get user record
        variants = {username}
        if '@' in username:
            base = username.split('@', 1)[0]
            variants.add(base)
        variants.update({v + '@pve' for v in list(variants)})
        variants.update({v + '@pam' for v in list(variants)})
        
        user_row = User.query.filter(User.username.in_(variants)).first()
        if not user_row:
            # User not found - no builder VMs
            for vm in vms:
                vm['is_builder_vm'] = False
            return
        
        # Get all direct assignments (class_id=NULL) for this user
        builder_vmids = set()
        direct_assignments = VMAssignment.query.filter_by(
            assigned_user_id=user_row.id,
            class_id=None
        ).all()
        
        for assign in direct_assignments:
            if assign.proxmox_vmid:
                builder_vmids.add(assign.proxmox_vmid)
        
        logger.debug("User %s has %d builder VMs: %s", username, len(builder_vmids), builder_vmids)
        
        # Mark VMs accordingly
        for vm in vms:
            vm['is_builder_vm'] = vm['vmid'] in builder_vmids and not vm.get('is_template', False)
            
    except Exception:
        logger.exception("Failed to augment builder VM info for user %s", username)
        # On error, mark all as non-builder VMs
        for vm in vms:
            vm['is_builder_vm'] = False


@api_vms_bp.route("/vm/<int:vmid>/status")
@login_required
def api_vm_status(vmid: int):
    """
    Get status for a single VM from database (optimized for post-action refresh).
    
    Query parameters:
    - cluster: Cluster ID (optional, will try to find VM in any cluster if omitted)
    """
    from flask import session

    from app.services.proxmox_service import get_clusters_from_db
    from app.services.inventory_service import get_vm_from_inventory
    from app.services.user_manager import is_admin_user, require_user
    
    user = require_user()
    cluster_id = request.args.get('cluster', session.get("cluster_id", get_clusters_from_db()[0]["id"]))
    
    try:
        # Query from database
        vm = get_vm_from_inventory(cluster_id, vmid)
        
        if not vm:
            return jsonify({"error": "VM not found"}), 404
        
        # Check access permissions (admin sees all, users see only their VMs)
        if not is_admin_user(user):
            owned_vmids = _resolve_user_owned_vmids(user)
            if vmid not in owned_vmids:
                return jsonify({"error": "VM not accessible"}), 403
        
        # Augment with RDP availability
        _augment_rdp_availability([vm])
        
        return jsonify(vm)
        
    except Exception as e:
        logger.exception("Failed to get status for VM %d", vmid)
        return jsonify({"error": str(e)}), 500


@api_vms_bp.route("/vm/<int:vmid>/start", methods=["POST"])
@login_required
def api_vm_start(vmid: int):
    """Start a VM and immediately update database.
    
    Control operations (start/stop) MUST go through Proxmox API.
    Database is updated immediately after for fast UI refresh.
    """
    from app.services.inventory_service import get_vm_from_inventory, update_vm_status
    from app.services.proxmox_service import get_proxmox_admin
    from app.services.user_manager import is_admin_user, require_user
    
    user = require_user()
    # Handle both JSON and non-JSON requests
    cluster_id = session.get("cluster_id", get_clusters_from_db()[0]["id"])
    if request.is_json and request.json:
        cluster_id = request.json.get('cluster_id', cluster_id)
    
    try:
        # Get VM from database for validation
        vm = get_vm_from_inventory(cluster_id, vmid)
        if not vm:
            return jsonify({"ok": False, "error": "VM not found"}), 404
        
        # Check access permissions
        if not is_admin_user(user):
            owned_vmids = _resolve_user_owned_vmids(user)
            if vmid not in owned_vmids:
                return jsonify({"ok": False, "error": "VM not accessible"}), 403
        
        # Execute start via Proxmox API
        proxmox = get_proxmox_admin()
        node = vm['node']
        vm_type = vm['type']
        
        # Query cluster to find actual node (VM may have been migrated)
        actual_node = node
        try:
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                if int(r.get('vmid', -1)) == int(vmid):
                    actual_node = r.get('node')
                    if actual_node != node:
                        logger.info(f"VM {vmid} was migrated from {node} to {actual_node}, using actual node")
                    break
        except Exception as e:
            logger.warning(f"Cluster resources query failed: {e}, using database node {node}")
        
        # Start the VM
        try:
            if vm_type == 'qemu':
                proxmox.nodes(actual_node).qemu(vmid).status.start.post()
            else:  # lxc
                proxmox.nodes(actual_node).lxc(vmid).status.start.post()
        except Exception as start_error:
            # If VM is already running, that's okay
            if "already running" in str(start_error).lower():
                logger.info(f"VM {vmid} already running")
            else:
                raise
        
        # Immediately update database status
        update_vm_status(cluster_id, vmid, 'running')
        
        # Trigger background IP discovery for this VM after a delay (VM needs time to boot)
        # Capture app instance before threading
        from flask import current_app
        app = current_app._get_current_object()
        
        def _check_vm_ip_after_start():
            """Background task to check VM IP after it boots."""
            import time
            time.sleep(15)  # Wait 15 seconds for VM to boot and get IP
            try:
                with app.app_context():
                    from app.services.background_sync import trigger_immediate_sync
                    logger.info(f"Triggering IP check for VM {vmid} after start")
                    trigger_immediate_sync()
            except Exception as e:
                logger.error(f"Failed to trigger IP check for VM {vmid}: {e}")
        
        import threading
        threading.Thread(target=_check_vm_ip_after_start, daemon=True).start()
        
        logger.info(f"Started VM {vmid} on node {node}")
        return jsonify({"ok": True})
        
    except Exception as e:
        logger.exception(f"Failed to start VM {vmid}")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_vms_bp.route("/vm/<int:vmid>/stop", methods=["POST"])
@login_required
def api_vm_stop(vmid: int):
    """Stop a VM and immediately update database.
    
    Control operations (start/stop) MUST go through Proxmox API.
    Database is updated immediately after for fast UI refresh.
    """
    from app.services.inventory_service import get_vm_from_inventory, update_vm_status
    from app.services.proxmox_service import get_proxmox_admin
    from app.services.user_manager import is_admin_user, require_user
    
    user = require_user()
    # Handle both JSON and non-JSON requests
    cluster_id = session.get("cluster_id", get_clusters_from_db()[0]["id"])
    if request.is_json and request.json:
        cluster_id = request.json.get('cluster_id', cluster_id)
    
    try:
        # Get VM from database for validation
        vm = get_vm_from_inventory(cluster_id, vmid)
        if not vm:
            return jsonify({"ok": False, "error": "VM not found"}), 404
        
        # Check access permissions
        if not is_admin_user(user):
            owned_vmids = _resolve_user_owned_vmids(user)
            if vmid not in owned_vmids:
                return jsonify({"ok": False, "error": "VM not accessible"}), 403
        
        # Execute stop via Proxmox API
        proxmox = get_proxmox_admin()
        node = vm['node']
        vm_type = vm['type']
        
        # Query cluster to find actual node (VM may have been migrated)
        actual_node = node
        try:
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                if int(r.get('vmid', -1)) == int(vmid):
                    actual_node = r.get('node')
                    if actual_node != node:
                        logger.info(f"VM {vmid} was migrated from {node} to {actual_node}, using actual node")
                    break
        except Exception as e:
            logger.warning(f"Cluster resources query failed: {e}, using database node {node}")
        
        # Stop the VM
        try:
            if vm_type == 'qemu':
                proxmox.nodes(actual_node).qemu(vmid).status.shutdown.post()
            else:  # lxc
                proxmox.nodes(actual_node).lxc(vmid).status.shutdown.post()
        except Exception as stop_error:
            # If VM is already stopped or doesn't exist, that's okay
            if "not running" in str(stop_error).lower() or "does not exist" in str(stop_error).lower():
                logger.info(f"VM {vmid} already stopped or doesn't exist")
            else:
                raise
        
        # Immediately update database
        update_vm_status(cluster_id, vmid, 'stopped')
        
        logger.info(f"Stopped VM {vmid} on node {actual_node}")
        return jsonify({"ok": True})
        
    except Exception as e:
        logger.exception(f"Failed to stop VM {vmid}")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_vms_bp.route("/vm/<int:vmid>/eject-iso", methods=["POST"])
@login_required
def api_vm_eject_iso(vmid: int):
    """Eject ISO/installation media from VM CD/DVD drive.
    
    Removes ISO from ide2 (CD/DVD drive) so VM can boot from disk instead of installation media.
    Useful after OS installation is complete.
    """
    from app.services.inventory_service import get_vm_from_inventory
    from app.services.proxmox_service import get_proxmox_admin
    from app.services.user_manager import is_admin_user, require_user
    
    user = require_user()
    cluster_id = session.get("cluster_id", get_clusters_from_db()[0]["id"])
    if request.is_json and request.json:
        cluster_id = request.json.get('cluster_id', cluster_id)
    
    try:
        # Get VM from database for validation
        vm = get_vm_from_inventory(cluster_id, vmid)
        if not vm:
            return jsonify({"ok": False, "error": "VM not found"}), 404
        
        # Check access permissions
        if not is_admin_user(user):
            owned_vmids = _resolve_user_owned_vmids(user)
            if vmid not in owned_vmids:
                return jsonify({"ok": False, "error": "VM not accessible"}), 403
        
        # Only QEMU VMs have CD/DVD drives (LXC doesn't support ISO mounting)
        if vm['type'] != 'qemu':
            return jsonify({"ok": False, "error": "Only QEMU VMs support ISO ejection"}), 400
        
        # Execute eject via Proxmox API
        proxmox = get_proxmox_admin()
        node = vm['node']
        
        # Query cluster to find actual node (VM may have been migrated)
        actual_node = node
        try:
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                if int(r.get('vmid', -1)) == int(vmid):
                    actual_node = r.get('node')
                    if actual_node != node:
                        logger.info(f"VM {vmid} was migrated from {node} to {actual_node}, using actual node")
                    break
        except Exception as e:
            logger.warning(f"Cluster resources query failed: {e}, using database node {node}")
        
        # Eject ISO by setting ide2 to 'none' or 'cdrom,media=cdrom'
        # This removes the ISO but keeps the CD/DVD drive
        try:
            proxmox.nodes(actual_node).qemu(vmid).config.put(ide2='none')
            logger.info(f"Ejected ISO from VM {vmid} on node {actual_node}")
            return jsonify({
                "ok": True, 
                "message": "Installation media ejected successfully. VM can now boot from disk."
            })
        except Exception as eject_error:
            error_msg = str(eject_error)
            if "does not have" in error_msg.lower() or "not found" in error_msg.lower():
                return jsonify({
                    "ok": False, 
                    "error": "No CD/DVD drive found (ide2). VM may already have ISO ejected."
                }), 400
            raise
        
    except Exception as e:
        logger.exception(f"Failed to eject ISO from VM {vmid}")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_vms_bp.route("/vm/<int:vmid>/convert-to-template", methods=["POST"])
@login_required
def api_vm_convert_to_template(vmid: int):
    """Convert a VM to a template.
    
    WARNING: This action is irreversible. Once converted to a template, the VM
    can no longer be started directly and can only be used as a base for cloning.
    """
    from app.services.inventory_service import get_vm_from_inventory
    from app.services.proxmox_operations import convert_vm_to_template
    from app.services.user_manager import is_admin_user, require_user
    
    user = require_user()
    cluster_id = session.get("cluster_id", get_clusters_from_db()[0]["id"])
    if request.is_json and request.json:
        cluster_id = request.json.get('cluster_id', cluster_id)
    
    try:
        # Get VM from database for validation
        vm = get_vm_from_inventory(cluster_id, vmid)
        if not vm:
            return jsonify({"ok": False, "error": "VM not found"}), 404
        
        # Check access permissions
        if not is_admin_user(user):
            owned_vmids = _resolve_user_owned_vmids(user)
            if vmid not in owned_vmids:
                return jsonify({"ok": False, "error": "VM not accessible"}), 403
        
        # Only QEMU VMs can be templates (LXC uses different mechanism)
        if vm['type'] != 'qemu':
            return jsonify({"ok": False, "error": "Only QEMU VMs can be converted to templates"}), 400
        
        # Get cluster IP for convert_vm_to_template function
        cluster_ip = None
        for cluster in get_clusters_from_db():
            if cluster["id"] == cluster_id:
                cluster_ip = cluster["host"]
                break
        
        if not cluster_ip:
            return jsonify({"ok": False, "error": "Cluster configuration not found"}), 500
        
        # Execute conversion
        node = vm['node']
        success, message = convert_vm_to_template(vmid, node, cluster_ip)
        
        if success:
            logger.info(f"VM {vmid} converted to template successfully by user {user}")
            
            # Trigger background sync to update VMInventory
            try:
                from app.services.background_sync import trigger_immediate_sync
                trigger_immediate_sync()
            except Exception as sync_err:
                logger.warning(f"Failed to trigger sync after template conversion: {sync_err}")
            
            return jsonify({
                "ok": True,
                "message": message
            })
        else:
            return jsonify({"ok": False, "error": message}), 400
        
    except Exception as e:
        logger.exception(f"Failed to convert VM {vmid} to template")
        return jsonify({"ok": False, "error": str(e)}), 500

