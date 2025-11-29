#!/usr/bin/env python3
"""
Proxmox Operations - VM cloning, template management, and snapshots.

This module provides higher-level operations for class-based VM management.
"""

import logging
import re
from typing import List, Dict, Tuple, Any, Optional

from app.services.proxmox_service import get_proxmox_admin_for_cluster
from app.config import CLUSTERS

logger = logging.getLogger(__name__)

# Default cluster IP for class operations (restricted to single cluster)
CLASS_CLUSTER_IP = "10.220.15.249"


def sanitize_vm_name(name: str, fallback: str = "vm") -> str:
    """Sanitize a proposed VM name to satisfy Proxmox (DNS-style) constraints.

    Rules applied (approx RFC 1123 hostname subset):
    - Lowercase all characters
    - Allow only a-z, 0-9, hyphen; replace others with '-'
    - Collapse consecutive hyphens
    - Trim leading/trailing hyphens
    - Ensure starts/ends with alphanumeric (prepend/append fallback tokens if needed)
    - Limit length to 63 chars
    - If result empty, use fallback
    This keeps behavior predictable and prevents 400 clone errors for invalid names.
    """
    if not isinstance(name, str):
        name = str(name) if name is not None else ""
    original = name
    name = name.lower().strip()
    # Replace invalid chars with '-'
    name = re.sub(r"[^a-z0-9-]", "-", name)
    # Collapse multiple hyphens
    name = re.sub(r"-+", "-", name)
    # Strip leading/trailing hyphens
    name = name.strip('-')
    if not name:
        name = fallback
    # Ensure starts with alphanumeric
    if not name[0].isalnum():
        name = f"{fallback}-{name}" if name else fallback
    # Ensure ends with alphanumeric
    if not name[-1].isalnum():
        name = f"{name}0"
    # Truncate to 63 characters (typical hostname limit)
    if len(name) > 63:
        name = name[:63].rstrip('-')
        if not name:
            name = fallback
    if name != original:
        logger.info(f"sanitize_vm_name: '{original}' -> '{name}'")
    return name


def list_proxmox_templates(cluster_ip: str = None) -> List[Dict[str, Any]]:
    """List all VM templates on a Proxmox cluster.
    
    Args:
        cluster_ip: IP of the Proxmox cluster (restricted to 10.220.15.249)
    
    Returns:
        List of template dicts with keys: vmid, name, node, description
    """
    try:
        # Restrict to 10.220.15.249 cluster only
        target_ip = CLASS_CLUSTER_IP
        if cluster_ip and cluster_ip != CLASS_CLUSTER_IP:
            logger.warning(f"Template listing restricted to {CLASS_CLUSTER_IP}, ignoring request for {cluster_ip}")
        
        # Find cluster by IP
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == target_ip:
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            logger.error(f"Cluster not found for IP: {target_ip}")
            return []
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        templates = []
        
        for node in proxmox.nodes.get():
            node_name = node["node"]
            
            # Get QEMU VMs
            for vm in proxmox.nodes(node_name).qemu.get():
                if vm.get("template") == 1:
                    templates.append({
                        "vmid": vm["vmid"],
                        "name": vm.get("name", f"VM-{vm['vmid']}"),
                        "node": node_name,
                        "description": vm.get("description", ""),
                        "type": "qemu"
                    })
        
        return templates
    except Exception as e:
        logger.exception(f"Failed to list templates: {e}")
        return []


def clone_vm_from_template(template_vmid: int, new_vmid: int, name: str, node: str, 
                           cluster_ip: str = None) -> Tuple[bool, str]:
    """Clone a VM from a template.
    
    Args:
        template_vmid: VMID of the template to clone
        new_vmid: VMID for the new VM
        name: Name for the new VM
        node: Node to create the VM on
        cluster_ip: IP of the Proxmox cluster (restricted to 10.220.15.249)
    
    Returns:
        Tuple of (success, message)
    """
    try:
        # Restrict to 10.220.15.249 cluster only
        target_ip = CLASS_CLUSTER_IP
        if cluster_ip and cluster_ip != CLASS_CLUSTER_IP:
            return False, f"VM deployment restricted to {CLASS_CLUSTER_IP} cluster only"
        
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == target_ip:
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, f"Cluster not found for IP: {target_ip}"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        safe_name = sanitize_vm_name(name)
        # Clone the template
        proxmox.nodes(node).qemu(template_vmid).clone.post(
            newid=new_vmid,
            name=safe_name,
            full=1  # Full clone (not linked)
        )

        logger.info(f"Cloned template {template_vmid} to {new_vmid} ({safe_name}) on {node}")
        return True, f"Successfully cloned VM {safe_name}"
        
    except Exception as e:
        logger.exception(f"Failed to clone VM: {e}")
        return False, f"Clone failed: {str(e)}"


def clone_vms_for_class(template_vmid: int, node: str, count: int, name_prefix: str,
                        cluster_ip: str = None, task_id: str = None) -> List[Dict[str, Any]]:
    """Clone multiple VMs from a template for a class.
    
    Args:
        template_vmid: VMID of the template
        node: Node to create VMs on
        count: Number of VMs to clone
        name_prefix: Prefix for VM names (will append number)
        cluster_ip: IP of the Proxmox cluster (restricted to 10.220.15.249)
        task_id: Optional task ID for progress tracking
    
    Returns:
        List of dicts with keys: vmid, name, node
    """
    from app.services.clone_progress import update_clone_progress
    
    created_vms = []
    
    try:
        # Restrict to 10.220.15.249 cluster only
        target_ip = CLASS_CLUSTER_IP
        if cluster_ip and cluster_ip != CLASS_CLUSTER_IP:
            logger.error(f"VM deployment restricted to {CLASS_CLUSTER_IP}, ignoring request for {cluster_ip}")
            return []
        
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == target_ip:
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            logger.error(f"Cluster not found for IP: {target_ip}")
            return []
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # Get next available VMID - use cluster resources API for complete view
        used_vmids = set()
        try:
            # Use cluster resources API to get ALL VMs and containers across all nodes
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                used_vmids.add(r["vmid"])
            logger.info(f"Found {len(used_vmids)} existing VMIDs via cluster resources API")
        except Exception as e:
            logger.warning(f"Cluster resources API failed, falling back to per-node query: {e}")
            # Fallback: query each node individually
            for node_info in proxmox.nodes.get():
                node_name = node_info["node"]
                try:
                    # Get both QEMU VMs and LXC containers
                    for vm in proxmox.nodes(node_name).qemu.get():
                        used_vmids.add(vm["vmid"])
                    for ct in proxmox.nodes(node_name).lxc.get():
                        used_vmids.add(ct["vmid"])
                except Exception as node_error:
                    logger.warning(f"Failed to query node {node_name}: {node_error}")
        
        next_vmid = 200  # Start from 200 for class VMs
        
        for i in range(count):
            # Find next available VMID
            while next_vmid in used_vmids:
                next_vmid += 1
            
            vm_name = f"{name_prefix}-{i+1}"
            vm_name = sanitize_vm_name(vm_name, fallback="classvm")
            
            # Update progress
            if task_id:
                update_clone_progress(task_id, current_vm=vm_name)
            
            success, msg = clone_vm_from_template(
                template_vmid=template_vmid,
                new_vmid=next_vmid,
                name=vm_name,
                node=node,
                cluster_ip=cluster_ip
            )
            
            if success:
                created_vms.append({
                    "vmid": next_vmid,
                    "name": vm_name,
                    "node": node
                })
                used_vmids.add(next_vmid)
                next_vmid += 1
                
                # Update progress
                if task_id:
                    update_clone_progress(task_id, completed=len(created_vms))
            else:
                logger.error(f"Failed to clone VM {vm_name}: {msg}")
                if task_id:
                    update_clone_progress(task_id, failed=i+1-len(created_vms), error=msg)
        
        return created_vms
        
    except Exception as e:
        logger.exception(f"Failed to clone VMs for class: {e}")
        if task_id:
            update_clone_progress(task_id, status="failed", error=str(e))
        return created_vms


def start_class_vm(vmid: int, node: str, cluster_ip: str = None) -> Tuple[bool, str]:
    """Start a class VM.
    
    Args:
        vmid: VM ID
        node: Node name
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, message)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, f"Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        proxmox.nodes(node).qemu(vmid).status.start.post()
        
        logger.info(f"Started VM {vmid} on {node}")
        return True, "VM started successfully"
        
    except Exception as e:
        logger.exception(f"Failed to start VM {vmid}: {e}")
        return False, f"Start failed: {str(e)}"


def stop_class_vm(vmid: int, node: str, cluster_ip: str = None) -> Tuple[bool, str]:
    """Stop a class VM.
    
    Args:
        vmid: VM ID
        node: Node name
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, message)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, f"Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        proxmox.nodes(node).qemu(vmid).status.shutdown.post()
        
        logger.info(f"Stopped VM {vmid} on {node}")
        return True, "VM stopped successfully"
        
    except Exception as e:
        logger.exception(f"Failed to stop VM {vmid}: {e}")
        return False, f"Stop failed: {str(e)}"


def get_vm_status(vmid: int, node: str = None, cluster_ip: str = None) -> Dict[str, Any]:
    """Get VM status.
    
    Args:
        vmid: VM ID
        node: Node name (optional, will be auto-discovered if not provided)
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Dict with VM status info (just the status string, not full object)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return "unknown"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # If node not provided, find it from cluster resources
        if not node or node == "qemu" or node == "None":
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                if r.get('vmid') == vmid:
                    node = r.get('node')
                    break
        
        if not node:
            return "unknown"
        
        status = proxmox.nodes(node).qemu(vmid).status.current.get()
        
        # Return just the status string (running/stopped) not the full object
        return status.get("status", "unknown")
        
    except Exception as e:
        logger.exception(f"Failed to get VM status: {e}")
        return "unknown"


def create_vm_snapshot(vmid: int, node: str, snapname: str, description: str = "",
                      cluster_ip: str = None) -> Tuple[bool, str]:
    """Create a snapshot of a VM.
    
    Args:
        vmid: VM ID
        node: Node name
        snapname: Snapshot name
        description: Snapshot description
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, message)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, "Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        proxmox.nodes(node).qemu(vmid).snapshot.post(
            snapname=snapname,
            description=description
        )
        
        logger.info(f"Created snapshot {snapname} for VM {vmid}")
        return True, f"Snapshot '{snapname}' created"
        
    except Exception as e:
        logger.exception(f"Failed to create snapshot: {e}")
        return False, f"Snapshot failed: {str(e)}"


def revert_vm_to_snapshot(vmid: int, node: str, snapname: str,
                          cluster_ip: str = None) -> Tuple[bool, str]:
    """Revert a VM to a snapshot.
    
    Args:
        vmid: VM ID
        node: Node name
        snapname: Snapshot name
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, message)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, "Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        proxmox.nodes(node).qemu(vmid).snapshot(snapname).rollback.post()
        
        logger.info(f"Reverted VM {vmid} to snapshot {snapname}")
        return True, f"Reverted to snapshot '{snapname}'"
        
    except Exception as e:
        logger.exception(f"Failed to revert to snapshot: {e}")
        return False, f"Revert failed: {str(e)}"


def delete_vm(vmid: int, node: str, cluster_ip: str = None) -> Tuple[bool, str]:
    """Delete a VM.
    
    Args:
        vmid: VM ID
        node: Node name
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, message)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, "Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        proxmox.nodes(node).qemu(vmid).delete()
        
        logger.info(f"Deleted VM {vmid} on {node}")
        return True, "VM deleted successfully"
        
    except Exception as e:
        logger.exception(f"Failed to delete VM {vmid}: {e}")
        return False, f"Delete failed: {str(e)}"


def convert_vm_to_template(vmid: int, node: str, cluster_ip: str = None) -> Tuple[bool, str]:
    """Convert a VM to a template.
    
    Args:
        vmid: VM ID
        node: Node name
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, message)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, "Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        proxmox.nodes(node).qemu(vmid).template.post()
        
        logger.info(f"Converted VM {vmid} to template on {node}")
        return True, "VM converted to template"
        
    except Exception as e:
        logger.exception(f"Failed to convert VM to template: {e}")
        return False, f"Conversion failed: {str(e)}"


def clone_template_for_class(template_vmid: int, node: str, name: str,
                             cluster_ip: str = None) -> Tuple[bool, Optional[int], str]:
    """Clone a template to create a working copy for a class.
    
    Args:
        template_vmid: VMID of the template to clone
        node: Node where template exists
        name: Name for the cloned VM
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, new_vmid, message)
    """
    try:
        # Validate node parameter
        if not node:
            logger.error(f"clone_template_for_class: node parameter is empty or None")
            return False, None, "Template node is not set. Please ensure the template has a valid node."
        
        target_ip = cluster_ip or CLASS_CLUSTER_IP
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == target_ip:
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, None, f"Cluster not found for IP: {target_ip}"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        safe_name = sanitize_vm_name(name, fallback="clonetemplate")
        logger.info(f"Cloning template VMID {template_vmid} from node '{node}' to create '{safe_name}' (original requested name: '{name}')")
        
        # Find next available VMID - use cluster resources API for complete view
        used_vmids = set()
        try:
            # Use cluster resources API to get ALL VMs and containers across all nodes
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                used_vmids.add(r["vmid"])
            logger.info(f"Found {len(used_vmids)} existing VMIDs via cluster resources API")
        except Exception as e:
            logger.warning(f"Cluster resources API failed, falling back to per-node query: {e}")
            # Fallback: query each node individually
            for node_info in proxmox.nodes.get():
                node_name = node_info["node"]
                try:
                    # Get both QEMU VMs and LXC containers
                    for vm in proxmox.nodes(node_name).qemu.get():
                        used_vmids.add(vm["vmid"])
                    for ct in proxmox.nodes(node_name).lxc.get():
                        used_vmids.add(ct["vmid"])
                except Exception as node_error:
                    logger.warning(f"Failed to query node {node_name}: {node_error}")
        
        # Find next available VMID starting from 100
        new_vmid = 100
        while new_vmid in used_vmids:
            new_vmid += 1
        
        logger.info(f"Selected new VMID: {new_vmid} (next available after checking {len(used_vmids)} existing VMs)")
        
        # Clone the template (full clone, not linked)
        logger.info(f"Calling proxmox.nodes('{node}').qemu({template_vmid}).clone.post(newid={new_vmid}, name='{safe_name}', full=1)")
        
        try:
            proxmox.nodes(node).qemu(template_vmid).clone.post(
                newid=new_vmid,
                name=safe_name,
                full=1  # Full clone
            )
        except Exception as clone_error:
            # If clone fails due to VMID conflict, try once more with next VMID
            if "already exists" in str(clone_error).lower() or "vmid" in str(clone_error).lower():
                logger.warning(f"VMID {new_vmid} conflict detected, trying next VMID...")
                new_vmid += 1
                while new_vmid in used_vmids:
                    new_vmid += 1
                
                logger.info(f"Retrying clone with VMID {new_vmid}")
                proxmox.nodes(node).qemu(template_vmid).clone.post(
                    newid=new_vmid,
                    name=safe_name,
                    full=1
                )
            else:
                raise
        
        logger.info(f"Cloned template {template_vmid} → {new_vmid} ({safe_name}) on {node}")
        return True, new_vmid, f"Successfully cloned template '{safe_name}' to VMID {new_vmid}"
        
    except Exception as e:
        logger.exception(f"Failed to clone template: {e}")
        return False, None, f"Clone failed: {str(e)}"
