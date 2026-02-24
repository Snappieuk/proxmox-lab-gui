#!/usr/bin/env python3
"""
Linked clone deployment service - snapshot-based VM cloning.

Creates true linked clones (snapshot-based) that share template disk for
maximum speed and minimal disk usage. VMs automatically created on same
storage backend as template (supports TRUENAS-NFS, NFS-Datastore, local-lvm, etc.).

Includes automatic load balancing across cluster nodes and fast deployment.
"""

import logging
from typing import List, Tuple, Dict

from app.models import db, VMAssignment, Class
from app.services.ssh_executor import get_pooled_ssh_executor_from_config
from app.services.proxmox_service import get_proxmox_admin_for_cluster, get_clusters_from_db

logger = logging.getLogger(__name__)


def get_available_vmid(cluster_config: dict, start_vmid: int = 100) -> int:
    """
    Find an available VM ID on the cluster.
    
    Args:
        cluster_config: Cluster configuration dict
        start_vmid: Starting point for search (default 100)
    
    Returns:
        First available VMID >= start_vmid
    """
    try:
        proxmox = get_proxmox_admin_for_cluster(cluster_config["id"])
        
        # Get all existing VMIDs from all nodes
        used_vmids = set()
        for node in proxmox.nodes.get():
            node_name = node['node']
            try:
                # Get QEMU VMs
                qemu_vms = proxmox.nodes(node_name).qemu.get()
                for vm in qemu_vms:
                    used_vmids.add(vm['vmid'])
            except Exception:
                pass
            
            try:
                # Get LXC containers
                lxc_vms = proxmox.nodes(node_name).lxc.get()
                for vm in lxc_vms:
                    used_vmids.add(vm['vmid'])
            except Exception:
                pass
        
        # Find first available
        candidate = start_vmid
        while candidate in used_vmids:
            candidate += 1
        
        return candidate
        
    except Exception as e:
        logger.error(f"Failed to get available VMID: {e}")
        # Return start_vmid as fallback
        return start_vmid


def get_nodes_for_load_balancing(cluster_config: dict, count: int = 1) -> List[str]:
    """
    Get list of nodes for load balancing VM placement.
    
    Distributes VMs across nodes based on current VM count.
    
    Args:
        cluster_config: Cluster configuration dict
        count: Number of nodes to return (for distributing VMs)
    
    Returns:
        List of node names for VM placement
    """
    try:
        proxmox = get_proxmox_admin_for_cluster(cluster_config["id"])
        nodes = proxmox.nodes.get()
        
        if not nodes:
            logger.error("No nodes available in cluster")
            return []
        
        # Get VM count per node
        node_vm_counts = {}
        for node in nodes:
            node_name = node['node']
            vm_count = 0
            
            try:
                qemu_vms = proxmox.nodes(node_name).qemu.get()
                vm_count += len(qemu_vms)
            except Exception:
                pass
            
            try:
                lxc_vms = proxmox.nodes(node_name).lxc.get()
                vm_count += len(lxc_vms)
            except Exception:
                pass
            
            node_vm_counts[node_name] = vm_count
        
        # Sort by VM count (least loaded first) and return top 'count' nodes
        sorted_nodes = sorted(node_vm_counts.items(), key=lambda x: x[1])
        return [node[0] for node in sorted_nodes[:count]]
        
    except Exception as e:
        logger.error(f"Failed to get load balanced nodes: {e}")
        return []


def get_template_storage(proxmox, node: str, template_vmid: int) -> str:
    """
    Get the storage location of a template VM's disk.
    
    Args:
        proxmox: ProxmoxAPI connection
        node: Node where template resides
        template_vmid: Template VM ID
    
    Returns:
        Storage name (e.g., "TRUENAS-NFS", "NFS-Datastore", "local-lvm")
    """
    try:
        config = proxmox.nodes(node).qemu(template_vmid).config.get()
        
        # Look for disk entries (scsi0, virtio0, ide0, etc.)
        for disk_key in ['scsi0', 'scsi1', 'virtio0', 'ide0', 'ide1', 'ide2']:
            if disk_key in config:
                disk_path = config[disk_key]
                # Format is typically "storage:path" or "storage:vmid/disk-name"
                if ':' in disk_path:
                    storage_name = disk_path.split(':')[0]
                    logger.info(f"Template VM {template_vmid} uses storage: {storage_name}")
                    return storage_name
        
        logger.warning(f"Could not determine template storage for VM {template_vmid}, using default")
        return "local-lvm"
        
    except Exception as e:
        logger.error(f"Failed to get template storage: {e}")
        return "local-lvm"


def deploy_linked_clones(
    class_id: int,
    template_vmid: int,
    num_students: int,
    deployment_node: str = None,
    cluster_id: int = None
) -> Tuple[bool, str, Dict]:
    """
    Deploy student VMs using qm clone (snapshot-based linked clones).
    
    Creates true linked clones that share the template's disk,
    dramatically faster than full clones. VMs created on same storage as template.
    
    Args:
        class_id: Database class ID
        template_vmid: Proxmox template VM ID to clone from
        num_students: Number of student VMs to create
        deployment_node: Optional single node for all deployments
        cluster_id: Optional cluster ID (uses default if not provided)
    
    Returns:
        (success: bool, message: str, vm_info: dict)
    """
    try:
        class_ = Class.query.get(class_id)
        if not class_:
            return False, f"Class {class_id} not found", {}
        
        # Get cluster config
        clusters = get_clusters_from_db()
        if cluster_id:
            cluster_config = next((c for c in clusters if c["id"] == cluster_id), None)
        else:
            cluster_config = clusters[0] if clusters else None
        
        if not cluster_config:
            return False, "No cluster configured", {}
        
        # Get Proxmox connection
        proxmox = get_proxmox_admin_for_cluster(cluster_config["id"])
        
        # Verify template exists and get its storage
        try:
            proxmox.nodes(deployment_node).qemu(template_vmid).status.current.get()
            logger.info(f"Template VM {template_vmid} found on node {deployment_node}")
        except Exception as e:
            logger.error(f"Template VM {template_vmid} not found on node {deployment_node}: {e}")
            return False, f"Template VM {template_vmid} not found: {str(e)}", {}
        
        # Get template's storage location
        template_storage = get_template_storage(proxmox, deployment_node, template_vmid)
        logger.info(f"Template VM {template_vmid} uses storage: {template_storage}")
        
        # Get SSH executor for the node
        try:
            ssh_executor = get_pooled_ssh_executor_from_config(cluster_config)
        except Exception as e:
            logger.error(f"Failed to get SSH connection: {e}")
            return False, f"SSH connection failed: {str(e)}", {}
        
        # Determine starting VMID
        start_vmid = get_available_vmid(cluster_config, start_vmid=100)
        
        created_vms = []
        errors = []
        
        # Create student VMs (linked clones - fast, snapshot-based)
        for i in range(num_students):
            new_vmid = start_vmid + i
            
            # Pick node for this VM (load balance if not single node deployment)
            if deployment_node:
                target_node = deployment_node
            else:
                nodes = get_nodes_for_load_balancing(cluster_config)
                target_node = nodes[i % len(nodes)] if nodes else deployment_node
            
            vm_name = f"{class_.name.replace(' ', '-').lower()}-student-{i+1}-{new_vmid}"
            
            try:
                # Execute qm clone command (WITHOUT --full true for true linked clones)
                # Linked clones are snapshot-based and much faster
                cmd = f"qm clone {template_vmid} {new_vmid} --name {vm_name} --storage {template_storage}"
                logger.info(f"Creating linked clone: {cmd}")
                
                output = ssh_executor.run_command(cmd)
                logger.info(f"Linked clone created: {output}")
                
                # Verify VM was created
                vm_config = proxmox.nodes(target_node).qemu(new_vmid).config.get()
                if vm_config:
                    created_vms.append({
                        "vmid": new_vmid,
                        "name": vm_name,
                        "node": target_node
                    })
                    logger.info(f"Created VM {new_vmid}: {vm_name} on {target_node}")
                else:
                    errors.append(f"VM {new_vmid} created but not verified")
                    
            except Exception as e:
                error_msg = f"Failed to create VM {new_vmid}: {str(e)}"
                logger.error(error_msg)
                errors.append(error_msg)
        
        # Create database records for created VMs
        try:
            for vm_info in created_vms:
                assignment = VMAssignment(
                    class_id=class_id,
                    proxmox_vmid=vm_info["vmid"],
                    vm_name=vm_info["name"],
                    node=vm_info["node"],
                    status="available"
                )
                db.session.add(assignment)
            
            db.session.commit()
            logger.info(f"Created {len(created_vms)} VM assignment records in database")
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Failed to create database records: {e}")
            return False, f"Database error: {str(e)}", {}
        
        # Return results
        result_msg = f"Created {len(created_vms)} linked clone VMs"
        if errors:
            result_msg += f", with {len(errors)} errors"
        
        return len(created_vms) > 0, result_msg, {
            "created_count": len(created_vms),
            "error_count": len(errors),
            "errors": errors,
            "vms": created_vms
        }
        
    except Exception as e:
        logger.exception(f"Linked clone deployment failed: {e}")
        return False, f"Deployment failed: {str(e)}", {}
