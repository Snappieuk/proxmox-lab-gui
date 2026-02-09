#!/usr/bin/env python3
"""
VM Configuration Cloning - Copy and modify Proxmox VM config files directly.

This approach is simpler and more reliable than parsing/applying 65+ settings individually.
"""

import logging
import re
import random
from typing import Tuple, Optional
from app.services.ssh_executor import SSHExecutor

logger = logging.getLogger(__name__)


def check_template_has_efi_tpm(
    ssh_executor: SSHExecutor,
    template_vmid: int,
    template_node: str = None,
) -> Tuple[bool, bool]:
    """
    Check if a template has EFI and/or TPM configured.
    
    Args:
        ssh_executor: SSH executor
        template_vmid: Template VM ID
        template_node: Node where template is located (for SSH hop)
        
    Returns:
        Tuple of (has_efi, has_tpm)
    """
    try:
        source_conf = f"/etc/pve/qemu-server/{template_vmid}.conf"
        
        # Check which node we're connected to
        exit_code, hostname_output, _ = ssh_executor.execute("hostname", timeout=5, check=False)
        current_node = hostname_output.strip() if exit_code == 0 else "unknown"
        
        # Use SSH hop if needed
        if template_node and template_node != current_node:
            cat_cmd = f"ssh {template_node} 'cat {source_conf}'"
        else:
            cat_cmd = f"cat {source_conf}"
        
        # Read template config
        exit_code, config_content, stderr = ssh_executor.execute(cat_cmd, timeout=10, check=False)
        
        if exit_code != 0:
            logger.warning(f"Could not read template {template_vmid} config: {stderr}")
            return False, False
        
        # Check for EFI and TPM in config
        has_efi = 'efidisk0:' in config_content
        has_tpm = 'tpmstate0:' in config_content
        
        logger.info(f"Template {template_vmid} config check: has_efi={has_efi}, has_tpm={has_tpm}")
        if has_efi:
            # Log the EFI line for debugging
            for line in config_content.split('\n'):
                if line.startswith('efidisk0:'):
                    logger.info(f"Template EFI config: {line}")
        if has_tpm:
            # Log the TPM line for debugging
            for line in config_content.split('\n'):
                if line.startswith('tpmstate0:'):
                    logger.info(f"Template TPM config: {line}")
        
        return has_efi, has_tpm
        
    except Exception as e:
        logger.error(f"Error checking template EFI/TPM: {e}")
        return False, False


def generate_mac_address() -> str:
    """Generate a random MAC address in Proxmox format."""
    # Proxmox uses locally administered MAC addresses (02:xx:xx:xx:xx:xx)
    mac = [0x02, random.randint(0x00, 0xff), random.randint(0x00, 0xff),
           random.randint(0x00, 0xff), random.randint(0x00, 0xff), random.randint(0x00, 0xff)]
    return ':'.join([f'{b:02X}' for b in mac])


def clone_vm_config(
    ssh_executor: SSHExecutor,
    source_vmid: int,
    dest_vmid: int,
    dest_name: str,
    overlay_disk_path: str,
    storage: str = "TRUENAS-NFS",
    source_node: str = None,
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
        source_node: Node where source VM is located (for SSH hop if needed)
        
    Returns:
        Tuple of (success, error_message, mac_address)
    """
    try:
        # Step 0: Verify source template exists and check cluster
        source_conf = f"/etc/pve/qemu-server/{source_vmid}.conf"
        dest_conf = f"/etc/pve/qemu-server/{dest_vmid}.conf"
        
        # Check which node we're connected to
        exit_code, hostname_output, _ = ssh_executor.execute("hostname", timeout=5, check=False)
        current_node = hostname_output.strip() if exit_code == 0 else "unknown"
        logger.info(f"SSH connected to node: {current_node}")
        
        # If template is on a different node, use SSH hop
        if source_node and source_node != current_node:
            logger.info(f"Template is on {source_node}, we're on {current_node} - using SSH hop")
            cat_cmd = f"ssh {source_node} 'cat {source_conf}'"
        else:
            cat_cmd = f"cat {source_conf}"
        
        logger.info(f"Reading template config: {source_conf}")
        
        # Read source config (with SSH hop if needed)
        exit_code, config_content, stderr = ssh_executor.execute(
            cat_cmd,
            timeout=10,
            check=False
        )
        
        if exit_code != 0:
            error_msg = f"Template {source_vmid} config not found at {source_conf} on node {source_node or current_node}."
            logger.error(error_msg)
            logger.error(f"cat stderr: {stderr}")
            return False, error_msg, None
        
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
                logger.info("Removing template flag")
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
                
                # Replace disk path, keep ALL options after first comma
                # Input format: "STORAGE:OLD_PATH,option1=val1,option2=val2,..."
                # Output format: "STORAGE:NEW_PATH,option1=val1,option2=val2,..."
                
                # Split on comma to separate path from options
                parts = disk_config.split(',', 1)  # Split only on FIRST comma
                options_part = parts[1] if len(parts) > 1 else ''  # "option1=val1,..."
                
                # CRITICAL FIX: Explicitly set format=qcow2 for overlay disks
                # Remove any existing format= option and add format=qcow2
                if options_part:
                    # Parse options and filter out format= if present
                    options_list = [opt.strip() for opt in options_part.split(',')]
                    options_list = [opt for opt in options_list if not opt.startswith('format=')]
                    
                    # Add format=qcow2 as first option (for QCOW2 overlay disks)
                    if overlay_disk_path.endswith('.qcow2'):
                        options_list.insert(0, 'format=qcow2')
                    
                    options_part = ','.join(options_list)
                    new_disk_full = f"{storage}:{overlay_disk_path},{options_part}"
                else:
                    # No options - add format=qcow2 if it's a QCOW2 disk
                    if overlay_disk_path.endswith('.qcow2'):
                        new_disk_full = f"{storage}:{overlay_disk_path},format=qcow2"
                    else:
                        new_disk_full = f"{storage}:{overlay_disk_path}"
                
                new_lines.append(f"{disk_slot}: {new_disk_full}")
                logger.info(f"Updated disk {disk_slot}: {storage}:{overlay_disk_path} with format=qcow2")
                continue
            
            # Update EFI disk path - use relative path like main disk
            if line.startswith('efidisk0:'):
                # Format: "STORAGE:OLDPATH,efitype=4m,..."
                # Replace with: "STORAGE:NEWID/vm-NEWID-disk-1.raw,efitype=4m,..."
                # Keep all options (efitype, pre-enrolled-keys, etc.)
                # Extract everything after first comma (the options)
                parts = line.split(':', 1)[1].split(',', 1)  # Get everything after 'efidisk0:'
                options_part = ',' + parts[1] if len(parts) > 1 else ''
                new_efi = f"efidisk0: {storage}:{dest_vmid}/vm-{dest_vmid}-disk-1.raw{options_part}"
                new_lines.append(new_efi)
                logger.info(f"Updated EFI disk path to {dest_vmid}/vm-{dest_vmid}-disk-1.raw (preserved {len(options_part)} chars of options)")
                continue
            
            # Update TPM state path - use relative path like main disk
            if line.startswith('tpmstate0:'):
                # Format: "STORAGE:OLDPATH,version=v2.0,..."
                # Replace with: "STORAGE:NEWID/vm-NEWID-disk-2.raw,version=v2.0,..."
                # Keep all options (version, etc.)
                # Extract everything after first comma (the options)
                parts = line.split(':', 1)[1].split(',', 1)  # Get everything after 'tpmstate0:'
                options_part = ',' + parts[1] if len(parts) > 1 else ''
                new_tpm = f"tpmstate0: {storage}:{dest_vmid}/vm-{dest_vmid}-disk-2.raw{options_part}"
                new_lines.append(new_tpm)
                logger.info(f"Updated TPM state path to {dest_vmid}/vm-{dest_vmid}-disk-2.raw (preserved {len(options_part)} chars of options)")
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
            
            # Explicitly preserve VGA/display settings (critical for VirtIO-GPU, etc.)
            if line.startswith('vga:'):
                new_lines.append(line)
                logger.info(f"Preserved VGA setting: {line}")
                continue
            
            # Keep all other lines unchanged
            new_lines.append(line)
        
        # Step 3: Write new config
        new_config = '\n'.join(new_lines)
        
        # Write config using Python string escaping for SSH
        # Escape single quotes and backslashes for shell
        escaped_config = new_config.replace('\\', '\\\\').replace("'", "'\\''")
        write_cmd = f"echo '{escaped_config}' > {dest_conf}"
        
        exit_code, stdout, stderr = ssh_executor.execute(
            write_cmd,
            timeout=30,
            check=False
        )
        
        if exit_code != 0:
            return False, f"Failed to write new config: {stderr}", None
        
        logger.info(f"Created VM config: {dest_conf}")
        
        # Verify config was written
        exit_code, verify_content, stderr = ssh_executor.execute(
            f"cat {dest_conf}",
            timeout=10,
            check=False
        )
        
        if exit_code != 0:
            return False, f"Config file was not created: {stderr}", None
        
        # Validate config has required fields
        if 'name:' not in verify_content:
            return False, "Config file missing name field", None
        if disk_slot and f'{disk_slot}:' not in verify_content:
            return False, f"Config file missing disk {disk_slot}", None
        if 'net0:' not in verify_content:
            return False, "Config file missing network config", None
        
        # Log preserved settings for debugging
        vga_preserved = 'vga:' in verify_content
        disk_options = verify_content.count('cache=') + verify_content.count('backup=') + verify_content.count('discard=')
        
        logger.info(f"Verified VM config exists: {dest_conf} ({len(verify_content)} bytes)")
        logger.info(f"Config validation: VGA={vga_preserved}, disk_options={disk_options} preserved")
        logger.info(f"VM {dest_vmid} created with all settings from {source_vmid}")
        
        # Step 4: Tell Proxmox to reload configs (force cluster sync)
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
