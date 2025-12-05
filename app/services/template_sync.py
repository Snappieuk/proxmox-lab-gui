"""
Template synchronization service - database-first architecture.

Syncs Proxmox templates to the Template database table for fast UI access.
Similar to background_sync.py for VMs, but for templates.
"""

import logging
import time
from datetime import datetime, timedelta
from threading import Thread

from app.config import CLUSTERS
from app.models import Template, db
from app.services.proxmox_service import get_proxmox_admin_for_cluster

logger = logging.getLogger(__name__)


def sync_templates_from_proxmox(full_sync=True):
    """
    Sync templates from all Proxmox clusters to database.
    
    Args:
        full_sync: If True, fetch all details including specs. If False, only verify existence.
    
    Returns:
        dict: Sync statistics (templates_found, templates_added, templates_updated, templates_removed)
    """
    stats = {
        'templates_found': 0,
        'templates_added': 0,
        'templates_updated': 0,
        'templates_removed': 0,
        'errors': []
    }
    
    # Track all templates found in Proxmox (cluster_ip, node, vmid tuples)
    found_templates = set()
    
    # Sync each cluster
    for cluster in CLUSTERS:
        cluster_id = cluster['id']
        cluster_ip = cluster['host']
        
        try:
            proxmox = get_proxmox_admin_for_cluster(cluster_id)
            if not proxmox:
                logger.warning(f"Could not connect to cluster {cluster_id} ({cluster_ip})")
                stats['errors'].append(f"Connection failed: {cluster_id}")
                continue
            
            # Get all nodes in this cluster
            nodes = proxmox.nodes.get()
            
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
                                is_replica=False,  # Will be determined later if replicated
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
                # Template no longer exists in Proxmox
                logger.warning(f"Removing stale template {template.name} (VMID {template.proxmox_vmid}) from database")
                db.session.delete(template)
                stats['templates_removed'] += 1
    
    # Commit all changes
    try:
        db.session.commit()
        logger.info(f"Template sync complete: {stats}")
    except Exception as e:
        logger.error(f"Failed to commit template sync: {e}")
        db.session.rollback()
        stats['errors'].append(f"Database commit failed: {str(e)}")
    
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
        
        # Parse disk configuration (scsi0, virtio0, ide0, sata0, etc.)
        disk_key = None
        for key in ['scsi0', 'virtio0', 'ide0', 'sata0']:
            if key in config:
                disk_key = key
                break
        
        if disk_key:
            disk_config = config[disk_key]
            template.disk_path = disk_config
            
            # Parse storage and size from string like "local-lvm:vm-100-disk-0,size=32G"
            parts = disk_config.split(',')
            if parts:
                template.disk_storage = parts[0].split(':')[0] if ':' in parts[0] else None
                
                # Find size parameter
                for part in parts:
                    if part.startswith('size='):
                        size_str = part.split('=')[1]
                        # Convert to GB (handle G, M, K suffixes)
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
                # Parse bridge from string like "virtio=XX:XX:XX:XX:XX:XX,bridge=vmbr0"
                for part in net_config.split(','):
                    if part.startswith('bridge='):
                        template.network_bridge = part.split('=')[1]
                        break
                break
        
        template.specs_cached_at = datetime.utcnow()
        
    except Exception as e:
        logger.error(f"Failed to fetch specs for template {vmid} on {node_name}: {e}")


def start_template_sync_daemon(app):
    """
    Start background thread that periodically syncs templates from Proxmox.
    
    Runs:
    - Full sync on startup
    - Full sync every 30 minutes
    - Quick verification every 5 minutes
    """
    def sync_loop():
        logger.info("Template sync daemon started")
        
        # Initial full sync
        with app.app_context():
            logger.info("Running initial template sync...")
            sync_templates_from_proxmox(full_sync=True)
        
        # Periodic sync
        last_full_sync = time.time()
        full_sync_interval = 30 * 60  # 30 minutes
        quick_sync_interval = 5 * 60   # 5 minutes
        
        while True:
            try:
                time.sleep(quick_sync_interval)
                
                current_time = time.time()
                do_full_sync = (current_time - last_full_sync) >= full_sync_interval
                
                with app.app_context():
                    if do_full_sync:
                        logger.info("Running full template sync...")
                        sync_templates_from_proxmox(full_sync=True)
                        last_full_sync = current_time
                    else:
                        logger.info("Running quick template verification...")
                        sync_templates_from_proxmox(full_sync=False)
            
            except Exception as e:
                logger.error(f"Template sync daemon error: {e}")
    
    # Start daemon thread
    thread = Thread(target=sync_loop, daemon=True, name="TemplateSyncDaemon")
    thread.start()
    logger.info("Template sync daemon thread started")
