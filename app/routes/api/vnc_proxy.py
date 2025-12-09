"""
VNC WebSocket Proxy - Proxies VNC WebSocket connections with authentication
"""

import logging
import asyncio
import websockets
from flask import Blueprint, request, session
from flask_sock import Sock

from app.utils.decorators import login_required
from app.services.user_manager import require_user
from app.services.proxmox_service import get_proxmox_admin_for_cluster
from app.config import CLUSTERS

logger = logging.getLogger(__name__)

vnc_proxy_bp = Blueprint('vnc_proxy', __name__)

# This will be initialized by the app factory
sock = None

def init_vnc_proxy(app):
    """Initialize WebSocket support for VNC proxy."""
    global sock
    try:
        sock = Sock(app)
        logger.info("VNC WebSocket proxy initialized")
        return True
    except Exception as e:
        logger.warning(f"Could not initialize VNC WebSocket proxy: {e}")
        return False


@vnc_proxy_bp.route('/vnc-proxy/<int:vmid>')
def vnc_proxy_page(vmid: int):
    """Proxy VNC WebSocket connections through Flask to Proxmox."""
    if sock is None:
        return "WebSocket support not available", 500
    
    try:
        user = require_user()
        cluster_id = request.args.get('cluster_id') or session.get('cluster_id')
        
        if not cluster_id:
            return "No cluster selected", 400
        
        # Get cluster config
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
        
        # Find VM
        vm_node = None
        vm_type = None
        for node in proxmox.nodes.get():
            node_name = node['node']
            
            # Check QEMU VMs
            try:
                for vm in proxmox.nodes(node_name).qemu.get():
                    if vm['vmid'] == vmid:
                        vm_node = node_name
                        vm_type = 'qemu'
                        break
            except:
                pass
            
            # Check LXC containers
            if not vm_node:
                try:
                    for vm in proxmox.nodes(node_name).lxc.get():
                        if vm['vmid'] == vmid:
                            vm_node = node_name
                            vm_type = 'lxc'
                            break
                except:
                    pass
            
            if vm_node:
                break
        
        if not vm_node:
            return f"VM {vmid} not found", 404
        
        # Generate VNC ticket
        if vm_type == 'qemu':
            ticket_data = proxmox.nodes(vm_node).qemu(vmid).vncproxy.post(websocket=1)
        else:
            ticket_data = proxmox.nodes(vm_node).lxc(vmid).vncproxy.post(websocket=1)
        
        ticket = ticket_data.get('ticket')
        vnc_port = ticket_data.get('port')
        
        if not ticket:
            return "Failed to generate VNC ticket", 500
        
        logger.info(f"Setting up VNC proxy for VM {vmid} (user: {user})")
        
        # Store connection info in session for the WebSocket handler
        session['vnc_vmid'] = vmid
        session['vnc_node'] = vm_node
        session['vnc_type'] = vm_type
        session['vnc_port'] = vnc_port
        session['vnc_ticket'] = ticket
        session['vnc_cluster'] = cluster_id
        
        from flask import render_template
        return render_template(
            'console_proxy.html',
            vmid=vmid,
            vm_name=f"VM {vmid}"
        )
        
    except Exception as e:
        logger.error(f"VNC proxy error: {e}", exc_info=True)
        return f"Error: {str(e)}", 500


if sock:
    @sock.route('/vnc-ws/<int:vmid>')
    def vnc_websocket(ws, vmid):
        """WebSocket endpoint that proxies to Proxmox VNC."""
        try:
            # Get connection info from session
            if session.get('vnc_vmid') != vmid:
                ws.close(reason="Invalid session")
                return
            
            vm_node = session.get('vnc_node')
            vm_type = session.get('vnc_type')
            vnc_port = session.get('vnc_port')
            ticket = session.get('vnc_ticket')
            cluster_id = session.get('vnc_cluster')
            
            if not all([vm_node, vm_type, vnc_port, ticket, cluster_id]):
                ws.close(reason="Missing connection parameters")
                return
            
            # Get cluster config
            cluster_config = None
            for cluster in CLUSTERS:
                if cluster['id'] == cluster_id:
                    cluster_config = cluster
                    break
            
            if not cluster_config:
                ws.close(reason="Cluster not found")
                return
            
            proxmox_host = cluster_config['host']
            proxmox_port = cluster_config.get('port', 8006)
            
            # Build Proxmox WebSocket URL
            from urllib.parse import quote
            proxmox_ws_url = (
                f"wss://{proxmox_host}:{proxmox_port}/api2/json/nodes/{vm_node}/"
                f"{vm_type}/{vmid}/vncwebsocket?port={vnc_port}&vncticket={quote(ticket)}"
            )
            
            logger.info(f"Proxying VNC WebSocket for VM {vmid}: {proxmox_ws_url[:80]}...")
            
            # Connect to Proxmox (this is the tricky part - need async)
            # For now, just close with a message
            ws.send("VNC Proxy: This feature requires async WebSocket support")
            ws.close()
            
        except Exception as e:
            logger.error(f"VNC WebSocket proxy error: {e}", exc_info=True)
            try:
                ws.close(reason=str(e))
            except:
                pass
