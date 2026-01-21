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
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.models import Class, VMAssignment, db
from app.services.ssh_executor import (
    SSHExecutor,
    get_ssh_executor_from_config,
    get_pooled_ssh_executor_from_config,
)
from app.services.vm_core import wait_for_vm_stopped
from app.services.vm_template import export_template_to_qcow2
from app.services.clone_progress import update_clone_progress

# Import utility functions from new modular structure
# These provide the canonical implementations
from app.services.vm_utils import (
    get_next_available_vmid_ssh as _get_next_available_vmid_canonical,
)
from app.services.vm_utils import (
    get_vm_mac_address_ssh as _get_vm_mac_address_canonical,
)
from app.services.vm_utils import sanitize_vm_name as _sanitize_vm_name_canonical

logger = logging.getLogger(__name__)

# Global lock for VMID allocation to prevent race conditions
# when multiple users create classes simultaneously
_vmid_allocation_lock = threading.Lock()

# Cache for reusing SSH connections per thread
_ssh_connection_cache = threading.local()


def get_cached_ssh_executor():
    """Get or create cached SSH executor for current thread.
    
    Uses global connection pool for better reuse across threads.
    """
    if not hasattr(_ssh_connection_cache, 'executor'):
        _ssh_connection_cache.executor = get_pooled_ssh_executor_from_config()
    return _ssh_connection_cache.executor


def allocate_vmid_prefix_for_class(ssh_executor: SSHExecutor) -> Optional[int]:
    """Allocate a unique 3-digit prefix for a class's VMIDs.
    
    Returns a prefix like 123, which means the class can use VMIDs 12300-12399.
    Ensures the prefix doesn't conflict with existing VMs.
    
    Args:
        ssh_executor: SSH executor for querying Proxmox
        
    Returns:
        3-digit prefix (100-999) or None if allocation fails
    """
    import random

    from app.services.proxmox_service import get_proxmox_admin
    
    with _vmid_allocation_lock:
        try:
            # Get all existing VMIDs from cluster
            proxmox = get_proxmox_admin()
            resources = proxmox.cluster.resources.get(type="vm")
            existing_vmids = {int(r.get('vmid', -1)) for r in resources}
            
            # Also check database for allocated prefixes
            from app.models import Class
            used_prefixes = {c.vmid_prefix for c in Class.query.all() if c.vmid_prefix}
            
            # Try to find an available prefix (100-999)
            # Avoid 100-199 (might be used for system/templates)
            # Use 200-999 for classes
            max_attempts = 100
            for _ in range(max_attempts):
                prefix = random.randint(200, 999)
                
                # Check if this prefix is already used by another class
                if prefix in used_prefixes:
                    continue
                
                # Check if any VMIDs in this range exist (prefix00 to prefix99)
                range_start = prefix * 100
                range_end = range_start + 100
                range_vmids = set(range(range_start, range_end))
                
                # If no overlap with existing VMIDs, this prefix is available
                if not range_vmids.intersection(existing_vmids):
                    logger.info(f"Allocated VMID prefix {prefix} (range {range_start}-{range_end-1})")
                    return prefix
            
            logger.error(f"Failed to allocate VMID prefix after {max_attempts} attempts")
            return None
            
        except Exception as e:
            logger.exception(f"Error allocating VMID prefix: {e}")
            return None


def get_vmid_for_class_vm(class_id: int, vm_index: int) -> Optional[int]:
    """Get VMID for a specific VM in a class.
    
    Args:
        class_id: Database ID of the class
        vm_index: Index of VM (0=teacher, 1-99=students)
        
    Returns:
        VMID like 12300 (teacher), 12301 (student 1), etc.
    """
    from app.models import Class
    
    class_ = Class.query.get(class_id)
    if not class_ or not class_.vmid_prefix:
        logger.error(f"Class {class_id} not found or has no VMID prefix")
        return None
    
    if vm_index < 0 or vm_index >= 100:
        logger.error(f"Invalid VM index {vm_index} (must be 0-99)")
        return None
    
    vmid = (class_.vmid_prefix * 100) + vm_index
    return vmid


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
            "pvesh get /cluster/resources --type vm --output-format json",
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
        
        # Get current node from VM config
        current_node = get_vm_current_node(ssh_executor, vmid)
        
        # Check if target is the same as current node
        if current_node and current_node.lower() == target_node.lower():
            logger.info(f"VM {vmid} is already on {target_node}, skipping migration")
            return True, ""  # Not an error - VM is already where we want it
        
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
            from app.services.proxmox_service import get_clusters_from_db
            from app.services.proxmox_service import get_proxmox_admin_for_cluster
            
            cluster_id = None
            for cluster in get_clusters_from_db():
                if cluster["host"] == "10.220.15.249":
                    cluster_id = cluster["id"]
                    break
            
            if cluster_id:
                proxmox = get_proxmox_admin_for_cluster(cluster_id)
                nodes = proxmox.nodes.get()
                template_node = nodes[0]['node'] if nodes else None
        
        if not template_node:
            return False, "No Proxmox nodes available", {}
        
        # For template-based classes: use template's memory/cores (preserve hardware specs)
        # For template-less classes: use class-level settings
        if template_vmid:
            # Template exists - will use template's hardware specs (extracted in create_class_vms)
            memory_to_use = None  # Signal to use template values
            cores_to_use = None   # Signal to use template values
        else:
            # No template - use class-level settings
            memory_to_use = class_obj.memory_mb or 2048
            cores_to_use = class_obj.cpu_cores or 2
        
        # Call create_class_vms with proper parameters
        result = create_class_vms(
            class_id=class_id,
            template_vmid=template_vmid,  # Can be None for template-less classes
            template_node=template_node,
            pool_size=num_students,
            class_name=class_obj.name,
            teacher_id=class_obj.teacher_id,
            memory=memory_to_use,
            cores=cores_to_use,
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
        ssh_executor = get_pooled_ssh_executor_from_config()
        # Pooled executor is already connected
        
        # Export template to temporary base QCOW2
        base_qcow2_path = f"{DEFAULT_TEMPLATE_STORAGE_PATH}/{class_prefix}-base.qcow2"
        
        logger.info(f"Exporting template {template_vmid} to {base_qcow2_path}")
        success, error, disk_controller_type, ostype, bios, machine, cpu, scsihw, memory, cores, sockets, efi_disk, tpm_state, boot_order, net_model, disk_options_str, net_options_str, other_settings = export_template_to_qcow2(
            ssh_executor=ssh_executor,
            template_vmid=template_vmid,
            node=template_node,
            output_path=base_qcow2_path,
        )
        
        if not success:
            return False, f"Failed to export template: {error}", []
        
        logger.info(f"Template hardware: controller={disk_controller_type}, ostype={ostype}, bios={bios}, machine={machine}, cpu={cpu}, scsihw={scsihw}, net={net_model}")
        logger.info(f"Template disk opts: {disk_options_str}, net opts: {net_options_str}, +{len(other_settings)} settings")
        logger.info(f"Template resources: memory={memory}MB, cores={cores}, sockets={sockets}")
        logger.info(f"Template boot: EFI={bool(efi_disk)}, TPM={bool(tpm_state)}, boot_order={boot_order}")
        
        # Create student VMs as overlays using prefix-based VMID allocation
        from app.services.vm_template import create_overlay_vm
        
        for i in range(count):
            try:
                # Student VMs use indices 1-99 (index 0 is teacher VM)
                vmid = get_vmid_for_class_vm(class_id, i + 1)
                student_name = f"{class_prefix}-student-{i+1}"
                hostname = f"{class_prefix}-s{i+1}"  # Short hostname (e.g., class1-s1)
                
                # Create student VM by cloning template config + overlay disk
                success, error, student_mac = create_overlay_vm(
                    ssh_executor=ssh_executor,
                    vmid=vmid,
                    name=student_name,
                    base_qcow2_path=base_qcow2_path,
                    node=template_node,
                    template_vmid=template_vmid,  # NEW: Clone config from template
                )
                
                if not success:
                    logger.error(f"Failed to create student VM {vmid}: {error}")
                    continue
                
                logger.info(f"Created student VM {vmid} ({student_name}) with MAC {student_mac}")
                
                # Create VMAssignment
                assignment = VMAssignment(
                    class_id=class_id,
                    proxmox_vmid=vmid,
                    vm_name=student_name,
                    mac_address=student_mac,
                    node=template_node,
                    assigned_user_id=None,
                    target_hostname=None,  # Hostname auto-rename DISABLED (requires guest agent)
                    hostname_configured=True,  # Skip auto-rename (prevents boot interference)
                    status='available',
                    is_template_vm=False,
                    is_teacher_vm=False,
                )
                db.session.add(assignment)
                
                created_vmids.append(vmid)
                logger.info(f"Created student VM {vmid} ({student_name}) with MAC {student_mac}")
                
                # DISABLED: Automatic hostname rename can cause boot issues
                # Enable this only if QEMU Guest Agent is installed in your templates
                # from app.services.hostname_service import auto_rename_vm_after_boot
                # from flask import current_app
                # auto_rename_vm_after_boot(vmid, hostname, template_node, app=current_app._get_current_object())
                
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
    
    # If memory/cores are None (template-based class), they will be extracted from template later
    if memory is not None and cores is not None:
        logger.info(f"Using class-specified hardware: {memory}MB RAM, {cores} cores")
    else:
        logger.info(f"Will use template hardware specs (to be extracted)")
    
    ssh_executor = None
    optimal_node = None
    proxmox = None
    try:
        # Get the template to find which node it's on
        from app.models import Template
        template_obj = None
        template_cluster_ip = None
        template_node_name = None
        
        if template_vmid:
            template_obj = Template.query.filter_by(proxmox_vmid=template_vmid).first()
            if template_obj:
                template_cluster_ip = template_obj.cluster_ip
                template_node_name = template_obj.node
                logger.info(f"Template {template_vmid} is on cluster {template_cluster_ip}, node {template_node_name}")
            else:
                logger.warning(f"Template {template_vmid} not found in database - cannot determine cluster/node!")
                result.error = f"Template {template_vmid} not found in database. Please register the template first."
                return result
        
        # Create SSH executor connecting to the SPECIFIC NODE where template exists
        if template_cluster_ip and template_node_name:
            # Find cluster config by IP
            from app.config import CLUSTERS
            cluster_config = None
            for cluster in CLUSTERS:
                if cluster.get('host') == template_cluster_ip:
                    cluster_config = cluster
                    break
            
            if not cluster_config:
                logger.error(f"No cluster config found for IP {template_cluster_ip}")
                result.error = f"Cluster {template_cluster_ip} not configured"
                return result
            
            # Connect to the SPECIFIC NODE where the template is located
            # Use node name as hostname (assumes node names are resolvable hostnames)
            ssh_executor = SSHExecutor(
                host=template_node_name,  # Connect to specific node, not cluster IP
                username=cluster_config.get('ssh_user', 'root'),
                password=cluster_config.get('ssh_password', cluster_config.get('password')),
            )
            logger.info(f"Creating SSH connection to template's node: {template_node_name} (cluster {template_cluster_ip})")
        else:
            # No template - use cached executor (default cluster)
            ssh_executor = get_cached_ssh_executor()
        
        ssh_executor.connect()
        logger.info(f"✓ SSH connected to {ssh_executor.host}")
        
        # Get Proxmox API connection for resource queries
        try:
            from app.services.proxmox_service import get_proxmox_admin
            proxmox = get_proxmox_admin()
        except Exception as e:
            logger.warning(f"Could not get Proxmox API connection: {e}")
        
        # Allocate VMID prefix for this class (e.g., 234 -> VMIDs 23400-23499)
        from app.models import Class, VMAssignment
        class_ = Class.query.get(class_id)
        if not class_:
            result.error = f"Class {class_id} not found"
            return result
        
        # Allow adding VMs to existing class (removed the check that prevented this)
        existing_vms = VMAssignment.query.filter_by(class_id=class_id).count()
        if existing_vms > 0:
            logger.info(f"Adding {pool_size} additional VMs to class {class_id} (currently has {existing_vms} VMs)")
        
        if not class_.vmid_prefix:
            vmid_prefix = allocate_vmid_prefix_for_class(ssh_executor)
            if not vmid_prefix:
                result.error = "Failed to allocate VMID range for class"
                return result
            
            class_.vmid_prefix = vmid_prefix
            db.session.commit()
            logger.info(f"Allocated VMID prefix {vmid_prefix} for class {class_id} (range: {vmid_prefix}00-{vmid_prefix}99)")
        else:
            logger.info(f"Class {class_id} already has VMID prefix {class_.vmid_prefix}")
        
        # Node selection will be done per-VM for better distribution
        from app.services.vm_utils import get_optimal_node
        logger.info(f"Using shared storage: {PROXMOX_STORAGE_NAME} (path: {DEFAULT_VM_IMAGES_PATH})")
        logger.info("Note: All nodes must have access to shared storage for multi-node deployment")
        
        # Check for deployment overrides
        if class_.deployment_node:
            logger.info(f"DEPLOYMENT OVERRIDE: All VMs will be created on node '{class_.deployment_node}' (single-node deployment)")
        if class_.deployment_cluster:
            logger.info(f"DEPLOYMENT CLUSTER: VMs will be created on cluster '{class_.deployment_cluster}'")
        
        # Track simulated VM allocations across nodes for load balancing
        simulated_vms_per_node = {}
        
        # Step 1: Handle template vs no-template workflow
        if template_vmid:
            # WITH TEMPLATE: Export template to class base QCOW2
            base_qcow2_path = f"{DEFAULT_TEMPLATE_STORAGE_PATH}/{class_prefix}-base.qcow2"
            
            # Check if base QCOW2 already exists (when adding VMs to existing class)
            check_cmd = f"test -f {base_qcow2_path} && echo 'exists' || echo 'not_found'"
            exit_code, stdout, stderr = ssh_executor.execute(check_cmd, check=False)
            base_exists = stdout.strip() == 'exists'
            
            if base_exists:
                logger.info(f"Base QCOW2 already exists for class {class_id}: {base_qcow2_path}")
                logger.info("Skipping template export - using existing base for new VMs")
                result.details.append(f"Using existing class base: {base_qcow2_path}")
                
                # Get template metadata from existing base (for VM creation)
                # Default values if we can't determine from existing VMs
                disk_controller_type = 'scsi'
                ostype = 'l26'
                bios = 'seabios'
                machine = 'pc'
                cpu = 'host'
                scsihw = 'virtio-scsi-single'
                memory = 2048
                cores = 2
                sockets = 1
                efi_disk = None
                tpm_state = None
                boot_order = None  # No boot order for existing base
                net_model = 'virtio'  # Default network model
                disk_options_str = ''  # No disk options
                net_options_str = ''  # No network options
                other_settings = {}  # No other settings
                
                # Try to get metadata from teacher VM if it exists
                teacher_vmid = get_vmid_for_class_vm(class_id, 0)
                teacher_config_cmd = f"qm config {teacher_vmid} 2>/dev/null"
                exit_code, config_out, _ = ssh_executor.execute(teacher_config_cmd, check=False)
                if exit_code == 0:
                    # Parse config to extract metadata
                    for line in config_out.split('\n'):
                        if line.startswith('scsihw:'):
                            scsihw = line.split(':', 1)[1].strip()
                        elif line.startswith('memory:'):
                            memory = int(line.split(':', 1)[1].strip())
                        elif line.startswith('cores:'):
                            cores = int(line.split(':', 1)[1].strip())
                        elif line.startswith('ostype:'):
                            ostype = line.split(':', 1)[1].strip()
                        elif line.startswith('bios:'):
                            bios = line.split(':', 1)[1].strip()
                    logger.info(f"Retrieved metadata from teacher VM: {scsihw}, {memory}MB, {cores} cores, {ostype}, {bios}")
            else:
                # Base doesn't exist - export template (first time creating VMs for this class)
                result.details.append(f"Exporting template {template_vmid} to class base (this may take several minutes for large disks)...")
                logger.info(f"Starting template export: VMID {template_vmid}, node {template_node}, output {base_qcow2_path}")
                logger.info(f"NOTE: Disk export is a blocking operation - no VMs can be created until export completes")
                logger.info(f"Starting template export: VMID {template_vmid}, node {template_node}, output {base_qcow2_path}")
                logger.info(f"NOTE: Disk export is a blocking operation - no VMs can be created until export completes")
            
                # Update progress: exporting template
                if class_.clone_task_id:
                    update_clone_progress(
                        class_.clone_task_id,
                        message=f"Exporting template disk (this may take several minutes)...",
                        progress_percent=5
                    )
                
                success, error, disk_controller_type, ostype, bios, machine, cpu, scsihw, template_memory, template_cores, sockets, efi_disk, tpm_state, boot_order, net_model, disk_options_str, net_options_str, other_settings = export_template_to_qcow2(
                    ssh_executor=ssh_executor,
                    template_vmid=template_vmid,
                    node=template_node,
                    output_path=base_qcow2_path,
                )
                
                if not success:
                    error_msg = f"Failed to export template {template_vmid}: {error}"
                    result.error = error_msg
                    logger.error(error_msg)
                    return result
                
                # If memory/cores were not specified (None), use template values
                # Otherwise use the values passed to this function (class override)
                if memory is None:
                    memory = template_memory
                    logger.info(f"Using template memory: {memory}MB")
                else:
                    logger.info(f"Overriding template memory ({template_memory}MB) with class setting: {memory}MB")
                    
                if cores is None:
                    cores = template_cores
                    logger.info(f"Using template cores: {cores}")
                else:
                    logger.info(f"Overriding template cores ({template_cores}) with class setting: {cores}")
                
                logger.info(f"Template export completed successfully: {base_qcow2_path} (controller: {disk_controller_type}, ostype: {ostype}, bios: {bios}, scsihw: {scsihw}, net: {net_model}, disk_opts: {disk_options_str}, net_opts: {net_options_str}, +{len(other_settings)} settings, memory: {memory}MB, cores: {cores}, EFI: {bool(efi_disk)}, TPM: {bool(tpm_state)}, boot_order: {boot_order})")
                result.details.append(f"Class base created: {base_qcow2_path} ({disk_controller_type} disk, {bios} firmware, {scsihw} controller, {net_model} network, {memory}MB RAM, {cores} cores, {'UEFI' if efi_disk else 'BIOS'}, boot: {boot_order})")
            
            # Update progress: template export complete (or skipped)
            if class_.clone_task_id:
                update_clone_progress(
                    class_.clone_task_id,
                    message="Template export complete, creating teacher VM...",
                    progress_percent=15
                )
            
            # Step 2: Create teacher VM as overlay (NOT full clone) - skip if already exists
            teacher_vmid = get_vmid_for_class_vm(class_id, 0)
            if not teacher_vmid:
                result.error = "Failed to allocate teacher VM ID"
                return result
            
            # Initialize teacher VM variables (used in both new and existing VM paths)
            teacher_name = f"{class_prefix}-teacher"
            teacher_mac = None
            teacher_actual_node = None
            
            # Check if teacher VM exists in Proxmox (not just database)
            check_vm_cmd = f"qm status {teacher_vmid} 2>/dev/null"
            exit_code, _, _ = ssh_executor.execute(check_vm_cmd, check=False)
            teacher_exists_in_proxmox = (exit_code == 0)
            
            if teacher_exists_in_proxmox:
                logger.info(f"Teacher VM {teacher_vmid} already exists in Proxmox - skipping creation")
                result.details.append(f"Teacher VM already exists (VMID: {teacher_vmid})")
                
                # Get teacher MAC from existing VM
                mac_cmd = f"qm config {teacher_vmid} | grep 'net0:' | grep -oP 'virtio=[0-9A-F:]+'"
                exit_code, mac_out, _ = ssh_executor.execute(mac_cmd, check=False)
                if exit_code == 0 and mac_out.strip():
                    teacher_mac = mac_out.strip().replace('virtio=', '')
                    logger.info(f"Retrieved teacher MAC: {teacher_mac}")
                else:
                    teacher_mac = None
                    logger.warning("Could not retrieve teacher VM MAC address")
                
                # Get teacher VM's actual node
                teacher_actual_node = get_vm_current_node(ssh_executor, teacher_vmid)
                if not teacher_actual_node:
                    logger.warning(f"Could not determine node for teacher VM {teacher_vmid}, using template_node as fallback")
                    teacher_actual_node = template_node
            else:
                # Teacher VM doesn't exist in Proxmox - recreate it (even if DB record exists)
                logger.info(f"Teacher VM {teacher_vmid} not found in Proxmox - creating/recreating")
                result.details.append("Creating teacher VM as overlay...")
            
                # Select optimal node for teacher VM (with simulated load balancing or override)
                if class_.deployment_node:
                    teacher_optimal_node = class_.deployment_node
                    logger.info(f"Using deployment override node for teacher VM: {teacher_optimal_node}")
                else:
                    teacher_optimal_node = get_optimal_node(ssh_executor, proxmox, vm_memory_mb=memory, simulated_vms_per_node=simulated_vms_per_node)
                    logger.info(f"Selected optimal node for teacher VM: {teacher_optimal_node}")
                
                logger.info(f"Creating teacher VM with VMID {teacher_vmid}")
                
                # Import create_overlay_VM from vm_template module
                from app.services.vm_template import create_overlay_vm
                
                success, error, teacher_mac = create_overlay_vm(
                    ssh_executor=ssh_executor,
                    vmid=teacher_vmid,
                    name=teacher_name,
                    base_qcow2_path=base_qcow2_path,
                    node=template_node,
                    template_vmid=template_vmid,  # NEW: Pass template VMID for config cloning
                )
                
                if not success:
                    result.error = f"Failed to create teacher VM: {error}"
                    logger.error(f"Teacher VM creation failed: {error}")
                    return result
                
                logger.info(f"Teacher VM {teacher_vmid} created successfully")
                
                # Query Proxmox to find actual node where VM was created
                # (SSH commands run on connected node, not necessarily template_node)
                teacher_actual_node = get_vm_current_node(ssh_executor, teacher_vmid)
                if not teacher_actual_node:
                    logger.warning(f"Could not determine node for teacher VM {teacher_vmid}, using template_node as fallback")
                    teacher_actual_node = template_node
                
                result.details.append(f"Teacher VM {teacher_vmid} created on {teacher_actual_node}")
                logger.info(f"Teacher VM {teacher_vmid} confirmed on node {teacher_actual_node}")
            
            # Step 3: Create class-base VM (reserved for template management, never assigned to students)
            # This VM is used when pushing template updates via save-and-deploy
            class_base_vmid = class_.vmid_prefix * 100 + 99  # e.g., 234 * 100 + 99 = 23499
            class_base_name = f"{class_prefix}-base"
            
            # Check if class-base VM already exists
            check_base_cmd = f"qm status {class_base_vmid} 2>/dev/null"
            exit_code, _, _ = ssh_executor.execute(check_base_cmd, check=False)
            base_exists = (exit_code == 0)
            
            if base_exists:
                logger.info(f"Class-base VM {class_base_vmid} already exists - skipping creation")
                result.details.append(f"Class-base VM already exists (VMID: {class_base_vmid})")
                result.class_base_vmid = class_base_vmid
            else:
                result.details.append("Creating class-base VM (reserved for template management)...")
                logger.info(f"Creating class-base VM with VMID {class_base_vmid}")
                
                # Import create_overlay_vm from vm_template module
                from app.services.vm_template import create_overlay_vm
                
                success, error, base_mac = create_overlay_vm(
                    ssh_executor=ssh_executor,
                    vmid=class_base_vmid,
                    name=class_base_name,
                    base_qcow2_path=base_qcow2_path,
                    node=template_node,
                    template_vmid=template_vmid,  # NEW: Pass template VMID for config cloning
                )
                
                if not success:
                    result.error = f"Failed to create class-base VM: {error}"
                    logger.error(f"Class-base VM creation failed: {error}")
                    return result
                
                result.class_base_vmid = class_base_vmid
                result.details.append(f"Class-base VM created: {class_base_vmid} (reserved)")
                
                # Query actual node for class-base VM
                class_base_actual_node = get_vm_current_node(ssh_executor, class_base_vmid)
                if not class_base_actual_node:
                    logger.warning(f"Could not determine node for class-base VM {class_base_vmid}, using template_node")
                    class_base_actual_node = template_node
                
                # Create VMAssignment for class-base (marked as reserved, never assigned)
                class_base_assignment = VMAssignment(
                    class_id=class_id,
                    proxmox_vmid=class_base_vmid,
                    vm_name=class_base_name,
                    mac_address=base_mac,
                    node=class_base_actual_node,
                    assigned_user_id=None,
                    status='reserved',  # NEVER assigned to students
                    is_template_vm=True,  # Hidden from student view
                    is_teacher_vm=False,
                )
                db.session.add(class_base_assignment)
                logger.info(f"Class-base VM {class_base_vmid} created on {class_base_actual_node} (status=reserved)")
            
            # Update progress: ready for student VMs
            if class_.clone_task_id:
                update_clone_progress(
                    class_.clone_task_id,
                    completed=2,
                    message=f"Template management VM ready, creating {pool_size} student VMs...",
                    progress_percent=35
                )
            
        else:
            # WITHOUT TEMPLATE: Create empty VM shells
            result.details.append("Creating teacher VM shell (no template)...")
            
            # Default to scsi for template-less classes
            disk_controller_type = 'scsi'
            ostype = 'l26'  # Default to Linux
            
            # Select optimal node for teacher VM (with simulated load balancing or override)
            if class_.deployment_node:
                teacher_optimal_node = class_.deployment_node
                logger.info(f"Using deployment override node for teacher VM: {teacher_optimal_node}")
            else:
                teacher_optimal_node = get_optimal_node(ssh_executor, proxmox, vm_memory_mb=memory, simulated_vms_per_node=simulated_vms_per_node)
                logger.info(f"Selected optimal node for teacher VM: {teacher_optimal_node}")
            
            # Retry logic for VMID conflicts
            max_retries = 30
            teacher_vmid = None
            teacher_mac = None
            start_vmid = 100  # Track starting point for VMID search
            
            for retry in range(max_retries):
                # Use prefix-based VMID allocation (teacher VM is index 0)
                teacher_vmid = get_vmid_for_class_vm(class_id, 0)
                teacher_name = f"{class_prefix}-teacher"
                
                # Create empty VM shell without storage (will attach after migration)
                exit_code, stdout, stderr = ssh_executor.execute(
                    f"qm create {teacher_vmid} --name {teacher_name} "
                    f"--memory {memory} --cores {cores} "
                    f"--net0 virtio,bridge=vmbr0",
                    timeout=120,
                    check=False
                )
                
                if exit_code != 0:
                    result.error = f"Failed to create teacher VM shell: {stderr}"
                    return result
                
                break
            
            result.details.append(f"Teacher VM shell created (VMID: {teacher_vmid})")
            
            # Migrate to optimal node BEFORE attaching storage
            result.details.append(f"Migrating teacher VM to optimal node {teacher_optimal_node}...")
            success, error_msg = migrate_vm_to_node(ssh_executor, teacher_vmid, teacher_optimal_node, timeout=120)
            if not success:
                logger.warning(f"Failed to migrate teacher VM to {teacher_optimal_node}: {error_msg}. Continuing on current node.")
                result.details.append("Warning: Migration failed, teacher VM remains on current node")
                # Use the node where VM was created (connected SSH node)
                teacher_optimal_node = template_node
            else:
                result.details.append(f"Teacher VM migrated to {teacher_optimal_node}")
                # Wait briefly for migration to fully complete
                import time
                time.sleep(2)
            
            # Get teacher VM MAC address after migration completes
            teacher_mac = get_vm_mac_address(ssh_executor, teacher_vmid)
            
            # Now attach storage after migration (was included in qm create, need to add separately)
            result.details.append(f"Attaching storage to teacher VM ({disk_size_gb}GB)...")
            
            # Get VM's current node after migration
            current_node = get_vm_current_node(ssh_executor, teacher_vmid)
            if not current_node:
                current_node = teacher_optimal_node  # Fallback to expected node
            
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
            
            # Store the actual node where teacher VM ended up (after migration)
            teacher_actual_node = current_node
            
            # No base_qcow2_path in template-less workflow (students get empty disks)
            base_qcow2_path = None
            
            # Create class-base VM (acts as placeholder/reference, not used for overlays in template-less workflow)
            # Use index 99 (last student slot) to keep it within allocated range
            class_base_vmid = class_.vmid_prefix * 100 + 99  # e.g., 234 * 100 + 99 = 23499
            class_base_name = f"{class_prefix}-base"
            
            result.details.append("Creating class-base VM shell (no template)...")
            exit_code, stdout, stderr = ssh_executor.execute(
                f"qm create {class_base_vmid} --name {class_base_name} --memory {memory} --cores {cores} "
                f"--net0 virtio,bridge=vmbr0 "
                f"--scsi0 {PROXMOX_STORAGE_NAME}:{disk_size_gb} --boot order=scsi0",
                timeout=120
            )
            
            if exit_code != 0:
                result.error = f"Failed to create class-base VM shell: {stderr}"
                return result
            
            result.class_base_vmid = class_base_vmid
            result.details.append(f"Class-base VM created: {class_base_vmid} ({class_base_name})")
            
            # Query actual node for class-base VM
            class_base_actual_node = get_vm_current_node(ssh_executor, class_base_vmid)
            if not class_base_actual_node:
                logger.warning(f"Could not determine node for class-base VM {class_base_vmid}, using template_node")
                class_base_actual_node = template_node
            
            # Create VMAssignment for class-base (marked as template VM so it's hidden from students)
            base_mac = get_vm_mac_address(ssh_executor, class_base_vmid)
            class_base_assignment = VMAssignment(
                class_id=class_id,
                proxmox_vmid=class_base_vmid,
                vm_name=class_base_name,
                mac_address=base_mac,
                node=class_base_actual_node,  # Use actual node
                assigned_user_id=None,
                status='reserved',  # CRITICAL: Never assign to students (reserved for class management)
                is_template_vm=True,  # Mark as template so it's hidden from student view
                is_teacher_vm=False,
            )
            db.session.add(class_base_assignment)
        
        result.teacher_vmid = teacher_vmid
        result.details.append(f"Teacher VM created: {teacher_vmid} ({teacher_name})")
        
        # Update progress: teacher VM complete
        if class_.clone_task_id:
            update_clone_progress(
                class_.clone_task_id,
                completed=1 if base_qcow2_path else 0,
                message="Teacher VM created, preparing student VMs..." if pool_size > 0 else "Teacher VM created",
                progress_percent=30
            )
        
        # Use MAC address returned by create_overlay_vm (already retrieved)
        # No need to fetch it again with get_vm_mac_address()
        
        # Create VMAssignment for teacher
        teacher_assignment = VMAssignment(
            class_id=class_id,
            proxmox_vmid=teacher_vmid,
            vm_name=teacher_name,
            mac_address=teacher_mac,
            node=teacher_actual_node,  # Use actual node where VM was created
            assigned_user_id=teacher_id,
            status='assigned',
            is_template_vm=False,
            is_teacher_vm=True,
            assigned_at=datetime.utcnow(),
        )
        db.session.add(teacher_assignment)
        db.session.commit()  # Commit immediately so VMAssignment is available
        logger.info(f"Teacher VM assignment saved: VMID={teacher_vmid}, node={teacher_actual_node}")
        
        # Update VMInventory immediately for teacher VM (makes it visible in UI right away)
        try:
            from app.services.proxmox_service import get_clusters_from_db
            from app.services.inventory_service import persist_vm_inventory, update_vm_status

            # Get cluster_ip from class template, or fallback to default
            cluster_ip = None
            if class_.template and class_.template.cluster_ip:
                cluster_ip = class_.template.cluster_ip
            else:
                # Fallback to default cluster
                cluster_ip = "10.220.15.249"
            
            # Find cluster_id from cluster_ip
            cluster_id = None
            for cluster in get_clusters_from_db():
                if cluster["host"] == cluster_ip:
                    cluster_id = cluster["id"]
                    break
            
            if not cluster_id:
                logger.warning(f"Cannot add teacher VM to VMInventory - cluster not found for IP {cluster_ip}")
            else:
                # persist_vm_inventory expects a list of VM dicts
                teacher_vm_dict = {
                    'cluster_id': cluster_id,
                    'vmid': teacher_vmid,
                    'name': teacher_name,
                    'node': teacher_actual_node,
                    'status': 'stopped',  # VM just created, not started yet
                    'type': 'qemu',
                    'mac_address': teacher_mac,
                    'is_template': False,
                }
                persist_vm_inventory([teacher_vm_dict], cleanup_missing=False)
                logger.info(f"Teacher VM {teacher_vmid} added to VMInventory - visible in UI immediately")
        except Exception as e:
            logger.warning(f"Failed to add teacher VM to VMInventory: {e}")
        
        # Track used VMIDs in memory to avoid collisions during creation
        used_vmids_in_this_session = {teacher_vmid}
        
        # Step 3: Create student VMs as QCOW2 overlays
        if pool_size > 0:
            result.details.append(f"Creating {pool_size} student VMs...")
            
            # Find next available student index by querying existing VMAssignments
            existing_student_vmids = VMAssignment.query.filter(
                VMAssignment.class_id == class_id,
                VMAssignment.is_teacher_vm == False
            ).with_entities(VMAssignment.proxmox_vmid).all()
            
            # Extract indices from VMIDs (e.g., 23405 -> index 5)
            existing_indices = set()
            for (vmid,) in existing_student_vmids:
                index = vmid % 100  # Extract last 2 digits
                if 1 <= index <= 98:  # Valid student range (skip 0=teacher, 99=reserved)
                    existing_indices.add(index)
            
            # Start from highest existing index + 1 (or 1 if no students exist)
            next_student_index = max(existing_indices) + 1 if existing_indices else 1
            logger.info(f"Starting student VM creation from index {next_student_index} (existing indices: {sorted(existing_indices)})")
            
            students_created = 0
            current_index = next_student_index
            
            while students_created < pool_size:
                # Update progress for each student VM
                if class_.clone_task_id:
                    student_progress = 35 + (60 * (students_created / pool_size))  # Progress from 35% to 95%
                    update_clone_progress(
                        class_.clone_task_id,
                        completed=1 + students_created,
                        current_vm=f"student-{current_index}",
                        message=f"Creating student VM {students_created + 1} of {pool_size} (index {current_index})...",
                        progress_percent=student_progress
                    )
                
                # Check if we've hit the limit (indices 1-98 available for students)
                if current_index >= 99:
                    logger.error(f"Class VM limit reached - cannot create more students (need {pool_size - students_created} more)")
                    result.failed += pool_size - students_created
                    break
                
                # Use allocated VMID from class prefix
                vmid = get_vmid_for_class_vm(class_id, current_index)
                if not vmid:
                    logger.error(f"Failed to allocate VMID for student index {current_index}")
                    current_index += 1  # Try next index
                    continue
                
                student_name = f"{class_prefix}-student-{current_index}"
                
                # Select optimal node for this student VM (with simulated load balancing or override)
                if class_.deployment_node:
                    student_optimal_node = class_.deployment_node
                else:
                    student_optimal_node = get_optimal_node(ssh_executor, proxmox, vm_memory_mb=memory, simulated_vms_per_node=simulated_vms_per_node)
                    logger.info(f"Selected optimal node for {student_name}: {student_optimal_node} (current allocation: {simulated_vms_per_node})")
                
                # Determine disk slot based on controller type
                disk_slot = f"{disk_controller_type if base_qcow2_path else 'scsi'}0"
                
                try:
                    # Create VM shell with complete hardware config from template
                    if base_qcow2_path:
                        # Use create_vm_shell to get proper hardware config
                        from app.services.vm_core import create_vm_shell
                        success, error = create_vm_shell(
                            ssh_executor=ssh_executor,
                            vmid=vmid,
                            name=student_name,
                            memory=memory,
                            cores=cores,
                            ostype=ostype,
                            bios=bios,
                            machine=machine,
                            cpu=cpu,
                            scsihw=scsihw,
                            storage=PROXMOX_STORAGE_NAME,
                        )
                        
                        if not success:
                            logger.error(f"Failed to create VM shell {vmid}: {error}")
                            result.failed += 1
                            continue
                        
                        # Add EFI disk if template had one
                        if efi_disk:
                            logger.info(f"Adding EFI disk to student VM {vmid}")
                            efi_cmd = f"qm set {vmid} --efidisk0 {PROXMOX_STORAGE_NAME}:1,efitype=4m,pre-enrolled-keys=1"
                            exit_code, stdout, stderr = ssh_executor.execute(efi_cmd, timeout=30, check=False)
                            if exit_code != 0:
                                logger.warning(f"Failed to add EFI disk to VM {vmid}: {stderr}")
                        
                        # Add TPM if template had one
                        if tpm_state:
                            logger.info(f"Adding TPM to student VM {vmid}")
                            tpm_cmd = f"qm set {vmid} --tpmstate0 {PROXMOX_STORAGE_NAME}:1,version=v2.0"
                            exit_code, stdout, stderr = ssh_executor.execute(tpm_cmd, timeout=30, check=False)
                            if exit_code != 0:
                                logger.warning(f"Failed to add TPM to VM {vmid}: {stderr}")
                    else:
                        # Template-less: create basic VM shell
                        exit_code, stdout, stderr = ssh_executor.execute(
                            f"qm create {vmid} --name {student_name} "
                            f"--memory {memory} --cores {cores} "
                            f"--net0 virtio,bridge=vmbr0 "
                            f"--ostype l26",
                            timeout=60
                        )
                        
                        if exit_code != 0:
                            logger.error(f"Failed to create VM shell {vmid}: {stderr}")
                            result.failed += 1
                            continue
                        
                        # Enable guest agent for template-less VMs too
                        agent_cmd = f"qm set {vmid} --agent enabled=1,fstrim_cloned_disks=1"
                        ssh_executor.execute(agent_cmd, timeout=30, check=False)
                    
                    # Migrate to optimal node BEFORE attaching storage
                    logger.info(f"Migrating student VM {vmid} to {student_optimal_node}...")
                    migration_success, error_msg = migrate_vm_to_node(ssh_executor, vmid, student_optimal_node, timeout=120)
                    if not migration_success:
                        logger.warning(f"Failed to migrate VM {vmid} to {student_optimal_node}: {error_msg}. Continuing on current node.")
                    else:
                        logger.info(f"VM {vmid} migrated to {student_optimal_node}")
                        # Wait briefly for migration to complete
                        import time
                        time.sleep(2)
                    
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
                            f"pvesh set /nodes/{current_node}/qemu/{vmid}/config -{disk_slot} {PROXMOX_STORAGE_NAME}:{vmid}/vm-{vmid}-disk-0.qcow2 -boot c -bootdisk {disk_slot}",
                            timeout=60
                        )
                    else:
                        # No template - create VM with empty disk
                        # Get VM's current node after migration
                        current_node = get_vm_current_node(ssh_executor, vmid)
                        if not current_node:
                            current_node = optimal_node  # Fallback
                        
                        # Use scsi0 for template-less VMs (default)
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
                    # Check if error is VMID collision
                    error_str = str(e).lower()
                    if 'already exists' in error_str or ('vmid' in error_str and 'exist' in error_str):
                        logger.warning(f"VMID {vmid} collision detected - auto-incrementing to next index")
                        current_index += 1  # Try next index
                        continue  # Don't increment students_created or failed counter
                    else:
                        logger.exception(f"Error creating student VM {vmid}: {e}")
                        result.failed += 1
                        current_index += 1  # Move to next index
                        continue
                
                # Successfully created student VM - increment counters
                students_created += 1
                current_index += 1  # Move to next index for next iteration
                logger.info(f"Student VM progress: {students_created}/{pool_size} complete")
        
        # Step 4: Start VMs if requested
        if auto_start and result.teacher_vmid:
            result.details.append("Starting VMs...")
            ssh_executor.execute(f"qm start {result.teacher_vmid}", check=False)
            for vmid in result.student_vmids:
                ssh_executor.execute(f"qm start {vmid}", check=False)
        
        # Commit all VMAssignment records
        db.session.commit()
        
        # Final progress update
        if class_.clone_task_id:
            update_clone_progress(
                class_.clone_task_id,
                status="completed",
                message=f"Deployment complete: {len(result.student_vmids)} student VMs created",
                progress_percent=100
            )
        
        # Update VMInventory with MAC addresses for all created VMs
        try:
            from app.services.proxmox_service import get_clusters_from_db
            from app.services.inventory_service import persist_vm_inventory

            # Get cluster_id
            cluster_ip = None
            if class_.template and class_.template.cluster_ip:
                cluster_ip = class_.template.cluster_ip
            else:
                cluster_ip = "10.220.15.249"
            
            cluster_id = None
            for cluster in get_clusters_from_db():
                if cluster["host"] == cluster_ip:
                    cluster_id = cluster["id"]
                    break
            
            if cluster_id:
                vms_to_sync = []
                
                # Add teacher VM
                if result.teacher_vmid and teacher_mac:
                    vms_to_sync.append({
                        'cluster_id': cluster_id,
                        'vmid': result.teacher_vmid,
                        'name': teacher_name,
                        'node': teacher_actual_node if teacher_actual_node else template_node,
                        'status': 'stopped',
                        'type': 'qemu',
                        'mac_address': teacher_mac,
                        'is_template': False,
                    })
                
                # Add student VMs
                for i, vmid in enumerate(result.student_vmids):
                    assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
                    if assignment:
                        vms_to_sync.append({
                            'cluster_id': cluster_id,
                            'vmid': vmid,
                            'name': assignment.vm_name,
                            'node': assignment.node,
                            'status': 'stopped',
                            'type': 'qemu',
                            'mac_address': assignment.mac_address,
                            'is_template': False,
                        })
                
                # Batch persist all VMs
                if vms_to_sync:
                    persist_vm_inventory(vms_to_sync, cleanup_missing=False)
                    logger.info(f"Updated VMInventory for {len(vms_to_sync)} VMs - now visible in UI")
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
        ~VMAssignment.is_template_vm,
    ).all()
    
    if not student_assignments:
        return True, "No student VMs to update", 0
    
    ssh_executor = None
    updated_count = 0
    
    try:
        ssh_executor = get_pooled_ssh_executor_from_config()
        # Pooled executor is already connected
        
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
        success, error, disk_controller_type, ostype, bios, machine, cpu, scsihw, memory, cores, sockets, efi_disk, tpm_state = export_template_to_qcow2(
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


def recreate_teacher_vm_from_base(class_id: int, class_name: str) -> tuple[bool, str, Optional[int]]:
    """
    Recreate teacher VM from class base QCOW2 (used for reimage operation).
    
    Args:
        class_id: Database ID of the class
        class_name: Name of the class
        
    Returns:
        Tuple of (success, message, new_vmid)
    """
    from app.models import Class, VMAssignment, db
    from app.services.vm_template import create_overlay_vm
    from app.services.vm_utils import sanitize_vm_name, get_vm_current_node
    
    try:
        class_ = Class.query.get(class_id)
        if not class_:
            return False, f"Class {class_id} not found", None
        
        if not class_.vmid_prefix:
            return False, "Class has no VMID prefix allocated", None
        
        # Get class base QCOW2 path
        class_prefix = sanitize_vm_name(class_name)
        if not class_prefix:
            class_prefix = f"class-{class_id}"
        
        base_qcow2_path = f"{DEFAULT_TEMPLATE_STORAGE_PATH}/{class_prefix}-base.qcow2"
        
        # Get template specs (if available)
        memory = class_.memory_mb or 4096
        cores = class_.cpu_cores or 2
        
        # Determine node
        cluster_ip = class_.template.cluster_ip if class_.template else None
        if not cluster_ip:
            from app.services.proxmox_service import get_clusters_from_db
            if get_clusters_from_db():
                cluster_ip = get_clusters_from_db()[0]["host"]
            else:
                return False, "No clusters configured", None
        
        # Connect to SSH
        ssh_executor = get_cached_ssh_executor()
        ssh_executor.connect()
        
        # Allocate VMID (use index 0 = teacher)
        teacher_vmid = class_.vmid_prefix * 100  # e.g., 234 * 100 = 23400
        teacher_name = f"{class_prefix}-teacher"
        
        logger.info(f"Recreating teacher VM {teacher_vmid} from class base {base_qcow2_path}")
        
        # Get node from existing class-base VM or use default
        from app.services.proxmox_service import get_proxmox_admin_for_cluster
        proxmox = get_proxmox_admin_for_cluster(cluster_ip)
        
        # Find class-base VM to determine node
        class_base_vmid = class_.vmid_prefix * 100 + 99
        node = None
        try:
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                if r.get('vmid') == class_base_vmid:
                    node = r.get('node')
                    break
        except Exception:
            pass
        
        if not node:
            # Fallback to first available node
            from app.services.vm_utils import get_optimal_node
            node = get_optimal_node(ssh_executor, proxmox, vm_memory_mb=memory)
        
        # Get template specs from class-base VM if available
        disk_controller_type = "scsi"
        ostype = "l26"
        bios = "seabios"
        machine = None
        cpu = "host"
        scsihw = "virtio-scsi-single"
        efi_disk = None
        tpm_state = None
        
        try:
            config = proxmox.nodes(node).qemu(class_base_vmid).config.get()
            # Extract specs from class-base VM config
            if 'ostype' in config:
                ostype = config['ostype']
            if 'bios' in config:
                bios = config['bios']
            if 'machine' in config:
                machine = config['machine']
            if 'cpu' in config:
                cpu = config['cpu']
            if 'scsihw' in config:
                scsihw = config['scsihw']
            # Check for disk controller type
            for key in config.keys():
                if key.startswith('scsi'):
                    disk_controller_type = "scsi"
                    break
                elif key.startswith('virtio'):
                    disk_controller_type = "virtio"
                    break
                elif key.startswith('sata'):
                    disk_controller_type = "sata"
                    break
                elif key.startswith('ide'):
                    disk_controller_type = "ide"
                    break
        except Exception as e:
            logger.warning(f"Could not get class-base VM config: {e}, using defaults")
        
        # Create teacher VM as overlay
        success, error, mac_address = create_overlay_vm(
            ssh_executor=ssh_executor,
            vmid=teacher_vmid,
            name=teacher_name,
            base_qcow2_path=base_qcow2_path,
            node=node,
            memory=memory,
            cores=cores,
            disk_controller_type=disk_controller_type,
            ostype=ostype,
            bios=bios,
            machine=machine,
            cpu=cpu,
            scsihw=scsihw,
            efi_disk=efi_disk,
            tpm_state=tpm_state,
            boot_order=None,  # No boot order available in this context
            net_model='virtio',  # Default network model
        )
        
        if not success:
            ssh_executor.disconnect()
            return False, f"Failed to create teacher VM: {error}", None
        
        # Query actual node
        actual_node = get_vm_current_node(ssh_executor, teacher_vmid)
        if not actual_node:
            actual_node = node
        
        # Create VMAssignment record
        assignment = VMAssignment(
            proxmox_vmid=teacher_vmid,
            vm_name=teacher_name,
            node=actual_node,
            class_id=class_id,
            assigned_user_id=class_.teacher_id,
            status='available',
            is_teacher_vm=True,
            is_template_vm=False,
            mac_address=mac_address
        )
        db.session.add(assignment)
        db.session.commit()
        
        ssh_executor.disconnect()
        
        logger.info(f"Teacher VM {teacher_vmid} recreated successfully on node {actual_node}")
        return True, "Teacher VM recreated successfully", teacher_vmid
        
    except Exception as e:
        logger.exception(f"Failed to recreate teacher VM: {e}")
        if 'db' in locals():
            db.session.rollback()
        return False, str(e), None


def save_teacher_vm_to_base(class_id: int, teacher_vmid: int, teacher_node: str, class_name: str) -> tuple[bool, str]:
    """
    Export teacher VM disk to staging QCOW2 file (class-base-staging.qcow2).
    
    This creates a new backing file for student overlays WITHOUT affecting current students.
    Students are NOT affected until "Push to VMs" rebases their overlays.
    
    Workflow:
    1. Export teacher VM disk → class-{prefix}-staging.qcow2
    2. Students still use class-{prefix}-base.qcow2 (unchanged)
    3. "Push to VMs" will rebase student overlays from old base to staging
    
    Args:
        class_id: Database ID of the class
        teacher_vmid: VMID of the teacher's working VM
        teacher_node: Node where teacher VM resides
        class_name: Name of the class
        
    Returns:
        Tuple of (success, message)
    """
    from app.models import Class
    from app.services.vm_utils import sanitize_vm_name
    
    try:
        class_ = Class.query.get(class_id)
        if not class_:
            return False, f"Class {class_id} not found"
        
        # Get paths
        class_prefix = sanitize_vm_name(class_name)
        if not class_prefix:
            class_prefix = f"class-{class_id}"
        
        staging_qcow2_path = f"{DEFAULT_TEMPLATE_STORAGE_PATH}/{class_prefix}-staging.qcow2"
        
        logger.info(f"Exporting teacher VM {teacher_vmid} disk to staging QCOW2: {staging_qcow2_path}")
        
        # Connect to SSH
        ssh_executor = get_cached_ssh_executor()
        ssh_executor.connect()
        
        # Step 1: Find teacher VM disk
        find_cmd = f"qm config {teacher_vmid} | grep -E '^(scsi|virtio|sata|ide)[0-9]+:' | head -1"
        exit_code, disk_line, stderr = ssh_executor.execute(find_cmd, check=False)
        
        if exit_code != 0 or not disk_line:
            ssh_executor.disconnect()
            return False, f"Could not find disk for teacher VM {teacher_vmid}"
        
        # Parse disk spec
        parts = disk_line.split(':', 1)
        if len(parts) < 2:
            ssh_executor.disconnect()
            return False, f"Could not parse disk config: {disk_line}"
        
        disk_spec = parts[1].strip().split(',')[0]  # Remove options like ,size=32G
        
        # Convert to path
        if ':' in disk_spec:
            storage, disk_id = disk_spec.split(':', 1)
            teacher_disk_path = f"/mnt/pve/{storage}/images/{teacher_vmid}/{disk_id}.qcow2"
        else:
            teacher_disk_path = disk_spec
        
        logger.info(f"Teacher VM disk: {teacher_disk_path}")
        
        # Step 2: Remove old staging file if exists
        ssh_executor.execute(f"rm -f {staging_qcow2_path}", check=False)
        
        # Step 3: Export teacher VM disk to staging (collapse any layers to flat image)
        logger.info(f"Exporting teacher VM disk to staging (this may take a few minutes)")
        export_cmd = f"qemu-img convert -f qcow2 -O qcow2 {teacher_disk_path} {staging_qcow2_path}"
        exit_code, stdout, stderr = ssh_executor.execute(export_cmd, check=False, timeout=600)
        
        if exit_code != 0:
            ssh_executor.disconnect()
            return False, f"Failed to export teacher VM disk: {stderr}"
        
        ssh_executor.disconnect()
        
        logger.info(f"Successfully exported teacher VM {teacher_vmid} to staging QCOW2")
        return True, "Template saved to staging. Students not affected. Click 'Push to VMs' to update students."
        
    except Exception as e:
        logger.exception(f"Failed to save teacher VM to staging: {e}")
        return False, str(e)


def push_staging_to_students(class_id: int, class_name: str) -> tuple[bool, str, int]:
    """
    Push staging template to all student VMs by rebasing their overlays.
    
    This is FAST because it only changes overlay backing files, no data copying.
    
    Workflow:
    1. Stop all student VMs
    2. Swap: class-base.qcow2 ← class-staging.qcow2 (staging becomes new base)
    3. Rebase all student overlays to point to new base
    4. Student VMs now use teacher's updated template
    
    Args:
        class_id: Database ID of the class
        class_name: Name of the class
        
    Returns:
        Tuple of (success, message, updated_count)
    """
    from app.models import Class, VMAssignment
    from app.services.vm_utils import sanitize_vm_name
    
    try:
        class_ = Class.query.get(class_id)
        if not class_:
            return False, f"Class {class_id} not found", 0
        
        # Get paths
        class_prefix = sanitize_vm_name(class_name)
        if not class_prefix:
            class_prefix = f"class-{class_id}"
        
        base_qcow2_path = f"{DEFAULT_TEMPLATE_STORAGE_PATH}/{class_prefix}-base.qcow2"
        staging_qcow2_path = f"{DEFAULT_TEMPLATE_STORAGE_PATH}/{class_prefix}-staging.qcow2"
        old_base_backup = f"{DEFAULT_TEMPLATE_STORAGE_PATH}/{class_prefix}-base.old"
        
        # Check if staging exists
        ssh_executor = get_cached_ssh_executor()
        ssh_executor.connect()
        
        check_cmd = f"test -f {staging_qcow2_path} && echo 'exists' || echo 'not_found'"
        exit_code, stdout, _ = ssh_executor.execute(check_cmd, check=False)
        
        if stdout.strip() != 'exists':
            ssh_executor.disconnect()
            return False, "No staging template found. Click 'Save Template' first.", 0
        
        # Get all student VMs (exclude teacher and class-base)
        student_vms = VMAssignment.query.filter_by(
            class_id=class_id,
            is_teacher_vm=False,
            is_template_vm=False
        ).all()
        
        if not student_vms:
            ssh_executor.disconnect()
            return True, "No student VMs to update", 0
        
        logger.info(f"Pushing staging template to {len(student_vms)} student VMs")
        
        # Step 1: Stop all student VMs
        logger.info("Stopping all student VMs")
        for vm in student_vms:
            ssh_executor.execute(f"qm stop {vm.proxmox_vmid}", check=False, timeout=60)
        
        # Wait for all to stop
        import time
        time.sleep(10)
        
        # Step 2: Backup old base and swap staging → base
        logger.info(f"Swapping staging → base: {staging_qcow2_path} → {base_qcow2_path}")
        ssh_executor.execute(f"mv {base_qcow2_path} {old_base_backup}", check=False)
        ssh_executor.execute(f"mv {staging_qcow2_path} {base_qcow2_path}", check=True)
        
        # Step 3: Rebase all student overlays to new base (FAST - no data copy!)
        updated_count = 0
        for vm in student_vms:
            try:
                # Find student VM disk
                find_cmd = f"qm config {vm.proxmox_vmid} | grep -E '^(scsi|virtio|sata|ide)[0-9]+:' | head -1"
                exit_code, disk_line, _ = ssh_executor.execute(find_cmd, check=False)
                
                if exit_code != 0 or not disk_line:
                    logger.warning(f"Could not find disk for VM {vm.proxmox_vmid}")
                    continue
                
                # Parse disk spec
                parts = disk_line.split(':', 1)
                if len(parts) < 2:
                    continue
                
                disk_spec = parts[1].strip().split(',')[0]
                
                # Convert to path
                if ':' in disk_spec:
                    storage, disk_id = disk_spec.split(':', 1)
                    student_disk_path = f"/mnt/pve/{storage}/images/{vm.proxmox_vmid}/{disk_id}.qcow2"
                else:
                    student_disk_path = disk_spec
                
                # Rebase overlay to new base (FAST!)
                logger.info(f"Rebasing VM {vm.proxmox_vmid} overlay to new base")
                rebase_cmd = f"qemu-img rebase -u -f qcow2 -b {base_qcow2_path} {student_disk_path}"
                exit_code, _, stderr = ssh_executor.execute(rebase_cmd, check=False, timeout=30)
                
                if exit_code == 0:
                    updated_count += 1
                else:
                    logger.error(f"Failed to rebase VM {vm.proxmox_vmid}: {stderr}")
                    
            except Exception as e:
                logger.error(f"Error processing VM {vm.proxmox_vmid}: {e}")
        
        # Clean up old base backup
        ssh_executor.execute(f"rm -f {old_base_backup}", check=False)
        
        ssh_executor.disconnect()
        
        logger.info(f"Successfully pushed staging to {updated_count}/{len(student_vms)} student VMs")
        return True, f"Updated {updated_count} student VMs with new template", updated_count
        
    except Exception as e:
        logger.exception(f"Failed to push staging to students: {e}")
        # Try to restore old base
        try:
            if 'ssh_executor' in locals() and ssh_executor:
                ssh_executor.execute(f"mv {old_base_backup} {base_qcow2_path}", check=False)
                ssh_executor.disconnect()
        except Exception:
            pass
        return False, str(e), 0


def recreate_student_vms_with_new_specs(class_id: int, cpu_cores: int, memory_mb: int) -> None:
    """
    Recreate all student VMs in a class with new hardware specifications.
    
    This function deletes existing student VMs (excluding teacher VM) and recreates
    them with updated CPU and RAM specs. The recreation maintains the same VM IDs
    and overlay structure from the class base template.
    
    Args:
        class_id: Database ID of the class
        cpu_cores: New CPU core count for VMs
        memory_mb: New memory in MB for VMs
    """
    from app import create_app
    from app.services.proxmox_service import get_proxmox_admin_for_cluster
    
    app = create_app()
    
    with app.app_context():
        try:
            logger.info(f"Starting VM recreation for class {class_id} with {cpu_cores} cores and {memory_mb}MB RAM")
            
            # Get class details
            class_obj = Class.query.get(class_id)
            if not class_obj:
                logger.error(f"Class {class_id} not found")
                return
            
            # Get student VMs (exclude teacher VM which is index 0)
            student_vms = VMAssignment.query.filter_by(
                class_id=class_id,
                status='assigned'
            ).filter(
                VMAssignment.vm_name.notlike('%teacher%')
            ).all()
            
            if not student_vms:
                logger.info(f"No student VMs found for class {class_id}")
                return
            
            logger.info(f"Found {len(student_vms)} student VMs to recreate")
            
            # Get SSH connection
            ssh_executor = get_pooled_ssh_executor_from_config()
            
            # Get Proxmox connection
            cluster_id = class_obj.deployment_cluster
            if not cluster_id:
                logger.error(f"No deployment cluster configured for class {class_id}")
                return
            
            proxmox = get_proxmox_admin_for_cluster(int(cluster_id))
            
            # Get class base QCOW2 path
            class_prefix = sanitize_vm_name(class_obj.name)
            if not class_prefix:
                class_prefix = f"class-{class_id}"
            
            base_qcow2_path = f"{DEFAULT_TEMPLATE_STORAGE_PATH}/{class_prefix}-base.qcow2"
            
            # Verify base template exists
            exit_code, _, _ = ssh_executor.execute(f"test -f {base_qcow2_path}", check=False)
            if exit_code != 0:
                logger.error(f"Base template not found at {base_qcow2_path}")
                return
            
            # Process each student VM
            recreated_count = 0
            for vm in student_vms:
                try:
                    vmid = vm.proxmox_vmid
                    node = vm.node
                    
                    if not node:
                        logger.warning(f"VM {vmid} has no node assignment, skipping")
                        continue
                    
                    logger.info(f"Recreating VM {vmid} on node {node}")
                    
                    # Stop VM if running
                    try:
                        status = proxmox.nodes(node).qemu(vmid).status.current.get()
                        if status.get('status') == 'running':
                            logger.info(f"Stopping VM {vmid}")
                            proxmox.nodes(node).qemu(vmid).status.shutdown.post()
                            wait_for_vm_stopped(ssh_executor, vmid, node, timeout=120)
                    except Exception as e:
                        logger.warning(f"Could not check/stop VM {vmid}: {e}")
                    
                    # Delete existing VM
                    logger.info(f"Deleting VM {vmid}")
                    try:
                        proxmox.nodes(node).qemu(vmid).delete()
                    except Exception as e:
                        logger.error(f"Failed to delete VM {vmid}: {e}")
                        continue
                    
                    # Wait a moment for deletion to complete
                    import time
                    time.sleep(2)
                    
                    # Recreate VM with new specs
                    student_name = vm.vm_name or f"{class_prefix}-student-{vmid}"
                    
                    # Create VM shell with new specs
                    logger.info(f"Creating new VM {vmid} with {cpu_cores} cores and {memory_mb}MB RAM")
                    exit_code, stdout, stderr = ssh_executor.execute(
                        f"qm create {vmid} --name {student_name} --memory {memory_mb} --cores {cpu_cores} "
                        f"--net0 virtio,bridge=vmbr0",
                        timeout=60
                    )
                    
                    if exit_code != 0:
                        logger.error(f"Failed to create VM shell {vmid}: {stderr}")
                        continue
                    
                    # Remove the default disk that qm create might add
                    ssh_executor.execute(f"rm -rf {DEFAULT_VM_IMAGES_PATH}/{vmid}", check=False)
                    
                    # Create overlay disk pointing to class base
                    overlay_path = f"{DEFAULT_VM_IMAGES_PATH}/{vmid}/vm-{vmid}-disk-0.qcow2"
                    ssh_executor.execute(f"mkdir -p {DEFAULT_VM_IMAGES_PATH}/{vmid}", check=False)
                    
                    logger.info(f"Creating overlay disk for VM {vmid}")
                    exit_code, stdout, stderr = ssh_executor.execute(
                        f"qemu-img create -f qcow2 -F qcow2 -b {base_qcow2_path} {overlay_path}",
                        timeout=120
                    )
                    
                    if exit_code != 0:
                        logger.error(f"Failed to create overlay for VM {vmid}: {stderr}")
                        continue
                    
                    # Attach disk to VM
                    exit_code, stdout, stderr = ssh_executor.execute(
                        f"qm set {vmid} --scsi0 {overlay_path}",
                        timeout=60
                    )
                    
                    if exit_code != 0:
                        logger.error(f"Failed to attach disk to VM {vmid}: {stderr}")
                        continue
                    
                    # Set boot order
                    ssh_executor.execute(f"qm set {vmid} --boot order=scsi0", check=False)
                    
                    recreated_count += 1
                    logger.info(f"Successfully recreated VM {vmid}")
                    
                except Exception as e:
                    logger.error(f"Error recreating VM {vmid}: {e}", exc_info=True)
                    continue
            
            logger.info(f"VM recreation complete: {recreated_count}/{len(student_vms)} VMs successfully recreated")
            
        except Exception as e:
            logger.error(f"Fatal error during VM recreation: {e}", exc_info=True)

