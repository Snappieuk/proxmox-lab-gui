#!/usr/bin/env python3
"""
Unified Background Sync Service

Continuously polls Proxmox clusters and updates the database with:
- VM Inventory (VMInventory table)
- Templates (Template table)
- ISO Images (ISOImage table)

This keeps the database (single source of truth) synchronized with actual Proxmox state.

Architecture:
- VM Inventory: Full sync every 10min, Quick sync every 2min
- Templates: Full sync every 30min, Quick verification every 5min
- ISOs: Full sync every 30min, Quick verification every 5min
- Exponential backoff on errors

All GUI queries read from database tables, never directly from Proxmox API.
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict

from app.services.health_service import (
    register_daemon_started,
    register_daemon_stopped,
    update_daemon_sync,
    update_resource_vm_inventory,
)

logger = logging.getLogger(__name__)

# ARP scanner availability flag
ARP_SCANNER_AVAILABLE = True  # Module exists and can be imported

# Global sync state
_sync_thread = None
_sync_running = False
_sync_stats = {
    # VM inventory sync stats
    'last_full_sync': None,
    'last_quick_sync': None,
    'full_sync_count': 0,
    'quick_sync_count': 0,
    'last_error': None,
    'vms_synced': 0,
    'sync_duration': 0,
    
    # Template sync stats
    'last_template_full_sync': None,
    'last_template_quick_sync': None,
    'template_full_sync_count': 0,
    'template_quick_sync_count': 0,
    'templates_synced': 0,
    
    # ISO sync stats
    'last_iso_full_sync': None,
    'last_iso_quick_sync': None,
    'iso_full_sync_count': 0,
    'iso_quick_sync_count': 0,
    'isos_synced': 0,
}


def start_background_sync(app):
    """Start the unified background sync daemon thread.
    
    Manages sync for VM inventory, templates, and ISO images.
    
    Args:
        app: Flask app instance (for context)
    """
    global _sync_thread, _sync_running
    
    if _sync_running:
        logger.warning("Background sync already running")
        return
    
    _sync_running = True
    
    def _sync_loop():
        """Main sync loop with exponential backoff and multiple sync schedules."""
        # Register daemon as started
        register_daemon_started('background_sync')
        
        error_count = 0
        max_backoff = 300  # 5 minutes
        
        while _sync_running:
            try:
                with app.app_context():
                    now = datetime.utcnow()
                    
                    # VM Inventory: Full sync every 10 minutes (600 seconds)
                    if (_sync_stats['last_full_sync'] is None or 
                        (now - _sync_stats['last_full_sync']).total_seconds() >= 600):
                        logger.info("Starting full VM inventory sync...")
                        _perform_full_sync()
                        # Report successful full sync to health service
                        update_daemon_sync('background_sync', full_sync=True, 
                                         items_processed=_sync_stats.get('vms_synced', 0))
                        error_count = 0
                    
                    # VM Inventory: Quick sync every 2 minutes (120 seconds)
                    elif (_sync_stats['last_quick_sync'] is None or
                          (now - _sync_stats['last_quick_sync']).total_seconds() >= 120):
                        _perform_quick_sync()
                        # Report successful quick sync to health service
                        update_daemon_sync('background_sync', full_sync=False,
                                         items_processed=_sync_stats.get('vms_synced', 0))
                        error_count = 0
                    
                    # Templates: Full sync every 30 minutes (1800 seconds)
                    if (_sync_stats['last_template_full_sync'] is None or
                        (now - _sync_stats['last_template_full_sync']).total_seconds() >= 1800):
                        logger.info("Starting full template sync...")
                        _perform_template_full_sync()
                        error_count = 0
                    
                    # Templates: Quick verification every 5 minutes (300 seconds)
                    elif (_sync_stats['last_template_quick_sync'] is None or
                          (now - _sync_stats['last_template_quick_sync']).total_seconds() >= 300):
                        _perform_template_quick_sync()
                        error_count = 0
                    
                    # ISOs: Full sync every 30 minutes (1800 seconds)
                    if (_sync_stats['last_iso_full_sync'] is None or
                        (now - _sync_stats['last_iso_full_sync']).total_seconds() >= 1800):
                        logger.info("Starting full ISO sync...")
                        _perform_iso_full_sync()
                        error_count = 0
                    
                    # ISOs: Quick verification every 5 minutes (300 seconds)
                    elif (_sync_stats['last_iso_quick_sync'] is None or
                          (now - _sync_stats['last_iso_quick_sync']).total_seconds() >= 300):
                        _perform_iso_quick_sync()
                        error_count = 0
                finally:
                    # CRITICAL: Clean up DB session to prevent connection pool exhaustion
                    from app.models import db
                    db.session.remove()
                
                # Sleep 60 seconds between checks
                time.sleep(60)
                
            except Exception as e:
                error_count += 1
                backoff = min(2 ** error_count, max_backoff)
                error_msg = str(e)
                logger.exception(f"Background sync error (attempt {error_count}): {e}")
                _sync_stats['last_error'] = error_msg
                
                # Report sync error to health service
                update_daemon_sync('background_sync', error=error_msg)
                
                try:
                    from app.models import db
                    db.session.remove()
                except Exception as cleanup_err:
                    logger.warning(f"Failed to cleanup DB session on error: {cleanup_err}")
                time.sleep(backoff)
    
    _sync_thread = threading.Thread(
        target=_sync_loop,
        daemon=True,
        name="UnifiedBackgroundSync"
    )
    _sync_thread.start()
    logger.info("Unified background sync daemon started (VM inventory + templates + ISOs)")


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


def _perform_template_full_sync():
    """Perform complete template sync from all Proxmox clusters."""
    try:
        stats = sync_templates_from_proxmox(full_sync=True)
        
        # Update stats
        _sync_stats['last_template_full_sync'] = datetime.utcnow()
        _sync_stats['template_full_sync_count'] += 1
        _sync_stats['templates_synced'] = stats.get('templates_found', 0)
        
        logger.info(f"Template full sync completed: {stats}")
        
    except Exception as e:
        logger.exception(f"Template full sync failed: {e}")
        _sync_stats['last_error'] = str(e)
        raise


def _perform_template_quick_sync():
    """Quick template verification: Check if cached templates still exist."""
    try:
        stats = sync_templates_from_proxmox(full_sync=False)
        
        # Update stats
        _sync_stats['last_template_quick_sync'] = datetime.utcnow()
        _sync_stats['template_quick_sync_count'] += 1
        
        logger.info(f"Template quick sync completed: {stats}")
        
    except Exception as e:
        logger.exception(f"Template quick sync failed: {e}")
        raise


def _perform_iso_full_sync():
    """Perform complete ISO sync from all Proxmox clusters."""
    try:
        stats = sync_isos_from_proxmox(full_sync=True)
        
        # Update stats
        _sync_stats['last_iso_full_sync'] = datetime.utcnow()
        _sync_stats['iso_full_sync_count'] += 1
        _sync_stats['isos_synced'] = stats.get('isos_found', 0)
        
        logger.info(f"ISO full sync completed: {stats}")
        
    except Exception as e:
        logger.exception(f"ISO full sync failed: {e}")
        _sync_stats['last_error'] = str(e)
        raise


def _perform_iso_quick_sync():
    """Quick ISO verification: Check if cached ISOs still exist."""
    try:
        stats = quick_iso_verification()
        
        # Update stats
        _sync_stats['last_iso_quick_sync'] = datetime.utcnow()
        _sync_stats['iso_quick_sync_count'] += 1
        
        logger.info(f"ISO quick sync completed: {stats}")
        
    except Exception as e:
        logger.exception(f"ISO quick sync failed: {e}")
        raise


def sync_templates_from_proxmox(full_sync=True):
    """
    Sync templates from all Proxmox clusters to database.
    
    Args:
        full_sync: If True, fetch all details including specs. If False, only verify existence.
    
    Returns:
        dict: Sync statistics (templates_found, templates_added, templates_updated, templates_removed)
    """
    from app.services.proxmox_service import get_clusters_from_db, get_proxmox_admin_for_cluster
    from app.models import Template, db
    
    logger.info(f"Starting template sync (full_sync={full_sync})")
    
    stats = {
        'templates_found': 0,
        'templates_added': 0,
        'templates_updated': 0,
        'templates_removed': 0,
        'errors': []
    }
    
    # Disable autoflush to prevent premature commits during iteration
    db.session.autoflush = False
    
    # Track all templates found in Proxmox (cluster_ip, node, vmid tuples)
    found_templates = set()

    
    # Sync each cluster
    for cluster in get_clusters_from_db():
        cluster_id = cluster['id']
        cluster_ip = cluster['host']
        
        try:
            proxmox = get_proxmox_admin_for_cluster(cluster_id)
            if not proxmox:
                logger.warning(f"Could not connect to cluster {cluster_id} ({cluster_ip})")
                stats['errors'].append(f"Connection failed: {cluster_id}")
                continue
            
            # Test connection with a simple API call
            try:
                proxmox.version.get()
            except Exception as e:
                logger.warning(f"Cluster {cluster_id} ({cluster_ip}) connection test failed: {e}")
                stats['errors'].append(f"Connection test failed: {cluster_id} - {str(e)}")
                continue
            
            # Get all nodes in this cluster
            try:
                nodes = proxmox.nodes.get()
            except Exception as e:
                logger.warning(f"Failed to get nodes for cluster {cluster_id}: {e}")
                stats['errors'].append(f"Failed to get nodes: {cluster_id} - {str(e)}")
                continue
            
            for node_data in nodes:
                node_name = node_data['node']
                
                try:
                    # Get QEMU VMs (templates are QEMU VMs with template=1)
                    vms = proxmox.nodes(node_name).qemu.get()
                    
                    for vm in vms:
                        # Only process templates
                        if not vm.get('template', 0):
                            continue
                        
                        vmid = vm['vmid']
                        vm_name = vm.get('name', f'template-{vmid}')
                        
                        stats['templates_found'] += 1
                        found_templates.add((cluster_ip, node_name, vmid))
                        
                        # Check if template exists in database
                        existing = Template.query.filter_by(
                            cluster_ip=cluster_ip,
                            node=node_name,
                            proxmox_vmid=vmid
                        ).first()
                        
                        if existing:
                            # Update existing template
                            existing.name = vm_name
                            existing.last_verified_at = datetime.utcnow()
                            
                            if full_sync:
                                # Fetch and cache specs
                                _update_template_specs(existing, proxmox, node_name, vmid)
                            
                            stats['templates_updated'] += 1
                        else:
                            # Add new template
                            new_template = Template(
                                name=vm_name,
                                proxmox_vmid=vmid,
                                cluster_ip=cluster_ip,
                                node=node_name,
                                is_replica=False,
                                is_class_template=False,
                                last_verified_at=datetime.utcnow()
                            )

                            if full_sync:
                                # Fetch and cache specs
                                _update_template_specs(new_template, proxmox, node_name, vmid)
                            
                            db.session.add(new_template)
                            stats['templates_added'] += 1
                            logger.info(f"Added template {vm_name} (VMID {vmid}) from {cluster_ip}/{node_name}")
                
                except Exception as e:
                    # Use debug level for expected connection issues
                    if 'hostname lookup' in str(e) or 'No route to host' in str(e) or '595 Errors' in str(e):
                        logger.debug(f"Node {cluster_ip}/{node_name} unreachable (expected if node is offline): {e}")
                        # Don't add expected offline errors to error list - they're normal
                    else:
                        logger.error(f"Error syncing templates from {cluster_ip}/{node_name}: {e}")
                        stats['errors'].append(f"{cluster_ip}/{node_name}: {str(e)}")
        
        except Exception as e:
            logger.error(f"Error connecting to cluster {cluster_id} ({cluster_ip}): {e}")
            stats['errors'].append(f"Cluster {cluster_id}: {str(e)}")
    
    # Remove templates that no longer exist in Proxmox (only non-class templates)
    if full_sync:
        all_db_templates = Template.query.filter_by(is_class_template=False).all()
        for template in all_db_templates:
            key = (template.cluster_ip, template.node, template.proxmox_vmid)
            if key not in found_templates:
                logger.warning(f"Removing stale template {template.name} (VMID {template.proxmox_vmid}) from database")
                db.session.delete(template)
                stats['templates_removed'] += 1
    
    # Commit all changes with retry logic
    max_retries = 3
    retry_count = 0
    while retry_count < max_retries:
        try:
            db.session.commit()
            db.session.close()
            logger.info(f"Template sync complete: {stats}")
            break
        except Exception as e:
            retry_count += 1
            db.session.rollback()
            db.session.close()
            
            if "database is locked" in str(e).lower() and retry_count < max_retries:
                wait_time = retry_count * 0.5
                logger.warning(f"Database locked, retrying in {wait_time}s (attempt {retry_count}/{max_retries})")
                time.sleep(wait_time)
            else:
                logger.error(f"Failed to commit template sync: {e}")
                stats['errors'].append(f"Database commit failed: {str(e)}")
                break
    
    # Re-enable autoflush
    db.session.autoflush = True
    
    return stats


def _update_template_specs(template, proxmox, node_name, vmid):
    """
    Fetch and cache template specs from Proxmox API.
    
    Args:
        template: Template database object
        proxmox: ProxmoxAPI connection
        node_name: Node name
        vmid: VM ID
    """
    try:
        # Get VM config
        config = proxmox.nodes(node_name).qemu(vmid).config.get()
        
        # Extract specs
        template.cpu_cores = config.get('cores', 1)
        template.cpu_sockets = config.get('sockets', 1)
        template.memory_mb = config.get('memory', 2048)
        template.os_type = config.get('ostype', 'l26')
        
        # Parse disk configuration
        disk_key = None
        for key in ['scsi0', 'virtio0', 'ide0', 'sata0']:
            if key in config:
                disk_key = key
                break
        
        if disk_key:
            disk_config = config[disk_key]
            template.disk_path = disk_config
            
            # Parse storage and size
            parts = disk_config.split(',')
            if parts:
                template.disk_storage = parts[0].split(':')[0] if ':' in parts[0] else None
                
                # Find size parameter
                for part in parts:
                    if part.startswith('size='):
                        size_str = part.split('=')[1]
                        if size_str.endswith('G'):
                            template.disk_size_gb = float(size_str[:-1])
                        elif size_str.endswith('M'):
                            template.disk_size_gb = float(size_str[:-1]) / 1024
                        elif size_str.endswith('K'):
                            template.disk_size_gb = float(size_str[:-1]) / (1024 * 1024)
                        break
        
        # Parse network configuration
        for key in ['net0', 'net1', 'net2']:
            if key in config:
                net_config = config[key]
                for part in net_config.split(','):
                    if part.startswith('bridge='):
                        template.network_bridge = part.split('=')[1]
                        break
                break
        
        template.specs_cached_at = datetime.utcnow()
        
    except Exception as e:
        logger.error(f"Failed to fetch specs for template {vmid} on {node_name}: {e}")


def sync_isos_from_proxmox(full_sync=True):
    """
    Sync ISO images from all Proxmox clusters to database.
    
    Args:
        full_sync: If True, scan all storages for ISOs. If False, only verify cached ISOs exist.
    
    Returns:
        dict: Sync statistics (isos_found, isos_added, isos_updated, isos_removed)
    """
    from app.services.proxmox_service import get_clusters_from_db, get_proxmox_admin_for_cluster
    from app.models import ISOImage, db
    
    logger.info(f"Starting ISO sync (full_sync={full_sync})")
    
    stats = {
        'isos_found': 0,
        'isos_added': 0,
        'isos_updated': 0,
        'isos_removed': 0,
        'errors': []
    }
    
    # Disable autoflush to avoid mid-operation locks
    db.session.autoflush = False
    
    # Track all ISOs found in Proxmox (volid set)
    found_volids = set()
    
    # Sync each cluster
    for cluster in get_clusters_from_db():
        cluster_id = cluster['id']
        cluster_ip = cluster['host']
        
        try:
            proxmox = get_proxmox_admin_for_cluster(cluster_id)
            if not proxmox:
                logger.warning(f"Could not connect to cluster {cluster_id} ({cluster_ip})")
                stats['errors'].append(f"Connection failed: {cluster_id}")
                continue
            
            # Test connection
            try:
                proxmox.version.get()
            except Exception as e:
                logger.warning(f"Cluster {cluster_id} ({cluster_ip}) connection test failed: {e}")
                stats['errors'].append(f"Connection test failed: {cluster_id} - {str(e)}")
                continue
            
            # Get all nodes in this cluster
            nodes = proxmox.nodes.get()
            
            for node_data in nodes:
                node_name = node_data['node']
                
                try:
                    # Get all storages on this node
                    storages = proxmox.nodes(node_name).storage.get()
                    
                    for storage_info in storages:
                        storage_name = storage_info['storage']
                        content_types = storage_info.get('content', '').split(',')
                        
                        # Skip storages that don't support ISO content
                        if 'iso' not in content_types:
                            continue
                        
                        if not storage_info.get('enabled', True):
                            continue
                        
                        try:
                            # Get ISO files from this storage
                            content = proxmox.nodes(node_name).storage(storage_name).content.get(content='iso')
                            
                            for item in content:
                                # Only process .iso files
                                volid = item.get('volid', '')
                                if not volid.lower().endswith('.iso'):
                                    continue
                                
                                name = item.get('volid', '').split('/')[-1]
                                size = item.get('size', 0)
                                
                                stats['isos_found'] += 1
                                found_volids.add(volid)
                                
                                # Check if ISO exists in database
                                existing = ISOImage.query.filter_by(volid=volid).first()
                                
                                if existing:
                                    # Update existing ISO
                                    existing.name = name
                                    existing.size = size
                                    existing.node = node_name
                                    existing.storage = storage_name
                                    existing.cluster_id = cluster_id
                                    existing.last_seen = datetime.utcnow()
                                    stats['isos_updated'] += 1
                                else:
                                    # Add new ISO
                                    new_iso = ISOImage(
                                        volid=volid,
                                        name=name,
                                        size=size,
                                        node=node_name,
                                        storage=storage_name,
                                        cluster_id=cluster_id,
                                        discovered_at=datetime.utcnow(),
                                        last_seen=datetime.utcnow()
                                    )
                                    db.session.add(new_iso)
                                    stats['isos_added'] += 1
                        
                        except Exception as e:
                            logger.warning(f"Failed to scan storage {storage_name} on {node_name}: {e}")
                            stats['errors'].append(f"Storage scan failed: {node_name}/{storage_name} - {str(e)}")
                
                except Exception as e:
                    # Use debug level for expected connection issues
                    if 'hostname lookup' in str(e) or 'No route to host' in str(e):
                        logger.debug(f"Node {node_name} unreachable (expected if node is offline): {e}")
                    else:
                        logger.warning(f"Failed to scan node {node_name}: {e}")
                        stats['errors'].append(f"Node scan failed: {node_name} - {str(e)}")
        
        except Exception as e:
            logger.error(f"Failed to sync cluster {cluster_id}: {e}")
            stats['errors'].append(f"Cluster sync failed: {cluster_id} - {str(e)}")
    
    # Remove ISOs that no longer exist in Proxmox
    if full_sync:
        stale_isos = ISOImage.query.filter(ISOImage.volid.notin_(found_volids)).all()
        for iso in stale_isos:
            db.session.delete(iso)
            stats['isos_removed'] += 1
    
    # Commit all changes with retry logic
    max_retries = 5
    retry_count = 0
    while retry_count < max_retries:
        try:
            db.session.commit()
            db.session.close()
            logger.info(f"ISO sync complete: {stats}")
            break
        except Exception as e:
            retry_count += 1
            db.session.rollback()
            db.session.close()
            
            if "database is locked" in str(e).lower() and retry_count < max_retries:
                wait_time = retry_count * 1.0
                logger.warning(f"Database locked, retrying in {wait_time}s (attempt {retry_count}/{max_retries})")
                time.sleep(wait_time)
            else:
                logger.error(f"Failed to commit ISO sync changes: {e}")
                stats['errors'].append(f"Database commit failed: {str(e)}")
                break
    
    # Re-enable autoflush
    db.session.autoflush = True
    
    return stats


def quick_iso_verification():
    """
    Quick ISO verification - just update last_seen for cached ISOs.
    Much faster than full sync.
    """
    from app.services.proxmox_service import get_proxmox_admin_for_cluster
    from app.models import ISOImage, db
    
    logger.info("Starting quick ISO verification")
    
    stats = {
        'isos_verified': 0,
        'isos_removed': 0,
        'errors': []
    }
    
    # Get all cached ISOs
    cached_isos = ISOImage.query.all()
    
    for iso in cached_isos:
        try:
            proxmox = get_proxmox_admin_for_cluster(iso.cluster_id)
            if not proxmox:
                continue
            
            # Quick check: just verify storage exists and is accessible
            try:
                content = proxmox.nodes(iso.node).storage(iso.storage).content.get(content='iso')
                
                # Check if our ISO still exists
                found = False
                for item in content:
                    if item.get('volid') == iso.volid:
                        found = True
                        break
                
                if found:
                    iso.last_seen = datetime.utcnow()
                    stats['isos_verified'] += 1
                else:
                    # ISO no longer exists
                    db.session.delete(iso)
                    stats['isos_removed'] += 1
            
            except Exception as e:
                logger.warning(f"Could not verify ISO {iso.volid}: {e}")
                stats['errors'].append(f"Verification failed: {iso.volid} - {str(e)}")
        
        except Exception as e:
            logger.warning(f"Error verifying ISO {iso.volid}: {e}")
            stats['errors'].append(f"Error: {iso.volid} - {str(e)}")
    
    # Commit changes
    try:
        db.session.commit()
        logger.info(f"Quick ISO verification complete: {stats}")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to commit verification changes: {e}")
    
    return stats


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
    """Get current sync statistics for all sync types.
    
    Returns:
        Dict with sync stats for VMs, templates, and ISOs
    """
    from app.models import VMInventory, Template, ISOImage

    # Get database stats for VMs
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
    
    # Get template stats
    total_templates = Template.query.count()
    
    # Get ISO stats
    total_isos = ISOImage.query.count()
    
    return {
        **_sync_stats,
        'running': _sync_running,
        
        # VM stats
        'total_vms': total_vms,
        'fresh_vms': fresh_vms,
        'error_vms': error_vms,
        'running_vms': running_vms,
        'last_full_sync_iso': _sync_stats['last_full_sync'].isoformat() if _sync_stats['last_full_sync'] else None,
        'last_quick_sync_iso': _sync_stats['last_quick_sync'].isoformat() if _sync_stats['last_quick_sync'] else None,
        
        # Template stats
        'total_templates': total_templates,
        'last_template_full_sync_iso': _sync_stats['last_template_full_sync'].isoformat() if _sync_stats['last_template_full_sync'] else None,
        'last_template_quick_sync_iso': _sync_stats['last_template_quick_sync'].isoformat() if _sync_stats['last_template_quick_sync'] else None,
        
        # ISO stats
        'total_isos': total_isos,
        'last_iso_full_sync_iso': _sync_stats['last_iso_full_sync'].isoformat() if _sync_stats['last_iso_full_sync'] else None,
        'last_iso_quick_sync_iso': _sync_stats['last_iso_quick_sync'].isoformat() if _sync_stats['last_iso_quick_sync'] else None,
    }
