"""
VM Deployment Service - Build VMs from scratch with full configuration options

Based on the deployment.py architecture with support for:
- Linux VMs (Ubuntu, Debian, CentOS, Rocky, Alma) with cloud-init
- Windows VMs with VirtIO drivers
- Custom ISO uploads
- Cloud image templates
- Advanced hardware configuration
"""

import logging
import time
import subprocess
import os
import tempfile
from typing import Optional, Dict, Any, Tuple

from app.services.proxmox_service import get_proxmox_admin_for_cluster
from app.services.ssh_executor import SSHExecutor, get_ssh_executor_from_config
from app.config import CLUSTERS

logger = logging.getLogger(__name__)


def build_vm_from_scratch(
    cluster_id: str,
    vm_name: str,
    os_type: str,
    iso_file: str,
    # Hardware configuration
    cpu_cores: int = 2,
    cpu_sockets: int = 1,
    memory_mb: int = 2048,
    disk_size_gb: int = 32,
    # Network configuration
    network_bridge: str = "vmbr0",
    # Storage configuration
    storage: str = "local-lvm",
    # Advanced hardware options
    cpu_type: str = "host",
    cpu_flags: Optional[str] = None,
    cpu_limit: Optional[int] = None,
    numa_enabled: bool = False,
    bios_type: str = "seabios",
    machine_type: str = "pc",
    vga_type: str = "std",
    boot_order: str = "cdn",
    scsihw: str = "virtio-scsi-pci",
    tablet: bool = True,
    agent_enabled: bool = True,
    onboot: bool = False,
    # Template options
    convert_to_template: bool = False,
    # Optional node override (if we already know where ISO is)
    target_node: Optional[str] = None
) -> Tuple[bool, Optional[int], str]:
    """
    Build a VM from scratch with ISO-based installation.
    VM will be created on the node where the ISO is located.
    
    Args:
        cluster_id: Proxmox cluster ID
        vm_name: Name for the VM
        os_type: OS type (ubuntu, debian, centos, rocky, alma, windows)
        iso_file: ISO file path (e.g., 'local:iso/ubuntu-22.04.iso')
        target_node: Optional node name (if we already know where ISO is)
        ... (see parameters above)
        
    Returns:
        Tuple of (success: bool, vmid: Optional[int], message: str)
    """
    try:
        logger.info(f"Building VM from scratch: {vm_name} on cluster {cluster_id} with ISO {iso_file}")
        
        # Get Proxmox connection
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        if not proxmox:
            return False, None, f"Failed to connect to cluster {cluster_id}"
        
        # Use provided node or look up in database first, fallback to scanning
        if target_node:
            node = target_node
            logger.info(f"Using provided target node: {node}")
        else:
            # Check database first (fast!)
            from app.models import ISOImage
            iso_record = ISOImage.query.filter_by(volid=iso_file, cluster_id=cluster_id).first()
            if iso_record:
                node = iso_record.node
                logger.info(f"Found ISO node in database cache: {node}")
            else:
                # Fallback to scanning nodes (slow)
                logger.info(f"ISO not in cache, scanning nodes for {iso_file}...")
                node = _find_node_with_iso(proxmox, iso_file)
                if not node:
                    return False, None, f"Could not find ISO {iso_file} on any node"
        
        logger.info(f"Building VM on node {node}")
        
        # Get cluster config for SSH operations
        cluster_config = None
        for cluster in CLUSTERS:
            if cluster['id'] == cluster_id:
                cluster_config = cluster
                break
        
        if not cluster_config:
            return False, None, f"Cluster {cluster_id} not found in configuration"
        
        # Get next available VMID
        logger.info("Allocating VMID...")
        vmid = proxmox.cluster.nextid.get()
        logger.info(f"Allocated VMID {vmid}")
        
        # Determine VM type (Windows vs Linux)
        is_windows = os_type.lower() in ['windows', 'win10', 'win11', 'windows10', 'windows11']
        
        # Deploy VM with ISO
        success, message = _deploy_vm_with_iso(
            proxmox=proxmox,
            cluster_config=cluster_config,
            node=node,
            vmid=vmid,
            vm_name=vm_name,
            os_type=os_type,
            is_windows=is_windows,
            cpu_cores=cpu_cores,
            cpu_sockets=cpu_sockets,
            memory_mb=memory_mb,
            disk_size_gb=disk_size_gb,
            storage=storage,
            iso_file=iso_file,
            network_bridge=network_bridge,
            cpu_type=cpu_type,
            cpu_flags=cpu_flags,
            cpu_limit=cpu_limit,
            numa_enabled=numa_enabled,
            bios_type=bios_type,
            machine_type=machine_type,
            vga_type=vga_type,
            boot_order=boot_order,
            scsihw=scsihw,
            tablet=tablet,
            agent_enabled=agent_enabled,
            onboot=onboot,
            convert_to_template=convert_to_template
        )
        
        if success:
            return True, vmid, message
        else:
            return False, vmid, message
            
    except Exception as e:
        logger.error(f"Failed to build VM from scratch: {e}", exc_info=True)
        return False, None, str(e)


def _find_node_with_iso(proxmox, iso_file: str) -> Optional[str]:
    """Find which node has the specified ISO."""
    try:
        # Get all nodes
        nodes = proxmox.nodes.get()
        
        # Extract storage from iso_file (format: storage:iso/filename)
        if ':' not in iso_file:
            logger.error(f"Invalid ISO path format: {iso_file}")
            return None
        
        storage_part = iso_file.split(':')[0]
        
        # Check each node for the ISO
        for node in nodes:
            try:
                node_name = node['node']
                logger.debug(f"Checking node {node_name} for ISO {iso_file}")
                # Get storage content for this node
                storage_content = proxmox.nodes(node_name).storage(storage_part).content.get()
                for item in storage_content:
                    if item.get('volid') == iso_file:
                        logger.info(f"Found ISO {iso_file} on node {node_name}")
                        return node_name
            except Exception as e:
                logger.debug(f"Could not check node {node.get('node')}: {e}")
                continue
        
        logger.warning(f"ISO {iso_file} not found on any node")
        return None
        
    except Exception as e:
        logger.error(f"Failed to find node with ISO: {e}")
        return None


def _deploy_vm_with_iso(
    proxmox,
    cluster_config: Dict,
    node: str,
    vmid: int,
    vm_name: str,
    os_type: str,
    is_windows: bool,
    cpu_cores: int,
    cpu_sockets: int,
    memory_mb: int,
    disk_size_gb: int,
    storage: str,
    iso_file: str,
    network_bridge: str,
    cpu_type: str,
    cpu_flags: Optional[str],
    cpu_limit: Optional[int],
    numa_enabled: bool,
    bios_type: str,
    machine_type: str,
    vga_type: str,
    boot_order: str,
    scsihw: str,
    tablet: bool,
    agent_enabled: bool,
    onboot: bool,
    convert_to_template: bool
) -> Tuple[bool, str]:
    """Deploy a VM with ISO-based installation."""
    try:
        logger.info(f"Creating VM {vmid} ({os_type}) on node {node} with ISO {iso_file}")
        
        # Determine OS type for Proxmox
        if is_windows:
            ostype = "win10"
            # Use UEFI for Windows by default
            if bios_type == "seabios":
                bios_type = "ovmf"
        else:
            ostype = "l26"  # Linux 2.6+ kernel
        
        # Build VM configuration
        vm_config = {
            "vmid": vmid,
            "name": vm_name,
            "cores": cpu_cores,
            "sockets": cpu_sockets,
            "memory": memory_mb,
            "scsihw": scsihw,
            "scsi0": f"{storage}:{disk_size_gb}",
            "net0": f"virtio,bridge={network_bridge}",
            "ostype": ostype,
            "cpu": cpu_type,
            "bios": bios_type,
            "machine": machine_type,
            "vga": vga_type,
            "boot": "order=ide2;scsi0;net0",  # Boot from CD (ISO) first, then disk, then network
            "onboot": 1 if onboot else 0,
            "agent": 1 if agent_enabled else 0,
            "tablet": 1 if tablet else 0
        }
        
        # Add CPU flags if specified
        if cpu_flags:
            vm_config["cpu"] = f"{cpu_type},{cpu_flags}"
        
        # Add CPU limit if specified
        if cpu_limit:
            vm_config["cpulimit"] = cpu_limit
        
        # Add NUMA if enabled
        if numa_enabled:
            vm_config["numa"] = 1
        
        # Add ISO
        vm_config["ide2"] = f"{iso_file},media=cdrom"
        
        # For Windows, try to add VirtIO drivers ISO
        if is_windows:
            # Extract storage from iso_file
            iso_storage = iso_file.split(':')[0] if ':' in iso_file else 'local'
            virtio_iso = _ensure_virtio_iso(proxmox, node, iso_storage)
            if virtio_iso:
                logger.info(f"Adding VirtIO ISO: {virtio_iso}")
                vm_config["ide0"] = f"{virtio_iso},media=cdrom"
        
        # Create VM
        logger.info(f"Creating VM with config: {vm_config}")
        proxmox.nodes(node).qemu.post(**vm_config)
        
        logger.info(f"VM {vmid} created successfully")
        
        # Wait for VM creation to finalize
        time.sleep(2)
        
        # Get MAC address from VM config
        mac_address = None
        try:
            vm_config_result = proxmox.nodes(node).qemu(vmid).config.get()
            net0 = vm_config_result.get('net0', '')
            if net0:
                # Parse net0 format: "virtio=XX:XX:XX:XX:XX:XX,bridge=vmbr0"
                for part in net0.split(','):
                    if '=' in part:
                        key, value = part.split('=', 1)
                        if key.strip() == 'virtio' or key.strip() == 'e1000':
                            mac_address = value.strip()
                            logger.info(f"Extracted MAC address: {mac_address}")
                            break
        except Exception as e:
            logger.warning(f"Failed to extract MAC address for VM {vmid}: {e}")
        
        # Update VMInventory database so VM appears in web UI immediately
        try:
            from app.models import VMInventory, db
            from datetime import datetime
            
            logger.info(f"Adding VM {vmid} to VMInventory database")
            
            # Create VMInventory record
            vm_inventory = VMInventory(
                cluster_id=cluster_config['id'],
                vmid=vmid,
                name=vm_name,
                node=node,
                type='qemu',
                status='stopped',
                is_template=convert_to_template,  # Fixed: use is_template not template
                # Hardware info
                cores=cpu_cores,
                memory=memory_mb,
                disk_size=disk_size_gb * 1024 * 1024 * 1024,  # Convert GB to bytes
                # Network info (MAC discovered above, IP will be discovered on first sync)
                ip=None,
                mac_address=mac_address,
                # Timestamps
                last_updated=datetime.utcnow(),
                last_status_check=datetime.utcnow()
            )
            db.session.add(vm_inventory)
            db.session.commit()
            logger.info(f"VM {vmid} added to VMInventory database with MAC {mac_address}")
            
            # Trigger immediate background sync to update VM status and enable API actions
            try:
                from app.services.background_sync import trigger_immediate_sync
                logger.info(f"Triggering immediate background sync for new VM {vmid}")
                trigger_immediate_sync()
            except Exception as sync_error:
                logger.warning(f"Failed to trigger background sync: {sync_error}")
                
        except Exception as e:
            logger.warning(f"Failed to add VM {vmid} to VMInventory database: {e}")
            # Don't fail the whole operation if database update fails
        
        # Convert to template if requested
        if convert_to_template:
            logger.info(f"Converting VM {vmid} to template")
            proxmox.nodes(node).qemu(vmid).template.post()
            
            # Update template flag in database
            try:
                from app.models import VMInventory, db
                vm_record = VMInventory.query.filter_by(cluster_id=cluster_config['id'], vmid=vmid).first()
                if vm_record:
                    vm_record.is_template = True  # Fixed: use is_template not template
                    db.session.commit()
                    logger.info(f"Updated VMInventory template flag for VM {vmid}")
            except Exception as e:
                logger.warning(f"Failed to update template flag in database: {e}")
            
            return True, f"VM {vmid} created and converted to template successfully. Ready to use in classes."
        
        # VM left stopped for manual installation
        return True, f"VM {vmid} created successfully. Boot VM via console to begin {os_type} installation."
            
    except Exception as e:
        logger.error(f"Failed to deploy VM: {e}", exc_info=True)
        return False, f"Failed to create VM: {str(e)}"


def list_available_isos(cluster_id: str) -> Tuple[bool, list, str]:
    """List available ISO images across all nodes in the cluster (DATABASE-FIRST architecture).
    
    Returns cached ISOs from database immediately. If cache is empty or stale (>5min),
    triggers background sync but still returns current database state.
    """
    from app.models import ISOImage, db
    from app.services.proxmox_service import get_proxmox_admin_for_cluster
    from datetime import datetime, timedelta
    import threading
    
    try:
        # ALWAYS return from database first (database-first architecture)
        all_cached_isos = ISOImage.query.filter_by(cluster_id=cluster_id).all()
        
        # Filter out container templates - only return actual ISO files
        actual_isos = [iso for iso in all_cached_isos if iso.name.lower().endswith('.iso')]
        
        # Check if cache is stale (>5 minutes old)
        five_minutes_ago = datetime.utcnow() - timedelta(minutes=5)
        fresh_isos = [iso for iso in actual_isos if iso.last_seen >= five_minutes_ago]
        
        if not fresh_isos and actual_isos:
            logger.info(f"ISO cache is stale for cluster {cluster_id}, triggering background refresh")
            # Trigger background sync but still return stale data (app context available from request)
            threading.Thread(target=_sync_isos_background, args=(cluster_id, None), daemon=True).start()
        elif not actual_isos:
            logger.info(f"No cached ISOs for cluster {cluster_id}, triggering immediate sync in background")
            # No ISOs at all - trigger background sync (app context available from request)
            threading.Thread(target=_sync_isos_background, args=(cluster_id, None), daemon=True).start()
        
        # Return whatever we have in database (even if empty or stale)
        iso_list = [iso.to_dict() for iso in actual_isos]
        logger.info(f"Returning {len(iso_list)} ISO files from database for cluster {cluster_id}")
        return True, iso_list, f"Found {len(iso_list)} ISO images"
        
    except Exception as e:
        logger.error(f"Failed to list ISOs: {e}", exc_info=True)
        return False, [], f"Failed to list ISOs: {str(e)}"


def trigger_iso_sync_all_clusters():
    """Trigger ISO sync for all active clusters on app startup."""
    from app.models import Cluster
    from flask import current_app
    import threading
    
    try:
        clusters = Cluster.query.filter_by(is_active=True).all()
        logger.info(f"Triggering ISO sync for {len(clusters)} active clusters")
        
        # Get app instance to pass to threads
        app = current_app._get_current_object()
        
        for cluster in clusters:
            threading.Thread(target=_sync_isos_background, args=(cluster.id, app), daemon=True).start()
            logger.info(f"Started ISO sync thread for cluster {cluster.name} (ID: {cluster.id})")
    except Exception as e:
        logger.error(f"Failed to trigger ISO sync on startup: {e}")


def _sync_isos_background(cluster_id: str, app=None):
    """Background thread to sync ISOs from Proxmox to database.
    
    Args:
        cluster_id: Cluster identifier
        app: Flask app instance for application context (required for database access)
    """
    from app.models import ISOImage, db
    from app.services.proxmox_service import get_proxmox_admin_for_cluster
    from datetime import datetime
    
    # If no app provided, try to get current app (for on-demand sync)
    if app is None:
        from flask import current_app
        try:
            app = current_app._get_current_object()
        except RuntimeError:
            logger.error(f"[ISO Sync] No Flask app context available for cluster {cluster_id}")
            return
    
    # Run within application context
    with app.app_context():
        try:
            logger.info(f"[ISO Sync] Starting background sync for cluster {cluster_id}")
            
            proxmox = get_proxmox_admin_for_cluster(cluster_id)
            if not proxmox:
                logger.error(f"[ISO Sync] Failed to connect to cluster {cluster_id}")
                return
            
            seen_volids = set()  # Track unique ISOs to avoid duplicates from shared storage
            iso_count = 0
            nodes = proxmox.nodes.get()
            
            logger.info(f"[ISO Sync] Scanning {len(nodes)} nodes for ISOs")
            
            for node in nodes:
                node_name = node['node']
                try:
                    # Get storages available on THIS specific node (not cluster-wide)
                    storages = proxmox.nodes(node_name).storage.get()
                    
                    for storage in storages:
                        storage_name = storage['storage']
                        
                        # Check if storage supports ISO content
                        content_types = storage.get('content', '').split(',')
                        if 'iso' not in content_types:
                            continue
                        
                        # Skip if storage is explicitly disabled (but allow if status is missing)
                        status = storage.get('status')
                        if status and status == 'unavailable':
                            logger.debug(f"Storage {storage_name} is unavailable on {node_name}, skipping")
                            continue
                        
                        try:
                            # List ISO files in this storage on this node
                            content = proxmox.nodes(node_name).storage(storage_name).content.get(content='iso')
                            
                            for item in content:
                                volid = item['volid']
                                
                                # Skip container templates (vzdump archives, tar.gz, tar.zst, etc.)
                                # Only include actual ISO files
                                filename = volid.split('/')[-1].lower()
                                if not filename.endswith('.iso'):
                                    logger.debug(f"Skipping non-ISO file: {volid}")
                                    continue
                                
                                # Skip if we've already seen this ISO (shared storage across nodes)
                                if volid in seen_volids:
                                    continue
                                
                                seen_volids.add(volid)
                                iso_count += 1
                                
                                # Update database cache
                                try:
                                    existing = ISOImage.query.filter_by(volid=volid).first()
                                    if existing:
                                        existing.last_seen = datetime.utcnow()
                                        existing.node = node_name
                                        existing.storage = storage_name
                                        existing.size = item.get('size', 0)
                                    else:
                                        new_iso = ISOImage(
                                            volid=volid,
                                            name=volid.split('/')[-1],
                                            size=item.get('size', 0),
                                            node=node_name,
                                            storage=storage_name,
                                            cluster_id=cluster_id
                                        )
                                        db.session.add(new_iso)
                                        logger.debug(f"[ISO Sync] Added new ISO: {volid}")
                                except Exception as db_error:
                                    logger.warning(f"[ISO Sync] Failed to cache ISO {volid}: {db_error}")
                                    
                        except Exception as e:
                            # Don't spam warnings for unavailable storage on nodes
                            if "is not available on node" in str(e):
                                logger.debug(f"Storage {storage_name} not available on {node_name}, skipping")
                            else:
                                logger.warning(f"Could not list ISOs in {node_name}:{storage_name}: {e}")
                            continue
                            
                except Exception as e:
                    logger.warning(f"Could not access node {node_name}: {e}")
                    continue
            
            # Commit database changes
            try:
                db.session.commit()
                logger.info(f"[ISO Sync] Successfully synced {iso_count} ISOs for cluster {cluster_id}")
            except Exception as e:
                logger.error(f"[ISO Sync] Failed to commit ISO cache: {e}")
                db.session.rollback()
                
        except Exception as e:
            logger.error(f"[ISO Sync] Failed to sync ISOs: {e}", exc_info=True)


def _ensure_virtio_iso(
    proxmox,
    node_name: str,
    storage: str = "local"
) -> Optional[str]:
    """
    Ensure VirtIO drivers ISO exists in storage.
    
    Returns:
        ISO path if available, None otherwise
    """
    try:
        virtio_filename = "virtio-win.iso"
        
        # Check if VirtIO ISO already exists
        logger.info(f"Checking for VirtIO ISO in {storage}:iso/")
        try:
            storage_content = proxmox.nodes(node_name).storage(storage).content.get()
            for item in storage_content:
                if item.get('volid', '').endswith(virtio_filename):
                    logger.info(f"Found VirtIO ISO: {item['volid']}")
                    return item['volid']
        except Exception as e:
            logger.warning(f"Could not check storage content: {e}")
        
        # VirtIO ISO not found
        logger.info(f"VirtIO ISO not found in {storage}")
        
        # Download VirtIO ISO (requires internet access from Proxmox node)
        virtio_url = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
        
        logger.info(f"Downloading VirtIO ISO from {virtio_url}")
        try:
            download_task = proxmox.nodes(node_name).storage(storage).download_url.post(
                content='iso',
                filename=virtio_filename,
                url=virtio_url
            )
            logger.info(f"VirtIO ISO download initiated: {download_task}")
            
            # Wait for download to complete (with timeout)
            max_wait = 300  # 5 minutes
            waited = 0
            while waited < max_wait:
                try:
                    storage_content = proxmox.nodes(node_name).storage(storage).content.get()
                    for item in storage_content:
                        if item.get('volid', '').endswith(virtio_filename):
                            logger.info(f"VirtIO ISO downloaded: {item['volid']}")
                            return item['volid']
                except Exception:
                    pass
                
                time.sleep(5)
                waited += 5
            
            logger.warning("VirtIO ISO download timed out")
            return None
            
        except Exception as e:
            logger.error(f"Failed to download VirtIO ISO: {e}")
            return None
        
    except Exception as e:
        logger.error(f"Failed to ensure VirtIO ISO: {e}")
        return None
