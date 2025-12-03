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
    
    user = require_user()
    logger.info("rdp_file: user=%s requesting vmid=%s", user, vmid)
    
    # Use skip_ip=True for initial lookup to avoid triggering ARP scan
    # We'll verify the IP separately if needed
    vm = find_vm_for_user(user, vmid, skip_ip=True)
    if not vm:
        logger.warning("rdp_file: VM %s not found for user %s", vmid, user)
        abort(404)

    logger.info("rdp_file: Found VM: vmid=%s, name=%s, type=%s, category=%s, ip=%s, rdp_available=%s", 
                vm.get('vmid'), vm.get('name'), vm.get('type'), vm.get('category'), 
                vm.get('ip'), vm.get('rdp_available'))

    # Fast IP verification: ping cached IP and check ARP table MAC
    # Only does full network scan if verification fails
    cached_ip = vm.get('ip')
    if cached_ip and cached_ip not in ("Checking...", "N/A", "Fetching...", ""):
        logger.info("rdp_file: Verifying cached IP %s for VM %s", cached_ip, vmid)
        try:
            cluster_id = vm.get('cluster_id', 'cluster1')
            verified_ip = fast_verify_vm_ip(
                cluster_id, 
                vm['node'], 
                vmid, 
                vm.get('type', 'qemu'), 
                cached_ip
            )
            if verified_ip and verified_ip != cached_ip:
                vm['ip'] = verified_ip
                logger.info("rdp_file: Updated IP for VM %s: %s -> %s", vmid, cached_ip, verified_ip)
            elif verified_ip:
                logger.info("rdp_file: IP %s verified for VM %s", verified_ip, vmid)
        except Exception as e:
            logger.warning("rdp_file: Fast IP verification failed for VM %s: %s", vmid, e)
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
