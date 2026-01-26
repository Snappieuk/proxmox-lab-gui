#!/usr/bin/env python3
"""
Proxmox API Service - High-level wrapper for Proxmox VE API operations.

This module provides a clean abstraction layer for common Proxmox operations,
including VNC console ticket generation for noVNC integration.
"""

import logging
from typing import Dict, Any, Optional, Tuple

from app.services.proxmox_service import get_proxmox_admin_for_cluster

logger = logging.getLogger(__name__)


def get_vnc_ticket(node: str, vmid: int, vm_type: str = 'kvm', cluster_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Generate a VNC ticket and port for accessing a VM console via noVNC.
    
    This ticket is required for authenticating WebSocket connections to Proxmox's
    VNC proxy. With generate-password=1, the ticket is valid for 2 hours.
    
    Args:
        node: Proxmox node name where the VM is located (e.g., 'pve1', 'pve2')
        vmid: Virtual machine ID
        vm_type: Type of VM - 'kvm' for QEMU VMs, 'lxc' for LXC containers
        cluster_id: Optional cluster ID. If None, uses current session cluster.
        
    Returns:
        Dictionary containing:
            - ticket: VNC authentication ticket (URL-encoded string)
            - port: VNC WebSocket port number
            - cert: SSL certificate fingerprint (for verification)
            - upid: Unique process ID for this console session
            
    Raises:
        ValueError: If vm_type is invalid
        Exception: If Proxmox API call fails
        
    Example:
        >>> vnc_info = get_vnc_ticket('pve1', 112, 'kvm')
        >>> print(f"Connect to VNC on port {vnc_info['port']}")
        >>> print(f"Use ticket: {vnc_info['ticket'][:20]}...")
        
    Notes:
        - Ticket expires in 2 hours (7200s) with generate-password=1
        - For WebSocket URL format: wss://<host>:8006/api2/json/nodes/<node>/<vm_type>/<vmid>/vncwebsocket?port=<port>&vncticket=<ticket>
        - Must also send PVEAuthCookie in WebSocket request headers
    """
    # Validate VM type
    valid_types = ['kvm', 'qemu', 'lxc']
    if vm_type not in valid_types:
        raise ValueError(f"Invalid vm_type '{vm_type}'. Must be one of: {', '.join(valid_types)}")
    
    # Normalize QEMU type
    if vm_type == 'qemu':
        vm_type = 'kvm'
    
    logger.info(f"Generating VNC ticket for {vm_type.upper()} VM {vmid} on node {node}")
    
    try:
        # Get Proxmox client for specified cluster
        proxmox = get_proxmox_admin_for_cluster(cluster_id) if cluster_id else get_proxmox_admin_for_cluster(None)
        
        # Call Proxmox API to generate VNC ticket
        # Parameters:
        #   - websocket=1: Enable WebSocket mode (required for noVNC)
        #   - generate-password=1: Extend ticket lifetime from 60s to 7200s (2 hours)
        if vm_type == 'kvm':
            ticket_data = proxmox.nodes(node).qemu(vmid).vncproxy.post(websocket=1, **{'generate-password': 1})
        else:  # lxc
            ticket_data = proxmox.nodes(node).lxc(vmid).vncproxy.post(websocket=1, **{'generate-password': 1})
        
        # Extract ticket information
        ticket = ticket_data.get('ticket')
        vnc_port = ticket_data.get('port')
        cert = ticket_data.get('cert', '')
        upid = ticket_data.get('upid', '')
        
        if not ticket or not vnc_port:
            raise Exception("Incomplete VNC ticket data received from Proxmox")
        
        logger.info(f"VNC ticket generated successfully: port={vnc_port}, ticket_length={len(ticket)}")
        
        return {
            'ticket': ticket,
            'port': vnc_port,
            'cert': cert,
            'upid': upid
        }
        
    except Exception as e:
        logger.error(f"Failed to generate VNC ticket for VM {vmid}: {e}", exc_info=True)
        raise Exception(f"VNC ticket generation failed: {str(e)}")


def get_auth_ticket(username: str, password: str, cluster_id: Optional[str] = None) -> Tuple[str, str]:
    """
    Generate Proxmox authentication ticket (PVEAuthCookie) and CSRF token.
    
    This is required in addition to the VNC ticket for WebSocket authentication.
    
    Args:
        username: Proxmox username (e.g., 'root@pam')
        password: Proxmox password
        cluster_id: Optional cluster ID. If None, uses current session cluster.
        
    Returns:
        Tuple of (auth_cookie, csrf_token)
        
    Raises:
        Exception: If authentication fails
        
    Example:
        >>> cookie, csrf = get_auth_ticket('root@pam', 'password')
        >>> # Use cookie in WebSocket connection headers
    """
    try:
        proxmox = get_proxmox_admin_for_cluster(cluster_id) if cluster_id else get_proxmox_admin_for_cluster(None)
        
        auth_result = proxmox.access.ticket.post(username=username, password=password)
        
        pve_auth_cookie = auth_result.get('ticket')
        csrf_token = auth_result.get('CSRFPreventionToken')
        
        if not pve_auth_cookie or not csrf_token:
            raise Exception("Incomplete auth data received from Proxmox")
        
        logger.info(f"Authentication ticket generated for user {username}")
        
        return pve_auth_cookie, csrf_token
        
    except Exception as e:
        logger.error(f"Failed to generate auth ticket: {e}", exc_info=True)
        raise Exception(f"Authentication failed: {str(e)}")


def find_vm_location(vmid: int, cluster_id: Optional[str] = None) -> Optional[Dict[str, str]]:
    """
    Find which node and type (kvm/lxc) a VM is located on.
    
    Args:
        vmid: Virtual machine ID to search for
        cluster_id: Optional cluster ID. If None, searches current session cluster.
        
    Returns:
        Dictionary with 'node', 'type', and 'name' if found, None if not found
        
    Example:
        >>> vm_info = find_vm_location(112)
        >>> if vm_info:
        >>>     print(f"VM 112 is on {vm_info['node']} as {vm_info['type']}")
    """
    try:
        proxmox = get_proxmox_admin_for_cluster(cluster_id) if cluster_id else get_proxmox_admin_for_cluster(None)
        
        # Get list of nodes
        nodes = proxmox.nodes.get()
        
        for node_info in nodes:
            node = node_info['node']
            
            # Check QEMU VMs
            try:
                vms = proxmox.nodes(node).qemu.get()
                for vm in vms:
                    if vm['vmid'] == vmid:
                        return {
                            'node': node,
                            'type': 'kvm',
                            'name': vm.get('name', f'VM-{vmid}')
                        }
            except Exception:
                pass
            
            # Check LXC containers
            try:
                containers = proxmox.nodes(node).lxc.get()
                for vm in containers:
                    if vm['vmid'] == vmid:
                        return {
                            'node': node,
                            'type': 'lxc',
                            'name': vm.get('name', f'CT-{vmid}')
                        }
            except Exception:
                pass
        
        logger.warning(f"VM {vmid} not found on any node")
        return None
        
    except Exception as e:
        logger.error(f"Error finding VM {vmid}: {e}", exc_info=True)
        return None
