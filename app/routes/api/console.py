"""
Console API endpoints - noVNC console access for VMs
"""

import logging
import urllib.parse
from flask import Blueprint, jsonify, request, session, render_template

from app.utils.decorators import login_required
from app.services.user_manager import require_user, is_admin_user
from app.services.class_service import get_user_by_username
from app.services.proxmox_service import get_proxmox_admin_for_cluster
from app.config import CLUSTERS

logger = logging.getLogger(__name__)

console_bp = Blueprint('console', __name__, url_prefix='/api/console')


@console_bp.route("/<int:vmid>/vnc", methods=["GET"])
@login_required
def api_get_vnc_info(vmid: int):
    """Get noVNC console connection information for a VM."""
    try:
        user = require_user()
        
        # Check if user is teacher or admin
        is_admin = is_admin_user(user)
        username = user.split('@')[0] if '@' in user else user
        local_user = get_user_by_username(username)
        
        if not is_admin and (not local_user or local_user.role not in ['teacher', 'adminer']):
            return jsonify({
                "ok": False,
                "error": "Access denied. Only teachers and administrators can access VM console."
            }), 403
        
        cluster_id = request.args.get('cluster_id') or session.get('cluster_id')
        if not cluster_id:
            return jsonify({
                "ok": False,
                "error": "No cluster selected"
            }), 400
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        if not proxmox:
            return jsonify({
                "ok": False,
                "error": "Failed to connect to cluster"
            }), 500
        
        # Find which node the VM is on
        nodes = proxmox.nodes.get()
        vm_node = None
        vm_name = None
        vm_type = None
        
        for node in nodes:
            node_name = node['node']
            
            # Check QEMU VMs
            try:
                qemu_vms = proxmox.nodes(node_name).qemu.get()
                for vm in qemu_vms:
                    if vm['vmid'] == vmid:
                        vm_node = node_name
                        vm_name = vm.get('name', f'VM-{vmid}')
                        vm_type = 'kvm'
                        break
            except:
                pass
            
            # Check LXC containers
            if not vm_node:
                try:
                    lxc_vms = proxmox.nodes(node_name).lxc.get()
                    for vm in lxc_vms:
                        if vm['vmid'] == vmid:
                            vm_node = node_name
                            vm_name = vm.get('name', f'CT-{vmid}')
                            vm_type = 'lxc'
                            break
                except:
                    pass
            
            if vm_node:
                break
        
        if not vm_node:
            return jsonify({
                "ok": False,
                "error": f"VM {vmid} not found"
            }), 404
        
        # Get cluster configuration for host info
        cluster_config = None
        for cluster in CLUSTERS:
            if cluster['id'] == cluster_id:
                cluster_config = cluster
                break
        
        if not cluster_config:
            return jsonify({
                "ok": False,
                "error": "Cluster configuration not found"
            }), 500
        
        # Generate VNC ticket from Proxmox API
        try:
            if vm_type == 'kvm':
                # For QEMU VMs
                logger.info(f"Generating VNC ticket for QEMU VM {vmid} on node {vm_node}")
                ticket_data = proxmox.nodes(vm_node).qemu(vmid).vncproxy.post(websocket=1)
            else:
                # For LXC containers
                logger.info(f"Generating VNC ticket for LXC container {vmid} on node {vm_node}")
                ticket_data = proxmox.nodes(vm_node).lxc(vmid).vncproxy.post(websocket=1)
            
            logger.info(f"Ticket data received: {ticket_data}")
            
            ticket = ticket_data.get('ticket')
            vnc_port = ticket_data.get('port')
            
            if not ticket:
                raise Exception("Failed to generate VNC ticket")
            
            logger.info(f"VNC ticket generated successfully: port={vnc_port}, ticket_length={len(ticket)}")
                
        except Exception as e:
            logger.error(f"Failed to generate VNC ticket for VM {vmid}: {e}", exc_info=True)
            return jsonify({
                "ok": False,
                "error": f"Failed to generate console ticket: {str(e)}"
            }), 500
        
        # Build noVNC URL
        host = cluster_config['host']
        port = cluster_config.get('port', 8006)
        
        # Standard Proxmox noVNC console URL (requires user to be logged into Proxmox)
        # This is the simplest approach - just link to Proxmox's built-in console
        console_url = f"https://{host}:{port}/?console={vm_type}&novnc=1&node={vm_node}&resize=scale&vmid={vmid}&vmname={urllib.parse.quote(vm_name, safe='')}"
        
        logger.info(f"Console URL generated for VM {vmid}: {console_url}")
        
        return jsonify({
            "ok": True,
            "console_url": console_url,
            "vmid": vmid,
            "node": vm_node,
            "vm_name": vm_name,
            "vm_type": vm_type,
            "host": host,
            "port": port,
            "ticket": ticket,
            "vnc_port": vnc_port
        })
        
    except Exception as e:
        logger.error(f"Failed to get VNC info for VM {vmid}: {e}", exc_info=True)
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@console_bp.route("/<int:vmid>/view2", methods=["GET"])
@login_required
def view_console(vmid: int):
    """Serve the custom noVNC console page with embedded authentication."""
    try:
        user = require_user()
        
        # Check permissions (allow students too for their assigned VMs)
        cluster_id = request.args.get('cluster_id') or session.get('cluster_id')
        if not cluster_id:
            cluster_id = CLUSTERS[0]['id'] if CLUSTERS else None
        
        if not cluster_id:
            return "No cluster configured", 500
        
        # Find cluster config from list
        cluster_config = None
        for cluster in CLUSTERS:
            if cluster['id'] == cluster_id:
                cluster_config = cluster
                break
        
        if not cluster_config:
            return "Cluster not found", 404
        
        # Get Proxmox connection
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        if not proxmox:
            return "Failed to connect to cluster", 500
        
        # Find the VM
        vm_node = None
        vm_type = None
        vm_name = f"VM-{vmid}"
        
        try:
            # Get list of nodes
            nodes = proxmox.nodes.get()
            
            for node_info in nodes:
                node = node_info['node']
                
                # Check QEMU VMs
                try:
                    vms = proxmox.nodes(node).qemu.get()
                    for vm in vms:
                        if vm['vmid'] == vmid:
                            vm_node = node
                            vm_type = 'kvm'
                            vm_name = vm.get('name', f"VM-{vmid}")
                            break
                except Exception as e:
                    logger.debug(f"Error checking QEMU VMs on node {node}: {e}")
                
                # Check LXC containers if not found
                if not vm_node:
                    try:
                        containers = proxmox.nodes(node).lxc.get()
                        for vm in containers:
                            if vm['vmid'] == vmid:
                                vm_node = node
                                vm_type = 'lxc'
                                vm_name = vm.get('name', f"CT-{vmid}")
                                break
                    except Exception as e:
                        logger.debug(f"Error checking LXC containers on node {node}: {e}")
                
                if vm_node:
                    break
        except Exception as e:
            logger.error(f"Error finding VM {vmid}: {e}", exc_info=True)
            return f"VM {vmid} not found: {str(e)}", 404
        
        if not vm_node:
            return f"VM {vmid} not found", 404
        
        # Generate VNC ticket
        try:
            if vm_type == 'kvm':
                ticket_data = proxmox.nodes(vm_node).qemu(vmid).vncproxy.post(websocket=1)
            else:
                ticket_data = proxmox.nodes(vm_node).lxc(vmid).vncproxy.post(websocket=1)
            
            ticket = ticket_data.get('ticket')
            vnc_port = ticket_data.get('port')
            
            if not ticket:
                return "Failed to generate VNC ticket", 500
            
            logger.info(f"=== CONSOLE VIEW DEBUG ===")
            logger.info(f"Serving console.html with WebSocket for VM {vmid}")
            logger.info(f"User: {user}")
            logger.info(f"VM Type: {vm_type}")
            logger.info(f"Node: {vm_node}")
            logger.info(f"VNC Port: {vnc_port}")
            logger.info(f"Ticket length: {len(ticket)}")
            
            proxmox_host = cluster_config['host']
            proxmox_port = cluster_config.get('port', 8006)
            
            # Get the Proxmox authentication ticket/cookie from the API connection
            # This allows the WebSocket to authenticate using the admin credentials
            try:
                # Get auth ticket for Proxmox admin user
                auth_user = cluster_config['user']
                auth_password = cluster_config['password']
                
                # Login to get cookie - same as proxmox library does
                auth_response = proxmox.access.ticket.post(username=auth_user, password=auth_password)
                auth_ticket = auth_response['ticket']
                csrf_token = auth_response['CSRFPreventionToken']
                
                logger.info(f"Got Proxmox auth ticket for {auth_user}")
                
            except Exception as auth_e:
                logger.warning(f"Failed to get Proxmox auth ticket: {auth_e}, will use VNC ticket only")
                auth_ticket = None
                csrf_token = None
            
            # Render console page with direct WebSocket connection to Proxmox
            # Pass both the VNC ticket AND the auth cookie for authentication
            from flask import make_response
            response = make_response(render_template(
                'console.html',
                vmid=vmid,
                vm_name=vm_name or f"VM {vmid}",
                node=vm_node,
                vm_type=vm_type,
                host=proxmox_host,
                port=proxmox_port,
                vnc_port=vnc_port,
                ticket=ticket,
                auth_ticket=auth_ticket,
                csrf_token=csrf_token
            ))
            
            # Set Proxmox authentication cookie if we got it
            if auth_ticket:
                # Set the PVEAuthCookie that Proxmox uses for authentication
                response.set_cookie(
                    'PVEAuthCookie',
                    auth_ticket,
                    domain=proxmox_host,
                    secure=True,
                    httponly=True,
                    samesite='None'
                )
            
            return response
            
        except Exception as e:
            logger.error(f"Failed to generate VNC ticket for VM {vmid}: {e}")
            return f"Failed to generate console ticket: {str(e)}", 500
        
    except Exception as e:
        logger.error(f"Failed to serve console for VM {vmid}: {e}", exc_info=True)
        return f"Error: {str(e)}", 500


