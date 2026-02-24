"""
Console API endpoints - noVNC console access for VMs

This module provides VNC console access through a WebSocket proxy architecture.
For a cleaner API abstraction, see app.services.proxmox_service module which provides
get_vnc_ticket(), get_auth_ticket(), and find_vm_location() helper functions.
"""

import logging
import ssl
import urllib.parse
from flask import Blueprint, jsonify, request, session, render_template

from app.utils.decorators import login_required
from app.services.user_manager import require_user, is_admin_user
from app.services.proxmox_service import get_proxmox_admin_for_cluster, get_clusters_from_db

logger = logging.getLogger(__name__)

console_bp = Blueprint('console', __name__, url_prefix='/api/console')

# Server-side cache for VNC connection data (to avoid session cookie overflow)
# Key: vmid, Value: dict with connection details
_vnc_connection_cache = {}

# Try to import websocket support
try:
    from flask_sock import Sock  # noqa: F401
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
        
        # Check if user has access to this VM (admin, or VM assigned to user, or teacher/co-owner of class)
        is_admin = is_admin_user(user)
        
        if not is_admin:
            # Check if user has permission to access this VM
            from app.models import VMAssignment, User
            username = user.split('@')[0] if '@' in user else user
            
            # Build username variants
            variants = {username, user}
            if '@' in username:
                base = username.split('@', 1)[0]
                variants.add(base)
            variants.update({v + '@pve' for v in list(variants)})
            variants.update({v + '@pam' for v in list(variants)})
            
            user_row = User.query.filter(User.username.in_(variants)).first()
            
            if not user_row:
                return jsonify({
                    "ok": False,
                    "error": "User not found"
                }), 403
            
            # Check if user has access to this VM
            has_access = False
            
            # Check direct VM assignment
            assignment = VMAssignment.query.filter_by(
                proxmox_vmid=vmid,
                assigned_user_id=user_row.id
            ).first()
            
            if assignment:
                has_access = True
            else:
                # Check if user is teacher/co-owner of class that owns this VM
                from app.models import Class
                vm_assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
                
                if vm_assignment and vm_assignment.class_id:
                    class_ = Class.query.get(vm_assignment.class_id)
                    if class_ and class_.is_owner(user_row):
                        has_access = True
            
            if not has_access:
                return jsonify({
                    "ok": False,
                    "error": "Access denied. You do not have permission to access this VM console."
                }), 403
        
        cluster_id = request.args.get('cluster_id') or session.get('cluster_id')
        if not cluster_id:
            clusters = get_clusters_from_db()
            cluster_id = clusters[0]['id'] if clusters else None
        
        if not cluster_id:
            return jsonify({
                "ok": False,
                "error": "No cluster configured"
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
            except Exception:  # noqa: E722
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
                except Exception:  # noqa: E722
                    pass
            
            if vm_node:
                break
        
        if not vm_node:
            return jsonify({
                "ok": False,
                "error": f"VM {vmid} not found"
            }), 404
        
        # Get cluster configuration for host info
        clusters = get_clusters_from_db()
        cluster_config = None
        for cluster in clusters:
            if cluster['id'] == cluster_id:
                cluster_config = cluster
                break
        
        if not cluster_config:
            return jsonify({
                "ok": False,
                "error": "Cluster configuration not found"
            }), 500
        
        # Generate VNC ticket from Proxmox API with extended timeout
        # Note: generate-password=1 extends ticket lifetime from 60s to 7200s (2 hours)
        try:
            if vm_type == 'kvm':
                # For QEMU VMs
                logger.info(f"Generating VNC ticket for QEMU VM {vmid} on node {vm_node}")
                ticket_data = proxmox.nodes(vm_node).qemu(vmid).vncproxy.post(websocket=1, **{'generate-password': 1})
            else:
                # For LXC containers
                logger.info(f"Generating VNC ticket for LXC container {vmid} on node {vm_node}")
                ticket_data = proxmox.nodes(vm_node).lxc(vmid).vncproxy.post(websocket=1, **{'generate-password': 1})
            
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
    
    # Get ticket from server-side cache instead of session
    conn_data = _vnc_connection_cache.get(vmid)
    if not conn_data or 'ticket' not in conn_data:
        logger.error(f"No ticket found in cache for VM {vmid}")
        return jsonify({"ok": False, "error": "No ticket found"}), 404
    
    ticket = conn_data['ticket']
    logger.info(f"✓ Returning VNC ticket for VM {vmid} (length={len(ticket)})")
    return jsonify({"ok": True, "ticket": ticket})


@console_bp.route("/<int:vmid>/view2", methods=["GET"])
@login_required
def view_console(vmid: int):
    """Serve the noVNC console page. Ticket generation happens in WebSocket handler."""
    try:
        user = require_user()
        logger.info(f"[VNC VIEW2] User '{user}' accessing console for VM {vmid}")
        
        # Check if user has access to this VM (admin, or VM assigned to user, or teacher/co-owner of class)
        is_admin = is_admin_user(user)
        logger.info(f"[VNC VIEW2] User '{user}' is_admin={is_admin}")
        
        if not is_admin:
            # Check if user has permission to access this VM
            from app.models import VMAssignment, User, Class
            username = user.split('@')[0] if '@' in user else user
            
            # Build username variants
            variants = {username, user}
            if '@' in username:
                base = username.split('@', 1)[0]
                variants.add(base)
            variants.update({v + '@pve' for v in list(variants)})
            variants.update({v + '@pam' for v in list(variants)})
            
            user_row = User.query.filter(User.username.in_(variants)).first()
            
            if not user_row:
                return "Access denied: User not found", 403
            
            # Check if user has access to this VM
            has_access = False
            
            # Check direct VM assignment
            assignment = VMAssignment.query.filter_by(
                proxmox_vmid=vmid,
                assigned_user_id=user_row.id
            ).first()
            
            if assignment:
                has_access = True
            else:
                # Check if user is teacher/co-owner of class that owns this VM
                vm_assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
                
                if vm_assignment and vm_assignment.class_id:
                    class_ = Class.query.get(vm_assignment.class_id)
                    if class_ and class_.is_owner(user_row):
                        has_access = True
            
            if not has_access:
                return "Access denied: You do not have permission to access this VM console", 403
        
        # Get cluster info
        cluster_id = request.args.get('cluster_id') or session.get('cluster_id')
        clusters = get_clusters_from_db()
        
        if not cluster_id:
            cluster_id = clusters[0]['id'] if clusters else None
        
        if not cluster_id:
            return "No cluster configured", 500
        
        # Find cluster config
        cluster_config = None
        for cluster in clusters:
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
        # Store connection details in server-side cache instead of session
        # This avoids session cookie overflow when opening multiple consoles
        # CRITICAL: Store authorized user for WebSocket validation
        _vnc_connection_cache[vmid] = {
            'cluster_id': cluster_id,
            'node': vm_node,
            'vm_type': vm_type,
            'host': proxmox_host,
            'proxmox_port': proxmox_port,
            'username': username,
            'password': password,
            'authorized_user': user,  # Store who accessed /view2 for WebSocket validation
        }
        
        # Only store a small reference in session
        session[f'vnc_{vmid}_active'] = True
        
        try:
            proxmox = get_proxmox_admin_for_cluster(cluster_id)
            if vm_type == 'qemu':
                vnc_data = proxmox.nodes(vm_node).qemu(vmid).vncproxy.post(websocket=1)
                if vnc_data:
                    temp_ticket = vnc_data['ticket']
                    _vnc_connection_cache[vmid]['ticket'] = temp_ticket
                    logger.info("Generated temp VNC ticket for frontend auth")
        except Exception as e:
            logger.warning(f"Could not generate temp ticket: {e}")
        
        logger.info(f"Session prepared for VM {vmid} console (node={vm_node}, type={vm_type}, cluster={cluster_id})")
        
        # Render console page - will connect to /ws/vnc/<vmid> WebSocket proxy
        # Pass vmid so frontend can retrieve ticket from session via API if needed
        try:
            logger.info(f"[VNC VIEW2] Rendering console.html template for VM {vmid}")
            rendered = render_template(
                'console.html',
                vmid=vmid,
                vm_name=vm_name,
                websocket_available=WEBSOCKET_AVAILABLE
            )
            logger.info(f"[VNC VIEW2] Template rendered successfully (length={len(rendered)})")
            return rendered
        except Exception as render_error:
            logger.error(f"[VNC VIEW2] Template rendering failed: {render_error}", exc_info=True)
            return f"Template error: {str(render_error)}", 500
        
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
        
        Permission is checked by requiring user to visit /view2 first, which populates
        the cache with authorized_user. WebSocket cannot access session after upgrade.
        """
        proxmox_ws = None
        forward_thread = None
        
        logger.info(f"[VNC WS] ========== WebSocket connection attempt for VM {vmid} ==========")
        logger.info(f"[VNC WS] Client connected from: {ws.environ.get('REMOTE_ADDR', 'unknown')}")
        logger.info(f"[VNC WS] WebSocket protocol: {ws.environ.get('HTTP_SEC_WEBSOCKET_PROTOCOL', 'none')}")
        
        try:
            # CRITICAL: Get connection info from cache (populated by /view2 route after permission check)
            logger.info(f"[VNC WS] Looking up connection data for VM {vmid}...")
            conn_data = _vnc_connection_cache.get(vmid)
            
            if not conn_data:
                logger.error(f"[VNC WS] ❌ No connection data for VM {vmid} - user must visit /console/{vmid}/view2 first")
                logger.error(f"[VNC WS] Current cache keys: {list(_vnc_connection_cache.keys())}")
                try:
                    ws.send('ERROR: No connection data - visit console page first')
                    ws.close(1008, 'No connection data')
                except Exception as e:
                    logger.error(f"[VNC WS] Error sending close message: {e}")
                return
            
            # Log authorized access (authorized_user was set in /view2 after permission check)
            logger.info(f"[VNC WS] Found cache data for VM {vmid}, authorized_user: {conn_data.get('authorized_user', 'N/A')}")
            authorized_user = conn_data.get('authorized_user', 'unknown')
            logger.info(f"WebSocket opened for VM {vmid} console by {authorized_user}")
            
            # Extract connection parameters from cache
            # Extract connection parameters from cache
            cluster_id = conn_data['cluster_id']
            node = conn_data['node']
            vm_type = conn_data['vm_type']
            host = conn_data['host']
            proxmox_port = conn_data['proxmox_port']
            username = conn_data['username']
            password = conn_data['password']
            
            logger.info(f"WebSocket opened for VM {vmid} console (node={node}, type={vm_type}, cluster={cluster_id})")
            
            # Get Proxmox client
            logger.debug(f"[{vmid}] Step 1: Getting Proxmox client for cluster {cluster_id}")
            proxmox = get_proxmox_admin_for_cluster(cluster_id) if cluster_id else get_proxmox_admin_for_cluster(None)
            
            # Generate VNC ticket NOW (just-in-time) to avoid timeout
            logger.info(f"[{vmid}] Step 2: Generating VNC ticket for VM {vmid}...")
            if vm_type == 'qemu':
                vnc_data = proxmox.nodes(node).qemu(vmid).vncproxy.post(websocket=1)
                logger.info(f"[{vmid}] Step 2: VNC ticket generated successfully")
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
            logger.info(f"[{vmid}] Step 3: VNC ticket generated: port={vnc_port}")
            
            # Generate PVEAuthCookie - this authenticates the WebSocket connection
            logger.info(f"[{vmid}] Step 4: Generating PVEAuthCookie for user {username}...")
            try:
                auth_result = proxmox.access.ticket.post(username=username, password=password)
                pve_auth_cookie = auth_result['ticket']
                logger.info(f"[{vmid}] Step 4: ✓ PVEAuthCookie generated successfully (length={len(pve_auth_cookie)})")
            except Exception as auth_error:
                logger.error(f"[{vmid}] Step 4: ❌ Failed to generate PVEAuthCookie: {auth_error}")
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
            
            logger.info(f"[{vmid}] Step 5: Connecting to Proxmox VNC WebSocket...")
            logger.info(f"[{vmid}]   Node: {node}, Type: {vm_type}, VMID: {vmid}, VNC Port: {vnc_port}")
            logger.debug(f"[{vmid}]   URL: {proxmox_ws_url[:80]}...")
            logger.debug(f"[{vmid}]   Ticket (first 30 chars): {ticket[:30]}...")
            logger.debug(f"[{vmid}]   Auth Cookie (first 30 chars): {pve_auth_cookie[:30]}...")
            
            # Store ticket in session for frontend VNC authentication
            session[f'vnc_{vmid}_ticket_for_auth'] = ticket
            
            # Connect to Proxmox with authentication (disable SSL verification for self-signed certs)
            # Need BOTH: PVEAuthCookie for WebSocket AND vncticket in URL for VNC protocol
            logger.debug(f"[{vmid}] Step 6: Creating WebSocket client...")
            proxmox_ws = websocket.WebSocket(sslopt={"cert_reqs": ssl.CERT_NONE})
            
            try:
                logger.debug(f"[{vmid}] Step 7: Initiating connection (timeout=30s)...")
                proxmox_ws.connect(
                    proxmox_ws_url,
                    cookie=f"PVEAuthCookie={pve_auth_cookie}",
                    suppress_origin=True,
                    timeout=30
                )
                logger.info(f"[{vmid}] Step 7: ✓✓✓ Connected to Proxmox VNC successfully ✓✓✓")
                logger.info(f"[{vmid}] Proxmox WebSocket state: connected={proxmox_ws.connected}")
                logger.info(f"[{vmid}] Step 8: ✓ VNC stream ready - starting bidirectional forwarding")
                
            except websocket.WebSocketTimeoutException:
                logger.error("❌ Connection timeout to Proxmox VNC server (30 seconds)")
                logger.error("   This usually means: 1) Proxmox server is slow/overloaded, 2) Firewall blocking WebSocket, 3) VM not responding")
                logger.error(f"   Connection: wss://{host}:{proxmox_port} → node {node} → VM {vmid}")
                logger.error(f"   VNC port: {vnc_port}")
                try:
                    ws.send('Connection timeout - Proxmox server did not respond within 30 seconds')
                    ws.close()
                except Exception:
                    pass
                return
            except Exception as conn_error:
                logger.error(f"❌ Failed to connect to Proxmox WebSocket: {conn_error}")
                logger.error(f"   Error type: {type(conn_error).__name__}")
                logger.error(f"   Connection details: host={host}, port={proxmox_port}, vmid={vmid}")
                logger.error(f"   VNC port: {vnc_port}")
                logger.error(f"   Ticket (first 50 chars): {ticket[:50] if len(ticket) > 50 else ticket}")
                import traceback
                logger.error(f"   Full traceback: {traceback.format_exc()}")
                try:
                    ws.send(f'Connection error: {str(conn_error)}')
                    ws.close()
                except Exception:
                    pass
                return
            
            # Bidirectional forwarding with proper error handling and optimization
            stop_forwarding = threading.Event()
            
            def forward_to_browser():
                """Forward Proxmox → Browser with minimal overhead"""
                try:
                    logger.info(f"[{vmid}] Forward thread started (Proxmox → Browser)")
                    bytes_forwarded = 0
                    while not stop_forwarding.is_set():
                        try:
                            # Use recv() instead of recv_data() for better performance
                            data = proxmox_ws.recv()
                            if not data:
                                logger.info(f"[{vmid}] Proxmox sent empty data, closing")
                                break
                            bytes_forwarded += len(data)
                            # Send immediately without buffering
                            ws.send(data)
                            if bytes_forwarded < 200:  # Log first few messages
                                logger.debug(f"[{vmid}] Forwarded {len(data)} bytes to browser (total: {bytes_forwarded})")
                        except Exception as e:
                            if not stop_forwarding.is_set():
                                logger.info(f"[{vmid}] Forward to browser stopped: {e}")
                            break
                except Exception as e:
                    logger.error(f"[{vmid}] Forward thread error: {e}")
                finally:
                    logger.info(f"[{vmid}] Forward thread ending (total bytes: {bytes_forwarded})")
                    stop_forwarding.set()
            
            # Start forwarding thread with higher priority
            forward_thread = threading.Thread(target=forward_to_browser, daemon=True)
            forward_thread.start()
            
            # Forward browser → Proxmox (main loop) with minimal buffering
            logger.info(f"[{vmid}] Main loop started (Browser → Proxmox)")
            bytes_received = 0
            try:
                while not stop_forwarding.is_set():
                    try:
                        data = ws.receive()
                        if data is None:
                            logger.info(f"[{vmid}] Browser closed connection")
                            break
                        bytes_received += len(data)
                        if bytes_received < 200:  # Log first few messages
                            logger.debug(f"[{vmid}] Received {len(data)} bytes from browser (total: {bytes_received})")
                        # Send immediately with binary opcode for VNC
                        proxmox_ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
                    except Exception as e:
                        if not stop_forwarding.is_set():
                            logger.info(f"[{vmid}] Forward to Proxmox stopped: {e}")
                        break
            except Exception as main_error:
                logger.error(f"[{vmid}] Main loop error: {main_error}", exc_info=True)
            finally:
                stop_forwarding.set()
                logger.info(f"[{vmid}] Main loop ended (total bytes received: {bytes_received})")
            
            logger.info(f"[{vmid}] ========== VNC proxy closed for VM {vmid} ==========")
            
        except Exception as e:
            logger.error(f"[{vmid}] VNC WebSocket proxy error: {e}", exc_info=True)
        finally:
            # Cleanup
            logger.info(f"[{vmid}] Cleaning up WebSocket resources...")
            if forward_thread and forward_thread.is_alive():
                logger.info(f"[{vmid}] Waiting for forward thread...")
                forward_thread.join(timeout=2)
            if proxmox_ws:
                try:
                    proxmox_ws.close()
                except Exception:
                    pass
            try:
                ws.close()
            except Exception:
                pass


