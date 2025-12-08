#!/usr/bin/env python3
"""
Template Migration Service - Migrate templates between Proxmox clusters.

Handles the actual disk copying and VM creation across clusters.
"""

import logging
import re
from typing import Optional

from app.models import Template, db
from app.services.ssh_executor import get_pooled_ssh_executor
from app.services.proxmox_service import get_proxmox_admin_for_cluster

logger = logging.getLogger(__name__)

# Default storage paths (can be overridden)
DEFAULT_STORAGE_PATHS = {
    'local': '/var/lib/vz/images',
    'local-lvm': '/dev/pve',
    'local-zfs': '/dev/zvol/rpool/data'
}


def get_storage_path(storage_name: str, storage_type: str) -> str:
    """Get storage path based on storage type."""
    if storage_type == 'dir':
        return f"/var/lib/vz/images"  # Default for directory storage
    elif storage_type == 'zfspool':
        return f"/dev/zvol/{storage_name}"
    elif storage_type == 'lvmthin' or storage_type == 'lvm':
        return f"/dev/pve"
    else:
        # Fallback to default paths
        return DEFAULT_STORAGE_PATHS.get(storage_name, f"/var/lib/vz/images")


def migrate_template_between_clusters(
    source_template: Template,
    destination_cluster_id: str,
    destination_storage: str,
    operation: str = 'copy',
    new_name: Optional[str] = None
) -> bool:
    """Migrate a template from one cluster to another.
    
    Args:
        source_template: Source Template object
        destination_cluster_id: Destination cluster ID
        destination_storage: Storage pool name on destination
        operation: 'copy' or 'move'
        new_name: Optional new name for template
        
    Returns:
        True if successful, False otherwise
    """
    logger.info(f"Starting template migration: {source_template.name} (VMID {source_template.proxmox_vmid}) "
                f"to cluster {destination_cluster_id}")
    
    try:
        # Get source cluster connection
        from app.config import CLUSTERS
        
        source_cluster = next((c for c in CLUSTERS if c["host"] == source_template.cluster_ip), None)
        if not source_cluster:
            logger.error(f"Source cluster not found for IP {source_template.cluster_ip}")
            return False
        
        destination_cluster = next((c for c in CLUSTERS if c["id"] == destination_cluster_id), None)
        if not destination_cluster:
            logger.error(f"Destination cluster {destination_cluster_id} not found")
            return False
        
        source_proxmox = get_proxmox_admin_for_cluster(source_cluster["id"])
        dest_proxmox = get_proxmox_admin_for_cluster(destination_cluster_id)
        
        # Get source VM config
        source_node = source_template.node
        source_vmid = source_template.proxmox_vmid
        
        logger.info(f"Fetching source VM config from {source_node}/{source_vmid}")
        source_config = source_proxmox.nodes(source_node).qemu(source_vmid).config.get()
        
        # Find available VMID on destination
        logger.info("Finding available VMID on destination cluster")
        dest_resources = dest_proxmox.cluster.resources.get(type="vm")
        used_vmids = {int(r.get('vmid', 0)) for r in dest_resources}
        
        dest_vmid = source_vmid
        while dest_vmid in used_vmids:
            dest_vmid += 1
        
        logger.info(f"Using destination VMID: {dest_vmid}")
        
        # Select destination node (pick first available)
        dest_nodes = dest_proxmox.nodes.get()
        if not dest_nodes:
            logger.error("No nodes available on destination cluster")
            return False
        
        dest_node = dest_nodes[0]['node']
        logger.info(f"Using destination node: {dest_node}")
        
        # Get SSH connections
        source_ssh = get_pooled_ssh_executor(
            host=source_cluster["host"],
            username=source_cluster["user"].split("@")[0],
            password=source_cluster["password"]
        )
        
        dest_ssh = get_pooled_ssh_executor(
            host=destination_cluster["host"],
            username=destination_cluster["user"].split("@")[0],
            password=destination_cluster["password"]
        )
        
        # Extract disk info from source config
        disk_configs = {}
        for key, value in source_config.items():
            if key.startswith(('scsi', 'sata', 'virtio', 'ide')) and isinstance(value, str):
                if ':' in value and 'cdrom' not in value.lower():
                    disk_configs[key] = value
        
        if not disk_configs:
            logger.error("No disks found in source template")
            return False
        
        logger.info(f"Found {len(disk_configs)} disk(s) to migrate: {list(disk_configs.keys())}")
        
        # Create destination VM shell
        logger.info(f"Creating VM shell on destination: {dest_node}/{dest_vmid}")
        
        vm_name = new_name or source_template.name
        memory = source_config.get('memory', 2048)
        cores = source_config.get('cores', 2)
        sockets = source_config.get('sockets', 1)
        
        # Build VM creation command
        create_cmd = (
            f"qm create {dest_vmid} "
            f"--name {vm_name} "
            f"--memory {memory} "
            f"--cores {cores} "
            f"--sockets {sockets}"
        )
        
        # Add optional parameters
        if 'ostype' in source_config:
            create_cmd += f" --ostype {source_config['ostype']}"
        if 'cpu' in source_config:
            create_cmd += f" --cpu {source_config['cpu']}"
        if 'machine' in source_config:
            create_cmd += f" --machine {source_config['machine']}"
        if 'bios' in source_config:
            create_cmd += f" --bios {source_config['bios']}"
        if 'scsihw' in source_config:
            create_cmd += f" --scsihw {source_config['scsihw']}"
        
        exit_code, stdout, stderr = dest_ssh.execute(create_cmd, timeout=60)
        if exit_code != 0:
            logger.error(f"Failed to create VM shell: {stderr}")
            return False
        
        logger.info("VM shell created successfully")
        
        # Migrate each disk
        for disk_key, disk_config in disk_configs.items():
            logger.info(f"Migrating disk {disk_key}: {disk_config}")
            
            # Parse disk config (format: storage:size or storage:vmid/disk-name)
            parts = disk_config.split(',')[0]  # Get just the storage part
            storage_parts = parts.split(':')
            
            if len(storage_parts) < 2:
                logger.warning(f"Could not parse disk config: {disk_config}")
                continue
            
            source_storage = storage_parts[0]
            disk_identifier = storage_parts[1]
            
            # Build source disk path
            source_path = f"/dev/zvol/{source_storage}/vm-{source_vmid}-{disk_key}-0"
            
            # Check if it's a raw disk or qcow2
            check_cmd = f"file {source_path} || qemu-img info {source_path}"
            exit_code, stdout, stderr = source_ssh.execute(check_cmd, timeout=30, check=False)
            
            is_qcow2 = 'QCOW2' in stdout or 'qcow2' in stdout.lower()
            disk_format = 'qcow2' if is_qcow2 else 'raw'
            
            logger.info(f"Source disk format: {disk_format}")
            
            # Create destination disk path
            dest_path = f"{destination_storage}:{dest_vmid}/{disk_key}-0.{disk_format}"
            
            # Copy disk using qemu-img convert over SSH
            logger.info(f"Copying disk {disk_key} from {source_node} to {dest_node}...")
            
            # Use qemu-img convert piped through SSH
            copy_cmd = (
                f"qemu-img convert -O {disk_format} -p {source_path} - | "
                f"ssh -o StrictHostKeyChecking=no root@{destination_cluster['host']} "
                f"\"cat > /tmp/temp-disk-{dest_vmid}-{disk_key}.img && "
                f"qm importdisk {dest_vmid} /tmp/temp-disk-{dest_vmid}-{disk_key}.img {destination_storage} && "
                f"rm /tmp/temp-disk-{dest_vmid}-{disk_key}.img\""
            )
            
            logger.info("Starting disk copy (this may take several minutes)...")
            exit_code, stdout, stderr = source_ssh.execute(copy_cmd, timeout=3600, check=False)
            
            if exit_code != 0:
                logger.error(f"Disk copy failed: {stderr}")
                # Clean up and return
                dest_ssh.execute(f"qm destroy {dest_vmid}", timeout=60, check=False)
                return False
            
            logger.info(f"Disk {disk_key} copied successfully")
            
            # Attach disk to VM
            attach_cmd = f"qm set {dest_vmid} --{disk_key} {destination_storage}:vm-{dest_vmid}-disk-0"
            exit_code, stdout, stderr = dest_ssh.execute(attach_cmd, timeout=60, check=False)
            
            if exit_code != 0:
                logger.warning(f"Failed to attach disk: {stderr}")
        
        # Copy EFI disk if exists
        if 'efidisk0' in source_config:
            logger.info("Copying EFI disk...")
            efi_cmd = f"qm set {dest_vmid} --efidisk0 {destination_storage}:1,efitype=4m,pre-enrolled-keys=1"
            dest_ssh.execute(efi_cmd, timeout=60, check=False)
        
        # Copy TPM if exists
        if 'tpmstate0' in source_config:
            logger.info("Copying TPM state...")
            tpm_cmd = f"qm set {dest_vmid} --tpmstate0 {destination_storage}:1,version=v2.0"
            dest_ssh.execute(tpm_cmd, timeout=60, check=False)
        
        # Copy network config
        for key in source_config:
            if key.startswith('net'):
                logger.info(f"Copying network config {key}")
                net_cmd = f"qm set {dest_vmid} --{key} {source_config[key]}"
                dest_ssh.execute(net_cmd, timeout=30, check=False)
        
        # Convert to template
        logger.info("Converting destination VM to template...")
        template_cmd = f"qm template {dest_vmid}"
        exit_code, stdout, stderr = dest_ssh.execute(template_cmd, timeout=60)
        
        if exit_code != 0:
            logger.error(f"Failed to convert to template: {stderr}")
            return False
        
        # Create template record in database
        logger.info("Creating template record in database...")
        
        new_template = Template(
            name=vm_name,
            proxmox_vmid=dest_vmid,
            node=dest_node,
            cluster_ip=destination_cluster["host"],
            cpu_cores=cores,
            cpu_sockets=sockets,
            memory_mb=memory,
            os_type=source_config.get('ostype'),
            disk_storage=destination_storage
        )
        
        db.session.add(new_template)
        db.session.commit()
        
        logger.info(f"Template migrated successfully: {vm_name} (VMID {dest_vmid})")
        
        # If operation is 'move', delete source template
        if operation == 'move':
            logger.info("Move operation - deleting source template...")
            try:
                delete_cmd = f"qm destroy {source_vmid}"
                source_ssh.execute(delete_cmd, timeout=60, check=False)
                
                # Delete from database
                db.session.delete(source_template)
                db.session.commit()
                
                logger.info("Source template deleted")
            except Exception as e:
                logger.error(f"Failed to delete source template: {e}")
        
        return True
        
    except Exception as e:
        logger.exception(f"Template migration failed: {e}")
        return False
