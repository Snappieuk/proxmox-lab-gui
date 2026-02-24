#!/usr/bin/env python3
"""
Template Migration Service - Migrate templates between Proxmox clusters.

Handles the actual disk copying and VM creation across clusters.
"""

import logging

from app.models import Template, db
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
        return "/var/lib/vz/images"  # Default for directory storage
    elif storage_type == 'zfspool':
        return f"/dev/zvol/{storage_name}"
    elif storage_type == 'lvmthin' or storage_type == 'lvm':
        return "/dev/pve"
    else:
        # Fallback to default paths
        return DEFAULT_STORAGE_PATHS.get(storage_name, "/var/lib/vz/images")


def migrate_template_between_clusters(
    source_template: Template,
    source_cluster: dict,
    destination_cluster: dict,
    destination_storage: str,
    operation: str = 'copy'
) -> bool:
    """Migrate/copy a template from one cluster to another.
    
    Workflow:
    1. Get source template config and disk info
    2. Find destination node with specified storage
    3. Export source disk to QCOW2
    4. Copy QCOW2 to destination via SCP
    5. Create VM shell on destination with same specs
    6. Attach copied disk
    7. Convert to template
    8. If operation='move', delete source template
    
    Args:
        source_template: Source Template database object
        source_cluster: Source cluster config dict (with id, name, host, user, password)
        destination_cluster: Destination cluster config dict
        destination_storage: Storage name on destination (default: "TRUENAS")
        operation: 'copy' (keep source) or 'move' (delete source after success)
        
    Returns:
        True if successful, False otherwise
    """
    logger.info(f"Starting template migration: {source_template.name} (VMID {source_template.proxmox_vmid}) "
                f"from {source_cluster['name']} to {destination_cluster['name']}")
    
    try:
        source_proxmox = get_proxmox_admin_for_cluster(source_cluster["id"])
        dest_proxmox = get_proxmox_admin_for_cluster(destination_cluster["id"])
        
        source_node = source_template.node
        source_vmid = source_template.proxmox_vmid
        # Note: source_name not used in current implementation
        
        # Step 1: Get template config from source
        logger.info(f"Step 1: Getting template config from {source_node}/{source_vmid}")
        source_config = source_proxmox.nodes(source_node).qemu(source_vmid).config.get()
        
        # Note: CPU/memory specs extracted for reference but not used in current implementation
        # They are preserved in the VM shell creation from hardware config
        
        # Get network config (not currently used in migration)
        # Get primary disk (scsi0)
        scsi0 = source_config.get("scsi0", "")
        if not scsi0:
            return {"ok": False, "error": "Template has no scsi0 disk"}
        
        # Parse disk config: "STORAGE:DISK_ID,size=10G"
        disk_parts = scsi0.split(",")
        disk_location = disk_parts[0]
        disk_size_str = next((p.split("=")[1] for p in disk_parts if p.startswith("size=")), "10G")
        
        if ":" not in disk_location:
            return {"ok": False, "error": f"Invalid disk location: {disk_location}"}
        
        source_storage, disk_id = disk_location.split(":", 1)
        logger.info(f"Source disk: {source_storage}:{disk_id}, size: {disk_size_str}")
        
        # Step 2: Find destination node with TRUENAS storage
        logger.info("Step 2: Finding destination node with TRUENAS storage...")
        dest_nodes = dest_proxmox.nodes.get()
        dest_node = None
        
        for node in dest_nodes:
            node_name = node["node"]
            try:
                storages = dest_proxmox.nodes(node_name).storage.get()
                for storage in storages:
                    if storage["storage"] == destination_storage and "images" in storage.get("content", ""):
                        dest_node = node_name
                        break
                if dest_node:
                    break
            except Exception as e:
                logger.warning(f"Failed to check storage on {node_name}: {e}")
        
        if not dest_node:
            return {"ok": False, "error": f"No node with {destination_storage} storage found"}
        
        logger.info(f"Selected destination node: {dest_node}")
        
        # Get next available VMID
        dest_vmids = [vm["vmid"] for vm in dest_proxmox.cluster.resources.get(type="vm")]
        dest_vmid = max(dest_vmids) + 1 if dest_vmids else 100
        logger.info(f"Assigned VMID: {dest_vmid}")
        
        # Get SSH connections
        from app.services.ssh_executor import get_ssh_executor_from_config
        
        source_ssh = get_ssh_executor_from_config(source_cluster)
        source_ssh.connect(source_node)
        
        dest_ssh = get_ssh_executor_from_config(destination_cluster)
        dest_ssh.connect(dest_node)
        
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
        
        vm_name = source_template.name
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
            # Note: disk_identifier not used in current implementation
            
            # Build source disk path
            source_path = f"/dev/zvol/{source_storage}/vm-{source_vmid}-{disk_key}-0"
            
            # Check if it's a raw disk or qcow2
            check_cmd = f"file {source_path} || qemu-img info {source_path}"
            exit_code, stdout, stderr = source_ssh.execute(check_cmd, timeout=30, check=False)
            
            is_qcow2 = 'QCOW2' in stdout or 'qcow2' in stdout.lower()
            disk_format = 'qcow2' if is_qcow2 else 'raw'
            
            logger.info(f"Source disk format: {disk_format}")
            
            # Note: dest_path constructed but not directly used (passed inline to commands)
            
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
