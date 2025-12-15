#!/usr/bin/env python3
"""
Class IP Refresh API - Force IP discovery for class VMs.

Triggers immediate IP discovery using guest agent and ARP scanning.
"""

import logging
from flask import Blueprint, jsonify, request

from app.models import Class, VMAssignment
from app.services.class_service import get_class_by_id
from app.utils.decorators import login_required

logger = logging.getLogger(__name__)

class_ip_refresh_bp = Blueprint('class_ip_refresh', __name__, url_prefix='/api/classes')


@class_ip_refresh_bp.route("/<int:class_id>/refresh-ips", methods=["POST"])
@login_required
def refresh_class_ips(class_id: int):
    """Force IP discovery for all VMs in a class.
    
    Attempts to discover IPs using:
    1. QEMU guest agent (for running VMs)
    2. ARP scanning (for all VMs with MAC addresses)
    """
    from app.services.user_manager import require_user, is_admin_user
    from app.services.class_service import get_user_by_username
    
    user = require_user()
    class_ = get_class_by_id(class_id)
    
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check permissions
    is_admin = is_admin_user(user)
    username = user.split('@')[0] if '@' in user else user
    local_user = get_user_by_username(username)
    
    if not is_admin and not class_.is_owner(local_user):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    logger.info(f"Force IP refresh requested for class {class_id} ({class_.name})")
    
    # Get all VMs for this class
    vm_assignments = VMAssignment.query.filter_by(class_id=class_id).all()
    
    if not vm_assignments:
        return jsonify({"ok": True, "message": "No VMs in this class", "discovered": 0})
    
    discovered_count = 0
    updated_vms = []
    
    # Get Proxmox connection
    from app.services.proxmox_service import get_proxmox_admin_for_cluster
    from app.services.proxmox_service import get_clusters_from_db
    
    cluster_ip = class_.template.cluster_ip if class_.template else None
    cluster_id = None
    
    for cluster in get_clusters_from_db():
        if cluster["host"] == (cluster_ip or get_clusters_from_db()[0]["host"]):
            cluster_id = cluster["id"]
            break
    
    if not cluster_id:
        return jsonify({"ok": False, "error": "Cluster not found"}), 500
    
    try:
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # Build MAC address map for ARP scanning
        vm_mac_map = {}
        
        for assignment in vm_assignments:
            vmid = assignment.proxmox_vmid
            node = assignment.node
            mac_address = assignment.mac_address
            
            # Try guest agent first (for QEMU VMs)
            discovered_ip = None
            
            try:
                # Check if VM is running
                status = proxmox.nodes(node).qemu(vmid).status.current.get()
                
                if status.get('status') == 'running':
                    logger.info(f"Checking guest agent for VM {vmid}...")
                    
                    try:
                        # Try guest agent
                        agent_data = proxmox.nodes(node).qemu(vmid).agent('network-get-interfaces').get()
                        
                        if agent_data and 'result' in agent_data:
                            for iface in agent_data['result']:
                                if iface.get('name') in ['eth0', 'ens18', 'ens19']:
                                    for ip_info in iface.get('ip-addresses', []):
                                        if ip_info.get('ip-address-type') == 'ipv4':
                                            ip_addr = ip_info.get('ip-address', '').strip()
                                            if ip_addr and not ip_addr.startswith('127.'):
                                                discovered_ip = ip_addr
                                                logger.info(f"Guest agent discovered IP {ip_addr} for VM {vmid}")
                                                break
                                if discovered_ip:
                                    break
                    except Exception as e:
                        logger.debug(f"Guest agent failed for VM {vmid}: {e}")
            except Exception as e:
                logger.debug(f"Failed to check VM {vmid} status: {e}")
            
            # Update if IP discovered via guest agent
            if discovered_ip:
                assignment.cached_ip = discovered_ip
                from datetime import datetime
                assignment.ip_cached_at = datetime.utcnow()
                discovered_count += 1
                updated_vms.append({"vmid": vmid, "ip": discovered_ip, "method": "guest-agent"})
            elif mac_address:
                # Add to ARP scan map
                composite_key = f"{node}:{vmid}"
                vm_mac_map[composite_key] = mac_address
        
        # Commit guest agent discoveries
        if discovered_count > 0:
            from app.models import db
            db.session.commit()
            logger.info(f"Updated {discovered_count} IPs via guest agent")
        
        # Run ARP scan for remaining VMs
        if vm_mac_map:
            logger.info(f"Running ARP scan for {len(vm_mac_map)} VMs...")
            
            from app.services.arp_scanner import discover_ips_via_arp
            from app.services.settings_service import get_arp_subnets
            from app.services.proxmox_service import get_clusters_from_db
            
            # Get ARP subnets from cluster configuration
            clusters = get_clusters_from_db()
            subnets = get_arp_subnets(clusters[0]) if clusters else []
            
            # Force immediate ARP scan (not background)
            arp_results = discover_ips_via_arp(vm_mac_map, subnets=subnets, background=False)
            
            for composite_key, ip in arp_results.items():
                if ip:
                    node, vmid_str = composite_key.split(':', 1)
                    vmid = int(vmid_str)
                    
                    # Find assignment and update
                    assignment = next((a for a in vm_assignments if a.proxmox_vmid == vmid), None)
                    if assignment:
                        assignment.cached_ip = ip
                        from datetime import datetime
                        assignment.ip_cached_at = datetime.utcnow()
                        discovered_count += 1
                        updated_vms.append({"vmid": vmid, "ip": ip, "method": "arp-scan"})
                        logger.info(f"ARP scan discovered IP {ip} for VM {vmid}")
            
            # Commit ARP discoveries
            if updated_vms:
                from app.models import db
                db.session.commit()
        
        # Also trigger background sync to update VMInventory
        from app.services.background_sync import trigger_immediate_sync
        trigger_immediate_sync()
        
        logger.info(f"IP refresh complete: {discovered_count} IPs discovered for class {class_id}")
        
        return jsonify({
            "ok": True,
            "message": f"Discovered {discovered_count} IP address(es)",
            "discovered": discovered_count,
            "updated_vms": updated_vms
        })
        
    except Exception as e:
        logger.exception(f"Failed to refresh IPs for class {class_id}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
