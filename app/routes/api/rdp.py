#!/usr/bin/env python3
"""
RDP API routes blueprint.

Handles RDP file generation and download.
"""

import logging

from flask import Blueprint, Response, render_template, url_for, abort, current_app

from app.utils.decorators import login_required
from app.services.rdp_service import build_rdp


from app.services.proxmox_client import find_vm_for_user, fast_verify_vm_ip

logger = logging.getLogger(__name__)

api_rdp_bp = Blueprint('api_rdp', __name__)


@api_rdp_bp.route("/rdp/<int:vmid>.rdp")
@login_required
def rdp_file(vmid: int):
    """Generate and download RDP file for a VM."""
    from app.services.user_manager import require_user
    from app.services.inventory_service import fetch_vm_by_vmid
    from app.services.user_manager import is_admin_user
    
    user = require_user()
    logger.info("rdp_file: user=%s requesting vmid=%s", user, vmid)
    
    # OPTIMIZATION: Direct database lookup instead of loading all VMs
    vm_data = fetch_vm_by_vmid(vmid)
    if not vm_data:
        logger.warning("rdp_file: VM %s not found in database", vmid)
        abort(404)
    
    # Check permission (admin or assigned user)
    admin = is_admin_user(user)
    if not admin:
        # Check if user has access to this VM
        from flask import has_app_context
        from app.models import VMAssignment, User
        
        accessible = False
        
        if has_app_context():
            try:
                # Normalize username variants
                user_variants = {user}
                if '@' in user:
                    base = user.split('@', 1)[0]
                    user_variants.add(base)
                    user_variants.add(f"{base}@pve")
                    user_variants.add(f"{base}@pam")
                else:
                    user_variants.add(f"{user}@pve")
                    user_variants.add(f"{user}@pam")
                
                # Find user record
                local_user = User.query.filter(User.username.in_(user_variants)).first()
                if local_user:
                    # Check VMAssignment
                    assignment = VMAssignment.query.filter_by(
                        proxmox_vmid=vmid,
                        assigned_user_id=local_user.id
                    ).first()
                    accessible = assignment is not None
            except Exception as e:
                logger.warning("rdp_file: failed to check VM assignment for user %s: %s", user, e)
        
        if not accessible:
            logger.warning("rdp_file: user %s does not have access to VM %s", user, vmid)
            abort(403)
    
    # Convert database record to VM dict
    vm = {
        'vmid': vm_data.vmid,
        'name': vm_data.name,
        'node': vm_data.node,
        'type': vm_data.type,  # Fixed: column name is 'type' not 'vmtype'
        'status': vm_data.status,
        'ip': vm_data.ip,
        'cluster_id': vm_data.cluster_id,
    }

    logger.info("rdp_file: Found VM: vmid=%s, name=%s, type=%s, ip=%s", 
                vm.get('vmid'), vm.get('name'), vm.get('type'), vm.get('ip'))

    # Fast IP verification ONLY if we have a cached IP
    # Skip verification if IP is already valid - just use it immediately
    cached_ip = vm.get('ip')
    if cached_ip and cached_ip not in ("Checking...", "N/A", "Fetching...", "", None):
        # IP exists - use it directly without verification for maximum speed
        # Background sync will update if it changes
        logger.info("rdp_file: Using cached IP %s for VM %s (no verification)", cached_ip, vmid)
    else:
        logger.info("rdp_file: No cached IP for VM %s, will use VM name as fallback", vmid)

    try:
        content = build_rdp(vm)
        filename = f"{vm.get('name', 'vm')}-{vmid}.rdp"
        
        logger.info("rdp_file: generated RDP for VM %s (%s) with IP %s", vmid, vm.get('name'), vm.get('ip'))
        
        # Ensure content is bytes for proper file download
        if isinstance(content, str):
            content = content.encode('utf-8')
        
        resp = Response(content, mimetype='application/x-rdp')
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp
    except ValueError as e:
        # Expected errors like missing IP address
        logger.warning("rdp_file: cannot generate RDP for VM %s: %s", vmid, e)
        return render_template(
            "error.html",
            error_title="RDP File Not Available",
            error_message=str(e),
            back_url=url_for("portal.portal")
        ), 503
    except Exception as e:
        logger.exception("rdp_file: failed to generate RDP for VM %s: %s", vmid, e)
        return render_template(
            "error.html",
            error_title="RDP Generation Failed",
            error_message=f"Failed to generate RDP file: {str(e)}",
            back_url=url_for("portal.portal")
        ), 500
