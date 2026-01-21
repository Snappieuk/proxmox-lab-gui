#!/usr/bin/env python3
"""
SSH API routes blueprint.

Handles SSH terminal page and WebSocket connections.
"""

import json
import logging

from flask import Blueprint, abort, render_template, request, url_for

from app.services.proxmox_client import find_vm_for_user, verify_vm_ip
from app.utils.decorators import login_required

logger = logging.getLogger(__name__)

# Check for WebSocket support
try:
    from flask_sock import Sock  # noqa: F401 - imported for feature detection
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    logger.warning("flask-sock not installed, WebSocket SSH terminal disabled")

api_ssh_bp = Blueprint('api_ssh', __name__)

# WebSocket instance will be set up in app factory
_sock = None


@api_ssh_bp.route("/ssh/test")
def ssh_test():
    """Test endpoint to check if WebSocket support is available."""
    from flask import jsonify
    return jsonify({
        "websocket_available": WEBSOCKET_AVAILABLE,
        "sock_initialized": _sock is not None,
        "message": "WebSocket SSH terminal is " + ("ENABLED" if WEBSOCKET_AVAILABLE and _sock else "DISABLED")
    })


@api_ssh_bp.route("/ssh/diagnostics")
@login_required
def ssh_diagnostics():
    """Serve SSH diagnostics page to help troubleshoot connection issues."""
    return render_template("ssh_diagnostic.html")


def init_websocket(app, sock):
    """Initialize WebSocket routes. Called from app factory."""
    global _sock
    _sock = sock
    
    if WEBSOCKET_AVAILABLE and sock:
        @sock.route("/ws/ssh/<int:vmid>")
        def ssh_websocket(ws, vmid: int):
            """WebSocket endpoint for SSH connection.
            
            Note: When running behind a reverse proxy (nginx/apache), ensure WebSocket
            upgrade headers are properly forwarded:
            
            Nginx example:
                location /ws/ {
                    proxy_pass http://localhost:8080;
                    proxy_http_version 1.1;
                    proxy_set_header Upgrade $http_upgrade;
                    proxy_set_header Connection "upgrade";
                    proxy_set_header Host $host;
                    proxy_set_header X-Real-IP $remote_addr;
                    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
                    proxy_set_header X-Forwarded-Proto $scheme;
                }
            """
            from app.services.ssh_service import SSHWebSocketHandler
            
            # Log proxy headers for debugging
            logger.info("WebSocket SSH connection attempt for VM %d", vmid)
            logger.debug("Headers: Upgrade=%s, Connection=%s, X-Forwarded-For=%s",
                        request.headers.get('Upgrade'),
                        request.headers.get('Connection'),
                        request.headers.get('X-Forwarded-For'))
            
            # Get IP from query parameters
            ip = request.args.get('ip')
            
            if not ip:
                logger.error("SSH WebSocket: No IP provided")
                try:
                    ws.send("\r\n\x1b[1;31mError: IP address not provided.\x1b[0m\r\n")
                except Exception:
                    pass
                return
            
            logger.info("SSH WebSocket: vmid=%d, ip=%s, waiting for credentials", vmid, ip)
            
            # Wait for credentials from client (sent as first message)
            try:
                import time
                time.sleep(0.1)
                
                creds_msg = ws.receive()
                if not creds_msg:
                    logger.error("SSH WebSocket: No credentials received for VM %d (IP: %s). Expected JSON with 'username' and 'password' fields.", vmid, ip)
                    return
                
                creds = json.loads(creds_msg)
                username = creds.get('username')
                password = creds.get('password')
                
                if not username or not password:
                    logger.error("SSH WebSocket: Invalid credentials for VM %d - missing %s", 
                                vmid, 
                                "username and password" if not username and not password 
                                else ("username" if not username else "password"))
                    ws.send("\r\n\x1b[1;31mError: Invalid credentials.\x1b[0m\r\n")
                    return
                
                logger.info("Received credentials for user: %s", username)
                
            except Exception as e:
                logger.error("Error receiving credentials: %s", e)
                ws.send("\r\n\x1b[1;31mError: Failed to receive credentials.\x1b[0m\r\n")
                return
            
            # Create SSH handler with credentials
            logger.info("Creating SSH handler for %s@%s", username, ip)
            handler = SSHWebSocketHandler(ws, ip, username=username, password=password)
            
            # Connect to SSH
            logger.info("Attempting SSH connection...")
            if not handler.connect():
                logger.error("SSH connection failed")
                ws.send("\r\n\x1b[1;31mFailed to establish SSH connection.\x1b[0m\r\n")
                return
            
            logger.info("SSH connection established, entering message loop")
            # Main message loop
            try:
                while handler.running:
                    try:
                        message = ws.receive()
                        if message is None:
                            logger.info("WebSocket receive returned None, closing")
                            break
                        
                        logger.debug("Received WebSocket message: %r", message[:100] if len(message) > 100 else message)
                        
                        # Parse message (expecting JSON with type and data)
                        try:
                            msg = json.loads(message)
                            msg_type = msg.get('type')
                            
                            if msg_type == 'input':
                                data = msg.get('data', '')
                                handler.handle_client_input(data)
                            elif msg_type == 'resize':
                                width = msg.get('width', 80)
                                height = msg.get('height', 24)
                                handler.resize_terminal(width, height)
                        except json.JSONDecodeError:
                            # If not JSON, treat as raw input
                            handler.handle_client_input(message)
                            
                    except Exception as e:
                        if "Connection closed" in str(e) or "ConnectionClosed" in str(type(e).__name__):
                            logger.debug("WebSocket closed by client")
                        else:
                            logger.error("WebSocket error in message loop: %s", e, exc_info=True)
                        break
            finally:
                logger.info("Exiting message loop, closing handler")
                handler.close()


@api_ssh_bp.route("/ssh/<int:vmid>")
@login_required
def ssh_terminal(vmid: int):
    """Render SSH terminal page for a VM."""
    from app.services.user_manager import require_user
    
    user = require_user()
    logger.info("SSH terminal request for VM %d by user %s", vmid, user)
    
    vm = find_vm_for_user(user, vmid)
    
    if not vm:
        logger.error("VM %d not found or not accessible by user %s", vmid, user)
        abort(404)
    
    logger.info("VM %d info: type=%s, node=%s, status=%s", 
                vmid, vm.get('type'), vm.get('node'), vm.get('status'))
    
    ip = request.args.get('ip') or vm.get('ip')
    name = request.args.get('name') or vm.get('name', f'VM {vmid}')
    
    logger.info("VM %d initial IP: %s (from query: %s, from vm: %s)", 
                vmid, ip, request.args.get('ip'), vm.get('ip'))
    
    # Verify cached IP is still correct
    if ip:
        cluster_id = vm.get('cluster_id', 'cluster1')
        verified_ip = verify_vm_ip(cluster_id, vm['node'], vmid, vm['type'], ip)
        if verified_ip and verified_ip != ip:
            ip = verified_ip
            logger.info("Updated IP for VM %d: %s -> %s", vmid, request.args.get('ip'), ip)
    
    if not ip or ip in ('N/A', 'Checking...', 'Fetching...'):
        logger.error("VM %d has no valid IP address (ip=%s)", vmid, ip)
        return render_template(
            "error.html",
            error_title="SSH Terminal Not Available",
            error_message=f"VM does not have an IP address (current: {ip or 'None'}). Please start the VM and wait for network configuration.",
            back_url=url_for("portal.portal")
        ), 503
    
    logger.info("Rendering SSH terminal for VM %d (%s) at IP %s", vmid, name, ip)
    return render_template(
        "ssh_terminal.html",
        vmid=vmid,
        vm_name=name,
        ip=ip
    )
