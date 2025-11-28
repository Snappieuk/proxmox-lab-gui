#!/usr/bin/env python3
"""
SSH API routes blueprint.

Handles SSH terminal page and WebSocket connections.
"""

import json
import logging

from flask import Blueprint, render_template, request, url_for, abort, current_app

from app.utils.decorators import login_required

# Import from legacy module
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'rdp-gen'))

from proxmox_client import find_vm_for_user, verify_vm_ip

logger = logging.getLogger(__name__)

# Check for WebSocket support
try:
    from flask_sock import Sock
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    logger.warning("flask-sock not installed, WebSocket SSH terminal disabled")

api_ssh_bp = Blueprint('api_ssh', __name__)

# WebSocket instance will be set up in app factory
_sock = None


def init_websocket(app, sock):
    """Initialize WebSocket routes. Called from app factory."""
    global _sock
    _sock = sock
    
    if WEBSOCKET_AVAILABLE and sock:
        @sock.route("/ws/ssh/<int:vmid>")
        def ssh_websocket(ws, vmid: int):
            """WebSocket endpoint for SSH connection."""
            from app.services.ssh_service import SSHWebSocketHandler
            
            logger.info("WebSocket SSH connection attempt for VM %d", vmid)
            
            # Get IP from query parameters
            ip = request.args.get('ip')
            
            if not ip:
                logger.error("SSH WebSocket: No IP provided")
                ws.send("\r\n\x1b[1;31mError: IP address not provided.\x1b[0m\r\n")
                return
            
            logger.info("SSH WebSocket: vmid=%d, ip=%s, waiting for credentials", vmid, ip)
            
            # Wait for credentials from client (sent as first message)
            try:
                import time
                time.sleep(0.1)
                
                creds_msg = ws.receive()
                if not creds_msg:
                    logger.error("No credentials received")
                    return
                
                creds = json.loads(creds_msg)
                username = creds.get('username')
                password = creds.get('password')
                
                if not username or not password:
                    logger.error("Invalid credentials message")
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
    vm = find_vm_for_user(user, vmid)
    
    if not vm:
        abort(404)
    
    ip = request.args.get('ip') or vm.get('ip')
    name = request.args.get('name') or vm.get('name', f'VM {vmid}')
    
    # Verify cached IP is still correct
    if ip:
        cluster_id = vm.get('cluster_id', 'cluster1')
        verified_ip = verify_vm_ip(cluster_id, vm['node'], vmid, vm['type'], ip)
        if verified_ip and verified_ip != ip:
            ip = verified_ip
            logger.info("Updated IP for VM %d: %s", vmid, ip)
    
    if not ip:
        return render_template(
            "error.html",
            error_title="SSH Terminal Not Available",
            error_message="VM does not have an IP address. Please start the VM and wait for network configuration.",
            back_url=url_for("portal.portal")
        ), 503
    
    return render_template(
        "ssh_terminal.html",
        vmid=vmid,
        vm_name=name,
        ip=ip
    )
