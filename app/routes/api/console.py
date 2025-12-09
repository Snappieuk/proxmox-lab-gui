"""
Console API endpoints - noVNC console access for VMs
"""

import logging
import urllib.parse
import ssl
from flask import Blueprint, jsonify, request, session, render_template

from app.utils.decorators import login_required
from app.services.user_manager import require_user, is_admin_user
from app.services.class_service import get_user_by_username
from app.services.proxmox_service import get_proxmox_admin_for_cluster
from app.config import CLUSTERS

logger = logging.getLogger(__name__)

console_bp = Blueprint('console', __name__, url_prefix='/api/console')

# Try to import flask-sock for WebSocket proxy
try:
    from flask_sock import Sock
    import websocket as ws_client
    sock = Sock()
    WEBSOCKET_AVAILABLE = True
except ImportError:
    logger.warning("flask-sock or websocket-client not available, WebSocket proxy disabled")
    WEBSOCKET_AVAILABLE = False
    sock = None


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


@console_bp.route("/<int:vmid>/view", methods=["GET"])
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
            
            logger.info(f"Serving console page for VM {vmid} (user: {user})")
            
            # Render the simple console template
            return render_template(
                'console_simple.html',
                vmid=vmid,
                vm_name=vm_name,
                node=vm_node,
                vm_type=vm_type,
                host=cluster_config['host'],
                port=cluster_config.get('port', 8006)
            )
            
        except Exception as e:
            logger.error(f"Failed to generate VNC ticket for VM {vmid}: {e}")
            return f"Failed to generate console ticket: {str(e)}", 500
        
    except Exception as e:
        logger.error(f"Failed to serve console for VM {vmid}: {e}", exc_info=True)
        return f"Error: {str(e)}", 500


@console_bp.route("/<int:vmid>/proxy", methods=["GET"])
@login_required
def websocket_proxy(vmid: int):
    """WebSocket proxy endpoint - tunnels VNC connection through Flask.
    
    This avoids SSL certificate issues by proxying the connection through
    the Flask app instead of connecting directly to Proxmox.
    """
    if not WEBSOCKET_AVAILABLE:
        return jsonify({"ok": False, "error": "WebSocket proxy not available"}), 500
    
    try:
        user = require_user()
        
        # Get parameters from query string
        node = request.args.get('node')
        vm_type = request.args.get('type', 'qemu')
        vnc_port = request.args.get('port')
        ticket = request.args.get('ticket')
        
        if not all([node, vnc_port, ticket]):
            return jsonify({"ok": False, "error": "Missing required parameters"}), 400
        
        # Get cluster from session
        cluster_id = session.get("cluster_id") or (CLUSTERS[0]['id'] if CLUSTERS else None)
        if not cluster_id:
            return jsonify({"ok": False, "error": "No cluster configured"}), 500
        
        # Find cluster config
        cluster_config = None
        for cluster in CLUSTERS:
            if cluster['id'] == cluster_id:
                cluster_config = cluster
                break
        
        if not cluster_config:
            return jsonify({"ok": False, "error": "Cluster not found"}), 404
        
        host = cluster_config['host']
        port = cluster_config.get('port', 8006)
        
        # Build Proxmox WebSocket URL
        encoded_ticket = urllib.parse.quote(ticket, safe='')
        proxmox_ws_url = f"wss://{host}:{port}/api2/json/nodes/{node}/{vm_type}/{vmid}/vncwebsocket?port={vnc_port}&vncticket={encoded_ticket}"
        
        logger.info(f"WebSocket proxy for VM {vmid}: connecting to {proxmox_ws_url[:100]}...")
        
        # Return connection info for WebSocket upgrade
        return jsonify({
            "ok": True,
            "proxmox_url": proxmox_ws_url,
            "vmid": vmid,
            "node": node
        })
        
    except Exception as e:
        logger.error(f"WebSocket proxy setup failed: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


if WEBSOCKET_AVAILABLE and sock:
    @sock.route('/api/console/<int:vmid>/ws')
    def websocket_tunnel(ws, vmid):
        """WebSocket tunnel handler - bidirectional proxy between client and Proxmox.
        
        This function handles the actual WebSocket connection and proxies data
        between the client browser and Proxmox VNC WebSocket.
        """
        try:
            # Get parameters from query string
            node = request.args.get('node')
            vm_type = request.args.get('type', 'qemu')
            vnc_port = request.args.get('port')
            ticket = request.args.get('ticket')
            
            if not all([node, vnc_port, ticket]):
                logger.error("Missing required WebSocket parameters")
                ws.close(reason="Missing parameters")
                return
            
            # Get cluster from session
            cluster_id = session.get("cluster_id") or (CLUSTERS[0]['id'] if CLUSTERS else None)
            if not cluster_id:
                logger.error("No cluster configured")
                ws.close(reason="No cluster configured")
                return
            
            # Find cluster config
            cluster_config = None
            for cluster in CLUSTERS:
                if cluster['id'] == cluster_id:
                    cluster_config = cluster
                    break
            
            if not cluster_config:
                logger.error("Cluster not found")
                ws.close(reason="Cluster not found")
                return
            
            host = cluster_config['host']
            port = cluster_config.get('port', 8006)
            
            # Build Proxmox WebSocket URL
            encoded_ticket = urllib.parse.quote(ticket, safe='')
            proxmox_ws_url = f"wss://{host}:{port}/api2/json/nodes/{node}/{vm_type}/{vmid}/vncwebsocket?port={vnc_port}&vncticket={encoded_ticket}"
            
            logger.info(f"Establishing WebSocket tunnel to VM {vmid}")
            
            # Create SSL context that doesn't verify certificates
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            # Connect to Proxmox WebSocket using websocket-client
            proxmox_ws = ws_client.WebSocket(sslopt={"cert_reqs": ssl.CERT_NONE})
            proxmox_ws.connect(
                proxmox_ws_url,
                timeout=10,
                subprotocols=['binary']
            )
            
            logger.info(f"Connected to Proxmox VNC WebSocket for VM {vmid}")
            
            # Proxy data bidirectionally
            import threading
            
            def forward_to_proxmox():
                """Forward data from client to Proxmox"""
                try:
                    while True:
                        data = ws.receive()
                        if data is None:
                            break
                        # Send as binary
                        if isinstance(data, bytes):
                            proxmox_ws.send_binary(data)
                        else:
                            proxmox_ws.send(data)
                except Exception as e:
                    logger.debug(f"Client->Proxmox forwarding ended: {e}")
                finally:
                    try:
                        proxmox_ws.close()
                    except:
                        pass
            
            def forward_to_client():
                """Forward data from Proxmox to client"""
                try:
                    while True:
                        # Receive from Proxmox
                        opcode, data = proxmox_ws.recv_data()
                        if not data:
                            break
                        ws.send(data)
                except Exception as e:
                    logger.debug(f"Proxmox->Client forwarding ended: {e}")
                finally:
                    try:
                        ws.close()
                    except:
                        pass
            
            # Start forwarding threads
            t1 = threading.Thread(target=forward_to_proxmox, daemon=True)
            t2 = threading.Thread(target=forward_to_client, daemon=True)
            t1.start()
            t2.start()
            
            # Wait for both to finish
            t1.join()
            t2.join()
            
            logger.info(f"WebSocket tunnel closed for VM {vmid}")
            
        except Exception as e:
            logger.error(f"WebSocket tunnel error: {e}", exc_info=True)
            try:
                ws.close(reason=str(e))
            except:
                pass

