#!/usr/bin/env python3
"""
VM Inventory Service

Manages the VMInventory database table - the single source of truth for all VM data.
All GUI queries read from this table for instant (<100ms) responses.
"""

import logging
from datetime import datetime
from typing import List, Dict, Optional, Any

logger = logging.getLogger(__name__)


def persist_vm_inventory(vms: List[Dict]) -> int:
    """Persist VM list to database inventory.
    
    Updates existing VMs and creates new ones. Does NOT delete missing VMs
    (they might be on other clusters or temporarily unavailable).
    
    Args:
        vms: List of VM dicts from get_all_vms()
        
    Returns:
        Number of VMs updated/created
    """
    from app.models import VMInventory, db
    
    updated_count = 0
    
    try:
        for vm in vms:
            cluster_id = vm.get('cluster_id', 'default')
            vmid = vm.get('vmid')
            
            if not vmid:
                continue
            
            # Find or create inventory record
            inventory = VMInventory.query.filter_by(
                cluster_id=cluster_id,
                vmid=vmid
            ).first()
            
            if not inventory:
                inventory = VMInventory(
                    cluster_id=cluster_id,
                    vmid=vmid
                )
                db.session.add(inventory)
            
            # Update all fields
            inventory.cluster_name = vm.get('cluster_name')
            inventory.name = vm.get('name')
            inventory.node = vm.get('node')
            inventory.status = vm.get('status')
            inventory.ip = vm.get('ip')
            inventory.type = vm.get('type')
            inventory.category = vm.get('category')
            
            # Hardware specs
            inventory.memory = vm.get('memory')
            inventory.cores = vm.get('cores')
            inventory.disk_size = vm.get('disk_size')
            
            # Runtime info
            inventory.uptime = vm.get('uptime')
            inventory.cpu_usage = vm.get('cpu_usage')
            inventory.memory_usage = vm.get('memory_usage')
            
            # Configuration
            inventory.is_template = vm.get('is_template', False)
            inventory.tags = vm.get('tags')
            
            # Availability flags
            inventory.rdp_available = vm.get('rdp_available', False)
            inventory.ssh_available = vm.get('ssh_available', False)
            
            # Update timestamp
            inventory.last_updated = datetime.utcnow()
            inventory.last_status_check = datetime.utcnow()
            inventory.sync_error = None  # Clear any previous errors
            
            updated_count += 1
        
        db.session.commit()
        logger.info(f"Persisted {updated_count} VMs to inventory")
        return updated_count
        
    except Exception as e:
        logger.exception(f"Failed to persist VM inventory: {e}")
        db.session.rollback()
        return 0


def fetch_vm_inventory(
    cluster_id: Optional[str] = None,
    search: Optional[str] = None,
    status: Optional[str] = None,
    is_template: Optional[bool] = None,
    vmids: Optional[set] = None
) -> List[Dict[str, Any]]:
    """Fetch VMs from inventory database.
    
    Args:
        cluster_id: Filter by cluster
        search: Search in name, IP, VMID
        status: Filter by status (running, stopped, etc.)
        is_template: Filter templates vs VMs
        vmids: Filter to specific VMIDs
        
    Returns:
        List of VM dicts
    """
    from app.models import VMInventory
    
    query = VMInventory.query
    
    if cluster_id:
        query = query.filter_by(cluster_id=cluster_id)
    
    if vmids is not None:
        query = query.filter(VMInventory.vmid.in_(vmids))
    
    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            (VMInventory.name.ilike(search_pattern)) |
            (VMInventory.ip.ilike(search_pattern)) |
            (VMInventory.vmid.like(search_pattern))
        )
    
    if status:
        query = query.filter_by(status=status)
    
    if is_template is not None:
        query = query.filter_by(is_template=is_template)
    
    # Convert to dicts
    results = query.order_by(VMInventory.name).all()
    return [vm.to_dict() for vm in results]


def get_vm_from_inventory(cluster_id: str, vmid: int) -> Optional[Dict[str, Any]]:
    """Get a single VM from inventory.
    
    Args:
        cluster_id: Cluster ID
        vmid: VM ID
        
    Returns:
        VM dict or None
    """
    from app.models import VMInventory
    
    vm = VMInventory.query.filter_by(
        cluster_id=cluster_id,
        vmid=vmid
    ).first()
    
    return vm.to_dict() if vm else None


def update_vm_status(cluster_id: str, vmid: int, status: str, ip: Optional[str] = None) -> bool:
    """Quick update of VM status in inventory.
    
    Used after start/stop operations to immediately reflect changes.
    
    Args:
        cluster_id: Cluster ID
        vmid: VM ID
        status: New status (running, stopped, etc.)
        ip: Optional IP address
        
    Returns:
        True if updated
    """
    from app.models import VMInventory, db
    
    try:
        vm = VMInventory.query.filter_by(
            cluster_id=cluster_id,
            vmid=vmid
        ).first()
        
        if vm:
            vm.status = status
            if ip:
                vm.ip = ip
            vm.last_status_check = datetime.utcnow()
            db.session.commit()
            return True
        
        return False
        
    except Exception as e:
        logger.exception(f"Failed to update VM status: {e}")
        db.session.rollback()
        return False


def mark_vm_sync_error(cluster_id: str, vmid: int, error: str):
    """Mark a VM as having sync errors.
    
    Args:
        cluster_id: Cluster ID
        vmid: VM ID
        error: Error message
    """
    from app.models import VMInventory, db
    
    try:
        vm = VMInventory.query.filter_by(
            cluster_id=cluster_id,
            vmid=vmid
        ).first()
        
        if vm:
            vm.sync_error = error
            vm.last_updated = datetime.utcnow()
            db.session.commit()
            
    except Exception as e:
        logger.exception(f"Failed to mark sync error: {e}")
        db.session.rollback()
