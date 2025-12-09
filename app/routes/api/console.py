"""
Console API endpoints - noVNC console access for VMs

This module provides VNC console access through a WebSocket proxy architecture.
For a cleaner API abstraction, see app.services.proxmox_api module which provides
get_vnc_ticket(), get_auth_ticket(), and find_vm_location() helper functions.
"""

import logging
import ssl
import urllib.parse
from flask import Blueprint, jsonify, request, session, render_template

from app.utils.decorators import login_required
from app.services.user_manager import require_user, is_admin_user
from app.services.class_service import get_user_by_username
from app.services.proxmox_service import get_proxmox_admin_for_cluster
from app.config import CLUSTERS

logger = logging.getLogger(__name__)

console_bp = Blueprint('console', __name__, url_prefix='/api/console')

# Try to import websocket support
try:
    from flask_sock import Sock
    import websocket
    import threading
    WEBSOCKET_AVAILABLE = True
    sock = None  # Will be initialized by app factory
except ImportError:
    WEBSOCKET_AVAILABLE = False
    sock = None
    logger.warning("flask-sock or websocket-client not available. WebSocket proxy disabled.")


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


@console_bp.route("/<int:vmid>/ticket", methods=["GET"])
@login_required
def get_vnc_ticket(vmid: int):
    """Get VNC ticket from session for VNC authentication."""
    session_keys = [k for k in session.keys() if 'vnc' in k.lower()]
    logger.info(f"Ticket request for VM {vmid}, session keys: {session_keys}")
    
    ticket = session.get(f'vnc_{vmid}_ticket_for_auth')
    if not ticket:
        logger.error(f"No ticket found in session for VM {vmid}")
        return jsonify({"ok": False, "error": "No ticket found"}), 404
    
    logger.info(f"✓ Returning VNC ticket for VM {vmid} (length={len(ticket)})")
    return jsonify({"ok": True, "ticket": ticket})


@console_bp.route("/<int:vmid>/view2", methods=["GET"])
@login_required
def view_console(vmid: int):
    """Serve the noVNC console page. Ticket generation happens in WebSocket handler."""
    try:
        user = require_user()
        
        # Get cluster info
        cluster_id = request.args.get('cluster_id') or session.get('cluster_id')
        if not cluster_id:
            cluster_id = CLUSTERS[0]['id'] if CLUSTERS else None
        
        if not cluster_id:
            return "No cluster configured", 500
        
        # Find cluster config
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
        
        # Find the VM across all nodes
        vm_node = None
        vm_type = None
        vm_name = f"VM-{vmid}"
        
        try:
            nodes = proxmox.nodes.get()
            
            for node_info in nodes:
                node = node_info['node']
                
                # Check QEMU VMs
                try:
                    vms = proxmox.nodes(node).qemu.get()
                    for vm in vms:
                        if vm['vmid'] == vmid:
                            vm_node = node
                            vm_type = 'qemu'
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
        
        # Store connection info in session for WebSocket proxy to use
        # Ticket generation happens just-in-time in the WebSocket handler
        proxmox_host = cluster_config['host']
        proxmox_port = cluster_config.get('port', 8006)
        username = cluster_config['user']
        password = cluster_config['password']
        
        # Generate a temporary VNC ticket NOW for the frontend to use
        # This ticket will be regenerated in the WebSocket handler (which is fine - both are valid)
        try:
            proxmox = get_proxmox_admin_for_cluster(cluster_id)
            if vm_type == 'qemu':
                vnc_data = proxmox.nodes(vm_node).qemu(vmid).vncproxy.post(websocket=1)
            else:
                # LXC doesn't support VNC
                vnc_data = None
            
            if vnc_data:
                temp_ticket = vnc_data['ticket']
                session[f'vnc_{vmid}_ticket_for_auth'] = temp_ticket
                logger.info(f"Generated temp VNC ticket for frontend auth")
        except Exception as e:
            logger.warning(f"Could not generate temp ticket: {e}")
        
        session[f'vnc_{vmid}_cluster_id'] = cluster_id
        session[f'vnc_{vmid}_node'] = vm_node
        session[f'vnc_{vmid}_type'] = vm_type
        session[f'vnc_{vmid}_host'] = proxmox_host
        session[f'vnc_{vmid}_proxmox_port'] = proxmox_port
        session[f'vnc_{vmid}_user'] = username
        session[f'vnc_{vmid}_password'] = password
        
        logger.info(f"Session prepared for VM {vmid} console (node={vm_node}, type={vm_type}, cluster={cluster_id})")
        
        # Render console page - will connect to /ws/vnc/<vmid> WebSocket proxy
        # Pass vmid so frontend can retrieve ticket from session via API if needed
        return render_template(
            'console.html',
            vmid=vmid,
            vm_name=vm_name,
            websocket_available=WEBSOCKET_AVAILABLE
        )
        
    except Exception as e:
        logger.error(f"Failed to serve console for VM {vmid}: {e}", exc_info=True)
        return f"Error: {str(e)}", 500


def init_websocket_proxy(app, sock_instance):
    """Initialize WebSocket proxy for VNC connections."""
    global sock
    sock = sock_instance
    
    if not WEBSOCKET_AVAILABLE:
        logger.warning("WebSocket proxy not available - install flask-sock and websocket-client")
        return
    
    @sock.route('/ws/vnc/<int:vmid>')
    def vnc_websocket_proxy(ws, vmid):
        """
        WebSocket proxy: Browser ← Flask ← Proxmox
        This handles all Proxmox authentication so the browser doesn't need to.
        Generates VNC ticket just-in-time to avoid timeout issues.
        """
        proxmox_ws = None
        forward_thread = None
        
        try:
            # Get connection info from session
            cluster_id = session.get(f'vnc_{vmid}_cluster_id')
            node = session.get(f'vnc_{vmid}_node')
            vm_type = session.get(f'vnc_{vmid}_type')
            host = session.get(f'vnc_{vmid}_host')
            proxmox_port = session.get(f'vnc_{vmid}_proxmox_port')  # Proxmox API port (8006)
            username = session.get(f'vnc_{vmid}_user')
            password = session.get(f'vnc_{vmid}_password')
            
            if not all([node, vm_type, host, username, password, proxmox_port]):
                logger.error(f"Missing VNC session data for VM {vmid}. Keys found: {[k for k in session.keys() if f'vnc_{vmid}' in k]}")
                try:
                    ws.close()
                except Exception:
                    pass
                return
            
            logger.info(f"WebSocket opened for VM {vmid} console (node={node}, type={vm_type}, cluster={cluster_id})")
            
            # Get Proxmox client
            proxmox = get_proxmox_admin_for_cluster(cluster_id) if cluster_id else get_proxmox_admin_for_cluster(None)
            
            # Generate VNC ticket NOW (just-in-time) to avoid timeout
            logger.info(f"Generating VNC ticket for VM {vmid}...")
            if vm_type == 'qemu':
                vnc_data = proxmox.nodes(node).qemu(vmid).vncproxy.post(websocket=1)
            elif vm_type == 'lxc':
                # LXC containers don't support VNC - they use terminal/console instead
                logger.error(f"LXC containers (vmid={vmid}) don't support VNC console. Use terminal/SSH instead.")
                try:
                    ws.close()
                except Exception:
                    pass
                return
            else:
                logger.error(f"Unsupported VM type: {vm_type}")
                try:
                    ws.close()
                except Exception:
                    pass
                return
            
            ticket = vnc_data['ticket']
            vnc_port = vnc_data['port']
            logger.info(f"VNC ticket generated: port={vnc_port}")
            
            # Generate PVEAuthCookie - this authenticates the WebSocket connection
            logger.info(f"Generating PVEAuthCookie for user {username}...")
            try:
                auth_result = proxmox.access.ticket.post(username=username, password=password)
                pve_auth_cookie = auth_result['ticket']
                logger.info(f"✓ PVEAuthCookie generated successfully (length={len(pve_auth_cookie)})")
            except Exception as auth_error:
                logger.error(f"❌ Failed to generate PVEAuthCookie: {auth_error}")
                try:
                    ws.close()
                except Exception:
                    pass
                return
            
            # Build Proxmox WebSocket URL with properly formatted query string
            import urllib.parse
            base_url = f"wss://{host}:{proxmox_port}/api2/json/nodes/{node}/{vm_type}/{vmid}/vncwebsocket"
            params = {
                'port': str(vnc_port),
                'vncticket': ticket
            }
            query_string = urllib.parse.urlencode(params)
            proxmox_ws_url = f"{base_url}?{query_string}"
            
            logger.info(f"Connecting to Proxmox VNC WebSocket...")
            logger.info(f"  Node: {node}, Type: {vm_type}, VMID: {vmid}, VNC Port: {vnc_port}")
            logger.info(f"  Ticket (first 30 chars): {ticket[:30]}...")
            logger.info(f"  Auth Cookie (first 30 chars): {pve_auth_cookie[:30]}...")
            
            # Store ticket in session for frontend VNC authentication
            session[f'vnc_{vmid}_ticket_for_auth'] = ticket
            
            # Connect to Proxmox with authentication (disable SSL verification for self-signed certs)
            # Need BOTH: PVEAuthCookie for WebSocket AND vncticket in URL for VNC protocol
            proxmox_ws = websocket.WebSocket(sslopt={"cert_reqs": ssl.CERT_NONE})
            
            try:
                proxmox_ws.connect(
                    proxmox_ws_url,
                    cookie=f"PVEAuthCookie={pve_auth_cookie}",
                    suppress_origin=True,
                    timeout=10
                )
                logger.info(f"✓ Connected to Proxmox VNC for VM {vmid}")
                logger.info(f"✓ VNC stream ready - forwarding will begin when browser connects")
                
            except Exception as conn_error:
                logger.error(f"❌ Failed to connect to Proxmox WebSocket: {conn_error}")
                logger.error(f"   Connection details: host={host}, port={proxmox_port}, vmid={vmid}")
                logger.error(f"   VNC port: {vnc_port}")
                logger.error(f"   Ticket (first 50 chars): {ticket[:50] if len(ticket) > 50 else ticket}")
                import traceback
                logger.error(f"   Full traceback: {traceback.format_exc()}")
                try:
                    ws.close()
                except Exception:
                    pass
                return
            
            # Bidirectional forwarding with proper error handling
            stop_forwarding = threading.Event()
            
            def forward_to_browser():
                """Forward Proxmox → Browser"""
                try:
                    while not stop_forwarding.is_set():
                        try:
                            data = proxmox_ws.recv()
                            if not data:
                                break
                            ws.send(data)
                        except Exception as e:
                            if not stop_forwarding.is_set():
                                logger.error(f"Error forwarding to browser: {e}")
                            break
                except Exception as e:
                    logger.error(f"Forward thread error: {e}")
                finally:
                    stop_forwarding.set()
            
            # Start forwarding thread
            forward_thread = threading.Thread(target=forward_to_browser, daemon=True)
            forward_thread.start()
            
            # Forward browser → Proxmox (main loop)
            try:
                while not stop_forwarding.is_set():
                    try:
                        data = ws.receive()
                        if data is None:
                            break
                        proxmox_ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
                    except Exception as e:
                        if not stop_forwarding.is_set():
                            logger.error(f"Error forwarding to Proxmox: {e}")
                        break
            finally:
                stop_forwarding.set()
            
            logger.info(f"VNC proxy closed for VM {vmid}")
            
        except Exception as e:
            logger.error(f"VNC WebSocket proxy error for VM {vmid}: {e}", exc_info=True)
        finally:
            # Cleanup
            if proxmox_ws:
                try:
                    proxmox_ws.close()
                except Exception:
                    pass
            try:
                ws.close()
            except Exception:
                pass


