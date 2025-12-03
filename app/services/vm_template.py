#!/usr/bin/env python3
"""
VM Template Management - Template and overlay generation logic.

This module provides template-related operations:
- Export template disks to QCOW2 base images
- Create student VMs as overlays from base images
- Full clone operations via SSH
- Template conversion and management

These are template-focused operations used by class_vm_service.py
and other high-level workflows.

Note: This module does NOT touch VMInventory/background sync - those remain
in inventory_service.py and background_sync.py.
"""

import logging
import os
import re
from typing import List, Optional, Tuple

from app.services.ssh_executor import SSHExecutor
from app.services.vm_core import (
    create_vm_shell,
    destroy_vm,
    attach_disk_to_vm,
    create_overlay_disk,
    convert_disk_to_qcow2,
    get_vm_config_ssh,
    get_vm_disk_path,
    stop_vm,
    wait_for_vm_stopped,
    PROXMOX_STORAGE_NAME,
    DEFAULT_TEMPLATE_STORAGE_PATH,
    DEFAULT_VM_IMAGES_PATH,
)
from app.services.vm_utils import (
    sanitize_vm_name,
    get_next_available_vmid_ssh,
    get_vm_mac_address_ssh,
    parse_disk_config,
    build_vm_name,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Template Export Operations
# ---------------------------------------------------------------------------

def export_template_to_qcow2(
    ssh_executor: SSHExecutor,
    template_vmid: int,
    node: str,
    output_path: str,
    disk: str = "scsi0",
    timeout: int = 600,
) -> Tuple[bool, str]:
    """
    Export a Proxmox template's disk to a standalone QCOW2 file.
    
    This creates a base QCOW2 file that can be used as a backing file
    for overlay disks. The output file is a complete, independent copy.
    
    Args:
        ssh_executor: SSH executor for running commands
        template_vmid: VMID of the source template
        node: Proxmox node name where template resides
        output_path: Full path for the output QCOW2 file
        disk: Disk to export (default: scsi0)
        timeout: Conversion timeout in seconds (default: 600)
        
    Returns:
        Tuple of (success, error_message)
    """
    logger.info(f"Exporting template {template_vmid} disk {disk} to {output_path}")
    
    try:
        # Get the disk path from the template config
        config = get_vm_config_ssh(ssh_executor, template_vmid)
        if not config:
            return False, f"Failed to get template {template_vmid} config"
        
        disk_config = config.get(disk)
        if not disk_config:
            return False, f"Disk {disk} not found in template {template_vmid}"
        
        # Parse disk configuration
        disk_info = parse_disk_config(disk_config)
        storage = disk_info.get('storage')
        volume = disk_info.get('volume')
        
        if not storage or not volume:
            return False, f"Could not parse disk config: {disk_config}"
        
        logger.info(f"Found disk on storage {storage}, volume {volume}")
        
        # Resolve source path
        # For NFS storage: /mnt/pve/{storage}/images/{vmid}/{volume}
        if '/' in volume:
            # Volume includes subdirectory (e.g., "100/vm-100-disk-0.qcow2")
            source_path = f"/mnt/pve/{storage}/images/{volume}"
        else:
            # Infer vmid directory
            source_path = f"/mnt/pve/{storage}/images/{template_vmid}/{volume}"
        
        logger.info(f"Converting {source_path} to {output_path}")
        
        # Use vm_core's convert function
        success, error = convert_disk_to_qcow2(
            ssh_executor=ssh_executor,
            source_path=source_path,
            output_path=output_path,
            timeout=timeout,
        )
        
        if success:
            logger.info(f"Successfully exported template to {output_path}")
        
        return success, error
        
    except Exception as e:
        logger.exception(f"Error exporting template: {e}")
        return False, str(e)


# ---------------------------------------------------------------------------
# Overlay VM Creation
# ---------------------------------------------------------------------------

def create_overlay_vm(
    ssh_executor: SSHExecutor,
    vmid: int,
    name: str,
    base_qcow2_path: str,
    node: str,
    memory: int = 2048,
    cores: int = 2,
    storage: str = None,
) -> Tuple[bool, str, Optional[str]]:
    """
    Create a VM using a QCOW2 overlay (copy-on-write).
    
    Creates an empty VM shell, generates an overlay disk backed by the
    base image, and attaches it to the VM.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID to create
        name: VM name
        base_qcow2_path: Path to backing QCOW2 file
        node: Proxmox node name
        memory: Memory in MB (default: 2048)
        cores: CPU cores (default: 2)
        storage: Storage name (default: PROXMOX_STORAGE_NAME)
        
    Returns:
        Tuple of (success, error_message, mac_address)
    """
    storage = storage or PROXMOX_STORAGE_NAME
    images_path = DEFAULT_VM_IMAGES_PATH
    
    try:
        # Step 1: Create VM shell
        success, error = create_vm_shell(
            ssh_executor=ssh_executor,
            vmid=vmid,
            name=name,
            memory=memory,
            cores=cores,
        )
        
        if not success:
            return False, f"Failed to create VM shell: {error}", None
        
        # Step 2: Create overlay disk
        overlay_dir = f"{images_path}/{vmid}"
        overlay_path = f"{overlay_dir}/vm-{vmid}-disk-0.qcow2"
        
        success, error = create_overlay_disk(
            ssh_executor=ssh_executor,
            output_path=overlay_path,
            backing_file=base_qcow2_path,
        )
        
        if not success:
            # Cleanup VM shell on failure
            destroy_vm(ssh_executor, vmid, purge=True)
            return False, f"Failed to create overlay disk: {error}", None
        
        # Step 3: Attach disk to VM
        disk_spec = f"{vmid}/vm-{vmid}-disk-0.qcow2"
        success, error = attach_disk_to_vm(
            ssh_executor=ssh_executor,
            vmid=vmid,
            disk_path=disk_spec,
            storage=storage,
            set_boot=True,
        )
        
        if not success:
            # Cleanup on failure
            destroy_vm(ssh_executor, vmid, purge=True)
            return False, f"Failed to attach disk: {error}", None
        
        # Get MAC address
        mac = get_vm_mac_address_ssh(ssh_executor, vmid)
        
        logger.info(f"Created overlay VM {vmid} ({name}) backed by {base_qcow2_path}")
        return True, "", mac
        
    except Exception as e:
        logger.exception(f"Error creating overlay VM: {e}")
        # Attempt cleanup
        try:
            destroy_vm(ssh_executor, vmid, purge=True)
        except Exception:
            pass
        return False, str(e), None


def create_student_overlays(
    ssh_executor: SSHExecutor,
    base_qcow2_path: str,
    class_prefix: str,
    count: int,
    node: str,
    start_vmid: int = None,
    memory: int = 2048,
    cores: int = 2,
) -> Tuple[List[dict], int, int]:
    """
    Create multiple student VMs as overlays from a base QCOW2.
    
    Args:
        ssh_executor: SSH executor for running commands
        base_qcow2_path: Path to the base QCOW2 file
        class_prefix: Prefix for VM names
        count: Number of student VMs to create
        node: Proxmox node name
        start_vmid: Starting VMID (default: auto-detect)
        memory: Memory per VM in MB (default: 2048)
        cores: CPU cores per VM (default: 2)
        
    Returns:
        Tuple of (created_vms, successful_count, failed_count)
        where created_vms is a list of dicts with vmid, name, mac
    """
    created_vms = []
    successful = 0
    failed = 0
    
    # Get starting VMID if not specified
    if start_vmid is None:
        start_vmid = get_next_available_vmid_ssh(ssh_executor)
    
    current_vmid = start_vmid
    
    for i in range(count):
        student_name = build_vm_name(class_prefix, "student", i + 1)
        
        # Find next available VMID
        while True:
            exit_code, _, _ = ssh_executor.execute(
                f"qm status {current_vmid}",
                check=False,
                timeout=10
            )
            if exit_code != 0:
                break
            current_vmid += 1
        
        vmid = current_vmid
        current_vmid += 1
        
        success, error, mac = create_overlay_vm(
            ssh_executor=ssh_executor,
            vmid=vmid,
            name=student_name,
            base_qcow2_path=base_qcow2_path,
            node=node,
            memory=memory,
            cores=cores,
        )
        
        if success:
            created_vms.append({
                'vmid': vmid,
                'name': student_name,
                'mac': mac,
                'node': node,
            })
            successful += 1
            logger.info(f"Created student VM {vmid} ({student_name})")
        else:
            failed += 1
            logger.error(f"Failed to create student VM: {error}")
    
    return created_vms, successful, failed


# ---------------------------------------------------------------------------
# Full Clone Operations (via SSH qm clone)
# ---------------------------------------------------------------------------

def full_clone_vm(
    ssh_executor: SSHExecutor,
    source_vmid: int,
    target_vmid: int,
    name: str,
    timeout: int = 300,
) -> Tuple[bool, str, Optional[str]]:
    """
    Create a full clone of a VM via SSH.
    
    Uses `qm clone --full` which creates an independent copy of the source VM.
    
    Args:
        ssh_executor: SSH executor for running commands
        source_vmid: Source VM ID to clone
        target_vmid: Target VM ID for the clone
        name: Name for the cloned VM
        timeout: Clone timeout in seconds (default: 300)
        
    Returns:
        Tuple of (success, error_message, mac_address)
    """
    safe_name = sanitize_vm_name(name)
    
    cmd = f"qm clone {source_vmid} {target_vmid} --name {safe_name} --full"
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(
            cmd,
            timeout=timeout,
            check=False
        )
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error(f"Failed to clone VM {source_vmid} -> {target_vmid}: {error_msg}")
            return False, error_msg, None
        
        # Get MAC address of cloned VM
        mac = get_vm_mac_address_ssh(ssh_executor, target_vmid)
        
        logger.info(f"Cloned VM {source_vmid} -> {target_vmid} ({safe_name})")
        return True, "", mac
        
    except Exception as e:
        logger.exception(f"Error cloning VM: {e}")
        return False, str(e), None


def linked_clone_vm(
    ssh_executor: SSHExecutor,
    source_vmid: int,
    target_vmid: int,
    name: str,
    timeout: int = 120,
) -> Tuple[bool, str, Optional[str]]:
    """
    Create a linked clone of a VM via SSH.
    
    Uses `qm clone` without --full, creating a copy-on-write clone.
    Note: Linked clones must be on the same storage as the source.
    
    Args:
        ssh_executor: SSH executor for running commands
        source_vmid: Source VM ID to clone (must be a template)
        target_vmid: Target VM ID for the clone
        name: Name for the cloned VM
        timeout: Clone timeout in seconds (default: 120)
        
    Returns:
        Tuple of (success, error_message, mac_address)
    """
    safe_name = sanitize_vm_name(name)
    
    cmd = f"qm clone {source_vmid} {target_vmid} --name {safe_name}"
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(
            cmd,
            timeout=timeout,
            check=False
        )
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error(f"Failed to linked clone VM {source_vmid} -> {target_vmid}: {error_msg}")
            return False, error_msg, None
        
        # Get MAC address of cloned VM
        mac = get_vm_mac_address_ssh(ssh_executor, target_vmid)
        
        logger.info(f"Linked clone VM {source_vmid} -> {target_vmid} ({safe_name})")
        return True, "", mac
        
    except Exception as e:
        logger.exception(f"Error creating linked clone: {e}")
        return False, str(e), None


# ---------------------------------------------------------------------------
# Template Conversion
# ---------------------------------------------------------------------------

def convert_to_template(
    ssh_executor: SSHExecutor,
    vmid: int,
) -> Tuple[bool, str]:
    """
    Convert a VM to a template via SSH.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID to convert
        
    Returns:
        Tuple of (success, error_message)
    """
    # First ensure VM is stopped
    status_code, stdout, _ = ssh_executor.execute(
        f"qm status {vmid}",
        check=False,
        timeout=10
    )
    
    if status_code == 0 and 'running' in stdout.lower():
        logger.info(f"Stopping VM {vmid} before template conversion")
        stop_vm(ssh_executor, vmid)
        if not wait_for_vm_stopped(ssh_executor, vmid, timeout=60):
            return False, f"VM {vmid} did not stop in time"
    
    # Convert to template
    cmd = f"qm template {vmid}"
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(
            cmd,
            timeout=60,
            check=False
        )
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error(f"Failed to convert VM {vmid} to template: {error_msg}")
            return False, error_msg
        
        logger.info(f"Converted VM {vmid} to template")
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error converting to template: {e}")
        return False, str(e)


# ---------------------------------------------------------------------------
# Template Push Operations
# ---------------------------------------------------------------------------

def update_overlay_backing_file(
    ssh_executor: SSHExecutor,
    vmid: int,
    new_base_path: str,
    storage: str = None,
) -> Tuple[bool, str]:
    """
    Update a VM's overlay disk to use a new backing file.
    
    This deletes the old overlay and creates a fresh one pointing to
    the new base. All VM changes are lost.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID to update
        new_base_path: Path to the new backing QCOW2 file
        storage: Storage name (default: PROXMOX_STORAGE_NAME)
        
    Returns:
        Tuple of (success, error_message)
    """
    storage = storage or PROXMOX_STORAGE_NAME
    images_path = DEFAULT_VM_IMAGES_PATH
    
    try:
        # Ensure VM is stopped
        stop_vm(ssh_executor, vmid)
        if not wait_for_vm_stopped(ssh_executor, vmid, timeout=30):
            return False, f"VM {vmid} did not stop in time"
        
        # Delete old overlay
        overlay_path = f"{images_path}/{vmid}/vm-{vmid}-disk-0.qcow2"
        ssh_executor.execute(f"rm -f {overlay_path}", check=False)
        
        # Create new overlay with updated backing file
        success, error = create_overlay_disk(
            ssh_executor=ssh_executor,
            output_path=overlay_path,
            backing_file=new_base_path,
        )
        
        if not success:
            return False, f"Failed to recreate overlay: {error}"
        
        logger.info(f"Updated VM {vmid} overlay to use {new_base_path}")
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error updating overlay: {e}")
        return False, str(e)


def push_base_to_students(
    ssh_executor: SSHExecutor,
    new_base_path: str,
    student_vmids: List[int],
) -> Tuple[int, int]:
    """
    Push a new base image to all student VMs.
    
    Recreates overlay disks for each student VM pointing to the new base.
    Student VMs will lose their current state.
    
    Args:
        ssh_executor: SSH executor for running commands
        new_base_path: Path to the new base QCOW2 file
        student_vmids: List of student VM IDs to update
        
    Returns:
        Tuple of (successful_count, failed_count)
    """
    successful = 0
    failed = 0
    
    for vmid in student_vmids:
        success, error = update_overlay_backing_file(
            ssh_executor=ssh_executor,
            vmid=vmid,
            new_base_path=new_base_path,
        )
        
        if success:
            successful += 1
        else:
            failed += 1
            logger.error(f"Failed to update VM {vmid}: {error}")
    
    logger.info(f"Pushed base to students: {successful} successful, {failed} failed")
    return successful, failed


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

def get_template_disk_info(
    ssh_executor: SSHExecutor,
    template_vmid: int,
) -> Optional[dict]:
    """
    Get information about a template's primary disk.
    
    Args:
        ssh_executor: SSH executor for running commands
        template_vmid: Template VM ID
        
    Returns:
        Dict with storage, volume, size_gb, format, or None if not found
    """
    config = get_vm_config_ssh(ssh_executor, template_vmid)
    if not config:
        return None
    
    # Check common disk slots
    for slot in ['scsi0', 'virtio0', 'sata0', 'ide0']:
        disk_config = config.get(slot)
        if disk_config:
            info = parse_disk_config(disk_config)
            info['slot'] = slot
            return info
    
    return None


def verify_base_image_exists(
    ssh_executor: SSHExecutor,
    base_path: str,
) -> bool:
    """
    Verify a base QCOW2 image exists and is readable.
    
    Args:
        ssh_executor: SSH executor for running commands
        base_path: Path to the base image
        
    Returns:
        True if file exists and is readable
    """
    exit_code, _, _ = ssh_executor.execute(
        f"test -r {base_path}",
        check=False,
        timeout=10
    )
    return exit_code == 0


def cleanup_base_image(
    ssh_executor: SSHExecutor,
    base_path: str,
) -> Tuple[bool, str]:
    """
    Remove a base QCOW2 image.
    
    Args:
        ssh_executor: SSH executor for running commands
        base_path: Path to the base image
        
    Returns:
        Tuple of (success, error_message)
    """
    try:
        exit_code, stdout, stderr = ssh_executor.execute(
            f"rm -f {base_path}",
            check=False,
            timeout=30
        )
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            return False, error_msg
        
        logger.info(f"Removed base image: {base_path}")
        return True, ""
        
    except Exception as e:
        return False, str(e)
