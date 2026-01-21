#!/usr/bin/env python3
"""
VM Core Operations - Low-level VM and disk operations.

This module provides core operations for VM management:
- Create/destroy VMs (shells, with disks)
- Stop/start VMs and wait for state changes
- Attach/detach disks
- Snapshot operations
- Direct qm/qemu-img commands via SSH

These are low-level operations used by higher-level modules like
class_vm_service.py and vm_template.py.

Note: This module does NOT touch VMInventory/background sync - those remain
in inventory_service.py and background_sync.py.
"""

import logging
import os
import time
from typing import Dict, Optional, Tuple

from app.services.ssh_executor import SSHExecutor
from app.services.vm_utils import (
    sanitize_vm_name,
)

logger = logging.getLogger(__name__)

# Storage name for Proxmox (configurable via environment)
PROXMOX_STORAGE_NAME = os.getenv("PROXMOX_STORAGE_NAME", "TRUENAS-NFS")

# Default storage paths for QCOW2 files
DEFAULT_TEMPLATE_STORAGE_PATH = os.getenv("QCOW2_TEMPLATE_PATH", "/mnt/pve/TRUENAS-NFS/images")
DEFAULT_VM_IMAGES_PATH = os.getenv("QCOW2_IMAGES_PATH", "/mnt/pve/TRUENAS-NFS/images")

# VM stop timeout settings
VM_STOP_TIMEOUT = int(os.getenv("VM_STOP_TIMEOUT", "60"))
VM_STOP_POLL_INTERVAL = int(os.getenv("VM_STOP_POLL_INTERVAL", "3"))


def get_vm_node(ssh_executor: SSHExecutor, vmid: int, proxmox=None) -> Optional[str]:
    """Find which node a VM is currently on.
    
    Uses Proxmox API first, falls back to SSH commands if needed.
    
    Args:
        ssh_executor: SSH executor (can be connected to any cluster node)
        vmid: VM ID to locate
        proxmox: Proxmox API connection (optional, will fetch if not provided)
        
    Returns:
        Node name where VM is located, or None if not found
    """
    try:
        # Try Proxmox API first (most reliable)
        if proxmox is None:
            try:
                from app.services.proxmox_service import get_proxmox_admin
                proxmox = get_proxmox_admin()
            except:
                pass
        
        if proxmox:
            try:
                resources = proxmox.cluster.resources.get(type='vm')
                for resource in resources:
                    if resource.get('vmid') == vmid:
                        node = resource.get('node')
                        logger.debug(f"VM {vmid} is on node: {node} (via API)")
                        return node
            except Exception as e:
                logger.debug(f"Could not use API to locate VM: {e}")
        
        # Fallback: try qm status on current node
        cmd = f"qm status {vmid} 2>/dev/null && hostname"
        exit_code, stdout, stderr = ssh_executor.execute(cmd, timeout=5, check=False)
        if exit_code == 0 and stdout.strip():
            # Extract hostname from output (last line)
            lines = [l.strip() for l in stdout.strip().split('\n') if l.strip()]
            node = lines[-1]
            logger.debug(f"VM {vmid} found on current node: {node}")
            return node
            
    except Exception as e:
        logger.warning(f"Failed to locate VM {vmid}: {e}")
    
    return None


# ---------------------------------------------------------------------------
# VM Shell Creation (empty VM without disk)
# ---------------------------------------------------------------------------

def create_vm_shell(
    ssh_executor: SSHExecutor,
    vmid: int,
    name: str,
    memory: int = 2048,
    cores: int = 2,
    network_bridge: str = "vmbr0",
    ostype: str = "l26",
    bios: str = None,
    machine: str = None,
    cpu: str = None,
    scsihw: str = None,
    storage: str = "local-zfs",
    net_model: str = "virtio",
    net_options: str = '',
    boot_order: str = None,
    other_settings: dict = None,
) -> Tuple[bool, str]:
    """
    Create an empty VM shell (no disk attached).
    
    The VM is created with network interface but no storage.
    Disks can be attached later via attach_disk_to_vm().
    For UEFI VMs (ovmf bios), an EFI disk is automatically added.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID to create
        name: VM name
        memory: Memory in MB (default: 2048)
        cores: CPU cores (default: 2)
        network_bridge: Network bridge (default: vmbr0)
        ostype: OS type (e.g., 'win10', 'win11', 'l26') - default: l26 (Linux)
        bios: BIOS type ('seabios' or 'ovmf' for UEFI) - default: auto-detect from ostype
        machine: Machine type (e.g., 'pc-q35-8.1') - default: auto-detect from ostype
        cpu: CPU type (e.g., 'host', 'kvm64') - default: auto-detect from ostype
        scsihw: SCSI hardware controller (e.g., 'virtio-scsi-pci', 'lsi') - default: auto-detect
        storage: Storage pool for EFI disk (default: local-zfs)
        net_model: Network interface model (virtio, e1000, rtl8139, etc.) - default: virtio
        net_options: Additional network options string (firewall=1,rate=100, etc.)
        boot_order: Boot order (e.g., 'order=scsi0;ide2;net0') - CRITICAL for Windows boot
        other_settings: Dict of additional VM settings to apply (agent, onboot, protection, etc.)
        
    Returns:
        Tuple of (success, error_message)
    """
    safe_name = sanitize_vm_name(name)
    other_settings = other_settings or {}
    
    # Extract boot_order from other_settings if not provided as parameter
    if boot_order is None and 'boot' in other_settings:
        boot_order = other_settings.pop('boot')
        logger.debug(f"Extracted boot order from other_settings: {boot_order}")
    
    # Remove settings that are already handled as explicit parameters
    # BUT ONLY if they were actually provided as parameters (not None)
    # This prevents duplicates while preserving template values
    if bios is not None:
        other_settings.pop('bios', None)
    if cores is not None:
        other_settings.pop('cores', None)
    if cpu is not None:
        other_settings.pop('cpu', None)
    if machine is not None:
        other_settings.pop('machine', None)
    if memory is not None:
        other_settings.pop('memory', None)
    if ostype is not None:
        other_settings.pop('ostype', None)
    if scsihw is not None:
        other_settings.pop('scsihw', None)
    
    # Agent is always removed since we apply it separately later
    other_settings.pop('agent', None)
    
    logger.info(f"Creating VM {vmid} with: ostype={ostype}, scsihw={scsihw}, bios={bios}, cpu={cpu}, machine={machine}, memory={memory}, cores={cores}")
    logger.info(f"Remaining other_settings to apply: {len(other_settings)} items")
    
    # Base command
    cmd = (
        f"qm create {vmid} --name {safe_name} "
        f"--memory {memory} --cores {cores} "
        f"--net0 {net_model},bridge={network_bridge}{net_options} "
        f"--ostype {ostype}"
    )
    
    # Add hardware settings (use explicit values if provided, otherwise use defaults)
    if cpu:
        cmd += f" --cpu {cpu}"
    if machine:
        cmd += f" --machine {machine}"
    if bios:
        cmd += f" --bios {bios}"
    if scsihw:
        cmd += f" --scsihw {scsihw}"
        logger.info(f"Setting SCSI controller for VM {vmid}: {scsihw}")
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(cmd, timeout=60, check=False)
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error(f"Failed to create VM shell {vmid}: {error_msg}")
            return False, error_msg
        
        logger.info(f"Created VM shell: {vmid} ({safe_name})")
        
        # Get Proxmox API connection for post-creation config (API auto-routes to correct node)
        try:
            from app.services.proxmox_service import get_proxmox_admin
            proxmox = get_proxmox_admin()
        except Exception as e:
            logger.warning(f"Could not get Proxmox API, falling back to SSH commands: {e}")
            proxmox = None
        
        # Verify which node the VM is on (may have auto-migrated)
        vm_node = get_vm_node(ssh_executor, vmid, proxmox)
        if not vm_node:
            logger.warning(f"Could not determine node for VM {vmid}, assuming current node")
            # Try to get current node from SSH
            exit_code, stdout, _ = ssh_executor.execute("hostname", timeout=5, check=False)
            vm_node = stdout.strip() if exit_code == 0 else "localhost"
        
        logger.info(f"VM {vmid} is on node: {vm_node}")
        
        # For UEFI VMs (ovmf), add EFI disk automatically
        if bios and bios.lower() == 'ovmf':
            logger.info(f"UEFI VM detected, adding EFI disk to VM {vmid}")
            if proxmox:
                try:
                    proxmox.nodes(vm_node).qemu(vmid).config.set(efidisk0=f"{storage}:1,efitype=4m,pre-enrolled-keys=1")
                    logger.info(f"Added EFI disk to VM {vmid} via API")
                except Exception as e:
                    logger.error(f"Failed to add EFI disk via API: {e}")
            else:
                # Fallback to SSH qm command
                efi_cmd = f"qm set {vmid} --efidisk0 {storage}:1,efitype=4m,pre-enrolled-keys=1"
                exit_code, stdout, stderr = ssh_executor.execute(efi_cmd, timeout=30, check=False)
                if exit_code != 0:
                    logger.error(f"Failed to add EFI disk via SSH: {stderr}")
        
        # Enable QEMU guest agent by default
        agent_enabled = False
        if proxmox:
            try:
                # Note: agent parameter must be passed as plain value, not key=value format
                proxmox.nodes(vm_node).qemu(vmid).config.set(agent='1')
                logger.info(f"Enabled QEMU guest agent for VM {vmid} via API")
                agent_enabled = True
            except Exception as e:
                logger.warning(f"Failed to enable guest agent via API: {e}, trying SSH fallback")
        
        if not agent_enabled:
            # Fallback to SSH qm command
            agent_cmd = f"qm set {vmid} --agent enabled=1,fstrim_cloned_disks=1"
            exit_code, stdout, stderr = ssh_executor.execute(agent_cmd, timeout=30, check=False)
            if exit_code == 0:
                logger.info(f"Enabled QEMU guest agent for VM {vmid} via SSH")
                agent_enabled = True
            else:
                logger.error(f"Failed to enable QEMU guest agent for VM {vmid}: {stderr}")
                # Don't fail the VM creation, but log prominently
        
        # Verify guest agent was actually enabled by reading config
        verify_cmd = f"qm config {vmid} | grep agent"
        exit_code, stdout, stderr = ssh_executor.execute(verify_cmd, timeout=10, check=False)
        if exit_code == 0 and 'enabled=1' in stdout:
            logger.info(f"Verified QEMU guest agent is enabled for VM {vmid}")
        else:
            logger.warning(f"Could not verify guest agent for VM {vmid}, output: {stdout}")
        
        # Apply boot order if provided (CRITICAL for Windows boot - must come before other settings)
        if boot_order:
            logger.info(f"Applying boot order to VM {vmid}: {boot_order}")
            if proxmox:
                try:
                    # boot_order comes as 'order=scsi0;ide2;net0' from template
                    proxmox.nodes(vm_node).qemu(vmid).config.set(boot=boot_order)
                    logger.info(f"✓ Boot order applied to VM {vmid} via API: {boot_order}")
                except Exception as e:
                    logger.error(f"✗ Failed to apply boot order via API: {e}")
            else:
                boot_cmd = f"qm set {vmid} --boot {boot_order}"
                exit_code, stdout, stderr = ssh_executor.execute(boot_cmd, timeout=30, check=False)
                if exit_code == 0:
                    logger.info(f"✓ Boot order applied to VM {vmid}: {boot_order}")
                else:
                    logger.error(f"✗ Failed to apply boot order to VM {vmid}: {stderr}")
        else:
            logger.debug(f"No boot order specified for VM {vmid}, using Proxmox default")
        
        # Apply all other settings from template (if provided)
        if other_settings:
            logger.info(f"Applying {len(other_settings)} additional settings to VM {vmid}")
            settings_applied = 0
            settings_failed = 0
            
            # Apply all settings at once via API (more efficient and reliable)
            if proxmox:
                try:
                    proxmox.nodes(vm_node).qemu(vmid).config.set(**other_settings)
                    settings_applied = len(other_settings)
                    logger.info(f"✓ Applied {settings_applied} settings to VM {vmid} via API")
                except Exception as e:
                    logger.error(f"Failed to apply settings via API: {e}")
                    settings_failed = len(other_settings)
            else:
                # Fallback to SSH qm commands
                skip_keys = ['name', 'vmid', 'digest', 'meta', 'template']
                
                for key, value in other_settings.items():
                    if key in skip_keys or value is None:
                        continue
                    
                    # Apply setting via qm command
                    try:
                        set_cmd = f"qm set {vmid} --{key} {value}"
                        exit_code, stdout, stderr = ssh_executor.execute(set_cmd, timeout=30, check=False)
                        if exit_code == 0:
                            settings_applied += 1
                            logger.debug(f"Applied setting {key}={value} to VM {vmid}")
                        else:
                            settings_failed += 1
                            logger.warning(f"Failed to apply setting {key}={value} to VM {vmid}: {stderr}")
                    except Exception as e:
                        settings_failed += 1
                    logger.warning(f"Error applying setting {key} to VM {vmid}: {e}")
            
            logger.info(f"Applied {settings_applied} settings to VM {vmid}, {settings_failed} failed")
        
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error creating VM shell {vmid}: {e}")
        return False, str(e)


def create_vm_with_disk(
    ssh_executor: SSHExecutor,
    vmid: int,
    name: str,
    disk_size_gb: int = 32,
    storage: str = None,
    memory: int = 2048,
    cores: int = 2,
    network_bridge: str = "vmbr0",
) -> Tuple[bool, str]:
    """
    Create a VM with an empty disk attached.
    
    Used for template-less classes where VMs need blank disks.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID to create
        name: VM name
        disk_size_gb: Disk size in GB (default: 32)
        storage: Storage name (default: PROXMOX_STORAGE_NAME)
        memory: Memory in MB (default: 2048)
        cores: CPU cores (default: 2)
        network_bridge: Network bridge (default: vmbr0)
        
    Returns:
        Tuple of (success, error_message)
    """
    storage = storage or PROXMOX_STORAGE_NAME
    safe_name = sanitize_vm_name(name)
    
    cmd = (
        f"qm create {vmid} --name {safe_name} "
        f"--memory {memory} --cores {cores} "
        f"--net0 virtio,bridge={network_bridge} "
        f"--scsihw virtio-scsi-pci "
        f"--scsi0 {storage}:{disk_size_gb} "
        f"--boot order=scsi0"
    )
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(cmd, timeout=120, check=False)
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error(f"Failed to create VM {vmid} with disk: {error_msg}")
            return False, error_msg
        
        logger.info(f"Created VM with disk: {vmid} ({safe_name}), {disk_size_gb}GB on {storage}")
        
        # Enable QEMU guest agent by default
        agent_cmd = f"qm set {vmid} --agent enabled=1,fstrim_cloned_disks=1"
        exit_code, stdout, stderr = ssh_executor.execute(agent_cmd, timeout=30, check=False)
        if exit_code == 0:
            logger.info(f"Enabled QEMU guest agent for VM {vmid}")
        else:
            logger.warning(f"Failed to enable QEMU guest agent for VM {vmid}: {stderr}")
        
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error creating VM {vmid}: {e}")
        return False, str(e)


# ---------------------------------------------------------------------------
# VM Destruction
# ---------------------------------------------------------------------------

def destroy_vm(ssh_executor: SSHExecutor, vmid: int, purge: bool = True) -> Tuple[bool, str]:
    """
    Destroy a VM and optionally purge its storage.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID to destroy
        purge: If True, also remove disk images (default: True)
        
    Returns:
        Tuple of (success, error_message)
    """
    purge_flag = "--purge" if purge else ""
    cmd = f"qm destroy {vmid} {purge_flag}".strip()
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(cmd, timeout=60, check=False)
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            # Check if VM doesn't exist (not an error in destroy context)
            if "does not exist" in error_msg.lower():
                logger.debug(f"VM {vmid} already destroyed or doesn't exist")
                return True, ""
            logger.error(f"Failed to destroy VM {vmid}: {error_msg}")
            return False, error_msg
        
        logger.info(f"Destroyed VM: {vmid}")
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error destroying VM {vmid}: {e}")
        return False, str(e)


# ---------------------------------------------------------------------------
# VM State Control
# ---------------------------------------------------------------------------

def start_vm(ssh_executor: SSHExecutor, vmid: int) -> Tuple[bool, str]:
    """
    Start a VM.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID to start
        
    Returns:
        Tuple of (success, error_message)
    """
    try:
        exit_code, stdout, stderr = ssh_executor.execute(
            f"qm start {vmid}",
            timeout=60,
            check=False
        )
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error(f"Failed to start VM {vmid}: {error_msg}")
            return False, error_msg
        
        logger.info(f"Started VM: {vmid}")
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error starting VM {vmid}: {e}")
        return False, str(e)


def stop_vm(ssh_executor: SSHExecutor, vmid: int, timeout: int = 60) -> Tuple[bool, str]:
    """
    Stop a VM (graceful shutdown).
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID to stop
        timeout: Timeout for shutdown command (default: 60)
        
    Returns:
        Tuple of (success, error_message)
    """
    try:
        exit_code, stdout, stderr = ssh_executor.execute(
            f"qm stop {vmid}",
            timeout=timeout,
            check=False
        )
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            # Check if VM is already stopped
            if "is not running" in error_msg.lower():
                logger.debug(f"VM {vmid} is already stopped")
                return True, ""
            logger.error(f"Failed to stop VM {vmid}: {error_msg}")
            return False, error_msg
        
        logger.info(f"Stopped VM: {vmid}")
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error stopping VM {vmid}: {e}")
        return False, str(e)


def wait_for_vm_stopped(
    ssh_executor: SSHExecutor,
    vmid: int,
    timeout: int = None,
    poll_interval: int = None
) -> bool:
    """
    Wait for a VM to fully stop.
    
    Polls the VM status until it's stopped or timeout is reached.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VMID to check
        timeout: Max time to wait in seconds (default: VM_STOP_TIMEOUT)
        poll_interval: Seconds between checks (default: VM_STOP_POLL_INTERVAL)
        
    Returns:
        True if VM is stopped, False if timeout reached
    """
    if timeout is None:
        timeout = VM_STOP_TIMEOUT
    if poll_interval is None:
        poll_interval = VM_STOP_POLL_INTERVAL
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            exit_code, stdout, stderr = ssh_executor.execute(
                f"qm status {vmid}",
                check=False,
                timeout=10
            )
            
            if exit_code != 0:
                # VM doesn't exist or error - consider stopped
                return True
            
            # Check if status is 'stopped'
            if 'stopped' in stdout.lower():
                logger.debug(f"VM {vmid} confirmed stopped")
                return True
            
            logger.debug(f"VM {vmid} status: {stdout.strip()}, waiting...")
            
        except Exception as e:
            logger.warning(f"Error checking VM {vmid} status: {e}")
        
        time.sleep(poll_interval)
    
    logger.warning(f"Timeout waiting for VM {vmid} to stop")
    return False


def get_vm_status_ssh(ssh_executor: SSHExecutor, vmid: int) -> Optional[str]:
    """
    Get VM status via SSH.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID to check
        
    Returns:
        Status string ('running', 'stopped', etc.) or None if VM doesn't exist
    """
    try:
        exit_code, stdout, stderr = ssh_executor.execute(
            f"qm status {vmid}",
            check=False,
            timeout=10
        )
        
        if exit_code != 0:
            return None
        
        # Parse status from output (format: "status: running" or similar)
        output = stdout.strip().lower()
        if 'running' in output:
            return 'running'
        elif 'stopped' in output:
            return 'stopped'
        else:
            return output
            
    except Exception as e:
        logger.debug(f"Failed to get status for VM {vmid}: {e}")
        return None


# ---------------------------------------------------------------------------
# Disk Operations
# ---------------------------------------------------------------------------

def attach_disk_to_vm(
    ssh_executor: SSHExecutor,
    vmid: int,
    disk_path: str,
    disk_slot: str = "scsi0",
    storage: str = None,
    set_boot: bool = True,
    disk_options: str = '',
) -> Tuple[bool, str]:
    """
    Attach a disk to a VM.
    
    Supports both storage:vmid/disk format and direct paths.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID to attach disk to
        disk_path: Either relative path (vmid/vm-vmid-disk-0.qcow2) or full path
        disk_slot: Disk slot (default: scsi0)
        storage: Storage name (default: PROXMOX_STORAGE_NAME)
        set_boot: If True, set this disk as boot disk (default: True)
        disk_options: Additional disk options string (cache=writeback,discard=on, etc.)
        
    Returns:
        Tuple of (success, error_message)
    """
    storage = storage or PROXMOX_STORAGE_NAME
    
    # Build disk specification
    if disk_path.startswith('/'):
        # Full path - need to import or convert
        logger.warning(f"Full path disk attachment not directly supported: {disk_path}")
        return False, "Full path disk attachment requires disk import"
    else:
        # Relative path - use storage:path format
        disk_spec = f"{storage}:{disk_path}"
        # Add disk options if provided
        if disk_options:
            disk_spec += f",{disk_options}"
    
    boot_opts = ""
    if set_boot:
        # Extract controller type from disk_slot (e.g., "scsi0" -> "scsi0")
        boot_opts = f" --boot c --bootdisk {disk_slot}"
    
    cmd = f"qm set {vmid} --{disk_slot} {disk_spec}{boot_opts}"
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(cmd, timeout=60, check=False)
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error(f"Failed to attach disk to VM {vmid}: {error_msg}")
            return False, error_msg
        
        logger.info(f"Attached disk to VM {vmid}: {disk_spec}")
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error attaching disk to VM {vmid}: {e}")
        return False, str(e)


def create_overlay_disk(
    ssh_executor: SSHExecutor,
    output_path: str,
    backing_file: str,
) -> Tuple[bool, str]:
    """
    Create a QCOW2 overlay disk backed by another QCOW2 file.
    
    Uses copy-on-write for efficient space usage. The overlay only stores
    differences from the backing file.
    
    Args:
        ssh_executor: SSH executor for running commands
        output_path: Full path for the new overlay file
        backing_file: Full path to the backing file (base QCOW2)
        
    Returns:
        Tuple of (success, error_message)
    """
    # Ensure output directory exists
    output_dir = os.path.dirname(output_path)
    ssh_executor.execute(f"mkdir -p {output_dir}", check=False)
    
    # Create overlay with backing file
    cmd = f"qemu-img create -f qcow2 -F qcow2 -b {backing_file} {output_path}"
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(cmd, timeout=120, check=False)
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error(f"Failed to create overlay disk: {error_msg}")
            return False, error_msg
        
        # Set permissions
        ssh_executor.execute(f"chmod 600 {output_path}", check=False)
        ssh_executor.execute(f"chown root:root {output_path}", check=False)
        
        logger.info(f"Created overlay disk: {output_path} -> {backing_file}")
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error creating overlay disk: {e}")
        return False, str(e)


def convert_disk_to_qcow2(
    ssh_executor: SSHExecutor,
    source_path: str,
    output_path: str,
    timeout: int = 600,
) -> Tuple[bool, str]:
    """
    Convert a disk image to standalone QCOW2 format.
    
    Used to export template disks for use as base images.
    
    Args:
        ssh_executor: SSH executor for running commands
        source_path: Source disk path
        output_path: Output QCOW2 file path
        timeout: Conversion timeout in seconds (default: 600)
        
    Returns:
        Tuple of (success, error_message)
    """
    # Ensure output directory exists
    output_dir = os.path.dirname(output_path)
    ssh_executor.execute(f"mkdir -p {output_dir}", check=False)
    
    cmd = f"qemu-img convert -O qcow2 {source_path} {output_path}"
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(cmd, timeout=timeout, check=False)
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error(f"Failed to convert disk to QCOW2: {error_msg}")
            return False, error_msg
        
        # Set readable permissions for the base image
        ssh_executor.execute(f"chmod 644 {output_path}", check=False)
        
        logger.info(f"Converted disk to QCOW2: {source_path} -> {output_path}")
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error converting disk: {e}")
        return False, str(e)


# ---------------------------------------------------------------------------
# Snapshot Operations (via SSH)
# ---------------------------------------------------------------------------

def create_snapshot_ssh(
    ssh_executor: SSHExecutor,
    vmid: int,
    snapname: str,
    description: str = "",
    include_ram: bool = False,
) -> Tuple[bool, str]:
    """
    Create a snapshot of a VM via SSH.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID
        snapname: Snapshot name (alphanumeric with hyphens)
        description: Snapshot description
        include_ram: Include RAM state (default: False)
        
    Returns:
        Tuple of (success, error_message)
    """
    # Sanitize snapshot name
    safe_name = sanitize_vm_name(snapname, fallback="snapshot")
    
    vmstate_opt = "--vmstate" if include_ram else ""
    desc_opt = f'--description "{description}"' if description else ""
    
    cmd = f"qm snapshot {vmid} {safe_name} {vmstate_opt} {desc_opt}".strip()
    cmd = ' '.join(cmd.split())  # Clean up extra spaces
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(cmd, timeout=300, check=False)
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error(f"Failed to create snapshot for VM {vmid}: {error_msg}")
            return False, error_msg
        
        logger.info(f"Created snapshot '{safe_name}' for VM {vmid}")
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error creating snapshot: {e}")
        return False, str(e)


def rollback_snapshot_ssh(
    ssh_executor: SSHExecutor,
    vmid: int,
    snapname: str,
) -> Tuple[bool, str]:
    """
    Rollback a VM to a snapshot via SSH.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID
        snapname: Snapshot name to rollback to
        
    Returns:
        Tuple of (success, error_message)
    """
    cmd = f"qm rollback {vmid} {snapname}"
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(cmd, timeout=300, check=False)
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error(f"Failed to rollback VM {vmid} to snapshot {snapname}: {error_msg}")
            return False, error_msg
        
        logger.info(f"Rolled back VM {vmid} to snapshot '{snapname}'")
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error rolling back snapshot: {e}")
        return False, str(e)


def delete_snapshot_ssh(
    ssh_executor: SSHExecutor,
    vmid: int,
    snapname: str,
) -> Tuple[bool, str]:
    """
    Delete a snapshot from a VM via SSH.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID
        snapname: Snapshot name to delete
        
    Returns:
        Tuple of (success, error_message)
    """
    cmd = f"qm delsnapshot {vmid} {snapname}"
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(cmd, timeout=300, check=False)
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            # Check if snapshot doesn't exist
            if "does not exist" in error_msg.lower():
                logger.debug(f"Snapshot {snapname} already deleted from VM {vmid}")
                return True, ""
            logger.error(f"Failed to delete snapshot {snapname} from VM {vmid}: {error_msg}")
            return False, error_msg
        
        logger.info(f"Deleted snapshot '{snapname}' from VM {vmid}")
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error deleting snapshot: {e}")
        return False, str(e)


# ---------------------------------------------------------------------------
# VM Configuration
# ---------------------------------------------------------------------------

def get_vm_config_ssh(ssh_executor: SSHExecutor, vmid: int, node: str = None) -> Optional[Dict[str, str]]:
    """
    Get VM configuration via SSH.
    
    Parses qm config output into a dictionary. Uses cluster-wide query
    if node is not specified.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID
        node: Specific node name (optional, will auto-detect if not provided)
        
    Returns:
        Dict of config key-value pairs, or None if VM doesn't exist
    """
    try:
        # If node not specified, find it via cluster resources
        if not node:
            exit_code, stdout, stderr = ssh_executor.execute(
                "pvesh get /cluster/resources --type vm --output-format json",
                check=False,
                timeout=30
            )
            
            if exit_code == 0 and stdout.strip():
                import json
                try:
                    vms = json.loads(stdout)
                    for vm in vms:
                        if vm.get('vmid') == vmid:
                            node = vm.get('node')
                            break
                except (json.JSONDecodeError, ValueError):
                    pass
            
            if not node:
                logger.warning(f"Could not find node for VM {vmid}")
                return None
        
        # Use pvesh to get config (works cluster-wide with node parameter)
        # pvesh returns JSON format, so parse it as JSON
        exit_code, stdout, stderr = ssh_executor.execute(
            f"pvesh get /nodes/{node}/qemu/{vmid}/config --output-format json",
            check=False,
            timeout=30
        )
        
        if exit_code != 0:
            return None
        
        import json
        try:
            config = json.loads(stdout.strip())
            # pvesh returns a dict already, no need to parse lines
            return config
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Failed to parse JSON config for VM {vmid}: {e}")
            return None
        
    except Exception as e:
        logger.debug(f"Failed to get config for VM {vmid}: {e}")
        return None


def set_vm_options(
    ssh_executor: SSHExecutor,
    vmid: int,
    **options
) -> Tuple[bool, str]:
    """
    Set VM configuration options via SSH.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID
        **options: Key-value pairs of options to set
        
    Returns:
        Tuple of (success, error_message)
    """
    if not options:
        return True, ""
    
    # Build options string
    opts_parts = []
    for key, value in options.items():
        opts_parts.append(f"--{key} {value}")
    
    cmd = f"qm set {vmid} {' '.join(opts_parts)}"
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(cmd, timeout=60, check=False)
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error(f"Failed to set options on VM {vmid}: {error_msg}")
            return False, error_msg
        
        logger.debug(f"Set options on VM {vmid}: {options}")
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error setting VM options: {e}")
        return False, str(e)


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

def vm_exists(ssh_executor: SSHExecutor, vmid: int) -> bool:
    """
    Check if a VM exists.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID to check
        
    Returns:
        True if VM exists, False otherwise
    """
    exit_code, _, _ = ssh_executor.execute(
        f"qm status {vmid}",
        check=False,
        timeout=10
    )
    return exit_code == 0


def get_vm_disk_path(
    ssh_executor: SSHExecutor,
    vmid: int,
    disk_slot: str = "scsi0",
) -> Optional[str]:
    """
    Get the full path to a VM's disk.
    
    Parses VM config and resolves storage path to filesystem path.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID
        disk_slot: Disk slot to check (default: scsi0)
        
    Returns:
        Full filesystem path to disk, or None if not found
    """
    config = get_vm_config_ssh(ssh_executor, vmid)
    if not config:
        return None
    
    disk_config = config.get(disk_slot)
    if not disk_config:
        return None
    
    # Parse storage:volume format
    # Example: "TRUENAS-NFS:100/vm-100-disk-0.qcow2,size=32G"
    parts = disk_config.split(',')[0]  # Get just storage:volume
    if ':' not in parts:
        return None
    
    storage, volume = parts.split(':', 1)
    
    # Resolve to filesystem path
    # For NFS storage, typically: /mnt/pve/{storage}/images/{vmid}/{volume}
    if '/' in volume:
        # Volume includes subdirectory
        disk_path = f"/mnt/pve/{storage}/images/{volume}"
    else:
        # Infer vmid directory
        disk_path = f"/mnt/pve/{storage}/images/{vmid}/{volume}"
    
    return disk_path
