#!/usr/bin/env python3
"""
Class VM Service - Integrates QCOW2 bulk cloning with class management.

This service provides the high-level workflow for creating class VMs:
1. Teacher selects a template
2. Template is cloned twice: teacher VM + class base template
3. Student VMs are created as QCOW2 overlays from the class base
4. VMAssignment records are created linking VMs to the class
5. Teacher can "save and deploy" to update all student VMs

The service uses SSH to execute commands on Proxmox nodes and
creates QCOW2 overlay disks for efficient, fast cloning.

REFACTORING NOTE (2024):
This service now imports helper functions from the new modular structure:
- app/services/vm_utils.py       - sanitize_vm_name, get_next_available_vmid_ssh, get_vm_mac_address_ssh
- app/services/vm_core.py        - create_vm_shell, create_overlay_disk, destroy_vm, etc.
- app/services/vm_template.py    - export_template_to_qcow2, create_overlay_vm, etc.

While some functions are reimplemented here for workflow-specific reasons,
the new modules provide the canonical implementations. For new features,
prefer importing from vm_core.py and vm_template.py.
"""

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.models import db, Class, VMAssignment, Template
from app.services.ssh_executor import (
    SSHExecutor,
    get_ssh_executor_from_config,
)

# Import utility functions from new modular structure
# These provide the canonical implementations
from app.services.vm_utils import (
    sanitize_vm_name as _sanitize_vm_name_canonical,
    get_next_available_vmid_ssh as _get_next_available_vmid_canonical,
    get_vm_mac_address_ssh as _get_vm_mac_address_canonical,
)
from app.services.vm_template import export_template_to_qcow2
from app.services.vm_core import wait_for_vm_stopped

logger = logging.getLogger(__name__)


def get_vm_current_node(ssh_executor: SSHExecutor, vmid: int) -> Optional[str]:
    """
    Get the current node where a VM is located.
    
    Queries cluster-wide resources to find which node hosts the VM.
    This is critical after migration to ensure commands target the correct node.
    
    Args:
        ssh_executor: SSH connection to Proxmox cluster
        vmid: VM ID to query
    
    Returns:
        Node name (e.g., 'netlab1') or None if not found
    """
    try:
        # Use pvesh to query cluster-wide resources
        exit_code, stdout, stderr = ssh_executor.execute(
            f"pvesh get /cluster/resources --type vm --output-format json",
            timeout=30,
            check=False
        )
        
        if exit_code == 0 and stdout.strip():
            import json
            try:
                vms = json.loads(stdout)
                for vm in vms:
                    # pvesh returns vmid as integer, ensure type match
                    vm_vmid = vm.get('vmid')
                    if vm_vmid is not None and int(vm_vmid) == int(vmid):
                        node = vm.get('node')
                        if node:
                            logger.debug(f"Found VM {vmid} on node {node}")
                            return node
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                logger.warning(f"Failed to parse cluster resources: {e}")
        
        logger.warning(f"Could not find node for VM {vmid} via cluster query")
        return None
        
    except Exception as e:
        logger.error(f"Error getting VM node for VMID {vmid}: {e}")
        return None


def migrate_vm_to_node(ssh_executor: SSHExecutor, vmid: int, target_node: str, timeout: int = 300) -> Tuple[bool, str]:
    """
    Migrate a VM to the target node using qm migrate.
    
    Args:
        ssh_executor: SSH connection to Proxmox cluster
        vmid: VM ID to migrate
        target_node: Target node name (e.g., 'netlab1', 'netlab3')
        timeout: Migration timeout in seconds
    
    Returns:
        Tuple of (success: bool, error_message: str)
    """
    try:
        # First check current node
        exit_code, stdout, stderr = ssh_executor.execute(
            f"qm status {vmid}",
            timeout=30,
            check=False
        )
        
        if exit_code != 0:
            return False, f"Failed to get VM status: {stderr}"
        
        # Online migration without shared storage (--online flag not needed for offline migration)
        # Since VMs have no disk attached yet, this should be very fast
        exit_code, stdout, stderr = ssh_executor.execute(
            f"qm migrate {vmid} {target_node}",
            timeout=timeout,
            check=False
        )
        
        if exit_code != 0:
            return False, f"Migration failed: {stderr}"
        
        logger.info(f"Successfully migrated VM {vmid} to {target_node}")
        return True, ""
        
    except Exception as e:
        logger.error(f"Error migrating VM {vmid} to {target_node}: {e}")
        return False, str(e)

# Storage name for Proxmox (configurable via environment)
PROXMOX_STORAGE_NAME = os.getenv("PROXMOX_STORAGE_NAME", "TRUENAS-NFS")

# Default storage paths for QCOW2 files
DEFAULT_TEMPLATE_STORAGE_PATH = os.getenv("QCOW2_TEMPLATE_PATH", "/mnt/pve/TRUENAS-NFS/images")
DEFAULT_VM_IMAGES_PATH = os.getenv("QCOW2_IMAGES_PATH", "/mnt/pve/TRUENAS-NFS/images")

# VM stop timeout in seconds
VM_STOP_TIMEOUT = int(os.getenv("VM_STOP_TIMEOUT", "60"))
VM_STOP_POLL_INTERVAL = int(os.getenv("VM_STOP_POLL_INTERVAL", "3"))


@dataclass
class ClassVMDeployResult:
    """Result of a class VM deployment operation."""
    class_id: int
    teacher_vmid: Optional[int] = None
    class_base_vmid: Optional[int] = None
    student_vmids: List[int] = field(default_factory=list)
    successful: int = 0
    failed: int = 0
    error: Optional[str] = None
    details: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Utility functions - delegate to canonical implementations in vm_utils.py
# These wrappers are kept for backward compatibility with existing callers.
# New code should import directly from app.services.vm_utils
# ---------------------------------------------------------------------------

def get_next_available_vmid(ssh_executor: SSHExecutor, start_vmid: int = 100) -> int:
    """Get the next available VMID in Proxmox.
    
    NOTE: This delegates to vm_utils.get_next_available_vmid_ssh().
    For new code, import directly from app.services.vm_utils.
    """
    return _get_next_available_vmid_canonical(ssh_executor, start=start_vmid)


def get_vm_mac_address(ssh_executor: SSHExecutor, vmid: int) -> Optional[str]:
    """Get the MAC address of a VM's first network interface.
    
    NOTE: This delegates to vm_utils.get_vm_mac_address_ssh().
    For new code, import directly from app.services.vm_utils.
    
    Args:
        ssh_executor: SSH executor
        vmid: VM ID
        
    Returns:
        MAC address string or None
    """
    return _get_vm_mac_address_canonical(ssh_executor, vmid)


def sanitize_vm_name(name: str) -> str:
    """Sanitize a string for use as a Proxmox VM name.
    
    NOTE: This delegates to vm_utils.sanitize_vm_name().
    For new code, import directly from app.services.vm_utils.
    """
    return _sanitize_vm_name_canonical(name)


def deploy_class_vms(
    class_id: int,
    num_students: int = 0
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    High-level function to deploy all VMs for a class.
    
    Creates:
    - 1 Teacher VM (full clone or empty shell)
    - 1 Class base template (for student overlays)
    - N Student VMs (QCOW2 overlays)
    
    Handles both template-based and template-less (none) classes.
    Updates VMInventory after creation for admin view.
    
    Args:
        class_id: Database ID of class
        num_students: Number of student VMs to create
        
    Returns:
        (success, message, vm_info_dict)
    """
    from app.services.inventory_service import persist_vm_inventory
    from app.services.proxmox_client import get_all_vms
    
    try:
        # Load class from database
        class_obj = Class.query.get(class_id)
        if not class_obj:
            return False, f"Class {class_id} not found", {}
        
        # Get template info (if any)
        template = class_obj.template
        template_vmid = template.proxmox_vmid if template else None
        template_node = template.node if template else None
        
        # If no template, get first available node
        if not template_node:
            from app.services.proxmox_service import get_proxmox_admin_for_cluster
            from app.config import CLUSTERS
            
            cluster_id = None
            for cluster in CLUSTERS:
                if cluster["host"] == "10.220.15.249":
                    cluster_id = cluster["id"]
                    break
            
            if cluster_id:
                proxmox = get_proxmox_admin_for_cluster(cluster_id)
                nodes = proxmox.nodes.get()
                template_node = nodes[0]['node'] if nodes else None
        
        if not template_node:
            return False, "No Proxmox nodes available", {}
        
        # Call create_class_vms with proper parameters
        result = create_class_vms(
            class_id=class_id,
            template_vmid=template_vmid,  # Can be None for template-less classes
            template_node=template_node,
            pool_size=num_students,
            class_name=class_obj.name,
            teacher_id=class_obj.teacher_id,
            memory=class_obj.memory_mb or 2048,
            cores=class_obj.cpu_cores or 2,
            disk_size_gb=class_obj.disk_size_gb or 32,
            auto_start=False,
        )
        
        if result.error:
            return False, result.error, {}
        
        # After successful creation, trigger VMInventory update
        try:
            # Fetch latest VM data and persist to VMInventory
            logger.info("Updating VMInventory after class VM creation...")
            vms = get_all_vms(skip_ips=True, force_refresh=True)
            persist_vm_inventory(vms, cleanup_missing=False)
            logger.info("VMInventory updated successfully")
        except Exception as e:
            logger.warning(f"Failed to update VMInventory after class creation: {e}")
            # Don't fail the whole operation if inventory update fails
        
        vm_info = {
            'teacher_vm_count': 1 if result.teacher_vmid else 0,
            'template_vm_count': 1 if result.class_base_vmid else 0,
            'student_vm_count': len(result.student_vmids),
            'teacher_vmid': result.teacher_vmid,
            'template_vmid': result.class_base_vmid,
            'student_vmids': result.student_vmids,
        }
        
        return True, f"Created {result.successful} VMs successfully", vm_info
        
    except Exception as e:
        logger.exception(f"Failed to deploy class VMs: {e}")
        return False, str(e), {}


def recreate_student_vms_from_template(
    class_id: int,
    template_vmid: int,
    template_node: str,
    count: int,
    class_name: str,
    memory: int = None,
    cores: int = None,
) -> Tuple[bool, str, List[int]]:
    """
    Recreate student VMs from template using disk copy approach.
    
    This is used when pushing template changes to students - we delete
    old student VMs and create fresh ones from the updated template.
    Uses the same VM specs (memory, cores) as defined in the class.
    
    Args:
        class_id: Database ID of the class
        template_vmid: Source template VMID
        template_node: Proxmox node where template resides
        count: Number of student VMs to create
        class_name: Name of the class (for VM naming)
        memory: Memory per VM in MB (if None, fetches from Class model)
        cores: CPU cores per VM (if None, fetches from Class model)
        
    Returns:
        Tuple of (success, message, list_of_vmids)
    """
    logger.info(f"Recreating {count} student VMs for class {class_id} from template {template_vmid}")
    
    # Fetch class specs if not provided
    if memory is None or cores is None:
        class_obj = Class.query.get(class_id)
        if class_obj:
            memory = class_obj.memory_mb or 4096
            cores = class_obj.cpu_cores or 2
        else:
            memory = 4096
            cores = 2
    
    logger.info(f"Using VM specs: {memory}MB RAM, {cores} cores")
    
    class_prefix = sanitize_vm_name(class_name)
    if not class_prefix:
        class_prefix = f"class-{class_id}"
    
    ssh_executor = None
    created_vmids = []
    
    try:
        ssh_executor = get_ssh_executor_from_config()
        ssh_executor.connect()
        
        # Export template to temporary base QCOW2
        base_qcow2_path = f"{DEFAULT_TEMPLATE_STORAGE_PATH}/{class_prefix}-base.qcow2"
        
        logger.info(f"Exporting template {template_vmid} to {base_qcow2_path}")
        success, error = export_template_to_qcow2(
            ssh_executor=ssh_executor,
            template_vmid=template_vmid,
            node=template_node,
            output_path=base_qcow2_path,
        )
        
        if not success:
            return False, f"Failed to export template: {error}", []
        
        # Create student VMs as overlays
        for i in range(count):
            try:
                vmid = get_next_available_vmid(ssh_executor)
                student_name = f"{class_prefix}-student-{i+1}"
                
                # Create VM shell with proper specs from class configuration
                exit_code, stdout, stderr = ssh_executor.execute(
                    f"qm create {vmid} --name {student_name} --memory {memory} --cores {cores} "
                    f"--net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-pci",
                    timeout=60
                )
                
                if exit_code != 0:
                    logger.error(f"Failed to create VM shell {vmid}: {stderr}")
                    continue
                
                # Create overlay disk
                overlay_path = f"{DEFAULT_VM_IMAGES_PATH}/{vmid}/vm-{vmid}-disk-0.qcow2"
                ssh_executor.execute(f"mkdir -p {DEFAULT_VM_IMAGES_PATH}/{vmid}", check=False)
                
                exit_code, stdout, stderr = ssh_executor.execute(
                    f"qemu-img create -f qcow2 -F qcow2 -b {base_qcow2_path} {overlay_path}",
                    timeout=120
                )
                
                if exit_code != 0:
                    logger.error(f"Failed to create overlay for {vmid}: {stderr}")
                    ssh_executor.execute(f"qm destroy {vmid}", check=False)
                    continue
                
                # Set permissions
                ssh_executor.execute(f"chmod 600 {overlay_path}", check=False)
                ssh_executor.execute(f"chown root:root {overlay_path}", check=False)
                
                # Attach disk
                exit_code, stdout, stderr = ssh_executor.execute(
                    f"qm set {vmid} --scsi0 {PROXMOX_STORAGE_NAME}:{vmid}/vm-{vmid}-disk-0.qcow2 --boot c --bootdisk scsi0",
                    timeout=60
                )
                
                if exit_code != 0:
                    logger.error(f"Failed to attach disk to {vmid}: {stderr}")
                    ssh_executor.execute(f"qm destroy {vmid}", check=False)
                    continue
                
                # Get MAC address
                student_mac = get_vm_mac_address(ssh_executor, vmid)
                
                # Create VMAssignment
                assignment = VMAssignment(
                    class_id=class_id,
                    proxmox_vmid=vmid,
                    vm_name=student_name,
                    mac_address=student_mac,
                    node=template_node,
                    assigned_user_id=None,
                    status='available',
                    is_template_vm=False,
                    is_teacher_vm=False,
                )
                db.session.add(assignment)
                
                created_vmids.append(vmid)
                logger.info(f"Created student VM {vmid} ({student_name})")
                
            except Exception as e:
                logger.exception(f"Error creating student VM: {e}")
        
        db.session.commit()
        
        message = f"Successfully created {len(created_vmids)} student VMs"
        logger.info(message)
        return True, message, created_vmids
        
    except Exception as e:
        logger.exception(f"Error recreating student VMs: {e}")
        db.session.rollback()
        return False, str(e), created_vmids
    finally:
        if ssh_executor:
            try:
                ssh_executor.disconnect()
            except Exception:
                pass


# wait_for_vm_stopped is now imported from vm_core at the top of this file
# Removed duplicate implementation - use canonical version from vm_core


# NOTE: The second get_next_available_vmid definition below was a duplicate.
# It has been removed. Use the get_next_available_vmid() function at the top of this file
# which delegates to vm_utils.get_next_available_vmid_ssh().


# export_template_to_qcow2 is now imported from vm_template at the top of this file
# Removed 100-line duplicate implementation - use canonical version from vm_template


def create_class_vms(
    class_id: int,
    template_vmid: Optional[int],  # Can be None for template-less classes
    template_node: str,
    pool_size: int,
    class_name: str,
    teacher_id: int,
    memory: int = 4096,
    cores: int = 2,
    disk_size_gb: int = 32,
    auto_start: bool = False,
) -> ClassVMDeployResult:
    """
    Create all VMs for a class using QCOW2 overlay approach.
    
    Workflow (with template):
    1. Export source template to a class-specific base QCOW2
    2. Create teacher VM as a full clone (can be modified)
    3. Create class base template VM (for overlay reference)
    4. Create student VMs as QCOW2 overlays from the class base
    5. Create VMAssignment records for all VMs with MAC addresses
    
    Workflow (without template - template_vmid=None):
    1. Create empty teacher VM shell with custom specs
    2. Create empty class base template shell
    3. Create student VM shells as QCOW2 overlays
    4. Create VMAssignment records for all VMs
    
    Args:
        class_id: Database ID of the class
        template_vmid: Source template VMID (None for template-less classes)
        template_node: Proxmox node where template resides
        pool_size: Number of student VMs to create
        class_name: Name of the class (for VM naming)
        teacher_id: User ID of the teacher
        memory: Memory per VM in MB
        cores: CPU cores per VM
        disk_size_gb: Disk size in GB (only used for template-less classes)
        auto_start: Whether to start VMs after creation
        
    Returns:
        ClassVMDeployResult with deployment status
    """
    result = ClassVMDeployResult(class_id=class_id)
    
    # Sanitize class name for VM naming
    class_prefix = sanitize_vm_name(class_name)
    if not class_prefix:
        class_prefix = f"class-{class_id}"
    
    logger.info(f"Creating VMs for class {class_id} ({class_name})")
    logger.info(f"Template VMID: {template_vmid}, Pool size: {pool_size}")
    
    ssh_executor = None
    optimal_node = None
    proxmox = None
    try:
        # Connect to Proxmox via SSH
        ssh_executor = get_ssh_executor_from_config()
        ssh_executor.connect()
        logger.info(f"SSH connected to {ssh_executor.host}")
        
        # Get Proxmox API connection for resource queries
        try:
            from app.services.proxmox_service import get_proxmox_admin
            proxmox = get_proxmox_admin()
        except Exception as e:
            logger.warning(f"Could not get Proxmox API connection: {e}")
        
        # Node selection will be done per-VM for better distribution
        from app.services.vm_utils import get_optimal_node
        logger.info(f"Using shared storage: {PROXMOX_STORAGE_NAME} (path: {DEFAULT_VM_IMAGES_PATH})")
        logger.info(f"Note: All nodes must have access to shared storage for multi-node deployment")
        
        # Step 1: Handle template vs no-template workflow
        if template_vmid:
            # WITH TEMPLATE: Export template to class base QCOW2
            base_qcow2_path = f"{DEFAULT_TEMPLATE_STORAGE_PATH}/{class_prefix}-base.qcow2"
            
            result.details.append(f"Exporting template {template_vmid} to class base...")
            success, error = export_template_to_qcow2(
                ssh_executor=ssh_executor,
                template_vmid=template_vmid,
                node=template_node,
                output_path=base_qcow2_path,
            )
            
            if not success:
                result.error = f"Failed to export template: {error}"
                return result
            
            result.details.append(f"Class base created: {base_qcow2_path}")
            
            # Step 2: Create teacher VM as overlay (NOT full clone)
            result.details.append("Creating teacher VM as overlay...")
            
            # Select optimal node for teacher VM
            teacher_optimal_node = get_optimal_node(ssh_executor, proxmox)
            logger.info(f"Selected optimal node for teacher VM: {teacher_optimal_node}")
            
            # Retry logic for VMID conflicts
            max_retries = 30
            teacher_vmid = None
            teacher_mac = None
            start_vmid = 300  # Track starting point for VMID search
            
            for retry in range(max_retries):
                teacher_vmid = get_next_available_vmid(ssh_executor, start_vmid)
                teacher_name = f"{class_prefix}-teacher"
                
                # Import create_overlay_vm from vm_template module
                from app.services.vm_template import create_overlay_vm
                
                success, error, teacher_mac = create_overlay_vm(
                    ssh_executor=ssh_executor,
                    vmid=teacher_vmid,
                    name=teacher_name,
                    base_qcow2_path=base_qcow2_path,
                    node=template_node,
                    memory=memory,
                    cores=cores,
                )
                
                if success:
                    break
                    
                # Check if error is due to VMID conflict
                if "already exists" in error.lower():
                    logger.warning(f"VMID {teacher_vmid} already exists (attempt {retry + 1}/{max_retries}), retrying with next VMID...")
                    start_vmid = teacher_vmid + 1  # Skip this VMID next time
                    continue
                else:
                    # Different error, don't retry
                    result.error = f"Failed to create teacher VM: {error}"
                    return result
            
            if not success:
                result.error = f"Failed to create teacher VM after {max_retries} retries: {error}"
                return result
            
            # Note: For template-based workflow, teacher VM is created on template_node
            # Migration could be added here if needed, but shared storage allows cross-node access
            result.details.append(f"Teacher VM {teacher_vmid} created on {template_node}")
            
        else:
            # WITHOUT TEMPLATE: Create empty VM shells
            result.details.append("Creating teacher VM shell (no template)...")
            
            # Retry logic for VMID conflicts
            max_retries = 30
            teacher_vmid = None
            teacher_mac = None
            start_vmid = 100  # Track starting point for VMID search
            
            for retry in range(max_retries):
                teacher_vmid = get_next_available_vmid(ssh_executor, start_vmid)
                teacher_name = f"{class_prefix}-teacher"
                
                # Create empty VM shell without storage (will attach after migration)
                exit_code, stdout, stderr = ssh_executor.execute(
                    f"qm create {teacher_vmid} --name {teacher_name} "
                    f"--memory {memory} --cores {cores} "
                    f"--net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-pci",
                    timeout=120,
                    check=False
                )
                
                if exit_code == 0:
                    break
                    
                # Check if error is due to VMID conflict
                if "already exists" in stderr.lower():
                    logger.warning(f"VMID {teacher_vmid} already exists (attempt {retry + 1}/{max_retries}), retrying with next VMID...")
                    start_vmid = teacher_vmid + 1  # Skip this VMID next time
                    continue
                else:
                    # Different error, don't retry
                    result.error = f"Failed to create teacher VM shell: {stderr}"
                    return result
            
            if exit_code != 0:
                result.error = f"Failed to create teacher VM shell after {max_retries} retries: {stderr}"
                return result
            
            result.details.append(f"Teacher VM shell created (VMID: {teacher_vmid})")
            
            # Migrate to optimal node BEFORE attaching storage
            result.details.append(f"Migrating teacher VM to optimal node {optimal_node}...")
            success, error_msg = migrate_vm_to_node(ssh_executor, teacher_vmid, optimal_node, timeout=120)
            if not success:
                logger.warning(f"Failed to migrate teacher VM to {optimal_node}: {error_msg}. Continuing on current node.")
                result.details.append(f"Warning: Migration failed, teacher VM remains on current node")
            else:
                result.details.append(f"Teacher VM migrated to {optimal_node}")
            
            # Get teacher VM MAC address (not returned by qm create)
            teacher_mac = get_vm_mac_address(ssh_executor, teacher_vmid)
            
            # Now attach storage after migration (was included in qm create, need to add separately)
            result.details.append(f"Attaching storage to teacher VM ({disk_size_gb}GB)...")
            
            # Get VM's current node after migration
            current_node = get_vm_current_node(ssh_executor, teacher_vmid)
            if not current_node:
                current_node = optimal_node  # Fallback to expected node
            
            # Use pvesh to set storage (works cluster-wide regardless of which node VM is on)
            exit_code, stdout, stderr = ssh_executor.execute(
                f"pvesh set /nodes/{current_node}/qemu/{teacher_vmid}/config -scsi0 {PROXMOX_STORAGE_NAME}:{disk_size_gb} -boot order=scsi0",
                timeout=120,
                check=False
            )
            
            if exit_code != 0:
                result.error = f"Failed to attach storage to teacher VM: {stderr}"
                # Clean up the VM
                ssh_executor.execute(f"qm destroy {teacher_vmid}", check=False)
                return result
            
            result.details.append(f"Teacher VM storage attached ({disk_size_gb}GB disk)")
            
            # No base_qcow2_path in template-less workflow (students get empty disks)
            base_qcow2_path = None
            
            # Create class-base VM (empty shell for student overlays)
            class_base_vmid = get_next_available_vmid(ssh_executor, teacher_vmid + 1)
            class_base_name = f"{class_prefix}-base"
            
            result.details.append(f"Creating class-base VM shell (no template)...")
            exit_code, stdout, stderr = ssh_executor.execute(
                f"qm create {class_base_vmid} --name {class_base_name} --memory {memory} --cores {cores} "
                f"--net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-pci "
                f"--scsi0 {PROXMOX_STORAGE_NAME}:{disk_size_gb} --boot order=scsi0",
                timeout=120
            )
            
            if exit_code != 0:
                result.error = f"Failed to create class-base VM shell: {stderr}"
                return result
            
            result.class_base_vmid = class_base_vmid
            result.details.append(f"Class-base VM created: {class_base_vmid} ({class_base_name})")
            
            # Create VMAssignment for class-base
            base_mac = get_vm_mac_address(ssh_executor, class_base_vmid)
            class_base_assignment = VMAssignment(
                class_id=class_id,
                proxmox_vmid=class_base_vmid,
                vm_name=class_base_name,
                mac_address=base_mac,
                node=template_node,
                assigned_user_id=None,
                status='available',
                is_template_vm=True,
                is_teacher_vm=False,
            )
            db.session.add(class_base_assignment)
        
        result.teacher_vmid = teacher_vmid
        result.details.append(f"Teacher VM created: {teacher_vmid} ({teacher_name})")
        
        # Use MAC address returned by create_overlay_vm (already retrieved)
        # No need to fetch it again with get_vm_mac_address()
        
        # Create VMAssignment for teacher
        teacher_assignment = VMAssignment(
            class_id=class_id,
            proxmox_vmid=teacher_vmid,
            vm_name=teacher_name,
            mac_address=teacher_mac,
            node=teacher_optimal_node,
            assigned_user_id=teacher_id,
            status='assigned',
            is_template_vm=False,
            is_teacher_vm=True,
            assigned_at=datetime.utcnow(),
        )
        db.session.add(teacher_assignment)
        
        # Track used VMIDs in memory to avoid collisions during creation
        # Include both teacher and class-base VMIDs if created
        if base_qcow2_path is None and 'class_base_vmid' in locals():
            # Template-less workflow: track teacher + class-base
            used_vmids_in_this_session = {teacher_vmid, class_base_vmid}
        else:
            # Template-based workflow: only teacher (no class-base in this flow)
            used_vmids_in_this_session = {teacher_vmid}
        
        # Step 3: Create student VMs as QCOW2 overlays
        if pool_size > 0:
            result.details.append(f"Creating {pool_size} student VMs...")
            
            for i in range(pool_size):
                # Find next available VMID that's not in our session's used set
                # Start search from last used VMID + 1
                start_search = max(used_vmids_in_this_session) + 1 if used_vmids_in_this_session else teacher_vmid + 1
                
                # Keep searching until we find an unused VMID
                vmid = None
                max_attempts = 200
                for attempt in range(max_attempts):
                    # Get next available VMID from cluster starting at start_search
                    candidate_vmid = get_next_available_vmid(ssh_executor, start_search)
                    
                    # Check if this VMID is already used in our session
                    if candidate_vmid not in used_vmids_in_this_session:
                        # Found an available VMID
                        vmid = candidate_vmid
                        break
                    else:
                        # This VMID was used in our session, try the next one
                        start_search = candidate_vmid + 1
                        logger.debug(f"VMID {candidate_vmid} already used in session, trying {start_search}")
                
                if vmid is None:
                    # Exhausted attempts
                    logger.error(f"Could not find available VMID after {max_attempts} attempts")
                    result.failed += 1
                    continue
                
                used_vmids_in_this_session.add(vmid)
                student_name = f"{class_prefix}-student-{i + 1}"
                
                # Select optimal node for this student VM
                student_optimal_node = get_optimal_node(ssh_executor, proxmox)
                logger.info(f"Selected optimal node for {student_name}: {student_optimal_node}")
                
                try:
                    # Create VM shell (will be on the connected node)
                    exit_code, stdout, stderr = ssh_executor.execute(
                        f"qm create {vmid} --name {student_name} "
                        f"--memory {memory} --cores {cores} "
                        f"--scsihw virtio-scsi-pci --net0 virtio,bridge=vmbr0",
                        timeout=60
                    )
                    
                    if exit_code != 0:
                        logger.error(f"Failed to create VM shell {vmid}: {stderr}")
                        result.failed += 1
                        continue
                    
                    # Migrate to optimal node BEFORE attaching storage
                    logger.info(f"Migrating student VM {vmid} to {student_optimal_node}...")
                    success, error_msg = migrate_vm_to_node(ssh_executor, vmid, student_optimal_node, timeout=120)
                    if not success:
                        logger.warning(f"Failed to migrate VM {vmid} to {student_optimal_node}: {error_msg}. Continuing on current node.")
                    else:
                        logger.info(f"VM {vmid} migrated to {student_optimal_node}")
                    
                    # Create disk - overlay if template exists, empty disk if not
                    if base_qcow2_path:
                        # Create overlay disk pointing to base
                        overlay_dir = f"{DEFAULT_VM_IMAGES_PATH}/{vmid}"
                        overlay_path = f"{overlay_dir}/vm-{vmid}-disk-0.qcow2"
                        
                        ssh_executor.execute(f"mkdir -p {overlay_dir}", check=False)
                        
                        exit_code, stdout, stderr = ssh_executor.execute(
                            f"qemu-img create -f qcow2 -F qcow2 -b {base_qcow2_path} {overlay_path}",
                            timeout=60
                        )
                        
                        if exit_code != 0:
                            logger.error(f"Failed to create overlay for {vmid}: {stderr}")
                            ssh_executor.execute(f"qm destroy {vmid}", check=False)
                            result.failed += 1
                            continue
                        
                        # Set permissions
                        ssh_executor.execute(f"chmod 600 {overlay_path}", check=False)
                        ssh_executor.execute(f"chown root:root {overlay_path}", check=False)
                        
                        # Attach disk to VM (get current node after migration)
                        current_node = get_vm_current_node(ssh_executor, vmid)
                        if not current_node:
                            current_node = student_optimal_node  # Fallback
                        
                        exit_code, stdout, stderr = ssh_executor.execute(
                            f"pvesh set /nodes/{current_node}/qemu/{vmid}/config -scsi0 {PROXMOX_STORAGE_NAME}:{vmid}/vm-{vmid}-disk-0.qcow2 -boot c -bootdisk scsi0",
                            timeout=60
                        )
                    else:
                        # No template - create VM with empty disk
                        # Get VM's current node after migration
                        current_node = get_vm_current_node(ssh_executor, vmid)
                        if not current_node:
                            current_node = optimal_node  # Fallback
                        
                        exit_code, stdout, stderr = ssh_executor.execute(
                            f"pvesh set /nodes/{current_node}/qemu/{vmid}/config -scsi0 {PROXMOX_STORAGE_NAME}:{disk_size_gb} -boot order=scsi0",
                            timeout=60
                        )
                    
                    if exit_code != 0:
                        logger.error(f"Failed to attach disk to {vmid}: {stderr}")
                        ssh_executor.execute(f"qm destroy {vmid}", check=False)
                        result.failed += 1
                        continue
                    
                    result.student_vmids.append(vmid)
                    result.successful += 1
                    
                    # Get MAC address for student VM
                    student_mac = get_vm_mac_address(ssh_executor, vmid)
                    
                    # Get actual current node for assignment
                    actual_node = get_vm_current_node(ssh_executor, vmid)
                    if not actual_node:
                        actual_node = student_optimal_node
                    
                    # Create VMAssignment for student VM (unassigned)
                    assignment = VMAssignment(
                        class_id=class_id,
                        proxmox_vmid=vmid,
                        vm_name=student_name,
                        mac_address=student_mac,
                        node=actual_node,
                        assigned_user_id=None,
                        status='available',
                        is_template_vm=False,
                        is_teacher_vm=False,
                    )
                    db.session.add(assignment)
                    
                    logger.info(f"Created student VM {vmid} ({student_name})")
                    
                except Exception as e:
                    logger.exception(f"Error creating student VM {vmid}: {e}")
                    result.failed += 1
        
        # Step 4: Start VMs if requested
        if auto_start and result.teacher_vmid:
            result.details.append("Starting VMs...")
            ssh_executor.execute(f"qm start {result.teacher_vmid}", check=False)
            for vmid in result.student_vmids:
                ssh_executor.execute(f"qm start {vmid}", check=False)
        
        # Commit all VMAssignment records
        db.session.commit()
        
        # Update VMInventory with MAC addresses for all created VMs
        try:
            from app.services.inventory_service import persist_vm_inventory
            from app.services.proxmox_service import get_proxmox_admin
            
            proxmox = get_proxmox_admin()
            
            # Get cluster resources to find all VMs
            resources = proxmox.cluster.resources.get(type="vm")
            
            # Update teacher VM
            if result.teacher_vmid:
                for r in resources:
                    if int(r.get('vmid', -1)) == int(result.teacher_vmid):
                        persist_vm_inventory(
                            proxmox_vmid=result.teacher_vmid,
                            name=r.get('name', ''),
                            node=r.get('node', ''),
                            status=r.get('status', 'unknown'),
                            vmtype=r.get('type', 'qemu'),
                            mac_address=teacher_mac if 'teacher_mac' in locals() else None,
                        )
                        break
            
            # Update student VMs
            for vmid in result.student_vmids:
                for r in resources:
                    if int(r.get('vmid', -1)) == int(vmid):
                        # Find the assignment to get MAC
                        assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
                        persist_vm_inventory(
                            proxmox_vmid=vmid,
                            name=r.get('name', ''),
                            node=r.get('node', ''),
                            status=r.get('status', 'unknown'),
                            vmtype=r.get('type', 'qemu'),
                            mac_address=assignment.mac_address if assignment else None,
                        )
                        break
            
            logger.info(f"Updated VMInventory for {len(result.student_vmids) + 1} VMs")
        except Exception as e:
            logger.warning(f"Failed to update VMInventory: {e}")
        
        result.details.append(f"Completed: {result.successful} student VMs created, {result.failed} failed")
        logger.info(f"Class {class_id} VM deployment complete: {result.successful} successful, {result.failed} failed")
        
        return result
        
    except Exception as e:
        logger.exception(f"Error deploying class VMs: {e}")
        db.session.rollback()
        result.error = str(e)
        return result
    finally:
        if ssh_executor:
            try:
                ssh_executor.disconnect()
            except Exception:
                pass


def save_and_deploy_teacher_vm(
    class_id: int,
    teacher_vmid: int,
    node: str,
) -> Tuple[bool, str, int]:
    """
    Save teacher's VM changes and deploy to all student VMs.
    
    This creates a new class base QCOW2 from the teacher's VM and
    updates all student VMs to use it as their backing file.
    
    Since we're replacing backing files, student VMs will lose their
    current state and start fresh from the new base.
    
    Args:
        class_id: Database ID of the class
        teacher_vmid: VMID of the teacher's VM
        node: Proxmox node where VMs reside
        
    Returns:
        Tuple of (success, message, number_of_updated_vms)
    """
    logger.info(f"Save and deploy for class {class_id}, teacher VM {teacher_vmid}")
    
    # Get class and its VMs
    class_obj = Class.query.get(class_id)
    if not class_obj:
        return False, "Class not found", 0
    
    class_prefix = sanitize_vm_name(class_obj.name) or f"class-{class_id}"
    
    # Get student VM assignments
    student_assignments = VMAssignment.query.filter(
        VMAssignment.class_id == class_id,
        VMAssignment.proxmox_vmid != teacher_vmid,
        VMAssignment.is_template_vm == False,
    ).all()
    
    if not student_assignments:
        return True, "No student VMs to update", 0
    
    ssh_executor = None
    updated_count = 0
    
    try:
        ssh_executor = get_ssh_executor_from_config()
        ssh_executor.connect()
        
        # Step 1: Stop teacher VM (required for disk export)
        logger.info(f"Stopping teacher VM {teacher_vmid}...")
        ssh_executor.execute(f"qm stop {teacher_vmid}", check=False, timeout=60)
        
        # Wait for VM to fully stop
        if not wait_for_vm_stopped(ssh_executor, teacher_vmid):
            return False, f"Teacher VM {teacher_vmid} did not stop in time", 0
        
        # Step 2: Create new class base from teacher's VM
        new_base_path = f"{DEFAULT_TEMPLATE_STORAGE_PATH}/{class_prefix}-base.qcow2"
        backup_path = f"{DEFAULT_TEMPLATE_STORAGE_PATH}/{class_prefix}-base.backup.qcow2"
        
        # Backup old base
        ssh_executor.execute(f"mv {new_base_path} {backup_path}", check=False)
        
        # Export teacher VM disk
        success, error = export_template_to_qcow2(
            ssh_executor=ssh_executor,
            template_vmid=teacher_vmid,
            node=node,
            output_path=new_base_path,
        )
        
        if not success:
            # Restore backup
            ssh_executor.execute(f"mv {backup_path} {new_base_path}", check=False)
            return False, f"Failed to export teacher VM: {error}", 0
        
        # Step 3: Update each student VM's overlay to use new base
        logger.info(f"Updating {len(student_assignments)} student VMs...")
        
        for assignment in student_assignments:
            vmid = assignment.proxmox_vmid
            
            try:
                # Stop student VM if running
                ssh_executor.execute(f"qm stop {vmid}", check=False, timeout=30)
                
                # Wait for VM to stop (shorter timeout for students)
                wait_for_vm_stopped(ssh_executor, vmid, timeout=30)
                
                # Get current disk path
                overlay_path = f"{DEFAULT_VM_IMAGES_PATH}/{vmid}/vm-{vmid}-disk-0.qcow2"
                
                # Create new overlay with updated backing file
                # First, remove old overlay
                ssh_executor.execute(f"rm -f {overlay_path}", check=False)
                
                # Create new overlay pointing to new base
                exit_code, stdout, stderr = ssh_executor.execute(
                    f"qemu-img create -f qcow2 -F qcow2 -b {new_base_path} {overlay_path}",
                    timeout=60
                )
                
                if exit_code != 0:
                    logger.error(f"Failed to recreate overlay for VM {vmid}: {stderr}")
                    continue
                
                # Set permissions
                ssh_executor.execute(f"chmod 600 {overlay_path}", check=False)
                ssh_executor.execute(f"chown root:root {overlay_path}", check=False)
                
                updated_count += 1
                logger.info(f"Updated VM {vmid}")
                
            except Exception as e:
                logger.exception(f"Error updating VM {vmid}: {e}")
        
        # Clean up backup
        ssh_executor.execute(f"rm -f {backup_path}", check=False)
        
        # Restart teacher VM
        ssh_executor.execute(f"qm start {teacher_vmid}", check=False)
        
        return True, f"Successfully updated {updated_count} student VMs", updated_count
        
    except Exception as e:
        logger.exception(f"Error in save and deploy: {e}")
        return False, str(e), updated_count
    finally:
        if ssh_executor:
            try:
                ssh_executor.disconnect()
            except Exception:
                pass
