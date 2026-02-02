#!/usr/bin/env python3
"""
Background VM Inventory Sync Service

Continuously polls Proxmox clusters and updates the VMInventory database.
This keeps the database (single source of truth) synchronized with actual Proxmox state.

Architecture:
- Full sync every 5 minutes: Complete inventory refresh
- Quick sync every 30 seconds: Update status of running VMs only
- Exponential backoff on errors

All GUI queries read from VMInventory table, never directly from Proxmox API.
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Global sync state
_sync_thread = None
_sync_running = False
_sync_stats = {
    'last_full_sync': None,
    'last_quick_sync': None,
    'full_sync_count': 0,
    'quick_sync_count': 0,
    'last_error': None,
    'vms_synced': 0,
    'sync_duration': 0,
}


def start_background_sync(app):
    """Start the background sync daemon thread.
    
    Args:
        app: Flask app instance (for context)
    """
    global _sync_thread, _sync_running
    
    if _sync_running:
        logger.warning("Background sync already running")
        return
    
    _sync_running = True
    
    def _sync_loop():
        """Main sync loop with exponential backoff."""
        error_count = 0
        max_backoff = 300  # 5 minutes
        
        while _sync_running:
            try:
                with app.app_context():
                    time.time()
                    
                    # Full sync every 10 minutes (600 seconds) - reduced frequency to avoid slowdowns
                    if (_sync_stats['last_full_sync'] is None or 
                        (datetime.utcnow() - _sync_stats['last_full_sync']).seconds >= 600):
                        logger.info("Starting full VM inventory sync...")
                        _perform_full_sync()
                        error_count = 0  # Reset on success
                    
                    # Quick sync every 2 minutes (120 seconds) - only check running VMs
                    elif (_sync_stats['last_quick_sync'] is None or
                          (datetime.utcnow() - _sync_stats['last_quick_sync']).seconds >= 120):
                        _perform_quick_sync()
                        error_count = 0  # Reset on success
                
                # Sleep 60 seconds between checks
                time.sleep(60)
                
            except Exception as e:
                error_count += 1
                backoff = min(2 ** error_count, max_backoff)
                logger.exception(f"Background sync error (attempt {error_count}): {e}")
                _sync_stats['last_error'] = str(e)
                time.sleep(backoff)
    
    _sync_thread = threading.Thread(
        target=_sync_loop,
        daemon=True,
        name="VMInventorySync"
    )
    _sync_thread.start()
    logger.info("Background VM inventory sync started")


def stop_background_sync():
    """Stop the background sync service."""
    global _sync_running
    _sync_running = False
    logger.info("Background sync stopped")


def _perform_full_sync():
    """Perform complete inventory sync from all Proxmox clusters."""
    start_time = time.time()
    
    try:
        from app.services.inventory_service import persist_vm_inventory
        from app.services.proxmox_service import get_all_vms
        from app.models import VMAssignment, db
        
        vms = get_all_vms(skip_ips=False, force_refresh=True)
        count = persist_vm_inventory(vms)
        
        # Update VMAssignment nodes from VMInventory (sync after migrations)
        updated_nodes = 0
        for vm in vms:
            vmid = vm.get('vmid')
            current_node = vm.get('node')
            
            if vmid and current_node:
                assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
                if assignment and assignment.node != current_node:
                    logger.info(f"Updating VMAssignment node for {vmid}: {assignment.node} -> {current_node}")
                    assignment.node = current_node
                    updated_nodes += 1
        
        if updated_nodes > 0:
            db.session.commit()
            db.session.close()  # Release lock immediately
            logger.info(f"Updated {updated_nodes} VMAssignment nodes after migration")
        
        # Update stats
        _sync_stats['last_full_sync'] = datetime.utcnow()
        _sync_stats['full_sync_count'] += 1
        _sync_stats['vms_synced'] = count
        _sync_stats['sync_duration'] = time.time() - start_time
        
        logger.info(f"Full sync completed: {count} VMs in {_sync_stats['sync_duration']:.1f}s")
        
    except Exception as e:
        logger.exception(f"Full sync failed: {e}")
        _sync_stats['last_error'] = str(e)
        raise


def _perform_quick_sync():
    """Quick sync: Update only running VMs' status.
    
    This is faster than full sync and catches state changes quickly.
    """
    try:
        from app.models import VMInventory, db
        from app.services.proxmox_service import get_proxmox_admin

        # Get recently active VMs (updated in last hour)
        cutoff = datetime.utcnow() - timedelta(hours=1)
        active_vms = VMInventory.query.filter(
            (VMInventory.status == 'running') |
            (VMInventory.status == 'running') |
            (VMInventory.last_status_check > cutoff)
        ).limit(50).all()  # Limit to prevent overload
        
        if not active_vms:
            _sync_stats['last_quick_sync'] = datetime.utcnow()
            return
        
        # Quick status check via Proxmox API
        proxmox = get_proxmox_admin()
        updated = 0
        
        for vm in active_vms:
            try:
                # Get current status
                if vm.type == 'qemu':
                    status_obj = proxmox.nodes(vm.node).qemu(vm.vmid).status.current.get()
                else:
                    status_obj = proxmox.nodes(vm.node).lxc(vm.vmid).status.current.get()
                
                new_status = status_obj.get('status', 'unknown')
                
                if vm.status != new_status:
                    vm.status = new_status
                    vm.last_status_check = datetime.utcnow()
                    updated += 1
                    
            except Exception as e:
                logger.debug(f"Quick sync failed for VM {vm.vmid}: {e}")
                continue
        
        if updated > 0:
            db.session.commit()
            db.session.close()  # Release lock immediately
            logger.info(f"Quick sync: {updated}/{len(active_vms)} VMs updated")
        
        _sync_stats['last_quick_sync'] = datetime.utcnow()
        _sync_stats['quick_sync_count'] += 1
        
    except Exception as e:
        logger.exception(f"Quick sync failed: {e}")
        raise


def trigger_immediate_sync() -> bool:
    """Trigger an immediate full sync (for admin/debugging).
    
    Returns:
        True if sync started
    """
    try:
        _perform_full_sync()
        return True
    except Exception as e:
        logger.exception(f"Immediate sync failed: {e}")
        return False


def get_sync_stats() -> Dict[str, Any]:
    """Get current sync statistics.
    
    Returns:
        Dict with sync stats
    """
    from app.models import VMInventory

    # Get database stats
    total_vms = VMInventory.query.count()
    
    # Count fresh VMs (updated in last 10 minutes)
    fresh_cutoff = datetime.utcnow() - timedelta(minutes=10)
    fresh_vms = VMInventory.query.filter(
        VMInventory.last_updated > fresh_cutoff
    ).count()
    
    # Count VMs with sync errors
    error_vms = VMInventory.query.filter(
        VMInventory.sync_error.isnot(None)
    ).count()
    
    # Count running VMs
    running_vms = VMInventory.query.filter_by(status='running').count()
    
    return {
        **_sync_stats,
        'running': _sync_running,
        'total_vms': total_vms,
        'fresh_vms': fresh_vms,
        'error_vms': error_vms,
        'running_vms': running_vms,
        'last_full_sync_iso': _sync_stats['last_full_sync'].isoformat() if _sync_stats['last_full_sync'] else None,
        'last_quick_sync_iso': _sync_stats['last_quick_sync'].isoformat() if _sync_stats['last_quick_sync'] else None,
    }
