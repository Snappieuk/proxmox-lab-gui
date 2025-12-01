#!/usr/bin/env python3
"""
QCOW2 Overlay Bulk Cloning Service

Fast, safe bulk VM cloning using QCOW2 overlay approach:
1. Export template disk (or use existing QCOW2 base)
2. Create VM shells with qm create
3. Create overlay QCOW2 files with backing-file reference
4. Attach disks to VMs
5. Configure cloud-init
6. Start VMs in batches

This approach is much faster than traditional Proxmox cloning because:
- No full disk copy required (COW overlay)
- Parallel VM creation possible
- No template locking issues

Design Decision: Why `qm` commands instead of Terraform?
---------------------------------------------------------
While the repository has a TerraformService for standard VM deployment, this module
uses direct `qm` commands because:

1. Terraform's Proxmox provider only supports cloning existing templates - it cannot
   create empty VM shells with custom backing-file QCOW2 overlays.

2. The QCOW2 overlay workflow requires:
   - Creating "empty" VM shells (no disk)
   - Creating QCOW2 overlays with backing-file references
   - Attaching these custom disks to VMs
   This is not supported by terraform-provider-proxmox.

3. Speed: Terraform adds init/plan overhead. For bulk operations where speed is
   critical, direct API/CLI calls are faster.

4. The existing TerraformService (terraform_service.py) remains the recommended
   approach for standard class VM deployments using Proxmox's built-in cloning.
   Use this QCOW2 service when you need the backing-file overlay optimization.
"""

import logging
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Default Proxmox storage paths (can be overridden via storage_path parameter)
DEFAULT_TEMPLATE_STORAGE_PATH = "/var/lib/vz/images/template"
DEFAULT_VM_IMAGES_PATH = "/var/lib/vz/images"


@dataclass
class CloudInitOptions:
    """Cloud-init configuration options for VMs."""
    username: str = "user"
    password: Optional[str] = None
    ssh_keys: Optional[List[str]] = None
    ip_config: str = "dhcp"  # "dhcp" or "ip=x.x.x.x/24,gw=x.x.x.x"
    dns_servers: Optional[List[str]] = None
    dns_domain: Optional[str] = None
    upgrade: bool = False


@dataclass
class CloneResult:
    """Result of a VM clone operation."""
    vmid: int
    name: str
    success: bool
    error: Optional[str] = None
    node: Optional[str] = None
    started: bool = False


@dataclass
class BulkCloneResult:
    """Result of a bulk clone operation."""
    total_requested: int
    successful: int
    failed: int
    results: List[CloneResult] = field(default_factory=list)
    base_qcow2_path: Optional[str] = None
    error: Optional[str] = None


def _run_command(
    cmd: List[str],
    check: bool = True,
    timeout: int = 300,
    capture_output: bool = True,
    dry_run: bool = False,
) -> subprocess.CompletedProcess:
    """
    Execute a shell command safely with proper error handling.
    
    Args:
        cmd: Command and arguments as list
        check: Raise CalledProcessError on non-zero exit
        timeout: Command timeout in seconds
        capture_output: Capture stdout/stderr
        dry_run: If True, log command but don't execute
        
    Returns:
        CompletedProcess result
        
    Raises:
        subprocess.CalledProcessError: If command fails and check=True
        subprocess.TimeoutExpired: If command times out
    """
    cmd_str = " ".join(cmd)
    logger.debug(f"Running command: {cmd_str}")
    
    if dry_run:
        logger.info(f"[DRY RUN] Would execute: {cmd_str}")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    
    try:
        result = subprocess.run(
            cmd,
            check=check,
            timeout=timeout,
            capture_output=capture_output,
            text=True,
        )
        if result.stdout:
            logger.debug(f"Command stdout: {result.stdout}")
        return result
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed: {cmd_str}")
        logger.error(f"Exit code: {e.returncode}")
        if e.stdout:
            logger.error(f"Stdout: {e.stdout}")
        if e.stderr:
            logger.error(f"Stderr: {e.stderr}")
        raise
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out after {timeout}s: {cmd_str}")
        raise


def _validate_template_disk(template_disk_path: str) -> Tuple[bool, str]:
    """
    Validate that template disk exists and is readable.
    
    Args:
        template_disk_path: Path to template disk file
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not template_disk_path:
        return False, "Template disk path is empty"
    
    path = Path(template_disk_path)
    
    if not path.exists():
        return False, f"Template disk not found: {template_disk_path}"
    
    if not path.is_file():
        return False, f"Template disk path is not a file: {template_disk_path}"
    
    if not os.access(template_disk_path, os.R_OK):
        return False, f"Template disk is not readable: {template_disk_path}"
    
    return True, ""


def _validate_storage(storage_id: str, storage_path: Optional[str] = None) -> Tuple[bool, str]:
    """
    Validate that storage exists and is accessible.
    
    Args:
        storage_id: Proxmox storage identifier
        storage_path: Optional explicit storage path to validate
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not storage_id:
        return False, "Storage ID is empty"
    
    # Basic format validation for storage ID
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9_-]*$', storage_id):
        return False, f"Invalid storage ID format: {storage_id}"
    
    # If explicit path provided, validate it
    if storage_path:
        path = Path(storage_path)
        if not path.exists():
            return False, f"Storage path not found: {storage_path}"
        if not path.is_dir():
            return False, f"Storage path is not a directory: {storage_path}"
        if not os.access(storage_path, os.W_OK):
            return False, f"Storage path is not writable: {storage_path}"
    
    return True, ""


def _is_vmid_used(vmid: int, node: str = "localhost", dry_run: bool = False) -> bool:
    """
    Check if a VMID is already in use.
    
    Args:
        vmid: VM ID to check
        node: Node to check on (default localhost)
        dry_run: If True, always return False
        
    Returns:
        True if VMID is in use, False otherwise
    """
    if dry_run:
        return False
    
    try:
        result = _run_command(
            ["qm", "status", str(vmid)],
            check=False,
            timeout=10,
        )
        # Exit code 0 means VM exists
        return result.returncode == 0
    except Exception as e:
        logger.warning(f"Error checking VMID {vmid}: {e}")
        # Assume not in use if check fails
        return False


def _get_next_available_vmid(start_vmid: int, count: int, dry_run: bool = False) -> List[int]:
    """
    Get a list of available VMIDs starting from start_vmid.
    
    Args:
        start_vmid: Starting VMID
        count: Number of VMIDs needed
        dry_run: If True, return sequential IDs without checking
        
    Returns:
        List of available VMIDs
    """
    available = []
    current = start_vmid
    
    while len(available) < count:
        if dry_run or not _is_vmid_used(current, dry_run=dry_run):
            available.append(current)
        current += 1
        
        # Safety limit to prevent infinite loop
        if current > start_vmid + count * 10:
            logger.warning(f"Could not find {count} available VMIDs starting from {start_vmid}")
            break
    
    return available


def _export_disk_to_qcow2(
    source_path: str,
    dest_path: str,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """
    Export/convert a disk to QCOW2 format.
    
    Args:
        source_path: Source disk path (can be raw, vmdk, qcow2, etc.)
        dest_path: Destination QCOW2 path
        dry_run: If True, log but don't execute
        
    Returns:
        Tuple of (success, error_message)
    """
    logger.info(f"Converting disk {source_path} to QCOW2 at {dest_path}")
    
    try:
        # Check if source is already qcow2 and dest doesn't exist
        if source_path.endswith('.qcow2') and not Path(dest_path).exists():
            # Just copy if same format
            logger.info(f"Source is already QCOW2, copying to {dest_path}")
            if not dry_run:
                shutil.copy2(source_path, dest_path)
        else:
            # Use qemu-img convert
            _run_command(
                [
                    "qemu-img", "convert",
                    "-f", "auto",  # Auto-detect source format
                    "-O", "qcow2",  # Output format
                    "-o", "preallocation=off",  # Don't preallocate
                    source_path,
                    dest_path,
                ],
                timeout=3600,  # 1 hour timeout for large disks
                dry_run=dry_run,
            )
        
        return True, ""
    except subprocess.CalledProcessError as e:
        return False, f"Failed to convert disk: {e.stderr if e.stderr else str(e)}"
    except Exception as e:
        return False, f"Disk conversion error: {str(e)}"


def _create_vm_shell(
    vmid: int,
    name: str,
    memory: int = 4096,
    cores: int = 2,
    sockets: int = 1,
    net_bridge: str = "vmbr0",
    scsihw: str = "virtio-scsi-pci",
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """
    Create a VM shell without disk using qm create.
    
    Args:
        vmid: VM ID
        name: VM name
        memory: Memory in MB
        cores: Number of CPU cores
        sockets: Number of CPU sockets
        net_bridge: Network bridge name
        scsihw: SCSI hardware type
        dry_run: If True, log but don't execute
        
    Returns:
        Tuple of (success, error_message)
    """
    logger.info(f"Creating VM shell: VMID={vmid}, name={name}")
    
    # Sanitize VM name (Proxmox DNS-style constraints)
    safe_name = re.sub(r'[^a-zA-Z0-9-]', '-', name.lower())
    safe_name = re.sub(r'-+', '-', safe_name).strip('-')
    if not safe_name:
        safe_name = f"vm-{vmid}"
    if len(safe_name) > 63:
        safe_name = safe_name[:63].rstrip('-')
    
    try:
        _run_command(
            [
                "qm", "create", str(vmid),
                "--name", safe_name,
                "--memory", str(memory),
                "--cores", str(cores),
                "--sockets", str(sockets),
                "--scsihw", scsihw,
                "--net0", f"virtio,bridge={net_bridge}",
                "--boot", "order=scsi0",
            ],
            timeout=60,
            dry_run=dry_run,
        )
        return True, ""
    except subprocess.CalledProcessError as e:
        return False, f"Failed to create VM shell: {e.stderr if e.stderr else str(e)}"
    except Exception as e:
        return False, f"VM creation error: {str(e)}"


def _create_overlay_disk(
    base_qcow2_path: str,
    overlay_path: str,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """
    Create a QCOW2 overlay disk with backing file.
    
    This creates a thin overlay that references the base image,
    allowing copy-on-write semantics for fast cloning.
    
    Args:
        base_qcow2_path: Path to base QCOW2 image
        overlay_path: Path for new overlay file
        dry_run: If True, log but don't execute
        
    Returns:
        Tuple of (success, error_message)
    """
    logger.info(f"Creating overlay disk: {overlay_path} -> {base_qcow2_path}")
    
    try:
        _run_command(
            [
                "qemu-img", "create",
                "-f", "qcow2",
                "-F", "qcow2",  # Backing file format
                "-b", base_qcow2_path,  # Backing file
                overlay_path,
            ],
            timeout=60,
            dry_run=dry_run,
        )
        return True, ""
    except subprocess.CalledProcessError as e:
        return False, f"Failed to create overlay: {e.stderr if e.stderr else str(e)}"
    except Exception as e:
        return False, f"Overlay creation error: {str(e)}"


def _attach_disk_to_vm(
    vmid: int,
    disk_path: str,
    disk_slot: str = "scsi0",
    storage_id: Optional[str] = None,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """
    Attach a disk to a VM.
    
    Args:
        vmid: VM ID
        disk_path: Path to disk file
        disk_slot: Disk slot (scsi0, virtio0, etc.)
        storage_id: Storage ID if using Proxmox storage (alternative to path)
        dry_run: If True, log but don't execute
        
    Returns:
        Tuple of (success, error_message)
    """
    logger.info(f"Attaching disk to VM {vmid}: {disk_slot}={disk_path}")
    
    try:
        # Use qm set to attach disk
        disk_spec = disk_path
        if storage_id:
            # Format: storage_id:path
            disk_spec = f"{storage_id}:0,import-from={disk_path}"
        
        _run_command(
            [
                "qm", "set", str(vmid),
                f"--{disk_slot}", disk_spec,
            ],
            timeout=120,
            dry_run=dry_run,
        )
        return True, ""
    except subprocess.CalledProcessError as e:
        return False, f"Failed to attach disk: {e.stderr if e.stderr else str(e)}"
    except Exception as e:
        return False, f"Disk attach error: {str(e)}"


def _configure_cloud_init(
    vmid: int,
    options: CloudInitOptions,
    storage_id: str = "local-lvm",
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """
    Configure cloud-init for a VM.
    
    Args:
        vmid: VM ID
        options: Cloud-init configuration options
        storage_id: Storage for cloud-init drive
        dry_run: If True, log but don't execute
        
    Returns:
        Tuple of (success, error_message)
    """
    logger.info(f"Configuring cloud-init for VM {vmid}")
    
    try:
        # Build cloud-init arguments
        args = ["qm", "set", str(vmid)]
        
        # Add cloud-init drive
        args.extend(["--ide2", f"{storage_id}:cloudinit"])
        
        # Set cloud-init options
        if options.username:
            args.extend(["--ciuser", options.username])
        
        if options.password:
            args.extend(["--cipassword", options.password])
        
        if options.ssh_keys:
            # SSH keys need to be URL-encoded or in a file
            ssh_key_str = "\n".join(options.ssh_keys)
            args.extend(["--sshkeys", ssh_key_str])
        
        if options.ip_config == "dhcp":
            args.extend(["--ipconfig0", "ip=dhcp"])
        else:
            args.extend(["--ipconfig0", options.ip_config])
        
        if options.dns_servers:
            args.extend(["--nameserver", " ".join(options.dns_servers)])
        
        if options.dns_domain:
            args.extend(["--searchdomain", options.dns_domain])
        
        _run_command(args, timeout=60, dry_run=dry_run)
        
        return True, ""
    except subprocess.CalledProcessError as e:
        return False, f"Failed to configure cloud-init: {e.stderr if e.stderr else str(e)}"
    except Exception as e:
        return False, f"Cloud-init config error: {str(e)}"


def _start_vm(vmid: int, dry_run: bool = False) -> Tuple[bool, str]:
    """
    Start a VM.
    
    Args:
        vmid: VM ID
        dry_run: If True, log but don't execute
        
    Returns:
        Tuple of (success, error_message)
    """
    logger.debug(f"Starting VM {vmid}")
    
    try:
        _run_command(
            ["qm", "start", str(vmid)],
            timeout=120,
            dry_run=dry_run,
        )
        return True, ""
    except subprocess.CalledProcessError as e:
        return False, f"Failed to start VM: {e.stderr if e.stderr else str(e)}"
    except Exception as e:
        return False, f"VM start error: {str(e)}"


def _start_vms_in_batches(
    vmids: List[int],
    batch_size: int = 5,
    batch_delay: float = 10.0,
    dry_run: bool = False,
) -> Dict[int, Tuple[bool, str]]:
    """
    Start VMs in batches to avoid overwhelming the host.
    
    Args:
        vmids: List of VM IDs to start
        batch_size: Number of VMs to start per batch
        batch_delay: Delay between batches in seconds
        dry_run: If True, log but don't execute
        
    Returns:
        Dict mapping vmid to (success, error_message)
    """
    results = {}
    
    for i in range(0, len(vmids), batch_size):
        batch = vmids[i:i + batch_size]
        logger.info(f"Starting batch {i // batch_size + 1}: VMIDs {batch}")
        
        for vmid in batch:
            success, error = _start_vm(vmid, dry_run=dry_run)
            results[vmid] = (success, error)
        
        # Wait between batches (except for last batch)
        if i + batch_size < len(vmids) and not dry_run:
            logger.info(f"Waiting {batch_delay}s before next batch...")
            time.sleep(batch_delay)
    
    return results


def _delete_vm(vmid: int, dry_run: bool = False) -> Tuple[bool, str]:
    """
    Delete a VM (for cleanup on failure).
    
    Args:
        vmid: VM ID to delete
        dry_run: If True, log but don't execute
        
    Returns:
        Tuple of (success, error_message)
    """
    logger.warning(f"Deleting VM {vmid} (cleanup)")
    
    try:
        # Stop first if running
        _run_command(
            ["qm", "stop", str(vmid)],
            check=False,
            timeout=30,
            dry_run=dry_run,
        )
        
        # Delete with disk removal
        _run_command(
            ["qm", "destroy", str(vmid), "--purge"],
            timeout=60,
            dry_run=dry_run,
        )
        return True, ""
    except Exception as e:
        return False, f"Failed to delete VM: {str(e)}"


def bulk_clone_from_template(
    template_disk_path: str,
    base_storage_id: str,
    vmid_start: int,
    count: int,
    name_prefix: str = "clone",
    concurrency: int = 5,
    memory: int = 4096,
    cores: int = 2,
    sockets: int = 1,
    net_bridge: str = "vmbr0",
    cloud_init_opts: Optional[CloudInitOptions] = None,
    cloud_init_storage: str = "local-lvm",
    start_batch_size: int = 5,
    start_batch_delay: float = 10.0,
    auto_start: bool = True,
    storage_path: Optional[str] = None,
    dry_run: bool = False,
) -> BulkCloneResult:
    """
    Bulk clone VMs from a template using QCOW2 overlay approach.
    
    This is the main entry point for the QCOW2 bulk cloning service.
    
    The workflow:
    1. Validate template disk and storage
    2. Create/verify QCOW2 base image
    3. Create VM shells (parallel)
    4. Create overlay QCOW2 for each VM (parallel)
    5. Attach disks to VMs (parallel)
    6. Configure cloud-init if requested (parallel)
    7. Start VMs in batches
    
    Args:
        template_disk_path: Path to template disk (QCOW2 or convertible format)
        base_storage_id: Proxmox storage ID for storing VM disks
        vmid_start: Starting VM ID
        count: Number of VMs to create
        name_prefix: Prefix for VM names (e.g., "lab-vm")
        concurrency: Max parallel operations
        memory: Memory per VM in MB
        cores: CPU cores per VM
        sockets: CPU sockets per VM
        net_bridge: Network bridge name
        cloud_init_opts: Optional cloud-init configuration
        cloud_init_storage: Storage for cloud-init drives
        start_batch_size: VMs to start per batch
        start_batch_delay: Delay between start batches (seconds)
        auto_start: Whether to start VMs after creation
        storage_path: Explicit storage path (overrides storage_id detection)
        dry_run: If True, log operations but don't execute
        
    Returns:
        BulkCloneResult with details of all operations
    """
    logger.info(f"Starting bulk clone: {count} VMs from {template_disk_path}")
    logger.info(f"Parameters: vmid_start={vmid_start}, memory={memory}MB, cores={cores}")
    
    result = BulkCloneResult(
        total_requested=count,
        successful=0,
        failed=0,
    )
    
    # Step 1: Validate inputs
    logger.info("Step 1: Validating inputs...")
    
    valid, error = _validate_template_disk(template_disk_path)
    if not valid:
        result.error = error
        logger.error(f"Template validation failed: {error}")
        return result
    
    valid, error = _validate_storage(base_storage_id, storage_path)
    if not valid:
        result.error = error
        logger.error(f"Storage validation failed: {error}")
        return result
    
    # Step 2: Prepare base QCOW2 image
    logger.info("Step 2: Preparing base QCOW2 image...")
    
    # Determine base qcow2 path
    if storage_path:
        base_dir = Path(storage_path)
    else:
        # Use default Proxmox storage path for templates
        base_dir = Path(DEFAULT_TEMPLATE_STORAGE_PATH)
    
    if not dry_run:
        base_dir.mkdir(parents=True, exist_ok=True)
    
    template_name = Path(template_disk_path).stem
    base_qcow2_path = base_dir / f"{template_name}-base.qcow2"
    
    # Convert or copy to base qcow2 if needed
    if not base_qcow2_path.exists() or dry_run:
        if template_disk_path.endswith('.qcow2'):
            # Already qcow2, just copy if different location
            if str(base_qcow2_path) != template_disk_path:
                success, error = _export_disk_to_qcow2(
                    template_disk_path,
                    str(base_qcow2_path),
                    dry_run=dry_run,
                )
                if not success:
                    result.error = f"Failed to prepare base image: {error}"
                    return result
            else:
                logger.info(f"Using existing base QCOW2: {base_qcow2_path}")
        else:
            # Convert to qcow2
            success, error = _export_disk_to_qcow2(
                template_disk_path,
                str(base_qcow2_path),
                dry_run=dry_run,
            )
            if not success:
                result.error = f"Failed to convert template: {error}"
                return result
    else:
        logger.info(f"Using existing base QCOW2: {base_qcow2_path}")
    
    result.base_qcow2_path = str(base_qcow2_path)
    
    # Step 3: Get available VMIDs
    logger.info("Step 3: Allocating VMIDs...")
    vmids = _get_next_available_vmid(vmid_start, count, dry_run=dry_run)
    
    if len(vmids) < count:
        result.error = f"Could only allocate {len(vmids)} of {count} VMIDs"
        logger.error(result.error)
        return result
    
    logger.info(f"Allocated VMIDs: {vmids[0]} - {vmids[-1]}")
    
    # Step 4: Create VMs in parallel
    logger.info(f"Step 4: Creating {count} VM shells (concurrency={concurrency})...")
    
    created_vmids = []
    
    def create_single_vm(vmid: int, index: int) -> CloneResult:
        """Create a single VM with overlay disk."""
        name = f"{name_prefix}-{index + 1}"
        clone_result = CloneResult(vmid=vmid, name=name, success=False)
        
        try:
            # Create VM shell
            success, error = _create_vm_shell(
                vmid=vmid,
                name=name,
                memory=memory,
                cores=cores,
                sockets=sockets,
                net_bridge=net_bridge,
                dry_run=dry_run,
            )
            
            if not success:
                clone_result.error = f"VM shell creation failed: {error}"
                return clone_result
            
            # Create overlay disk
            if storage_path:
                overlay_dir = Path(storage_path) / str(vmid)
            else:
                # Use default Proxmox VM images path
                overlay_dir = Path(DEFAULT_VM_IMAGES_PATH) / str(vmid)
            
            if not dry_run:
                overlay_dir.mkdir(parents=True, exist_ok=True)
            
            overlay_path = overlay_dir / f"vm-{vmid}-disk-0.qcow2"
            
            success, error = _create_overlay_disk(
                str(base_qcow2_path),
                str(overlay_path),
                dry_run=dry_run,
            )
            
            if not success:
                clone_result.error = f"Overlay creation failed: {error}"
                # Cleanup: delete VM shell
                _delete_vm(vmid, dry_run=dry_run)
                return clone_result
            
            # Attach disk to VM
            success, error = _attach_disk_to_vm(
                vmid=vmid,
                disk_path=str(overlay_path),
                disk_slot="scsi0",
                dry_run=dry_run,
            )
            
            if not success:
                clone_result.error = f"Disk attach failed: {error}"
                _delete_vm(vmid, dry_run=dry_run)
                return clone_result
            
            # Configure cloud-init if requested
            if cloud_init_opts:
                success, error = _configure_cloud_init(
                    vmid=vmid,
                    options=cloud_init_opts,
                    storage_id=cloud_init_storage,
                    dry_run=dry_run,
                )
                
                if not success:
                    logger.warning(f"Cloud-init config failed for VM {vmid}: {error}")
                    # Continue anyway - VM is usable without cloud-init
            
            clone_result.success = True
            return clone_result
            
        except Exception as e:
            clone_result.error = f"Unexpected error: {str(e)}"
            logger.exception(f"Error creating VM {vmid}: {e}")
            # Attempt cleanup
            _delete_vm(vmid, dry_run=dry_run)
            return clone_result
    
    # Execute VM creation in parallel
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(create_single_vm, vmid, i): vmid
            for i, vmid in enumerate(vmids)
        }
        
        for future in as_completed(futures):
            vmid = futures[future]
            try:
                clone_result = future.result()
                result.results.append(clone_result)
                
                if clone_result.success:
                    result.successful += 1
                    created_vmids.append(vmid)
                    logger.info(f"Created VM {vmid} ({clone_result.name})")
                else:
                    result.failed += 1
                    logger.error(f"Failed VM {vmid}: {clone_result.error}")
                    
            except Exception as e:
                result.failed += 1
                result.results.append(CloneResult(
                    vmid=vmid,
                    name=f"{name_prefix}-?",
                    success=False,
                    error=str(e),
                ))
                logger.exception(f"Future failed for VM {vmid}: {e}")
    
    # Step 5: Start VMs in batches
    if auto_start and created_vmids:
        logger.info(f"Step 5: Starting {len(created_vmids)} VMs in batches of {start_batch_size}...")
        
        start_results = _start_vms_in_batches(
            vmids=created_vmids,
            batch_size=start_batch_size,
            batch_delay=start_batch_delay,
            dry_run=dry_run,
        )
        
        # Update results with start status
        for clone_result in result.results:
            if clone_result.vmid in start_results:
                started, start_error = start_results[clone_result.vmid]
                clone_result.started = started
                if not started:
                    logger.warning(f"VM {clone_result.vmid} created but failed to start: {start_error}")
    else:
        logger.info("Step 5: Skipping VM start (auto_start=False or no VMs created)")
    
    # Summary
    logger.info(f"Bulk clone complete: {result.successful}/{result.total_requested} successful, {result.failed} failed")
    
    return result


def estimate_clone_time(
    count: int,
    template_size_gb: float = 20.0,
    concurrency: int = 5,
) -> float:
    """
    Estimate time for bulk clone operation.
    
    Args:
        count: Number of VMs to create
        template_size_gb: Template disk size in GB
        concurrency: Parallel operations
        
    Returns:
        Estimated time in seconds
    """
    # Rough estimates:
    # - Base conversion: 1 minute per 10GB
    # - VM shell creation: 2 seconds each
    # - Overlay creation: 1 second each
    # - Disk attach: 2 seconds each
    # - Cloud-init config: 1 second each
    # - VM start: 5 seconds each (in batches)
    
    base_conversion = (template_size_gb / 10) * 60  # Only if needed
    per_vm = 2 + 1 + 2 + 1  # shell + overlay + attach + cloud-init
    vm_time = (count / concurrency) * per_vm
    start_time = (count / 5) * (5 + 10)  # 5 VMs per batch, 10s delay
    
    return base_conversion + vm_time + start_time
