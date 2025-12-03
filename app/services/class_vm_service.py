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

logger = logging.getLogger(__name__)

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


def get_next_available_vmid(ssh_executor: SSHExecutor, start_vmid: int = 100) -> int:
    """Get the next available VMID in Proxmox."""
    exit_code, stdout, stderr = ssh_executor.execute(
        f"pvesh get /cluster/nextid --vmid {start_vmid}",
        check=False
    )
    
    if exit_code == 0 and stdout.strip().isdigit():
        return int(stdout.strip())
    
    # Fallback: increment and check
    vmid = start_vmid
    while True:
        exit_code, stdout, stderr = ssh_executor.execute(
            f"qm status {vmid}",
            check=False
        )
        if exit_code != 0:  # VMID doesn't exist
            return vmid
        vmid += 1


def get_vm_mac_address(ssh_executor: SSHExecutor, vmid: int) -> Optional[str]:
    """Get the MAC address of a VM's first network interface.
    
    Args:
        ssh_executor: SSH executor
        vmid: VM ID
        
    Returns:
        MAC address string or None
    """
    try:
        exit_code, stdout, stderr = ssh_executor.execute(
            f"qm config {vmid}",
            check=False,
            timeout=10
        )
        
        if exit_code == 0 and stdout:
            # Look for net0 line and extract MAC
            # Format can be: "net0: virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0"
            # Or with generated MAC: "net0: virtio=12:34:56:78:9A:BC,bridge=vmbr0,firewall=1"
            for line in stdout.split('\n'):
                if line.startswith('net0:'):
                    # Extract everything after 'net0:'
                    net_config = line.split(':', 1)[1].strip()
                    # Look for MAC address pattern (XX:XX:XX:XX:XX:XX)
                    import re
                    mac_match = re.search(r'([0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2})', net_config)
                    if mac_match:
                        return mac_match.group(1).upper()
        
        return None
    except Exception as e:
        logger.warning(f"Failed to get MAC address for VM {vmid}: {e}")
        return None


def sanitize_vm_name(name: str) -> str:
    """Sanitize a string for use as a Proxmox VM name."""
    # Convert to lowercase, replace spaces and underscores with hyphens
    name = name.lower().replace(' ', '-').replace('_', '-')
    # Remove any non-alphanumeric characters except hyphens
    name = re.sub(r'[^a-z0-9-]', '', name)
    # Remove consecutive hyphens
    name = re.sub(r'-+', '-', name)
    # Remove leading/trailing hyphens
    name = name.strip('-')
    return name or 'vm'


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


def wait_for_vm_stopped(ssh_executor: SSHExecutor, vmid: int, timeout: int = None) -> bool:
    """
    Wait for a VM to fully stop.
    
    Polls the VM status until it's stopped or timeout is reached.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VMID to check
        timeout: Max time to wait in seconds (default: VM_STOP_TIMEOUT)
        
    Returns:
        True if VM is stopped, False if timeout reached
    """
    if timeout is None:
        timeout = VM_STOP_TIMEOUT
    
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
        
        time.sleep(VM_STOP_POLL_INTERVAL)
    
    logger.warning(f"Timeout waiting for VM {vmid} to stop")
    return False


def get_next_available_vmid(ssh_executor: SSHExecutor, start: int = 200) -> int:
    """
    Find the next available VMID on the Proxmox cluster.
    
    Args:
        ssh_executor: SSH executor for running commands
        start: Starting VMID to search from
        
    Returns:
        Next available VMID
    """
    vmid = start
    max_attempts = 1000
    
    for _ in range(max_attempts):
        try:
            exit_code, stdout, stderr = ssh_executor.execute(
                f"qm status {vmid}",
                check=False,
                timeout=10
            )
            if exit_code != 0:
                # VMID not in use
                return vmid
            vmid += 1
        except Exception:
            # Error checking - assume available
            return vmid
    
    raise RuntimeError(f"Could not find available VMID after {max_attempts} attempts starting from {start}")


def export_template_to_qcow2(
    ssh_executor: SSHExecutor,
    template_vmid: int,
    node: str,
    output_path: str,
    disk: str = "scsi0",
) -> Tuple[bool, str]:
    """
    Export a Proxmox template's disk to a QCOW2 file.
    
    This creates a base QCOW2 file that can be used as a backing file
    for overlay disks.
    
    Args:
        ssh_executor: SSH executor for running commands
        template_vmid: VMID of the source template
        node: Proxmox node name where template resides
        output_path: Full path for the output QCOW2 file
        disk: Disk to export (default: scsi0)
        
    Returns:
        Tuple of (success, error_message)
    """
    logger.info(f"Exporting template {template_vmid} disk {disk} to {output_path}")
    
    try:
        # First, get the disk path from the template config
        exit_code, stdout, stderr = ssh_executor.execute(
            f"qm config {template_vmid}",
            timeout=30
        )
        
        if exit_code != 0:
            return False, f"Failed to get template config: {stderr}"
        
        # Parse the config to find the disk
        disk_line = None
        for line in stdout.split('\n'):
            if line.startswith(f"{disk}:"):
                disk_line = line
                break
        
        if not disk_line:
            return False, f"Disk {disk} not found in template {template_vmid}"
        
        # Extract storage and volume info
        # Format: scsi0: TRUENAS-NFS:100/vm-100-disk-0.qcow2,size=32G
        match = re.search(r'([^:]+):([^,]+)', disk_line.split(':', 1)[1])
        if not match:
            return False, f"Could not parse disk line: {disk_line}"
        
        storage = match.group(1).strip()
        volume = match.group(2).strip()
        
        logger.info(f"Found disk on storage {storage}, volume {volume}")
        
        # Create output directory if needed
        output_dir = '/'.join(output_path.rsplit('/', 1)[:-1]) if '/' in output_path else '.'
        ssh_executor.execute(f"mkdir -p {output_dir}", check=False)
        
        # Use qemu-img to convert/copy the disk to a standalone QCOW2
        # We need to find the actual disk path
        # For NFS storage, it's typically /mnt/pve/<storage>/images/<vmid>/<volume>
        source_path = f"/mnt/pve/{storage}/images/{template_vmid}/{volume.split('/')[-1] if '/' in volume else f'vm-{template_vmid}-disk-0.qcow2'}"
        
        logger.info(f"Converting {source_path} to {output_path}")
        
        exit_code, stdout, stderr = ssh_executor.execute(
            f"qemu-img convert -O qcow2 {source_path} {output_path}",
            timeout=600  # 10 minutes for large disks
        )
        
        if exit_code != 0:
            return False, f"qemu-img convert failed: {stderr}"
        
        # Set proper permissions
        ssh_executor.execute(f"chmod 644 {output_path}", check=False)
        
        logger.info(f"Successfully exported template to {output_path}")
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error exporting template: {e}")
        return False, str(e)


def create_class_vms(
    class_id: int,
    template_vmid: Optional[int],  # Can be None for template-less classes
    template_node: str,
    pool_size: int,
    class_name: str,
    teacher_id: int,
    memory: int = 4096,
    cores: int = 2,
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
    try:
        # Connect to Proxmox via SSH
        ssh_executor = get_ssh_executor_from_config()
        ssh_executor.connect()
        logger.info(f"SSH connected to {ssh_executor.host}")
        
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
            
            # Step 2: Create teacher VM (full clone)
            result.details.append("Creating teacher VM...")
            teacher_vmid = get_next_available_vmid(ssh_executor)
            teacher_name = f"{class_prefix}-teacher"
            
            exit_code, stdout, stderr = ssh_executor.execute(
                f"qm clone {template_vmid} {teacher_vmid} --name {teacher_name} --full",
                timeout=300
            )
            
            if exit_code != 0:
                result.error = f"Failed to create teacher VM: {stderr}"
                return result
            
        else:
            # WITHOUT TEMPLATE: Create empty VM shells
            result.details.append("Creating teacher VM shell (no template)...")
            teacher_vmid = get_next_available_vmid(ssh_executor)
            teacher_name = f"{class_prefix}-teacher"
            
            # Create empty VM with custom specs and disk
            exit_code, stdout, stderr = ssh_executor.execute(
                f"qm create {teacher_vmid} --name {teacher_name} --memory {memory} --cores {cores} "
                f"--net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-pci "
                f"--scsi0 {PROXMOX_STORAGE_NAME}:32 --boot order=scsi0",
                timeout=120
            )
            
            if exit_code != 0:
                result.error = f"Failed to create teacher VM shell: {stderr}"
                return result
            
            result.details.append(f"Teacher VM shell created with 32GB disk")
            
            # Create class-base VM (empty shell for student overlays)
            class_base_vmid = get_next_available_vmid(ssh_executor, teacher_vmid + 1)
            class_base_name = f"{class_prefix}-base"
            
            result.details.append(f"Creating class-base VM shell (no template)...")
            exit_code, stdout, stderr = ssh_executor.execute(
                f"qm create {class_base_vmid} --name {class_base_name} --memory {memory} --cores {cores} "
                f"--net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-pci "
                f"--scsi0 {PROXMOX_STORAGE_NAME}:32 --boot order=scsi0",
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
        
        # Get MAC address for teacher VM
        teacher_mac = get_vm_mac_address(ssh_executor, teacher_vmid)
        
        # Create VMAssignment for teacher
        teacher_assignment = VMAssignment(
            class_id=class_id,
            proxmox_vmid=teacher_vmid,
            vm_name=teacher_name,
            mac_address=teacher_mac,
            node=template_node,
            assigned_user_id=teacher_id,
            status='assigned',
            is_template_vm=False,
            is_teacher_vm=True,
            assigned_at=datetime.utcnow(),
        )
        db.session.add(teacher_assignment)
        
        # Step 3: Create student VMs as QCOW2 overlays
        if pool_size > 0:
            result.details.append(f"Creating {pool_size} student VMs...")
            
            # Get starting VMID for students
            student_start_vmid = get_next_available_vmid(ssh_executor, teacher_vmid + 1)
            
            for i in range(pool_size):
                vmid = student_start_vmid + i
                student_name = f"{class_prefix}-student-{i + 1}"
                
                try:
                    # Create VM shell
                    exit_code, stdout, stderr = ssh_executor.execute(
                        f"qm create {vmid} --name {student_name} --memory {memory} --cores {cores} "
                        f"--scsihw virtio-scsi-pci --net0 virtio,bridge=vmbr0",
                        timeout=60
                    )
                    
                    if exit_code != 0:
                        logger.error(f"Failed to create VM shell {vmid}: {stderr}")
                        result.failed += 1
                        continue
                    
                    # Create overlay disk
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
                    
                    # Attach disk to VM
                    # Use storage:vmid/disk format for Proxmox
                    exit_code, stdout, stderr = ssh_executor.execute(
                        f"qm set {vmid} --scsi0 {PROXMOX_STORAGE_NAME}:{vmid}/vm-{vmid}-disk-0.qcow2 --boot c --bootdisk scsi0",
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
                    
                    # Create VMAssignment for student VM (unassigned)
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
