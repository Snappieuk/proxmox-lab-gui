"""
ISO synchronization service - database-first architecture.

Syncs ISO images from Proxmox storage to ISOImage database table for fast UI access.
Similar to template_sync.py but for ISO files.

Sync schedule:
- Full sync: Every 30 minutes (scan all nodes, update database)
- Quick verification: Every 5 minutes (check if cached ISOs still exist)
"""

import logging
import time
from datetime import datetime, timedelta
from threading import Thread

from app.services.proxmox_service import get_clusters_from_db
from app.models import ISOImage, db
from app.services.proxmox_service import get_proxmox_admin_for_cluster

logger = logging.getLogger(__name__)


def sync_isos_from_proxmox(full_sync=True):
    """
    Sync ISO images from all Proxmox clusters to database.
    
    Args:
        full_sync: If True, scan all storages for ISOs. If False, only verify cached ISOs exist.
    
    Returns:
        dict: Sync statistics (isos_found, isos_added, isos_updated, isos_removed)
    """
    logger.info(f"Starting ISO sync (full_sync={full_sync})")
    
    stats = {
        'isos_found': 0,
        'isos_added': 0,
        'isos_updated': 0,
        'isos_removed': 0,
        'errors': []
    }
    
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
                                # Only process .iso files (exclude container templates)
                                volid = item.get('volid', '')
                                if not volid.lower().endswith('.iso'):
                                    continue
                                
                                name = item.get('volid', '').split('/')[-1]  # Extract filename from volid
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
                    # Use debug level for expected connection issues (DNS failures, offline nodes)
                    if 'hostname lookup' in str(e) or 'No route to host' in str(e):
                        logger.debug(f"Node {node_name} unreachable (expected if node is offline): {e}")
                    else:
                        logger.warning(f"Failed to scan node {node_name}: {e}")
                        stats['errors'].append(f"Node scan failed: {node_name} - {str(e)}")
        
        except Exception as e:
            logger.error(f"Failed to sync cluster {cluster_id}: {e}")
            stats['errors'].append(f"Cluster sync failed: {cluster_id} - {str(e)}")
    
    # Remove ISOs that no longer exist in Proxmox (not seen in this sync)
    if full_sync:
        stale_isos = ISOImage.query.filter(ISOImage.volid.notin_(found_volids)).all()
        for iso in stale_isos:
            db.session.delete(iso)
            stats['isos_removed'] += 1
    
    # Commit all changes
    try:
        db.session.commit()
        logger.info(f"ISO sync complete: {stats}")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to commit ISO sync changes: {e}")
        stats['errors'].append(f"Database commit failed: {str(e)}")
    
    return stats


def quick_iso_verification():
    """
    Quick verification - just update last_seen for cached ISOs.
    Much faster than full sync, runs every 5 minutes.
    """
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


def _iso_sync_daemon(app):
    """
    Background daemon thread that syncs ISOs on a schedule.
    
    Sync intervals:
    - Full sync: Every 30 minutes
    - Quick verification: Every 5 minutes
    """
    logger.info("ISO sync daemon started")
    
    # Set to None to trigger immediate first sync
    last_full_sync = None
    last_quick_sync = datetime.min
    
    FULL_SYNC_INTERVAL = timedelta(minutes=30)
    QUICK_SYNC_INTERVAL = timedelta(minutes=5)
    
    while True:
        try:
            now = datetime.utcnow()
            
            # Run within app context
            with app.app_context():
                # Full sync every 30 minutes (or immediately on first run)
                if last_full_sync is None or (now - last_full_sync >= FULL_SYNC_INTERVAL):
                    logger.info("Running scheduled full ISO sync")
                    sync_isos_from_proxmox(full_sync=True)
                    last_full_sync = now
                    last_quick_sync = now  # Reset quick sync timer
                
                # Quick verification every 5 minutes (if not just done full sync)
                elif now - last_quick_sync >= QUICK_SYNC_INTERVAL:
                    logger.info("Running scheduled quick ISO verification")
                    quick_iso_verification()
                    last_quick_sync = now
            
            # Sleep for 1 minute before checking again
            time.sleep(60)
        
        except Exception as e:
            logger.error(f"ISO sync daemon error: {e}", exc_info=True)
            time.sleep(60)  # Wait before retrying


def start_iso_sync_daemon(app):
    """
    Start the ISO sync background daemon.
    
    Args:
        app: Flask application instance (needed for app context)
    """
    thread = Thread(target=_iso_sync_daemon, args=(app,), daemon=True, name="ISOSyncDaemon")
    thread.start()
    logger.info("ISO sync daemon thread started")
    return thread


def trigger_immediate_iso_sync():
    """
    Trigger an immediate full ISO sync (can be called from API endpoints).
    Runs in background thread to avoid blocking the request.
    """
    from flask import current_app
    import threading
    
    try:
        app = current_app._get_current_object()
        
        def _run_sync():
            with app.app_context():
                logger.info("Manual ISO sync triggered")
                sync_isos_from_proxmox(full_sync=True)
        
        thread = threading.Thread(target=_run_sync, daemon=True)
        thread.start()
        logger.info("Manual ISO sync thread started")
        return True
    except Exception as e:
        logger.error(f"Failed to trigger manual ISO sync: {e}")
        return False
