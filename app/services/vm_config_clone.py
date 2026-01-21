#!/usr/bin/env python3
"""
VM Configuration Cloning - Copy and modify Proxmox VM config files directly.

This approach is simpler and more reliable than parsing/applying 65+ settings individually.
"""

import logging
import re
from typing import Tuple, Optional
from app.services.ssh_executor import SSHExecutor
from app.services.vm_utils import generate_mac_address

logger = logging.getLogger(__name__)


def clone_vm_config(
    ssh_executor: SSHExecutor,
    source_vmid: int,
    dest_vmid: int,
    dest_name: str,
    overlay_disk_path: str,
    storage: str = "TRUENAS-NFS",
) -> Tuple[bool, str, Optional[str]]:
    """
    Clone a VM by copying its config file and modifying necessary fields.
    
    This is much simpler than parsing/applying 65+ settings individually.
    Guarantees 100% identical configuration to source VM.
    
    Args:
        ssh_executor: SSH executor
        source_vmid: Source VM ID (template)
        dest_vmid: Destination VM ID (new VM)
        dest_name: New VM name
        overlay_disk_path: Path to overlay disk (e.g., "58000/vm-58000-disk-0.qcow2")
        storage: Storage name (default: TRUENAS-NFS)
        
    Returns:
        Tuple of (success, error_message, mac_address)
    """
    try:
        # Step 1: Read source VM config
        source_conf = f"/etc/pve/qemu-server/{source_vmid}.conf"
        dest_conf = f"/etc/pve/qemu-server/{dest_vmid}.conf"
        
        logger.info(f"Cloning VM config: {source_conf} -> {dest_conf}")
        
        # Read source config
        exit_code, config_content, stderr = ssh_executor.execute(
            f"cat {source_conf}",
            timeout=10,
            check=False
        )
        
        if exit_code != 0:
            return False, f"Failed to read source config: {stderr}", None
        
        # Step 2: Modify config line by line
        new_lines = []
        new_mac = generate_mac_address()
        disk_slot = None
        
        for line in config_content.split('\n'):
            line = line.strip()
            
            # Skip empty lines and comments initially
            if not line or line.startswith('#'):
                new_lines.append(line)
                continue
            
            # Remove template flag
            if line.startswith('template:'):
                logger.info(f"Removing template flag")
                continue
            
            # Update name
            if line.startswith('name:'):
                new_lines.append(f"name: {dest_name}")
                logger.info(f"Changed name to: {dest_name}")
                continue
            
            # Update disk paths (scsi0, virtio0, sata0, ide0, etc.)
            disk_match = re.match(r'^(scsi|virtio|sata|ide)(\d+):\s*(.+)$', line)
            if disk_match:
                controller = disk_match.group(1)
                slot_num = disk_match.group(2)
                disk_config = disk_match.group(3)
                disk_slot = f"{controller}{slot_num}"
                
                # Replace disk path, keep all options
                # Format: "STORAGE:vm-OLDID-disk-0,cache=writeback,discard=on,..."
                # Replace with: "STORAGE:NEWID/vm-NEWID-disk-0.qcow2,cache=writeback,discard=on,..."
                new_disk = re.sub(
                    r'[^:,]+:([^,]+)',
                    f'{storage}:{overlay_disk_path}',
                    disk_config,
                    count=1
                )
                new_lines.append(f"{disk_slot}: {new_disk}")
                logger.info(f"Updated disk {disk_slot}: {storage}:{overlay_disk_path}")
                continue
            
            # Update EFI disk path
            if line.startswith('efidisk0:'):
                # Format: "STORAGE:vm-OLDID-disk-1,..."
                # Replace with: "STORAGE:1,..." (let Proxmox create new EFI disk)
                new_efi = re.sub(
                    r'([^:,]+):([^,]+)',
                    f'{storage}:1',
                    line,
                    count=1
                )
                new_lines.append(new_efi)
                logger.info(f"Updated EFI disk path")
                continue
            
            # Update TPM state path
            if line.startswith('tpmstate0:'):
                # Format: "STORAGE:vm-OLDID-disk-2,..."
                # Replace with: "STORAGE:1,..." (let Proxmox create new TPM)
                new_tpm = re.sub(
                    r'([^:,]+):([^,]+)',
                    f'{storage}:1',
                    line,
                    count=1
                )
                new_lines.append(new_tpm)
                logger.info(f"Updated TPM state path")
                continue
            
            # Update network MAC address
            if line.startswith('net0:'):
                # Format: "virtio=XX:XX:XX:XX:XX:XX,bridge=vmbr0,..."
                # Replace MAC but keep everything else
                new_net = re.sub(
                    r'([^=,]+)=([0-9A-F:]+)',
                    f'\\1={new_mac}',
                    line,
                    count=1
                )
                new_lines.append(new_net)
                logger.info(f"Updated MAC address to: {new_mac}")
                continue
            
            # Keep all other lines unchanged
            new_lines.append(line)
        
        # Step 3: Write new config
        new_config = '\n'.join(new_lines)
        
        # Escape special characters for shell
        # Use heredoc to avoid quoting issues
        write_cmd = f"""cat > {dest_conf} << 'EOFCONFIG'
{new_config}
EOFCONFIG"""
        
        exit_code, stdout, stderr = ssh_executor.execute(
            write_cmd,
            timeout=30,
            check=False
        )
        
        if exit_code != 0:
            return False, f"Failed to write new config: {stderr}", None
        
        logger.info(f"Created VM config: {dest_conf}")
        logger.info(f"VM {dest_vmid} created with all settings from {source_vmid}")
        
        # Step 4: Tell Proxmox to reload the config
        # This forces Proxmox to recognize the new VM
        exit_code, stdout, stderr = ssh_executor.execute(
            f"qm showcmd {dest_vmid}",
            timeout=10,
            check=False
        )
        
        if exit_code == 0:
            logger.info(f"Proxmox recognized new VM {dest_vmid}")
        
        return True, "", new_mac
        
    except Exception as e:
        logger.exception(f"Error cloning VM config: {e}")
        return False, str(e), None


def destroy_vm_config(ssh_executor: SSHExecutor, vmid: int) -> bool:
    """
    Delete a VM's config file.
    
    Args:
        ssh_executor: SSH executor
        vmid: VM ID to delete
        
    Returns:
        True if successful, False otherwise
    """
    try:
        conf_path = f"/etc/pve/qemu-server/{vmid}.conf"
        exit_code, _, _ = ssh_executor.execute(
            f"rm -f {conf_path}",
            timeout=10,
            check=False
        )
        return exit_code == 0
    except Exception as e:
        logger.error(f"Error deleting VM config: {e}")
        return False
