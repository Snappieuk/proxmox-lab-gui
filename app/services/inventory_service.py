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


def fetch_vm_by_vmid(vmid: int, cluster_id: Optional[str] = None):
    """Fetch a single VM from VMInventory database by VMID.
    
    Fast direct database query - much faster than loading all VMs.
    
    Args:
        vmid: VM ID to look up
        cluster_id: Optional cluster ID to narrow search
        
    Returns:
        VMInventory record or None
    """
    from app.models import VMInventory
    
    try:
        if cluster_id:
            return VMInventory.query.filter_by(
                cluster_id=cluster_id,
                vmid=vmid
            ).first()
        else:
            # Search all clusters
            return VMInventory.query.filter_by(vmid=vmid).first()
    except Exception as e:
        logger.error(f"Failed to fetch VM {vmid} from database: {e}")
        return None


def update_vm_status_immediate(cluster_id: str, vmid: int, status: str, ip: Optional[str] = None) -> bool:
    """Immediately update a single VM's status in the database.
    
    This is much faster than a full sync and should be called after VM operations
    (start, stop, etc.) to keep the UI responsive.
    
    Args:
        cluster_id: Cluster ID
        vmid: VM ID
        status: New status ('running', 'stopped', etc.)
        ip: Optional IP address to update
        
    Returns:
        True if updated successfully
    """
    from app.models import VMInventory, db
    
    try:
        inventory = VMInventory.query.filter_by(
            cluster_id=cluster_id,
            vmid=vmid
        ).first()
        
        if inventory:
            inventory.status = status
            inventory.last_status_check = datetime.utcnow()
            
            if ip and ip not in ('N/A', 'Fetching...', ''):
                inventory.ip = ip
                inventory.ip_updated_at = datetime.utcnow()
            
            db.session.commit()
            # logger.debug(f"Immediate status update: VM {cluster_id}/{vmid} -> {status}")
            return True
        else:
            # logger.debug(f"Cannot update status: VM {cluster_id}/{vmid} not in inventory")
            return False
            
    except Exception as e:
        logger.error(f"Failed to update VM {cluster_id}/{vmid} status: {e}")
        db.session.rollback()
        return False


def persist_vm_inventory(vms: List[Dict], cleanup_missing: bool = True) -> int:
    """Persist VM list to database inventory.
    
    Updates existing VMs and creates new ones. Optionally removes VMs that
    no longer exist in Proxmox.
    
    Args:
        vms: List of VM dicts from get_all_vms()
        cleanup_missing: If True, delete VMs from DB that aren't in the sync
        
    Returns:
        Number of VMs updated/created
    """
    from app.models import VMInventory, db
    from sqlalchemy.exc import IntegrityError, OperationalError
    import time
    
    updated_count = 0
    
    # Retry logic for database locked errors
    max_retries = 5
    retry_delay = 0.1  # Start with 100ms
    
    for attempt in range(max_retries):
        try:
            # Use a single timestamp for this sync
            from datetime import datetime
            sync_ts = datetime.utcnow()
            seen_clusters = set()
            for vm in vms:
                cluster_id = vm.get('cluster_id', 'default')
                seen_clusters.add(cluster_id)
                vmid = vm.get('vmid')
                
                if not vmid:
                    continue
                
                # Try to fetch existing record first
                inventory = VMInventory.query.filter_by(
                    cluster_id=cluster_id,
                    vmid=vmid
                ).first()
                
                if not inventory:
                    # Record doesn't exist - try to create it
                    inventory = VMInventory(
                        cluster_id=cluster_id,
                        vmid=vmid,
                        name=vm.get('name', 'Unknown'),
                        node=vm.get('node', 'Unknown'),
                        status=vm.get('status', 'unknown'),
                        type=vm.get('type', 'qemu')
                    )
                    db.session.add(inventory)
                    
                    try:
                        # Commit immediately to avoid holding locks
                        db.session.flush()
                    except IntegrityError:
                        # Another thread created it between our query and commit
                        db.session.rollback()
                        
                        # Fetch the record that was created by the other thread
                        inventory = VMInventory.query.filter_by(
                            cluster_id=cluster_id,
                            vmid=vmid
                        ).first()
                        
                        if not inventory:
                            # Extremely rare - skip this VM
                            continue
                
                # Update all fields
                inventory.name = vm.get('name')
                inventory.node = vm.get('node')
                inventory.status = vm.get('status')
                # Update IP from sync (always update to capture changes, but preserve if missing)
                new_ip = vm.get('ip')
                if new_ip and new_ip not in ('N/A', 'Fetching...', ''):
                    inventory.ip = new_ip
                elif not inventory.ip:
                    # First time seeing this VM - set placeholder
                    inventory.ip = vm.get('ip') or 'N/A'
                # Update MAC address if provided
                new_mac = vm.get('mac_address')
                if new_mac:
                    inventory.mac_address = new_mac
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
                inventory.last_updated = sync_ts
                inventory.last_status_check = sync_ts
                inventory.sync_error = None  # Clear any previous errors
                
                updated_count += 1
            
            # Remove VMs not touched in this sync for seen clusters (only after a successful pass)
            if cleanup_missing and vms:
                from app.models import VMInventory
                for cluster_id in seen_clusters:
                    missing = VMInventory.query.filter(
                        VMInventory.cluster_id == cluster_id,
                        VMInventory.last_updated < sync_ts
                    ).all()
                    if missing:
                        for rec in missing:
                            db.session.delete(rec)
            
            # Commit all changes
            db.session.commit()
            return updated_count
            
        except OperationalError as e:
            # Database locked - retry with exponential backoff
            db.session.rollback()
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** attempt)
                logger.warning(f"Database locked (attempt {attempt + 1}/{max_retries}), retrying in {wait_time:.2f}s...")
                time.sleep(wait_time)
                continue
            else:
                logger.error(f"Failed to persist VM inventory after {max_retries} attempts: database locked")
                return 0
        except Exception as e:
            logger.exception(f"Failed to persist VM inventory: {e}")
            db.session.rollback()
            return 0
    
    # Should not reach here
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
