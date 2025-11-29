#!/usr/bin/env python3
"""
Background VM Inventory Synchronization Service

This service continuously polls Proxmox clusters and updates the VMInventory
database table, ensuring the frontend always has up-to-date information without
making direct Proxmox API calls.

The service runs in a daemon thread and performs:
1. Full VM inventory sync every FULL_SYNC_INTERVAL seconds
2. Quick status updates for running VMs every QUICK_SYNC_INTERVAL seconds
3. IP address discovery and RDP/SSH port checks
4. Graceful handling of API errors with exponential backoff
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from app.config import CLUSTERS, VM_CACHE_TTL

logger = logging.getLogger(__name__)

# Sync intervals (seconds)
FULL_SYNC_INTERVAL = 300  # 5 minutes - complete inventory refresh
QUICK_SYNC_INTERVAL = 30  # 30 seconds - status-only updates for running VMs
ERROR_BACKOFF_BASE = 10  # Base backoff time on errors
ERROR_BACKOFF_MAX = 300  # Maximum backoff time

# Global control
_sync_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_last_full_sync = 0
_last_quick_sync = 0
_consecutive_errors = 0


def start_background_sync(app):
    """Start the background synchronization service.
    
    Args:
        app: Flask application instance (for app context)
    """
    global _sync_thread
    
    if _sync_thread and _sync_thread.is_alive():
        logger.warning("Background sync already running")
        return
    
    _stop_event.clear()
    _sync_thread = threading.Thread(
        target=_sync_worker,
        args=(app,),
        daemon=True,
        name="VMInventorySync"
    )
    _sync_thread.start()
    logger.info("Background VM inventory sync service started")


def stop_background_sync():
    """Stop the background synchronization service."""
    global _sync_thread
    
    if not _sync_thread or not _sync_thread.is_alive():
        logger.warning("Background sync not running")
        return
    
    logger.info("Stopping background sync service...")
    _stop_event.set()
    _sync_thread.join(timeout=10)
    _sync_thread = None
    logger.info("Background sync service stopped")


def trigger_immediate_sync():
    """Request an immediate full sync on next iteration."""
    global _last_full_sync
    _last_full_sync = 0
    logger.info("Immediate sync triggered")


def _sync_worker(app):
    """Main worker loop for background synchronization."""
    global _last_full_sync, _last_quick_sync, _consecutive_errors
    
    logger.info("VM inventory sync worker started")
    
    # Initial delay to let app fully initialize
    time.sleep(5)
    
    while not _stop_event.is_set():
        try:
            now = time.time()
            
            # Determine what type of sync to perform
            should_full_sync = (now - _last_full_sync) >= FULL_SYNC_INTERVAL
            should_quick_sync = (now - _last_quick_sync) >= QUICK_SYNC_INTERVAL
            
            if should_full_sync:
                with app.app_context():
                    _perform_full_sync()
                _last_full_sync = now
                _last_quick_sync = now  # Quick sync is included in full sync
                _consecutive_errors = 0  # Reset error counter on success
                
            elif should_quick_sync:
                with app.app_context():
                    _perform_quick_sync()
                _last_quick_sync = now
                _consecutive_errors = 0
            
            # Sleep interval (check stop event frequently)
            for _ in range(10):  # Check every second for 10 seconds
                if _stop_event.is_set():
                    break
                time.sleep(1)
                
        except Exception as e:
            _consecutive_errors += 1
            backoff = min(ERROR_BACKOFF_BASE * (2 ** _consecutive_errors), ERROR_BACKOFF_MAX)
            logger.exception(f"Background sync error (attempt {_consecutive_errors}): {e}")
            logger.info(f"Backing off for {backoff} seconds")
            time.sleep(backoff)
    
    logger.info("VM inventory sync worker stopped")


def _perform_full_sync():
    """Perform a complete inventory synchronization across all clusters."""
    logger.info("Starting full inventory sync")
    start_time = time.time()
    
    try:
        from app.services.proxmox_client import get_all_vms
        from app.services.inventory_service import persist_vm_inventory
        from app.services.arp_scanner import has_rdp_port_open, get_rdp_cache_time
        from app.services.rdp_service import build_rdp
        
        # Fetch all VMs from all clusters
        all_vms = get_all_vms(skip_ips=False, force_refresh=True)
        
        # Augment with RDP/SSH availability
        rdp_cache_valid = get_rdp_cache_time() > 0
        for vm in all_vms:
            # Check RDP availability
            rdp_can_build = False
            rdp_port_available = False
            try:
                build_rdp(vm)
                rdp_can_build = True
            except Exception:
                rdp_can_build = False
            
            if vm.get('category') == 'windows':
                rdp_port_available = True
            elif rdp_cache_valid and vm.get('ip') and vm['ip'] not in ('N/A', 'Fetching...', ''):
                rdp_port_available = has_rdp_port_open(vm['ip'])
            
            vm['rdp_available'] = rdp_can_build and rdp_port_available
            
            # SSH is typically available on Linux VMs with IP
            vm['ssh_available'] = (
                vm.get('category') == 'linux' and
                vm.get('ip') and
                vm['ip'] not in ('N/A', 'Fetching...', '')
            )
        
        # Persist to database
        persist_vm_inventory(all_vms)
        
        elapsed = time.time() - start_time
        logger.info(f"Full sync complete: {len(all_vms)} VMs in {elapsed:.2f}s")
        
    except Exception as e:
        logger.exception(f"Full sync failed: {e}")
        raise


def _perform_quick_sync():
    """Perform a quick status-only update for running VMs."""
    logger.info("Starting quick status sync")
    start_time = time.time()
    
    try:
        from app.models import VMInventory, db
        from app.services.proxmox_service import get_proxmox_admin_for_cluster
        from datetime import datetime, timedelta
        
        # Find VMs that are running or were recently active
        cutoff = datetime.utcnow() - timedelta(minutes=10)
        active_vms = VMInventory.query.filter(
            db.or_(
                VMInventory.status == 'running',
                VMInventory.last_status_check > cutoff
            )
        ).all()
        
        if not active_vms:
            logger.debug("No active VMs to sync")
            return
        
        # Group by cluster for efficient API calls
        by_cluster = {}
        for vm in active_vms:
            if vm.cluster_id not in by_cluster:
                by_cluster[vm.cluster_id] = []
            by_cluster[vm.cluster_id].append(vm)
        
        updated_count = 0
        for cluster_id, vms in by_cluster.items():
            try:
                proxmox = get_proxmox_admin_for_cluster(cluster_id)
                
                for vm in vms:
                    try:
                        # Get current status
                        if vm.type == 'lxc':
                            status_data = proxmox.nodes(vm.node).lxc(vm.vmid).status.current.get()
                        else:
                            status_data = proxmox.nodes(vm.node).qemu(vm.vmid).status.current.get()
                        
                        # Update status fields
                        vm.status = status_data.get('status', 'unknown')
                        vm.uptime = status_data.get('uptime')
                        vm.cpu_usage = status_data.get('cpu', 0) * 100 if status_data.get('cpu') else None
                        
                        # Calculate memory usage percentage
                        if status_data.get('mem') and status_data.get('maxmem'):
                            vm.memory_usage = (status_data['mem'] / status_data['maxmem']) * 100
                        
                        vm.last_status_check = datetime.utcnow()
                        vm.sync_error = None
                        updated_count += 1
                        
                    except Exception as e:
                        vm.sync_error = f"Status check failed: {str(e)[:200]}"
                        logger.debug(f"Quick sync failed for VM {vm.vmid}: {e}")
                
            except Exception as e:
                logger.warning(f"Quick sync failed for cluster {cluster_id}: {e}")
        
        db.session.commit()
        elapsed = time.time() - start_time
        logger.info(f"Quick sync complete: {updated_count}/{len(active_vms)} VMs in {elapsed:.2f}s")
        
    except Exception as e:
        logger.exception(f"Quick sync failed: {e}")
        raise


def get_sync_stats() -> dict:
    """Get statistics about the background sync service."""
    from app.models import VMInventory, db
    from datetime import datetime, timedelta
    
    now = datetime.utcnow()
    
    # Get sync statistics
    total_vms = VMInventory.query.count()
    
    # VMs updated in last 5 minutes (fresh)
    fresh_cutoff = now - timedelta(minutes=5)
    fresh_vms = VMInventory.query.filter(
        VMInventory.last_updated > fresh_cutoff
    ).count()
    
    # VMs with errors
    error_vms = VMInventory.query.filter(
        VMInventory.sync_error.isnot(None)
    ).count()
    
    # Running VMs
    running_vms = VMInventory.query.filter_by(status='running').count()
    
    return {
        'total_vms': total_vms,
        'fresh_vms': fresh_vms,
        'error_vms': error_vms,
        'running_vms': running_vms,
        'last_full_sync': _last_full_sync,
        'last_quick_sync': _last_quick_sync,
        'consecutive_errors': _consecutive_errors,
        'is_running': _sync_thread and _sync_thread.is_alive(),
    }
