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
    convert_to_template: bool = False
) -> Tuple[bool, Optional[int], str]:
    """
    Build a VM from scratch with ISO-based installation.
    VM will be created on the node where the ISO is located.
    
    Args:
        cluster_id: Proxmox cluster ID
        vm_name: Name for the VM
        os_type: OS type (ubuntu, debian, centos, rocky, alma, windows)
        iso_file: ISO file path (e.g., 'local:iso/ubuntu-22.04.iso')
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
        
        # Find which node has the ISO
        logger.info(f"Detecting node for ISO {iso_file}...")
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
            "boot": f"order={boot_order}",
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
        
        # Convert to template if requested
        if convert_to_template:
            logger.info(f"Converting VM {vmid} to template")
            proxmox.nodes(node).qemu(vmid).template.post()
            return True, f"VM {vmid} created and converted to template successfully. Ready to use in classes."
        
        # VM left stopped for manual installation
        return True, f"VM {vmid} created successfully. Boot VM via console to begin {os_type} installation."
            
    except Exception as e:
        logger.error(f"Failed to deploy VM: {e}", exc_info=True)
        return False, f"Failed to create VM: {str(e)}"


def list_available_isos(cluster_id: str) -> Tuple[bool, list, str]:
    """List available ISO images across all nodes in the cluster (with database cache)."""
    from app.models import ISOImage, db
    from app.services.proxmox_service import get_proxmox_admin_for_cluster
    from datetime import datetime, timedelta
    
    try:
        # First, try to return cached ISOs (updated within last 5 minutes)
        five_minutes_ago = datetime.utcnow() - timedelta(minutes=5)
        cached_isos = ISOImage.query.filter(
            ISOImage.cluster_id == cluster_id,
            ISOImage.last_seen >= five_minutes_ago
        ).all()
        
        if cached_isos:
            logger.info(f"Returning {len(cached_isos)} cached ISOs for cluster {cluster_id}")
            return True, [iso.to_dict() for iso in cached_isos], f"Found {len(cached_isos)} ISO images (cached)"
        
        # Cache miss or stale - scan Proxmox and update database
        logger.info(f"Scanning Proxmox for ISOs in cluster {cluster_id}")
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        if not proxmox:
            return False, [], f"Failed to connect to cluster {cluster_id}"
        
        isos = []
        seen_volids = set()  # Track unique ISOs to avoid duplicates from shared storage
        nodes = proxmox.nodes.get()
        
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
                            
                            # Skip if we've already seen this ISO (shared storage across nodes)
                            if volid in seen_volids:
                                continue
                            
                            seen_volids.add(volid)
                            
                            iso_data = {
                                'volid': volid,
                                'name': volid.split('/')[-1],
                                'size': item.get('size', 0),
                                'node': node_name,
                                'storage': storage_name
                            }
                            isos.append(iso_data)
                            
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
                                        name=iso_data['name'],
                                        size=item.get('size', 0),
                                        node=node_name,
                                        storage=storage_name,
                                        cluster_id=cluster_id
                                    )
                                    db.session.add(new_iso)
                            except Exception as db_error:
                                logger.warning(f"Failed to cache ISO {volid}: {db_error}")
                                
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
        except Exception as e:
            logger.error(f"Failed to commit ISO cache: {e}")
            db.session.rollback()
        
        logger.info(f"Found {len(isos)} ISO images across all nodes")
        return True, isos, f"Found {len(isos)} ISO images"
        
    except Exception as e:
        logger.error(f"Failed to list ISOs: {e}", exc_info=True)
        return False, [], f"Failed to list ISOs: {str(e)}"


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
