#!/usr/bin/env python3
"""
Mappings service - VM to user mappings via DATABASE (VMAssignment table).

FULLY MIGRATED TO DATABASE - JSON system removed.
All VM→user mappings now use VMAssignment table in database.

This module provides database-backed VM assignment functions.
"""

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def get_user_vm_map() -> Dict[str, List[int]]:
    """
    Get complete user-to-VMID mapping from database.
    
    Returns:
        dict: {username: [vmid1, vmid2, ...]}
    """
    from app.models import User
    
    mapping = {}
    try:
        users = User.query.all()
        for user in users:
            vmids = [a.proxmox_vmid for a in user.vm_assignments if a.proxmox_vmid]
            if vmids:
                mapping[user.username] = sorted(vmids)
    except Exception as e:
        logger.error("Error loading VM mappings from database: %s", e, exc_info=True)
    
    return mapping


def save_user_vm_map(mapping: Dict[str, List[int]]) -> None:
    """
    Save complete user-to-VMID mapping to database.
    
    Args:
        mapping: {username: [vmid1, vmid2, ...]}
        
    Creates/updates VMAssignment records in database.
    """
    from app.models import User, VMAssignment, db
    
    try:
        for username, vmids in mapping.items():
            user = User.query.filter_by(username=username).first()
            if not user:
                logger.warning("User %s not found, skipping VM assignments", username)
                continue
            
            # Get existing assignments for this user
            existing = {a.proxmox_vmid: a for a in user.vm_assignments}
            
            # Add new assignments
            for vmid in vmids:
                if vmid not in existing:
                    assignment = VMAssignment(
                        proxmox_vmid=vmid,
                        assigned_user_id=user.id,
                        status='assigned'
                    )
                    db.session.add(assignment)
            
            # Remove assignments not in new list
            for vmid, assignment in existing.items():
                if vmid not in vmids:
                    db.session.delete(assignment)
        
        db.session.commit()
        logger.info("Updated VM mappings in database")
    except Exception as e:
        logger.error("Error saving VM mappings to database: %s", e, exc_info=True)
        db.session.rollback()


def get_all_vm_ids_and_names() -> List[Dict[str, Any]]:
    """
    Get lightweight VM list (ID, name, cluster, node only - no IP lookups).
    
    Much faster than get_all_vms() because it skips expensive IP address lookups.
    Used by the mappings page where we only need to display VM IDs and names.
    
    Returns: List of {vmid, name, cluster_id, node}
    """
    from app.services.proxmox_service import get_clusters_from_db
# VALID_NODES deprecated - no longer used
    from app.services.proxmox_service import get_proxmox_admin_for_cluster
    
    results = []
    
    for cluster in get_clusters_from_db():
        cluster_id = cluster["id"]
        cluster_name = cluster["name"]
        try:
            proxmox = get_proxmox_admin_for_cluster(cluster_id)
            
            # Try cluster resources API first (fastest)
            try:
                resources = proxmox.cluster.resources.get(type="vm") or []
                for vm in resources:
                    # Skip templates
                    if vm.get("template") == 1:
                        continue
                    
                    node = vm.get("node")
                    if VALID_NODES and node not in VALID_NODES:
                        continue
                    
                    results.append({
                        "vmid": int(vm["vmid"]),
                        "name": vm.get("name", f"vm-{vm['vmid']}"),
                        "cluster_id": cluster_id,
                        "cluster_name": cluster_name,
                        "node": node,
                    })
                continue
            except Exception as e:
                logger.warning("Cluster resources API failed for %s, using node queries: %s", cluster_name, e)
            
            # Fallback: per-node queries
            nodes = proxmox.nodes.get() or []
            for node_data in nodes:
                node_name = node_data["node"]
                
                # Skip nodes not in VALID_NODES filter
                if VALID_NODES and node_name not in VALID_NODES:
                    continue
                
                # Get QEMU VMs
                try:
                    qemu_vms = proxmox.nodes(node_name).qemu.get()
                    for vm in qemu_vms:
                        # Skip templates
                        if vm.get("template") == 1:
                            continue
                        results.append({
                            "vmid": int(vm["vmid"]),
                            "name": vm.get("name", f"vm-{vm['vmid']}"),
                            "cluster_id": cluster_id,
                            "cluster_name": cluster_name,
                            "node": node_name,
                        })
                except Exception as e:
                    logger.warning("Failed to fetch QEMU VMs from %s/%s: %s", cluster_id, node_name, e)
                
                # Get LXC containers
                try:
                    lxc_vms = proxmox.nodes(node_name).lxc.get()
                    for vm in lxc_vms:
                        results.append({
                            "vmid": int(vm["vmid"]),
                            "name": vm.get("name", f"ct-{vm['vmid']}"),
                            "cluster_id": cluster_id,
                            "cluster_name": cluster_name,
                            "node": node_name,
                        })
                except Exception as e:
                    logger.warning("Failed to fetch LXC containers from %s/%s: %s", cluster_id, node_name, e)
        
        except Exception as e:
            logger.error("Failed to fetch VMs from cluster %s: %s", cluster_id, e)
    
    # Sort by vmid
    results.sort(key=lambda x: x["vmid"])
    return results


def set_user_vm_mapping(user: str, vmids: List[int]) -> None:
    """
    Set VM mappings for a specific user in database.
    
    Args:
        user: Username (e.g., "student1")
        vmids: List of VM IDs to assign to user
        
    Replaces existing mappings for this user.
    Creates VMAssignment records in database.
    """
    from app.models import User, VMAssignment, db
    
    try:
        user_row = User.query.filter_by(username=user).first()
        if not user_row:
            logger.warning("User %s not found, cannot set VM mappings", user)
            return
        
        # Get existing assignments
        existing = {a.proxmox_vmid: a for a in user_row.vm_assignments}
        
        # Add new assignments
        for vmid in vmids:
            if vmid not in existing:
                assignment = VMAssignment(
                    proxmox_vmid=vmid,
                    assigned_user_id=user_row.id,
                    status='assigned'
                )
                db.session.add(assignment)
        
        # Remove assignments not in new list
        for vmid, assignment in existing.items():
            if vmid not in vmids:
                db.session.delete(assignment)
        
        db.session.commit()
        logger.info("Updated mappings for user %s: %s VMs", user, len(vmids))
    except Exception as e:
        logger.error("Error setting VM mappings for user %s: %s", user, e, exc_info=True)
        db.session.rollback()


def invalidate_mappings_cache() -> None:
    """
    Invalidate the in-memory mappings cache (no-op for database-backed system).
    
    Kept for backward compatibility with code that calls this function.
    Database queries are always fresh, so no cache to invalidate.
    """
    logger.debug("invalidate_mappings_cache called (no-op for database-backed system)")

