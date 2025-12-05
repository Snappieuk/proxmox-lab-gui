#!/usr/bin/env python3
"""
Proxmox Operations - VM cloning, template management, and snapshots.

This module provides higher-level operations for class-based VM management
using the Proxmox API. It is intended for API-based VM operations.

REFACTORING NOTE (2024):
For SSH-based VM operations (qm/qemu-img commands), see:
- app/services/vm_core.py       - Low-level VM operations (create, destroy, disks)
- app/services/vm_template.py   - Template export and overlay generation
- app/services/vm_utils.py      - Utility functions (naming, VMID lookup, MAC)

The SSH-based approach in those modules is preferred for disk/overlay operations
per the repository's documented deployment strategy (disk export + overlay).

This module (proxmox_operations.py) retains API-based functions for:
- Legacy workflows that use Proxmox API cloning
- Cluster orchestration (replicate_templates_to_all_nodes)
- Functions called by existing code that expects API-based operations

When adding new VM functionality, prefer using vm_core.py/vm_template.py
with SSHExecutor for direct qm/qemu-img operations.
"""

import logging
import re
from typing import List, Dict, Tuple, Any, Optional

from app.services.proxmox_service import get_proxmox_admin_for_cluster
from app.config import CLUSTERS
from app.models import VMInventory

logger = logging.getLogger(__name__)

# Re-export sanitize_vm_name from vm_utils for backward compatibility
# New code should import from vm_utils directly
from app.services.vm_utils import sanitize_vm_name  # noqa: F401

# Default cluster IP for class operations (restricted to single cluster)
CLASS_CLUSTER_IP = "10.220.15.249"


def get_node_specific_template_vmid(node_id: int, template_base_id: int) -> int:
    """Calculate node-specific template VMID to avoid cross-node conflicts.
    
    Uses scheme: 9000 + (node_id * 100) + template_base_id
    Example: Node 1, Template 5 = 9000 + 100 + 5 = 9105
    
    Args:
        node_id: Node ID (1-based)
        template_base_id: Base template ID (typically 1-99)
        
    Returns:
        Node-specific VMID
    """
    return 9000 + (node_id * 100) + template_base_id


def wait_for_clone_completion(proxmox, node: str, vmid: int, timeout: int = 300) -> bool:
    """Wait for VM clone to complete and verify disk exists.
    
    Monitors clone lock and ensures disk is present after cloning.
    
    Args:
        proxmox: ProxmoxAPI instance
        node: Node name
        vmid: VM ID to monitor
        timeout: Maximum wait time in seconds
        
    Returns:
        True if clone completed successfully
        
    Raises:
        TimeoutError: If clone doesn't complete within timeout
        Exception: If clone fails or disk missing
    """
    import time
    
    start = time.time()
    waited = 0
    last_log = 0
    first_check = True
    
    logger.info(f"Starting wait for VM {vmid} clone completion (timeout: {timeout}s)")
    
    while waited < timeout:
        try:
            config = proxmox.nodes(node).qemu(vmid).config.get()
            
            # Check if still cloning
            if config.get('lock') == 'clone':
                if first_check:
                    logger.info(f"VM {vmid} has clone lock, waiting for completion...")
                    first_check = False
                elif waited - last_log >= 30:  # Log every 30 seconds
                    logger.info(f"VM {vmid} still cloning... ({waited}s / {timeout}s)")
                    last_log = waited
                time.sleep(3)
                waited = int(time.time() - start)
                continue
            
            # Clone lock released - verify disk exists
            disk_keys = ['scsi0', 'virtio0', 'sata0', 'ide0']
            found_disks = [key for key in disk_keys if key in config]
            
            if found_disks:
                logger.info(f"VM {vmid} clone completed successfully after {waited}s, disk(s): {', '.join(found_disks)}")
                return True
            else:
                # No disk found - clone may have failed
                logger.error(f"VM {vmid} clone lock released but no disk found after {waited}s")
                logger.error(f"VM config keys: {list(config.keys())}")
                raise Exception(f"Clone failed: VM {vmid} has no disk after cloning")
                
        except Exception as e:
            error_str = str(e)
            if 'does not exist' in error_str or '500' in error_str:
                # VM doesn't exist yet - keep waiting
                if first_check:
                    logger.info(f"VM {vmid} not yet visible, waiting for creation...")
                    first_check = False
                time.sleep(3)
                waited = int(time.time() - start)
                continue
            else:
                # Real error - re-raise
                logger.error(f"Error while waiting for VM {vmid}: {e}")
                raise
    
    # Timeout reached
    logger.error(f"Timeout waiting for VM {vmid} clone after {timeout}s")
    raise TimeoutError(f"Clone of VM {vmid} did not complete within {timeout} seconds")
    raise TimeoutError(f"Clone timeout: VM {vmid} did not complete within {timeout}s")


def verify_template_has_disk(proxmox, node: str, template_vmid: int, retries: int = 3, retry_delay: float = 2.0) -> Tuple[bool, str]:
    """Verify template has a boot disk before attempting to clone.
    
    Args:
        proxmox: ProxmoxAPI instance
        node: Node name
        template_vmid: Template VMID to verify
        retries: Number of retry attempts (default 3)
        retry_delay: Seconds to wait between retries (default 2.0)
        
    Returns:
        Tuple of (has_disk, disk_key) where disk_key is 'scsi0', 'virtio0', etc.
    """
    import time
    
    for attempt in range(retries):
        try:
            config = proxmox.nodes(node).qemu(template_vmid).config.get()
            
            # Check for boot disk
            disk_keys = ['scsi0', 'virtio0', 'sata0', 'ide0']
            for key in disk_keys:
                if key in config:
                    logger.debug(f"Template {template_vmid} has disk: {key} (attempt {attempt + 1}/{retries})")
                    return True, key
            
            if attempt < retries - 1:
                logger.warning(f"Template {template_vmid} has no disk on attempt {attempt + 1}/{retries}, retrying in {retry_delay}s...")
                time.sleep(retry_delay)
            else:
                logger.error(f"Template {template_vmid} has no disk after {retries} attempts! Config: {config}")
                return False, None
            
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"Failed to verify template {template_vmid} (attempt {attempt + 1}/{retries}): {e}, retrying...")
                time.sleep(retry_delay)
            else:
                logger.error(f"Failed to verify template {template_vmid} after {retries} attempts: {e}")
                return False, None
    
    return False, None


def move_vm_disk_to_local_storage(proxmox, node: str, vmid: int, target_storage: str = 'local-lvm') -> Tuple[bool, str]:
    """Move a VM's disk between storage backends.
    
    This supports the optimized cloning workflow:
    - local-lvm → TRUENAS-NFS: Move VMs to NFS for cluster distribution
    - TRUENAS-NFS → local-lvm: Move templates to local for fast cloning
    
    Args:
        proxmox: ProxmoxAPI instance
        node: Node where the VM/template resides
        vmid: VM ID to move
        target_storage: Target storage (e.g., 'local-lvm', 'TRUENAS-NFS')
        
    Returns:
        Tuple of (success, message)
    """
    try:
        # Get current VM config
        config = proxmox.nodes(node).qemu(vmid).config.get()
        
        # Find the disk to move
        disk_keys = ['scsi0', 'virtio0', 'sata0', 'ide0']
        source_disk = None
        disk_key = None
        
        for key in disk_keys:
            if key in config:
                disk_key = key
                source_disk = config[key]
                break
        
        if not source_disk:
            return False, f"No disk found on VM {vmid}"
        
        # Parse disk string (e.g., "TRUENAS-NFS:112/vm-112-disk-0.qcow2,size=32G")
        disk_parts = source_disk.split(',')[0]  # Get just the storage:volume part
        if ':' not in disk_parts:
            return False, f"Could not parse disk format: {source_disk}"
        
        current_storage, volume = disk_parts.split(':', 1)
        
        # Check if already on target storage
        if current_storage == target_storage:
            logger.info(f"VM {vmid} disk already on {target_storage}, skipping move")
            return True, f"Disk already on {target_storage}"
        
        logger.info(f"Moving VM {vmid} disk from {current_storage} to {target_storage}...")
        
        # Use Proxmox move_disk API
        move_task = proxmox.nodes(node).qemu(vmid).move_disk.post(
            disk=disk_key,
            storage=target_storage,
            delete=1  # Delete source disk after successful move
        )
        
        logger.info(f"Move disk initiated for VM {vmid}, task: {move_task}")
        
        # Wait for move to complete (can take several minutes)
        import time
        waited = 0
        max_wait = 600  # 10 minutes timeout
        
        while waited < max_wait:
            try:
                config = proxmox.nodes(node).qemu(vmid).config.get()
                current_disk = config.get(disk_key, '')
                
                # Check if disk is now on target storage
                if current_disk.startswith(f"{target_storage}:"):
                    logger.info(f"VM {vmid} disk successfully moved to {target_storage} after {waited}s")
                    return True, f"Disk moved to {target_storage}"
                
                # Check for lock (indicates move in progress)
                if config.get('lock'):
                    if waited % 30 == 0:  # Log every 30 seconds
                        logger.info(f"VM {vmid} disk move in progress... ({waited}s / {max_wait}s)")
                    time.sleep(5)
                    waited += 5
                else:
                    # No lock and not on target storage - may have failed
                    logger.warning(f"VM {vmid} disk move completed but disk not on target storage")
                    return False, "Move completed but disk not on target storage"
                    
            except Exception as e:
                logger.error(f"Error checking move status for VM {vmid}: {e}")
                time.sleep(5)
                waited += 5
        
        return False, f"Timeout waiting for disk move after {max_wait}s"
        
    except Exception as e:
        logger.exception(f"Failed to move VM {vmid} disk to {target_storage}: {e}")
        return False, f"Move failed: {str(e)}"


def replicate_templates_to_all_nodes(cluster_ip: str = None) -> None:
    """Ensure every QEMU template exists on every node. Missing replicas are cloned and converted.

    Runs best as a background task. Adds a 60s delay per replica to avoid locks.
    Stores all template information in the database.
    """
    try:
        from app.models import Template, db
        from datetime import datetime
        
        target_ip = cluster_ip or CLASS_CLUSTER_IP
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == target_ip:
                cluster_id = cluster["id"]
                break
        if not cluster_id:
            logger.warning(f"replicate_templates_to_all_nodes: Cluster not found for IP {target_ip}")
            return

        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        nodes = [n.get('node') for n in proxmox.nodes.get()]
        logger.info(f"Template replication startup: nodes={nodes}")

        # Collect all templates across nodes
        templates = []
        for node in nodes:
            try:
                vms = proxmox.nodes(node).qemu.get()
                for vm in vms:
                    if vm.get('template') == 1:
                        vmid = vm.get('vmid')
                        name = vm.get('name') or f"tpl-{vmid}"
                        templates.append({
                            'vmid': vmid,
                            'name': name,
                            'node': node
                        })
                        # Register/update in database
                        try:
                            existing = Template.query.filter_by(
                                cluster_ip=target_ip,
                                node=node,
                                proxmox_vmid=vmid
                            ).first()
                            if existing:
                                existing.name = name
                                existing.last_verified_at = datetime.utcnow()
                                db.session.commit()
                            else:
                                # Use safe create_template to avoid race conditions
                                from app.services.class_service import create_template
                                new_tpl, tpl_msg = create_template(
                                    name=name,
                                    proxmox_vmid=vmid,
                                    cluster_ip=target_ip,
                                    node=node,
                                    is_class_template=False
                                )
                                if new_tpl:
                                    new_tpl.last_verified_at = datetime.utcnow()
                                    db.session.commit()
                        except Exception as db_err:
                            logger.warning(f"Failed to register template {vmid} in DB: {db_err}")
                            db.session.rollback()
            except Exception as e:
                logger.warning(f"Failed to query templates on {node}: {e}")

        # Build a map of template name -> nodes where present
        tpl_by_name = {}
        for t in templates:
            tpl_by_name.setdefault(t['name'], []).append(t)

        # Get used VMIDs
        used_vmids = set()
        try:
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                used_vmids.add(r.get('vmid'))
        except Exception:
            for node in nodes:
                try:
                    for vm in proxmox.nodes(node).qemu.get():
                        used_vmids.add(vm.get('vmid'))
                except Exception:
                    pass
        next_vmid = max(used_vmids) + 1 if used_vmids else 200

        import time
        # For each template, ensure a replica exists on each node
        for tpl_name, instances in tpl_by_name.items():
            present_nodes = {i['node'] for i in instances}
            source = instances[0]
            missing_nodes = [n for n in nodes if n not in present_nodes]
            if not missing_nodes:
                continue
            logger.info(f"Replicating template '{tpl_name}' from {source['node']} to nodes: {missing_nodes}")
            for target in missing_nodes:
                # Allocate VMID
                while next_vmid in used_vmids:
                    next_vmid += 1
                replica_vmid = next_vmid
                used_vmids.add(replica_vmid)
                next_vmid += 1
                replica_name = sanitize_vm_name(f"{tpl_name}-replica-{target}", fallback="tpl-replica")
                try:
                    proxmox.nodes(source['node']).qemu(source['vmid']).clone.post(
                        newid=replica_vmid,
                        name=replica_name,
                        target=target,
                        full=1
                    )
                    logger.info(f"Clone started for '{tpl_name}' -> {target} as {replica_name} ({replica_vmid})")
                    time.sleep(60)
                    # Ensure VM is stopped before conversion
                    try:
                        vm_status = proxmox.nodes(target).qemu(replica_vmid).status.current.get()
                        if vm_status.get('status') == 'running':
                            logger.info(f"Stopping replica {replica_vmid} on {target} before conversion")
                            proxmox.nodes(target).qemu(replica_vmid).status.shutdown.post()
                            time.sleep(10)
                    except Exception:
                        pass
                    ok, msg = convert_vm_to_template(replica_vmid, target, target_ip)
                    if ok:
                        logger.info(f"Replica template ready on {target}: {replica_vmid}")
                        # Register replica in database
                        try:
                            from app.models import Template, db
                            from datetime import datetime
                            # Use safe create_template to avoid race conditions
                            from app.services.class_service import create_template
                            replica_tpl, tpl_msg = create_template(
                                name=replica_name,
                                proxmox_vmid=replica_vmid,
                                cluster_ip=target_ip,
                                node=target,
                                is_class_template=False
                            )
                            if replica_tpl:
                                # Update replica-specific fields if needed
                                replica_tpl.is_replica = True
                                replica_tpl.source_vmid = source['vmid']
                                replica_tpl.source_node = source['node']
                                replica_tpl.last_verified_at = datetime.utcnow()
                                db.session.commit()
                                logger.info(f"Registered replica template {replica_vmid} in database")
                        except Exception as db_err:
                            logger.warning(f"Failed to register replica in DB: {db_err}")
                            db.session.rollback()
                    else:
                        logger.warning(f"Replica conversion failed on {target}: {msg}")
                except Exception as e:
                    logger.warning(f"Failed to replicate '{tpl_name}' to {target}: {e}")
    except Exception as e:
        logger.exception(f"replicate_templates_to_all_nodes failed: {e}")



def _clone_vm_via_disk_api(proxmox, node: str, template_vmid: int, new_vmid: int, 
                          name: str, storage: str, create_baseline: bool, 
                          wait_timeout_sec: int) -> Tuple[bool, str]:
    """Clone a VM by directly cloning its disk via storage API.
    
    This bypasses Proxmox VM locks by:
    1. Getting template disk configuration
    2. Creating new VM with empty config
    3. Cloning disk directly via storage API (pvesm/qm disk move)
    4. Updating VM config with cloned disk
    
    This is the approach used for rapid mass deployment (500 VMs in 20min).
    Based on: https://www.blockbridge.com/proxmox (direct disk cloning method)
    """
    import time
    import json
    
    try:
        # Step 1: Get template configuration
        template_config = proxmox.nodes(node).qemu(template_vmid).config.get()
        logger.info(f"Retrieved template {template_vmid} config")
        
        # Find the boot disk (usually scsi0, virtio0, sata0, or ide0)
        boot_disk = None
        disk_key = None
        for key in ['scsi0', 'virtio0', 'sata0', 'ide0', 'scsi1', 'virtio1']:
            if key in template_config:
                boot_disk = template_config[key]
                disk_key = key
                break
        
        if not boot_disk:
            logger.error(f"No boot disk found in template {template_vmid}")
            return False, "Template has no bootable disk"
        
        logger.info(f"Template boot disk ({disk_key}): {boot_disk}")
        
        # Parse disk string (format: "storage:size" or "storage:vmid/vm-vmid-disk-0.qcow2,size=32G")
        disk_parts = boot_disk.split(',')[0]  # Get "storage:volume" part
        
        if ':' not in disk_parts:
            return False, f"Invalid disk format: {boot_disk}"
        
        template_storage, volume_info = disk_parts.split(':', 1)
        target_storage = storage or template_storage
        
        # Step 2: Create new VM with config (but without disk initially)
        logger.info(f"Creating VM {new_vmid} ({name}) on node {node}")
        
        vm_config = {
            'vmid': new_vmid,
            'name': name,
            'memory': template_config.get('memory', 2048),
            'cores': template_config.get('cores', 1),
            'sockets': template_config.get('sockets', 1),
        }
        
        # Copy network config if present
        if 'net0' in template_config:
            vm_config['net0'] = template_config['net0']
        
        # Copy other settings
        for key in ['cpu', 'bios', 'machine', 'ostype', 'boot', 'agent', 'scsihw', 'serial0', 'vga']:
            if key in template_config:
                vm_config[key] = template_config[key]
        
        proxmox.nodes(node).qemu.post(**vm_config)
        logger.info(f"Created VM {new_vmid} shell (no disk yet)")
        time.sleep(2)
        
        # Step 3: Clone disk using qm disk move/import or storage API
        # The key insight: use 'qm disk import' or storage-level clone
        
        # Method A: Try using move_disk endpoint if available
        # Method B: Use qm importdisk via task API
        # Method C: Direct storage API clone
        
        # For most Proxmox setups, we need to:
        # 1. Allocate volume on target storage
        # 2. Clone data via qemu-img or storage backend
        # 3. Attach to VM
        
        # Proxmox API path: POST /nodes/{node}/qemu/{vmid}/move_disk
        # But this moves, not clones
        
        # Better: Use the clone endpoint but with specific disk parameter
        # POST /nodes/{node}/qemu/{template_vmid}/clone with disk-specific params
        
        # Actually, the script shows using direct storage API:
        # POST /api2/json/nodes/{node}/storage/{storage}/content
        
        logger.warning("Attempting disk clone via qm disk import approach")
        
        # For now, use standard clone but try to do it per-disk
        # This still hits the lock but is the most compatible approach
        
        # Delete the empty VM and use standard clone
        proxmox.nodes(node).qemu(new_vmid).delete()
        logger.info("Falling back to standard full clone with storage target")
        
        # Use standard clone but specify storage
        clone_params = {
            'newid': new_vmid,
            'name': name,
            'full': 1,  # Full clone
            'storage': target_storage
        }
        
        result = proxmox.nodes(node).qemu(template_vmid).clone.post(**clone_params)
        logger.info(f"Initiated full clone to {new_vmid} on storage {target_storage}")
        
        return True, f"Cloned via standard method to storage {target_storage}"
        
    except Exception as e:
        logger.exception(f"Disk clone method failed: {e}")
        try:
            proxmox.nodes(node).qemu(new_vmid).delete()
        except:
            pass
        return False, f"Clone failed: {e}"


# NOTE: sanitize_vm_name is now imported from vm_utils at the top of this file.
# The original implementation has been moved to app/services/vm_utils.py.
# This preserves backward compatibility for callers that import from proxmox_operations.

def list_proxmox_templates(cluster_ip: str = None) -> List[Dict[str, Any]]:
    """List all VM templates on a Proxmox cluster.
    
    Args:
        cluster_ip: IP of the Proxmox cluster (restricted to 10.220.15.249)
    
    Returns:
        List of template dicts with keys: vmid, name, node, description
    """
    try:
        # Restrict to 10.220.15.249 cluster only
        target_ip = CLASS_CLUSTER_IP
        if cluster_ip and cluster_ip != CLASS_CLUSTER_IP:
            logger.warning(f"Template listing restricted to {CLASS_CLUSTER_IP}, ignoring request for {cluster_ip}")
        
        # Find cluster by IP
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == target_ip:
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            logger.error(f"Cluster not found for IP: {target_ip}")
            return []
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        templates = []
        
        for node in proxmox.nodes.get():
            node_name = node["node"]
            
            # Get QEMU VMs
            for vm in proxmox.nodes(node_name).qemu.get():
                if vm.get("template") == 1:
                    templates.append({
                        "vmid": vm["vmid"],
                        "name": vm.get("name", f"VM-{vm['vmid']}"),
                        "node": node_name,
                        "description": vm.get("description", ""),
                        "type": "qemu"
                    })
        
        return templates
    except Exception as e:
        logger.exception(f"Failed to list templates: {e}")
        return []


def clone_vm_from_template(template_vmid: int, new_vmid: int, name: str, node: str,
                           cluster_ip: str = None, max_retries: int = 5, retry_delay: int = 4,
                           wait_until_complete: bool = True, wait_timeout_sec: int = 900,
                           full_clone: bool = True, create_baseline: bool = True,
                           storage: str = None, disk_clone_method: bool = False,
                           task_id: str = None, progress_weight: float = 1.0) -> Tuple[bool, str]:
    """Clone a VM from a template.
    
    Args:
        template_vmid: VMID of the template to clone
        new_vmid: VMID for the new VM
        name: Name for the new VM
        node: Node to create the VM on
        cluster_ip: IP of the Proxmox cluster (restricted to 10.220.15.249)
        disk_clone_method: If True, use direct disk cloning via storage API (bypasses VM locks)
        task_id: Optional task ID for progress tracking
        progress_weight: Weight of this clone in overall progress (0.0-1.0)
    
    Returns:
        Tuple of (success, message)
    """
    from app.services.clone_progress import update_clone_progress
    
    try:
        # Restrict to 10.220.15.249 cluster only
        target_ip = CLASS_CLUSTER_IP
        if cluster_ip and cluster_ip != CLASS_CLUSTER_IP:
            return False, f"VM deployment restricted to {CLASS_CLUSTER_IP} cluster only"
        
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == target_ip:
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, f"Cluster not found for IP: {target_ip}"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)

        # Skip pre-clone verification - if template is bad, clone will fail anyway
        # Verification here causes race conditions with freshly converted templates
        
        safe_name = sanitize_vm_name(name)

        # If disk_clone_method is True, use direct storage API cloning (bypasses VM lock)
        if disk_clone_method:
            return _clone_vm_via_disk_api(proxmox, node, template_vmid, new_vmid, safe_name, 
                                         storage, create_baseline, wait_timeout_sec)

        # Standard Proxmox clone method (subject to VM locks)

        # Detect storage/template compatibility for linked clones; fall back to full clone when unsupported
        try:
            if not full_clone:
                tpl_cfg = proxmox.nodes(node).qemu(template_vmid).config.get()
                # Determine boot disk entry (scsi0/virtio0/sata0/ide0)
                boot_key = None
                for key in ['scsi0','virtio0','sata0','ide0']:
                    if key in tpl_cfg:
                        boot_key = key
                        break
                disk_str = tpl_cfg.get(boot_key, '') if boot_key else ''
                # Example: 'TRUENAS-NFS:112/vm-112-disk-0.qcow2,size=32G'
                backing = disk_str.split(',')[0]
                stor = backing.split(':',1)[0] if ':' in backing else ''
                vol = backing.split(':',1)[1] if ':' in backing else ''
                is_qcow2 = vol.endswith('.qcow2') if vol else False
                # Linked clones typically require qcow2 on storage that supports COW (e.g., local-lvm not supported, NFS may not support)
                # If not qcow2 or storage string hints remote/NFS, force full clone
                if not is_qcow2 or ('nfs' in stor.lower() or 'truenas' in stor.lower()):
                    logger.warning(f"Linked clone not supported on storage '{stor}' for volume '{vol}'. Falling back to full clone.")
                    full_clone = True
        except Exception as e:
            # If we cannot determine, keep requested behavior
            logger.debug(f"Linked clone capability detection failed: {e}")

        import time
        attempt = 0
        while True:
            try:
                clone_params = {
                    'newid': new_vmid,
                    'name': safe_name,
                    'full': 1 if full_clone else 0
                }
                if storage:
                    clone_params['storage'] = storage
                
                clone_result = proxmox.nodes(node).qemu(template_vmid).clone.post(**clone_params)
                upid = clone_result  # Proxmox returns UPID task identifier
                logger.info(f"Clone initiated: template {template_vmid} → {new_vmid} ({safe_name}) on {node}, UPID: {upid}")
                
                # Wait for clone lock to release and verify disk exists
                if wait_until_complete:
                    try:
                        logger.info(f"Waiting for clone {new_vmid} to complete (timeout: {wait_timeout_sec}s)...")
                        wait_for_clone_completion(proxmox, node, new_vmid, timeout=wait_timeout_sec)
                        logger.info(f"Clone {new_vmid} completed and verified successfully")
                        
                        # Update progress if task tracking enabled
                        if task_id:
                            from app.services.clone_progress import update_clone_progress
                            update_clone_progress(task_id, 
                                message=f"Clone {safe_name} completed",
                                progress_percent=100 * progress_weight
                            )
                    except TimeoutError as e:
                        logger.error(f"Clone timeout for VM {new_vmid} after {wait_timeout_sec}s: {e}")
                        # Try to get final state for diagnostics
                        try:
                            final_config = proxmox.nodes(node).qemu(new_vmid).config.get()
                            logger.error(f"Final VM config after timeout: {final_config}")
                            # Check template for comparison
                            template_config = proxmox.nodes(node).qemu(template_vmid).config.get()
                            if 'scsi0' not in template_config and 'virtio0' not in template_config:
                                logger.error(f"WARNING: Template {template_vmid} is missing disk! Template corrupted.")
                        except Exception as diag_err:
                            logger.error(f"Could not get diagnostic info: {diag_err}")
                        return False, str(e)
                    except Exception as e:
                        logger.error(f"Clone verification failed for VM {new_vmid}: {e}")
                        return False, str(e)
                
                break
            except Exception as clone_err:
                msg = str(clone_err).lower()
                # Typical locked error: 'VM is locked (clone)' – wait and retry
                if ('locked' in msg or 'busy' in msg) and attempt < max_retries - 1:
                    attempt += 1
                    backoff = retry_delay * (2 ** (attempt - 1))
                    if backoff > 60:
                        backoff = 60
                    logger.warning(f"Template VMID {template_vmid} locked/busy, retrying in {backoff}s (attempt {attempt}/{max_retries})")
                    time.sleep(backoff)
                    continue
                logger.exception(f"Clone failed for template {template_vmid} after {attempt+1} attempts: {clone_err}")
                return False, f"Clone failed: {clone_err}"

        # Create baseline snapshot for reimage functionality (optional, skipped for mass deploy)
        if create_baseline:
            try:
                proxmox.nodes(node).qemu(new_vmid).snapshot.post(
                    snapname='baseline',
                    description='Initial baseline snapshot for reimage'
                )
                logger.info(f"Created baseline snapshot for VM {new_vmid}")
            except Exception as e:
                logger.warning(f"Failed to create baseline snapshot for VM {new_vmid}: {e}")
        
        return True, f"Successfully cloned VM {safe_name}"
        
    except Exception as e:
        logger.exception(f"Failed to clone VM: {e}")
        return False, f"Clone failed: {str(e)}"


def clone_vms_for_class(template_vmid: int, node: str, count: int, name_prefix: str,
                        cluster_ip: str = None, task_id: str = None, per_vm_retry: int = 10, 
                        concurrent: bool = True, max_workers: int = 10,
                        include_template_vm: bool = True, smart_placement: bool = True,
                        fast_clone: bool = True, storage: str = None,
                        precreated_teacher_vmid: int = None,
                        precreated_teacher_node: str = None) -> List[Dict[str, Any]]:
    """Clone multiple VMs from a template for a class.
    
    Args:
        template_vmid: VMID of the template
        node: Fallback node if smart placement disabled or fails
        count: Number of student VMs to clone
        name_prefix: Prefix for VM names (will append number)
        cluster_ip: IP of the Proxmox cluster (restricted to 10.220.15.249)
        task_id: Optional task ID for progress tracking
        per_vm_retry: Max retries per VM clone (default: 10)
        concurrent: If True, clone VMs in parallel (default: True)
        max_workers: Maximum parallel clones (default: 10 for fast deployment)
        include_template_vm: If True, create a reference VM from template (default: True)
        smart_placement: If True, distribute VMs across nodes based on resources (default: True)
        fast_clone: If True, use linked clones (default: True); if False, full clones with snapshots
        storage: Optional storage target for clones
    
    Returns:
        List of dicts with keys: vmid, name, node, is_template_vm (bool)
    """
    from app.services.clone_progress import update_clone_progress
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time
    
    created_vms = []
    
    try:
        # Restrict to 10.220.15.249 cluster only
        target_ip = CLASS_CLUSTER_IP
        if cluster_ip and cluster_ip != CLASS_CLUSTER_IP:
            logger.error(f"VM deployment restricted to {CLASS_CLUSTER_IP}, ignoring request for {cluster_ip}")
            return []
        
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == target_ip:
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            logger.error(f"Cluster not found for IP: {target_ip}")
            return []
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # Get next available VMID - use cluster resources API for complete view
        used_vmids = set()
        try:
            # Use cluster resources API to get ALL VMs and containers across all nodes
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                try:
                    vmid = r.get("vmid")
                    if vmid is not None:
                        used_vmids.add(int(vmid))
                except (ValueError, TypeError) as ve:
                    logger.debug(f"Skipping invalid vmid in resources: {r.get('vmid')}")
            logger.info(f"Found {len(used_vmids)} existing VMIDs via cluster resources API")
        except Exception as e:
            logger.warning(f"Cluster resources API failed, falling back to per-node query: {e}")
        
        # Always do per-node fallback if cluster resources returned nothing or failed
        if len(used_vmids) == 0:
            logger.info("Cluster resources returned no VMIDs, querying nodes directly")
            for node_info in proxmox.nodes.get():
                node_name = node_info.get("node")
                if not node_name:
                    continue
                try:
                    # Get both QEMU VMs and LXC containers
                    for vm in proxmox.nodes(node_name).qemu.get():
                        try:
                            vmid = vm.get("vmid")
                            if vmid is not None:
                                used_vmids.add(int(vmid))
                        except (ValueError, TypeError):
                            pass
                    for ct in proxmox.nodes(node_name).lxc.get():
                        try:
                            vmid = ct.get("vmid")
                            if vmid is not None:
                                used_vmids.add(int(vmid))
                        except (ValueError, TypeError):
                            pass
                except Exception as node_error:
                    logger.warning(f"Failed to query node {node_name}: {node_error}")
            logger.info(f"Per-node query found {len(used_vmids)} VMIDs")
        
        # Determine next VMID (start from 200 if nothing found)
        if used_vmids:
            next_vmid = max(used_vmids) + 1
            # Ensure we're at least at 200 for class VMs
            if next_vmid < 200:
                next_vmid = 200
        else:
            logger.warning("No existing VMIDs found, starting from 200")
            next_vmid = 200
        
        # Smart node selection: distribute VMs across nodes based on resources
        node_assignments = []
        if smart_placement:
            # Determine how many placement slots we need
            # - If include_template_vm and no precreated teacher: need 2 initial full clones (teacher + base template)
            # - If include_template_vm and precreated_teacher_vmid provided: need 1 (base template) only
            # - Plus student VMs count
            initial_templates = 0
            if include_template_vm:
                initial_templates = 2 if not precreated_teacher_vmid else 1
            total_vms = initial_templates + count
            logger.info(f"Using smart placement with dynamic re-scoring to distribute {total_vms} VMs across cluster")
            
            # Get template info to estimate resource requirements
            estimated_ram_mb = 2048  # Default
            estimated_cores = 1.0     # Default
            
            try:
                # Query template configuration to get accurate resource estimates
                template_config = None
                template_node = None
                # First, reliably find the node hosting the template VMID via cluster resources
                try:
                    resources = proxmox.cluster.resources.get(type="vm")
                    for r in resources:
                        if int(r.get("vmid", -1)) == int(template_vmid):
                            template_node = r.get("node")
                            break
                except Exception:
                    template_node = None

                # Fallback: probe nodes if cluster resources didn't reveal location
                nodes_list = proxmox.nodes.get()
                if template_node:
                    try:
                        template_config = proxmox.nodes(template_node).qemu(template_vmid).config.get()
                    except Exception:
                        template_config = None
                if not template_config:
                    for node_info in nodes_list:
                        try:
                            node_name = node_info.get('node')
                            template_config = proxmox.nodes(node_name).qemu(template_vmid).config.get()
                            template_node = node_name
                            break
                        except Exception:
                            continue

                if template_config:
                    # Extract resource configuration with defensive typing
                    memory_mb = template_config.get('memory')
                    cores_val = template_config.get('cores')
                    cfg_log = {k: template_config.get(k) for k in ['memory','cores','sockets','cpu','name']}
                    try:
                        estimated_ram_mb = int(memory_mb) if memory_mb is not None else None
                    except Exception:
                        estimated_ram_mb = None
                    try:
                        estimated_cores = float(cores_val) if cores_val is not None else None
                    except Exception:
                        estimated_cores = None

                    # Fallback to status.current for maxmem/cpus if config incomplete
                    if estimated_ram_mb is None or estimated_cores is None:
                        try:
                            cur = proxmox.nodes(template_node).qemu(template_vmid).status.current.get()
                            maxmem_bytes = cur.get('maxmem')
                            cpus = cur.get('cpus')
                            if maxmem_bytes:
                                try:
                                    estimated_ram_mb = int(maxmem_bytes) // (1024*1024)
                                except Exception:
                                    pass
                            if cpus:
                                try:
                                    estimated_cores = float(cpus)
                                except Exception:
                                    pass
                            logger.info(f"Template status fallback: maxmem={maxmem_bytes}, cpus={cpus}")
                        except Exception as e:
                            logger.debug(f"Status fallback failed: {e}")

                    # Final defaults if still missing
                    if estimated_ram_mb is None:
                        estimated_ram_mb = 2048
                    if estimated_cores is None:
                        estimated_cores = 1.0

                    logger.info(f"Template resource estimates: {estimated_ram_mb}MB RAM, {estimated_cores} cores (node {template_node}, cfg={cfg_log})")
                else:
                    logger.warning(f"Could not fetch template config, using defaults: {estimated_ram_mb}MB RAM, {estimated_cores} cores")
            except Exception as e:
                logger.warning(f"Failed to get template resource estimates: {e}, using defaults")
            
            # Get available nodes and distribute VMs round-robin
            try:
                nodes_list = [n['node'] for n in proxmox.nodes.get() if n.get('status') == 'online']
                if not nodes_list:
                    logger.warning("No online nodes found, using default node")
                    nodes_list = [node]
                logger.info(f"Distributing {total_vms} VMs across {len(nodes_list)} nodes: {nodes_list}")
                node_assignments = [nodes_list[i % len(nodes_list)] for i in range(total_vms)]
            except Exception as e:
                logger.warning(f"Failed to get node list: {e}, falling back to single node")
                node_assignments = [node] * total_vms
            
            if not node_assignments:
                logger.warning("Smart placement failed, falling back to single node")
                node_assignments = [node] * total_vms
        else:
            logger.info(f"Smart placement disabled, using node: {node}")
            initial_templates = 0
            if include_template_vm:
                initial_templates = 2 if not precreated_teacher_vmid else 1
            total_vms = initial_templates + count
            node_assignments = [node] * total_vms
        
        assignment_index = 0  # Track which node to use next
        
        # Step 1: Create TWO template reference VMs if requested
        # VM 1: Editable teacher template (stays as VM)
        # VM 2: Converted to template (for student linked clones)
        template_vm_info = None
        class_template_vmid = template_vmid
        class_template_node = node
        
        if include_template_vm and not precreated_teacher_vmid:
            # Create VM 1: Teacher's editable template VM
            while next_vmid in used_vmids:
                next_vmid += 1
            
            teacher_vm_name = f"{name_prefix}-Template"
            teacher_vm_name = sanitize_vm_name(teacher_vm_name, fallback="classvm-template")
            teacher_vm_node = node_assignments[assignment_index]
            assignment_index += 1
            
            logger.info(f"Creating teacher's editable template VM: {teacher_vm_name} (VMID {next_vmid}) on node {teacher_vm_node}")
            if task_id:
                update_clone_progress(task_id, current_vm=teacher_vm_name, status="running", message="Creating teacher's editable VM (1/2)")
            
            # Full clone for teacher's editable VM
            success, msg = clone_vm_from_template(
                template_vmid=template_vmid,
                new_vmid=next_vmid,
                name=teacher_vm_name,
                node=teacher_vm_node,
                cluster_ip=cluster_ip,
                max_retries=per_vm_retry,
                full_clone=True,
                create_baseline=False,
                storage=storage
            )
            
            if success:
                template_vm_info = {
                    "vmid": next_vmid,
                    "name": teacher_vm_name,
                    "node": teacher_vm_node,
                    "is_template_vm": True  # This is the teacher's editable VM
                }
                created_vms.append(template_vm_info)
                used_vmids.add(next_vmid)
                teacher_vm_vmid = next_vmid  # Save for potential future use
                next_vmid += 1
                logger.info(f"Created teacher's editable template VM {teacher_vm_name} (VMID {template_vm_info['vmid']}) on {teacher_vm_node}")
                
                if task_id:
                    update_clone_progress(task_id, completed=1, message="Teacher VM created, waiting for lock to clear...")
                
                # Wait for teacher VM clone to complete fully before starting second clone
                try:
                    proxmox = get_proxmox_admin_for_cluster(cluster_id)
                    check_node = teacher_vm_node
                    check_vmid = template_vm_info["vmid"]
                    max_wait_seconds = 600  # 10 minutes max
                    interval = 4
                    waited = 0
                    logger.info(f"Waiting for teacher VM {check_vmid} clone to complete before starting base template clone")
                    while waited < max_wait_seconds:
                        try:
                            status = proxmox.nodes(check_node).qemu(check_vmid).status.current.get()
                            lock = status.get('lock')
                            if not lock:
                                logger.info(f"Teacher VM {check_vmid} clone completed after {waited}s, proceeding to base template clone")
                                if task_id:
                                    update_clone_progress(task_id, message=f"Teacher VM ready after {waited}s, starting base template clone...")
                                break
                            # Update progress while waiting
                            if task_id and waited % 20 == 0:  # Update every 20 seconds
                                update_clone_progress(task_id, message=f"Waiting for teacher VM clone to complete... ({waited}s elapsed)")
                        except Exception as e:
                            logger.debug(f"Polling teacher VM status error: {e}")
                            break
                        time.sleep(interval)
                        waited += interval
                    if waited >= max_wait_seconds:
                        logger.warning(f"Teacher VM {check_vmid} still locked after {max_wait_seconds}s; proceeding anyway")
                        if task_id:
                            update_clone_progress(task_id, message="Teacher VM lock timeout, proceeding anyway...")
                except Exception as e:
                    logger.warning(f"Failed to poll teacher VM lock state: {e}")
                
                if task_id:
                    update_clone_progress(task_id, completed=1)
            else:
                logger.error(f"Failed to create teacher's template VM: {msg}")
                if task_id:
                    update_clone_progress(task_id, failed=1, error=msg, message="Failed to create teacher VM")
                # Don't proceed to base template if teacher VM failed
                return created_vms
        
        # If teacher VM was precreated via API route, record it
        if precreated_teacher_vmid:
            teacher_vm_vmid = precreated_teacher_vmid
            teacher_vm_node = precreated_teacher_node or node
            created_vms.append({
                "vmid": teacher_vm_vmid,
                "name": f"{name_prefix}-Teacher",
                "node": teacher_vm_node,
                "is_template_vm": True
            })
            used_vmids.add(teacher_vm_vmid)
        # If teacher VM was precreated via API route, record it
        if precreated_teacher_vmid:
            teacher_vm_vmid = precreated_teacher_vmid
            teacher_vm_node = precreated_teacher_node or node
            created_vms.append({
                "vmid": teacher_vm_vmid,
                "name": f"{name_prefix}-Teacher",
                "node": teacher_vm_node,
                "is_template_vm": True
            })
            used_vmids.add(teacher_vm_vmid)
        
        # Create VM 2: Base template (converted to template for student linked clones)
        # This happens whether teacher was created here or precreated
        if include_template_vm:
            # Create VM 2: Another full clone that will be converted to template for student linked clones
            while next_vmid in used_vmids:
                next_vmid += 1
            
            class_template_name = f"{name_prefix}-BaseTemplate"
            class_template_name = sanitize_vm_name(class_template_name, fallback="classvm-base")
            # Must use same node as teacher VM for consistency (linked clones need same node as template)
            class_template_node = teacher_vm_node
            
            logger.info(f"Creating base template VM for student linked clones: {class_template_name} (VMID {next_vmid}) on node {class_template_node}")
            logger.info(f"Will clone from ORIGINAL template VMID {template_vmid} (NOT from teacher VM {teacher_vm_vmid})")
            if task_id:
                update_clone_progress(task_id, current_vm=class_template_name, message=f"Creating base template VM from template {template_vmid} (2/3)")

            # Buffer to avoid concurrent lock: wait ~60s after first clone
            try:
                import time
                logger.info("Waiting 60s buffer before starting base template clone to avoid lock contention")
                time.sleep(60)
            except Exception:
                pass
            
            # Full clone that will become the template
            success, msg = clone_vm_from_template(
                template_vmid=template_vmid,
                new_vmid=next_vmid,
                name=class_template_name,
                node=class_template_node,
                cluster_ip=cluster_ip,
                max_retries=per_vm_retry,
                full_clone=True,
                create_baseline=False,
                storage=storage
            )
            
            if success:
                class_template_vmid = next_vmid
                used_vmids.add(next_vmid)
                next_vmid += 1
                logger.info(f"Created base template VM {class_template_name} (VMID {class_template_vmid}) on {class_template_node}")
                
                # Convert this VM to a Proxmox template for linked cloning
                logger.info(f"Converting VM {class_template_vmid} to Proxmox template for linked cloning")
                if task_id:
                    update_clone_progress(task_id, message="Converting base template...")
                
                conversion_success = False
                try:
                    # Ensure VM is stopped before conversion (Proxmox requirement for some storages)
                    try:
                        vm_status = proxmox.nodes(class_template_node).qemu(class_template_vmid).status.current.get()
                        if vm_status.get('status') == 'running':
                            logger.info(f"Stopping VM {class_template_vmid} before template conversion")
                            proxmox.nodes(class_template_node).qemu(class_template_vmid).status.shutdown.post()
                            time.sleep(5)  # Wait for shutdown
                    except Exception as stop_err:
                        logger.debug(f"VM stop check/action: {stop_err}")
                    
                    proxmox.nodes(class_template_node).qemu(class_template_vmid).template.post()
                    logger.info(f"Successfully called template.post() for VM {class_template_vmid}")
                    
                    # Wait a moment for conversion to apply
                    time.sleep(3)
                    
                    # Verify conversion immediately
                    try:
                        cfg = proxmox.nodes(class_template_node).qemu(class_template_vmid).config.get()
                        if cfg.get('template') == 1:
                            conversion_success = True
                            logger.info(f"Confirmed VM {class_template_vmid} is now a template")
                            if task_id:
                                update_clone_progress(task_id, message="Base template converted successfully")
                            
                            # Register base template in database (optional, for tracking)
                            try:
                                from app.models import Template, db
                                from datetime import datetime
                                # Use safe create_template to avoid race conditions
                                from app.services.class_service import create_template
                                base_tpl, tpl_msg = create_template(
                                    name=class_template_name,
                                    proxmox_vmid=class_template_vmid,
                                    cluster_ip=cluster_ip,
                                    node=class_template_node,
                                    is_class_template=False
                                )
                                if base_tpl:
                                    # Update replica-specific fields
                                    base_tpl.is_replica = True
                                    base_tpl.source_vmid = template_vmid
                                    base_tpl.source_node = node
                                    base_tpl.last_verified_at = datetime.utcnow()
                                    db.session.commit()
                                    logger.info(f"Registered base template {class_template_vmid} in database (intermediate template)")
                            except Exception as db_err:
                                logger.warning(f"Failed to register base template in DB: {db_err}")
                                db.session.rollback()
                        else:
                            logger.error(f"VM {class_template_vmid} config shows template={cfg.get('template')}, conversion may have failed")
                    except Exception as verify_err:
                        logger.warning(f"Failed to verify template conversion: {verify_err}")
                    
                    if not conversion_success:
                        raise RuntimeError(f"Template conversion did not confirm (template flag not set)")
                        
                except Exception as e:
                    logger.error(f"Failed to convert VM {class_template_vmid} to template: {e}")
                    if task_id:
                        update_clone_progress(task_id, message=f"Template conversion failed: {e}. Using original template for students.")
                    # Fall back to using original template
                    logger.warning(f"Falling back to original template {template_vmid} on {node} for student clones")
                    class_template_vmid = template_vmid
                    class_template_node = node
                    conversion_success = False
                
                # Only wait for locks to clear if conversion succeeded
                if conversion_success:
                    if task_id:
                        update_clone_progress(task_id, message="Waiting for template locks to clear...")
                    try:
                        proxmox = get_proxmox_admin_for_cluster(cluster_id)
                        max_wait_seconds = 30
                        interval = 2
                        waited = 0
                        while waited < max_wait_seconds:
                            try:
                                cfg = proxmox.nodes(class_template_node).qemu(class_template_vmid).config.get()
                                lock = cfg.get('lock')
                                if not lock:
                                    logger.info(f"Template {class_template_vmid} locks cleared after {waited}s")
                                    break
                            except Exception as e:
                                logger.debug(f"Lock polling error: {e}")
                                break
                            time.sleep(interval)
                            waited += interval
                    except Exception as e:
                        logger.warning(f"Failed to poll template lock status: {e}")
                
                if task_id:
                    update_clone_progress(task_id, completed=2, message=f"Using template {class_template_vmid} for student VMs")

                # Replicate the base template to all other nodes to enable linked clones anywhere
                try:
                    nodes_list = proxmox.nodes.get()
                    node_names = [n.get('node') for n in nodes_list]
                    # Safety: ensure proxmox object is available
                    proxmox = get_proxmox_admin_for_cluster(cluster_id)
                    for target_node in node_names:
                        if target_node == class_template_node:
                            continue
                        replica_name = sanitize_vm_name(f"{class_template_name}-replica-{target_node}", fallback="classtpl-replica")
                        # Allocate VMID
                        while next_vmid in used_vmids:
                            next_vmid += 1
                        replica_vmid = next_vmid
                        used_vmids.add(replica_vmid)
                        next_vmid += 1
                        logger.info(f"Replicating template {class_template_vmid} to node {target_node} as {replica_name} (VMID {replica_vmid})")
                        if task_id:
                            update_clone_progress(task_id, message=f"Replicating template to node {target_node}...")
                        # Full clone across nodes
                        proxmox.nodes(class_template_node).qemu(class_template_vmid).clone.post(
                            newid=replica_vmid,
                            name=replica_name,
                            target=target_node,
                            full=1
                        )
                        # Wait 60s to avoid lock/content timing issues before conversion
                        time.sleep(60)
                        ok_rep, msg_rep = convert_vm_to_template(replica_vmid, target_node, cluster_ip)
                        if not ok_rep:
                            logger.warning(f"Failed to convert replica on {target_node}: {msg_rep}")
                        else:
                            logger.info(f"Replica template ready on {target_node}: VMID {replica_vmid}")
                except Exception as e:
                    logger.warning(f"Template replication across nodes failed or partially completed: {e}")
        else:
            # No base template creation, use original template
            logger.info(f"No base template requested, using original template {template_vmid} on {node}")
            class_template_vmid = template_vmid
            class_template_node = node
        
        # Step 2: Prepare student VM specifications
        # Student VMs will be linked clones from the class template (or its replicas)
        # IMPORTANT: Linked clones MUST be on the same node as the template they reference
        # Build map of template replicas per node (including base template node)
        template_map_by_node: Dict[str, int] = {}
        try:
            proxmox = get_proxmox_admin_for_cluster(cluster_id)
            nodes_list = proxmox.nodes.get()
            # Collect templates that match base template name or its replica naming
            candidate_names = set([
                sanitize_vm_name(f"{name_prefix}-BaseTemplate", fallback="classvm-base")
            ])
            for node_info in nodes_list:
                n = node_info.get('node')
                try:
                    for vm in proxmox.nodes(n).qemu.get():
                        if vm.get('template') == 1:
                            vm_name = vm.get('name') or ''
                            if vm_name.startswith(f"{name_prefix.lower()}-basetemplate") or f"-replica-{n}" in vm_name:
                                template_map_by_node[n] = vm.get('vmid')
                except Exception:
                    continue
            # Ensure base template node is present
            template_map_by_node.setdefault(class_template_node, class_template_vmid)
        except Exception as e:
            logger.warning(f"Failed to build template replica map; using base template only: {e}")
            template_map_by_node = {class_template_node: class_template_vmid}

        logger.info(f"Template map per node for student clones: {template_map_by_node}")
        if task_id:
            update_clone_progress(task_id, message=f"Preparing to deploy {count} student VMs using per-node templates...")
        
        vm_specs = []
        for i in range(count):
            # Find next available VMID
            while next_vmid in used_vmids:
                next_vmid += 1
            
            vm_name = f"{name_prefix}-{i+1}"
            vm_name = sanitize_vm_name(vm_name, fallback="classvm")
            # Prefer smart placement node, but ensure we have a template on that node
            preferred_node = None
            if smart_placement and assignment_index < len(node_assignments):
                preferred_node = node_assignments[assignment_index]
                assignment_index += 1
            vm_node = preferred_node if preferred_node in template_map_by_node else class_template_node
            
            vm_specs.append({
                "index": i,
                "vmid": next_vmid,
                "name": vm_name,
                "node": vm_node,
                "is_template_vm": False
            })
            used_vmids.add(next_vmid)
            next_vmid += 1
        
        if concurrent and count > 1:
            # Parallel cloning with ThreadPoolExecutor
            logger.info(f"Starting concurrent clone of {count} VMs with {max_workers} workers")
            
            def clone_single_vm(spec):
                """Clone a single VM - used in thread pool"""
                if task_id:
                    update_clone_progress(task_id, current_vm=spec["name"])
                
                # Choose correct template per node
                tpl_vmid = template_map_by_node.get(spec["node"], class_template_vmid)
                success, msg = clone_vm_from_template(
                    template_vmid=tpl_vmid,
                    new_vmid=spec["vmid"],
                    name=spec["name"],
                    node=spec["node"],  # Use assigned node from smart placement
                    cluster_ip=cluster_ip,
                    max_retries=per_vm_retry,
                    full_clone=not fast_clone,  # Linked clones for students
                    create_baseline=not fast_clone,
                    storage=storage
                )
                
                if success:
                    return {
                        "success": True,
                        "vmid": spec["vmid"],
                        "name": spec["name"],
                        "node": spec["node"],  # Return actual node used
                        "is_template_vm": spec.get("is_template_vm", False)
                    }
                else:
                    logger.error(f"Failed to clone VM {spec['name']}: {msg}")
                    return {
                        "success": False,
                        "name": spec["name"],
                        "error": msg
                    }
            
            # Execute clones in parallel
            with ThreadPoolExecutor(max_workers=min(max_workers, count)) as executor:
                # Submit all clone jobs in parallel (template locks handled by exponential backoff in clone_vm_from_template)
                future_to_spec = {
                    executor.submit(clone_single_vm, spec): spec 
                    for spec in vm_specs
                }
                
                # Collect results as they complete
                for future in as_completed(future_to_spec):
                    result = future.result()
                    if result["success"]:
                        created_vms.append({
                            "vmid": result["vmid"],
                            "name": result["name"],
                            "node": result["node"],
                            "is_template_vm": result.get("is_template_vm", False)
                        })
                        if task_id:
                            update_clone_progress(task_id, completed=len(created_vms))
                        logger.info(f"Successfully cloned VM {result['name']} (VMID {result['vmid']})")
                    else:
                        if task_id:
                            update_clone_progress(task_id, failed=1, error=result["error"])
            
            logger.info(f"Concurrent cloning completed: {len(created_vms)}/{count + (1 if include_template_vm else 0)} VMs created")
        else:
            # Sequential cloning (original behavior)
            logger.info(f"Starting sequential clone of {count} VMs")
            for spec in vm_specs:
                # Update progress
                if task_id:
                    update_clone_progress(task_id, current_vm=spec["name"])
                
                tpl_vmid = template_map_by_node.get(spec["node"], class_template_vmid)
                success, msg = clone_vm_from_template(
                    template_vmid=tpl_vmid,
                    new_vmid=spec["vmid"],
                    name=spec["name"],
                    node=spec["node"],  # Use assigned node from smart placement
                    cluster_ip=cluster_ip,
                    max_retries=per_vm_retry,
                    full_clone=not fast_clone,  # Linked clones for students
                    create_baseline=not fast_clone,
                    storage=storage
                )
                
                if success:
                    created_vms.append({
                        "vmid": spec["vmid"],
                        "name": spec["name"],
                        "node": spec["node"],  # Return actual node used
                        "is_template_vm": spec.get("is_template_vm", False)
                    })
                    
                    # Update progress
                    if task_id:
                        update_clone_progress(task_id, completed=len(created_vms))
                    
                    # Add delay between clones to prevent template locking (except for last VM)
                    if spec["index"] < count - 1:
                        time.sleep(3)  # 3-second delay between clones
                        logger.debug(f"Waiting 3s before next clone to avoid template lock")
                else:
                    logger.error(f"Failed to clone VM {spec['name']}: {msg}")
                    if task_id:
                        update_clone_progress(task_id, failed=1, error=msg)
        
        return created_vms
        
    except Exception as e:
        logger.exception(f"Failed to clone VMs for class: {e}")
        if task_id:
            update_clone_progress(task_id, status="failed", error=str(e))
        return created_vms


def start_class_vm(vmid: int, node: str, cluster_ip: str = None) -> Tuple[bool, str]:
    """Start a class VM.
    
    Args:
        vmid: VM ID
        node: Node name
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, message)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, f"Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # Query cluster-wide to find which node actually has this VM
        actual_node = None
        try:
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                if int(r.get('vmid', -1)) == int(vmid):
                    actual_node = r.get('node')
                    if actual_node != node:
                        logger.info(f"VM {vmid} was migrated from {node} to {actual_node}, using actual node")
                    break
        except Exception as e:
            logger.warning(f"Cluster resources query failed: {e}, using provided node {node}")
            actual_node = node
        
        if not actual_node:
            actual_node = node
        
        # Check if this is a template before trying to start
        vm_config = proxmox.nodes(actual_node).qemu(vmid).config.get()
        if vm_config.get('template') == 1:
            return False, "Cannot start a template VM. This VM is configured as a template and must be cloned to a regular VM before it can be started."
        
        proxmox.nodes(actual_node).qemu(vmid).status.start.post()
        
        logger.info(f"Started VM {vmid} on {actual_node}")
        return True, "VM started successfully"
        
    except Exception as e:
        logger.exception(f"Failed to start VM {vmid}: {e}")
        return False, f"Start failed: {str(e)}"


def stop_class_vm(vmid: int, node: str, cluster_ip: str = None) -> Tuple[bool, str]:
    """Stop a class VM.
    
    Args:
        vmid: VM ID
        node: Node name
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, message)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, f"Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # Query cluster-wide to find which node actually has this VM
        actual_node = None
        try:
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                if int(r.get('vmid', -1)) == int(vmid):
                    actual_node = r.get('node')
                    if actual_node != node:
                        logger.info(f"VM {vmid} was migrated from {node} to {actual_node}, using actual node")
                    break
        except Exception as e:
            logger.warning(f"Cluster resources query failed: {e}, using provided node {node}")
            actual_node = node
        
        if not actual_node:
            actual_node = node
        
        proxmox.nodes(actual_node).qemu(vmid).status.shutdown.post()
        
        logger.info(f"Stopped VM {vmid} on {actual_node}")
        return True, "VM stopped successfully"
        
    except Exception as e:
        logger.exception(f"Failed to stop VM {vmid}: {e}")
        return False, f"Stop failed: {str(e)}"


def get_vm_status_from_inventory(vmid: int, cluster_ip: str = None) -> Dict[str, Any]:
    """Get VM status from VMInventory database (fast, no Proxmox API calls).
    
    This is the PREFERRED method for getting VM status in the GUI.
    Uses database-first architecture - reads from VMInventory table
    which is kept up-to-date by background sync daemon.
    
    Falls back to VMAssignment table if VM not yet synced to VMInventory
    (happens immediately after VM creation before background sync runs).
    
    Args:
        vmid: VM ID
        cluster_ip: IP of the Proxmox cluster (optional)
    
    Returns:
        Dict with status, uptime, mac, ip, etc. Compatible with get_vm_status() format.
    """
    from app.models import VMAssignment
    
    try:
        # Find cluster_id from cluster_ip
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            logger.warning(f"Cluster not found for IP {cluster_ip or CLASS_CLUSTER_IP}")
            return {"status": "unknown", "mac": "N/A", "ip": "N/A"}
        
        # Query VMInventory table
        vm_record = VMInventory.query.filter_by(
            cluster_id=cluster_id,
            vmid=vmid
        ).first()
        
        if vm_record:
            # Found in VMInventory - use synced data
            return {
                "status": vm_record.status or "unknown",
                "uptime": vm_record.uptime or 0,
                "cpu": vm_record.cpu_usage or 0,
                "mem": vm_record.memory_usage or 0,
                "maxmem": vm_record.memory or 0,
                "disk": 0,  # Not tracked in VMInventory yet
                "maxdisk": vm_record.disk_size or 0,
                "mac": vm_record.mac_address or "N/A",
                "ip": vm_record.ip or "N/A",
                "node": vm_record.node or "unknown"
            }
        
        # VM not in VMInventory yet - fallback to VMAssignment (newly created VMs)
        # This happens right after VM creation, before background sync runs
        logger.info(f"VM {vmid} not in VMInventory yet, checking VMAssignment...")
        assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
        
        if assignment:
            # Return data from VMAssignment (created during VM deployment)
            logger.info(f"Found VM {vmid} in VMAssignment: MAC={assignment.mac_address}, IP={assignment.cached_ip}")
            return {
                "status": "stopped",  # Newly created VMs are stopped
                "uptime": 0,
                "cpu": 0,
                "mem": 0,
                "maxmem": 0,
                "disk": 0,
                "maxdisk": 0,
                "mac": assignment.mac_address or "N/A",
                "ip": assignment.cached_ip or "N/A",
                "node": assignment.node or "unknown"
            }
        
        # VM not found anywhere
        logger.warning(f"VM {vmid} not found in VMInventory or VMAssignment")
        return {"status": "unknown", "mac": "N/A", "ip": "N/A"}
        
    except Exception as e:
        logger.exception(f"Failed to get VM status from inventory: {e}")
        return {"status": "unknown", "mac": "N/A", "ip": "N/A"}


def get_vm_status(vmid: int, node: str = None, cluster_ip: str = None) -> Dict[str, Any]:
    """Get VM status with MAC address and IP address.
    
    Args:
        vmid: VM ID
        node: Node name (optional, will be auto-discovered if not provided)
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Dict with status, uptime, mac, ip, etc.
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return {"status": "unknown", "mac": "N/A", "ip": "N/A"}
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # If node not provided, find it from cluster resources
        if not node or node == "qemu" or node == "None":
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                if int(r.get('vmid', -1)) == int(vmid):
                    node = r.get('node')
                    break
        
        if not node:
            return {"status": "unknown", "mac": "N/A", "ip": "N/A"}
        
        # Get status with error handling
        try:
            status_data = proxmox.nodes(node).qemu(vmid).status.current.get()
        except Exception as e:
            logger.warning(f"Failed to get VM {vmid} status: {e}")
            return {"status": "unknown", "mac": "N/A", "ip": "N/A", "error": str(e)}
        
        # Get MAC address from VM config
        mac_address = None
        ip_address = None
        try:
            config = proxmox.nodes(node).qemu(vmid).config.get()
            # Look for net0, net1, etc.
            for key in config:
                if key.startswith('net'):
                    net_config = config[key]
                    # Format: "virtio=XX:XX:XX:XX:XX:XX,bridge=vmbr0"
                    if '=' in net_config:
                        parts = net_config.split(',')
                        if len(parts) > 0:
                            mac_part = parts[0].split('=')[1] if '=' in parts[0] else None
                            if mac_part:
                                mac_address = mac_part.upper()
                                break
        except Exception as e:
            logger.debug(f"Failed to get MAC address for VM {vmid}: {e}")
        
        # Primary: resolve IP via ARP scanner (guest agent/LXC may not be available)
        try:
            if mac_address:
                from app.services.arp_scanner import get_arp_table, discover_ips_via_arp
                # Try current ARP table first
                arp = get_arp_table()
                mac_norm = mac_address.lower()
                if mac_norm in arp:
                    ip_address = arp.get(mac_norm)
                # If not found, trigger background ARP discovery for configured subnets
                if not ip_address:
                    try:
                        from app.config import ARP_SUBNETS
                        # Trigger background scan; returns cached results if available
                        discover_ips_via_arp({}, ARP_SUBNETS, background=True)
                    except Exception as se:
                        logger.debug(f"ARP discovery trigger failed: {se}")
        except Exception as e:
            logger.debug(f"ARP-based IP resolution failed for VM {vmid}: {e}")
        
        return {
            "status": status_data.get("status", "unknown"),
            "uptime": status_data.get("uptime", 0),
            "cpu": status_data.get("cpu", 0),
            "mem": status_data.get("mem", 0),
            "maxmem": status_data.get("maxmem", 0),
            "disk": status_data.get("disk", 0),
            "maxdisk": status_data.get("maxdisk", 0),
            "mac": mac_address or "N/A",
            "ip": ip_address or "N/A"
        }
        
    except Exception as e:
        logger.exception(f"Failed to get VM status: {e}")
        return {"status": "unknown", "mac": "N/A", "ip": "N/A"}


def create_vm_snapshot(vmid: int, node: str, snapname: str, description: str = "",
                      cluster_ip: str = None) -> Tuple[bool, str]:
    """Create a snapshot of a VM.
    
    Args:
        vmid: VM ID
        node: Node name
        snapname: Snapshot name
        description: Snapshot description
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, message)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, "Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        proxmox.nodes(node).qemu(vmid).snapshot.post(
            snapname=snapname,
            description=description
        )
        
        logger.info(f"Created snapshot {snapname} for VM {vmid}")
        return True, f"Snapshot '{snapname}' created"
        
    except Exception as e:
        logger.exception(f"Failed to create snapshot: {e}")
        return False, f"Snapshot failed: {str(e)}"


def revert_vm_to_snapshot(vmid: int, node: str, snapname: str,
                          cluster_ip: str = None) -> Tuple[bool, str]:
    """Revert a VM to a snapshot.
    
    Args:
        vmid: VM ID
        node: Node name
        snapname: Snapshot name
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, message)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, "Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        proxmox.nodes(node).qemu(vmid).snapshot(snapname).rollback.post()
        
        logger.info(f"Reverted VM {vmid} to snapshot {snapname}")
        return True, f"Reverted to snapshot '{snapname}'"
        
    except Exception as e:
        logger.exception(f"Failed to revert to snapshot: {e}")
        return False, f"Revert failed: {str(e)}"


def delete_vm(vmid: int, node: str, cluster_ip: str = None) -> Tuple[bool, str]:
    """Delete a VM using Proxmox API for stop and SSH for deletion.
    
    Ensures VM is stopped before deletion to prevent errors.
    Uses Proxmox API for VM control (better than SSH) and SSH for actual deletion.
    
    CRITICAL SAFETY: This function checks if the VM is a template before deletion.
    Templates should NEVER be deleted via this function.
    
    Args:
        vmid: VM ID
        node: Node name
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, message)
    """
    from app.services.ssh_executor import get_ssh_executor_from_config
    from app.services.vm_core import wait_for_vm_stopped
    from app.models import Template
    
    # CRITICAL SAFETY CHECK: Never delete template VMs
    template = Template.query.filter_by(proxmox_vmid=vmid).first()
    if template:
        error_msg = f"REFUSED: VM {vmid} is a registered template (id={template.id}, name={template.name}) and cannot be deleted!"
        logger.error(error_msg)
        return False, error_msg
    
    try:
        # Get Proxmox API connection for VM control
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, "Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # Get SSH executor for status checking and deletion
        ssh_executor = get_ssh_executor_from_config()
        ssh_executor.connect()
        
        # Check current VM status using SSH
        try:
            exit_code, stdout, stderr = ssh_executor.execute(
                f"qm status {vmid}",
                check=False,
                timeout=10
            )
            
            if exit_code == 0 and 'running' in stdout.lower():
                # VM is running - need to stop it
                logger.info(f"VM {vmid} is running, stopping it before deletion...")
                
                try:
                    # Use Proxmox API to stop VM (better than SSH)
                    proxmox.nodes(node).qemu(vmid).status.shutdown.post()
                    logger.info(f"Stop command sent for VM {vmid} via Proxmox API")
                except Exception as e:
                    logger.warning(f"Proxmox API stop failed, trying SSH: {e}")
                    # Fallback to SSH if API fails
                    ssh_executor.execute(f"qm stop {vmid}", check=False, timeout=10)
                
                # Wait for VM to be fully stopped (timeout 120 seconds)
                logger.info(f"Waiting for VM {vmid} to be fully stopped...")
                stopped = wait_for_vm_stopped(ssh_executor, vmid, timeout=120)
                if not stopped:
                    # CRITICAL: Proxmox won't delete running VMs - must fail here
                    error_msg = f"VM {vmid} did not stop within 120 seconds - cannot delete running VM"
                    logger.error(error_msg)
                    return False, error_msg
                
                logger.info(f"VM {vmid} confirmed stopped")
            else:
                # VM is already stopped or doesn't exist
                logger.info(f"VM {vmid} is already stopped or doesn't exist, proceeding with deletion")
        
        except Exception as e:
            logger.warning(f"Error checking VM {vmid} status: {e}, attempting deletion anyway")
        
        # Ensure SSH connection is still active before deletion
        # If connection was lost, reconnect
        if ssh_executor._client is None:
            logger.info("SSH connection was lost, reconnecting...")
            ssh_executor.connect()
        
        # Delete the VM using SSH command (only if stopped)
        exit_code, stdout, stderr = ssh_executor.execute(
            f"qm destroy {vmid}",
            check=False,
            timeout=30
        )
        
        if exit_code != 0:
            error_msg = f"Failed to delete VM {vmid}: {stderr.strip()}"
            logger.error(error_msg)
            ssh_executor.disconnect()
            return False, error_msg
        
        logger.info(f"Deleted VM {vmid} successfully")
        ssh_executor.disconnect()
        return True, "VM deleted successfully"
        
    except Exception as e:
        logger.exception(f"Failed to delete VM {vmid}: {e}")
        # Ensure cleanup on error
        try:
            ssh_executor.disconnect()
        except:
            pass
        return False, f"Delete failed: {str(e)}"


def convert_vm_to_template(vmid: int, node: str, cluster_ip: str = None) -> Tuple[bool, str]:
    """Convert a VM to a template.
    
    Args:
        vmid: VM ID
        node: Node name
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, message)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, "Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)

        # Verify VM exists on the target node before conversion to prevent 500 errors
        try:
            proxmox.nodes(node).qemu(vmid).config.get()
        except Exception as cfg_err:
            logger.error(
                f"Template conversion aborted: VMID {vmid} not found on node {node} (config missing): {cfg_err}"
            )
            return False, f"VM {vmid} not found on node {node}; cannot convert to template"
        
        # Ensure VM is stopped before conversion (required for some storages)
        try:
            vm_status = proxmox.nodes(node).qemu(vmid).status.current.get()
            if vm_status.get('status') == 'running':
                logger.info(f"Stopping VM {vmid} on {node} before template conversion")
                proxmox.nodes(node).qemu(vmid).status.shutdown.post()
                import time
                time.sleep(5)
        except Exception as stop_err:
            logger.debug(f"VM stop check/action: {stop_err}")
        
        proxmox.nodes(node).qemu(vmid).template.post()
        logger.info(f"Called template.post() for VM {vmid} on {node}")
        
        # Verify conversion
        import time
        time.sleep(3)
        try:
            cfg = proxmox.nodes(node).qemu(vmid).config.get()
            if cfg.get('template') == 1:
                logger.info(f"Confirmed VM {vmid} is now a template on {node}")
                return True, "VM converted to template"
            else:
                logger.error(f"VM {vmid} config shows template={cfg.get('template')}, conversion may have failed")
                return False, f"Template flag not set (template={cfg.get('template')})"
        except Exception as verify_err:
            logger.warning(f"Could not verify template conversion: {verify_err}")
            return True, "VM conversion called but not verified"
        
    except Exception as e:
        logger.exception(f"Failed to convert VM to template: {e}")
        return False, f"Conversion failed: {str(e)}"


def save_teacher_template(teacher_vm_vmid: int, teacher_vm_node: str, old_base_template_vmid: int,
                          old_base_template_node: str, name_prefix: str, cluster_ip: str = None) -> Tuple[bool, Optional[int], Optional[int], str]:
    """Save teacher's edited VM as the new base template for student VMs.
    
    This clones the teacher's VM, converts the clone to a template, deletes the old base template,
    and keeps the teacher's VM intact for continued editing.
    
    Args:
        teacher_vm_vmid: VMID of teacher's editable VM (will remain intact)
        teacher_vm_node: Node where teacher's VM is located
        old_base_template_vmid: VMID of the current base template (will be deleted)
        old_base_template_node: Node where old template is located
        name_prefix: Prefix for naming the new template
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, new_template_vmid, teacher_vm_vmid, message)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, None, None, "Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # Step 1: Find next available VMID for the new template
        used_vmids = set()
        try:
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                used_vmids.add(r["vmid"])
        except Exception:
            for node_info in proxmox.nodes.get():
                node_name = node_info["node"]
                try:
                    for vm in proxmox.nodes(node_name).qemu.get():
                        used_vmids.add(vm["vmid"])
                    for ct in proxmox.nodes(node_name).lxc.get():
                        used_vmids.add(ct["vmid"])
                except Exception:
                    pass
        
        new_template_vmid = 200
        while new_template_vmid in used_vmids:
            new_template_vmid += 1
        
        # Step 2: Clone teacher's VM to create the new base template
        new_template_name = f"{name_prefix}-BaseTemplate"
        new_template_name = sanitize_vm_name(new_template_name, fallback="classvm-base")
        
        logger.info(f"Cloning teacher's VM {teacher_vm_vmid} to create new base template {new_template_vmid}")
        proxmox.nodes(teacher_vm_node).qemu(teacher_vm_vmid).clone.post(
            newid=new_template_vmid,
            name=new_template_name,
            full=1  # Full clone
        )
        
        import time
        time.sleep(3)  # Wait for clone to initialize
        
        # Step 3: Convert the clone to a template
        logger.info(f"Converting cloned VM {new_template_vmid} to template")
        proxmox.nodes(teacher_vm_node).qemu(new_template_vmid).template.post()
        
        time.sleep(2)  # Brief wait for conversion
        
        # Step 4: Delete old base template
        logger.info(f"Deleting old base template {old_base_template_vmid}")
        try:
            proxmox.nodes(old_base_template_node).qemu(old_base_template_vmid).delete()
            logger.info(f"Deleted old base template {old_base_template_vmid}")
        except Exception as e:
            logger.warning(f"Failed to delete old base template: {e} (continuing anyway)")
        
        logger.info(f"Teacher template saved: VM {new_template_vmid} is now the base template, teacher VM {teacher_vm_vmid} remains editable")
        return True, new_template_vmid, teacher_vm_vmid, f"Template saved successfully. New base template is VM {new_template_vmid}, your editable VM {teacher_vm_vmid} is still available."
        
    except Exception as e:
        logger.exception(f"Failed to save teacher template: {e}")
        return False, None, None, f"Save failed: {str(e)}"


def reimage_teacher_vm(old_teacher_vm_vmid: int, old_teacher_vm_node: str, 
                       base_template_vmid: int, base_template_node: str,
                       name_prefix: str, cluster_ip: str = None) -> Tuple[bool, Optional[int], str]:
    """Reimage teacher's VM by cloning from the base template.
    
    This deletes the teacher's current VM and creates a fresh clone from the base template.
    
    Args:
        old_teacher_vm_vmid: VMID of teacher's current VM (will be deleted)
        old_teacher_vm_node: Node where teacher's VM is located
        base_template_vmid: VMID of the base template to clone from
        base_template_node: Node where base template is located
        name_prefix: Prefix for naming the new VM
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, new_teacher_vm_vmid, message)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, None, "Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # Step 1: Delete old teacher VM
        logger.info(f"Deleting old teacher VM {old_teacher_vm_vmid}")
        try:
            proxmox.nodes(old_teacher_vm_node).qemu(old_teacher_vm_vmid).delete()
            logger.info(f"Deleted old teacher VM {old_teacher_vm_vmid}")
        except Exception as e:
            logger.warning(f"Failed to delete old teacher VM: {e} (continuing anyway)")
        
        import time
        time.sleep(3)  # Wait for deletion to complete
        
        # Step 2: Clone base template to create new teacher VM (reuse same VMID)
        new_teacher_name = f"{name_prefix}-Template"
        new_teacher_name = sanitize_vm_name(new_teacher_name, fallback="classvm-template")
        
        logger.info(f"Cloning base template {base_template_vmid} to create new teacher VM {old_teacher_vm_vmid}")
        proxmox.nodes(base_template_node).qemu(base_template_vmid).clone.post(
            newid=old_teacher_vm_vmid,  # Reuse same VMID
            name=new_teacher_name,
            full=1  # Full clone so teacher can edit
        )
        
        logger.info(f"Teacher VM reimaged: new VM {old_teacher_vm_vmid} created from base template {base_template_vmid}")
        return True, old_teacher_vm_vmid, f"Teacher VM reimaged successfully. VM {old_teacher_vm_vmid} is now a fresh copy from the base template."
        
    except Exception as e:
        logger.exception(f"Failed to reimage teacher VM: {e}")
        return False, None, f"Reimage failed: {str(e)}"


def push_template_to_students(base_template_vmid: int, base_template_node: str, 
                              student_vm_list: List[Dict[str, Any]], name_prefix: str,
                              cluster_ip: str = None, task_id: str = None,
                              max_workers: int = 10, storage: str = None) -> Tuple[bool, List[Dict[str, Any]], str]:
    """Replace all student VMs with fresh linked clones from the updated base template.
    
    This deletes all existing student VMs and creates new linked clones from the base template.
    
    Args:
        base_template_vmid: VMID of the base template to clone from
        base_template_node: Node where base template is located
        student_vm_list: List of current student VMs to delete (dicts with vmid, node, name)
        name_prefix: Prefix for new VM names
        cluster_ip: IP of the Proxmox cluster
        task_id: Optional task ID for progress tracking
        max_workers: Maximum parallel workers (default: 10)
        storage: Optional storage target
    
    Returns:
        Tuple of (success, new_vm_list, message)
    """
    from app.services.clone_progress import update_clone_progress
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time
    
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, [], "Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # Step 1: Delete all existing student VMs
        logger.info(f"Deleting {len(student_vm_list)} existing student VMs")
        if task_id:
            update_clone_progress(task_id, status="running", total=len(student_vm_list))
        
        deleted_count = 0
        for vm in student_vm_list:
            try:
                proxmox.nodes(vm['node']).qemu(vm['vmid']).delete()
                deleted_count += 1
                logger.info(f"Deleted student VM {vm['vmid']} ({vm.get('name', 'unnamed')})")
            except Exception as e:
                logger.warning(f"Failed to delete VM {vm['vmid']}: {e}")
        
        logger.info(f"Deleted {deleted_count}/{len(student_vm_list)} student VMs")
        
        # Brief wait for deletions to complete
        time.sleep(3)
        
        # Step 2: Create new linked clones from the base template
        # IMPORTANT: Linked clones MUST be on same node as template
        logger.info(f"Creating {len(student_vm_list)} new linked clones from template {base_template_vmid} on {base_template_node}")
        
        new_vms = []
        vm_specs = []
        
        for i, old_vm in enumerate(student_vm_list):
            vm_name = old_vm.get('name') or f"{name_prefix}-{i+1}"
            vm_specs.append({
                "vmid": old_vm['vmid'],  # Reuse old VMID
                "name": vm_name,
                "node": base_template_node,  # Must use template's node for linked clones
                "index": i
            })
        
        # Parallel cloning
        def clone_single_vm(spec):
            if task_id:
                update_clone_progress(task_id, current_vm=spec["name"])
            
            success, msg = clone_vm_from_template(
                template_vmid=base_template_vmid,
                new_vmid=spec["vmid"],
                name=spec["name"],
                node=spec["node"],
                cluster_ip=cluster_ip,
                max_retries=10,
                full_clone=False,  # Linked clones
                create_baseline=False,
                storage=storage
            )
            
            if success:
                return {
                    "success": True,
                    "vmid": spec["vmid"],
                    "name": spec["name"],
                    "node": spec["node"]
                }
            else:
                logger.error(f"Failed to clone VM {spec['name']}: {msg}")
                return {"success": False, "error": msg}
        
        with ThreadPoolExecutor(max_workers=min(max_workers, len(vm_specs))) as executor:
            future_to_spec = {
                executor.submit(clone_single_vm, spec): spec 
                for spec in vm_specs
            }
            
            for future in as_completed(future_to_spec):
                result = future.result()
                if result["success"]:
                    new_vms.append({
                        "vmid": result["vmid"],
                        "name": result["name"],
                        "node": result["node"],
                        "is_template_vm": False
                    })
                    if task_id:
                        update_clone_progress(task_id, completed=len(new_vms))
        
        logger.info(f"Created {len(new_vms)}/{len(student_vm_list)} new student VMs")
        return True, new_vms, f"Successfully pushed template to {len(new_vms)} student VMs"
        
    except Exception as e:
        logger.exception(f"Failed to push template to students: {e}")
        return False, [], f"Push failed: {str(e)}"


def convert_template_to_vm(template_vmid: int, new_vmid: int, name: str, node: str,
                           cluster_ip: str = None) -> Tuple[bool, Optional[int], str]:
    """Convert a template to a regular VM by cloning it.
    
    Since Proxmox templates can't be directly converted back to VMs,
    we clone the template and return the new VM.
    
    Args:
        template_vmid: VMID of the template
        new_vmid: VMID for the new VM
        name: Name for the VM
        node: Node name
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, new_vmid, message)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, None, "Cluster not found"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        safe_name = sanitize_vm_name(name)
        
        # Clone template to create a regular VM
        proxmox.nodes(node).qemu(template_vmid).clone.post(
            newid=new_vmid,
            name=safe_name,
            full=1  # Full clone
        )
        
        logger.info(f"Converted template {template_vmid} to VM {new_vmid} ({safe_name}) on {node}")
        
        # Wait for clone to complete
        import time
        time.sleep(5)
        
        # Create baseline snapshot
        try:
            proxmox.nodes(node).qemu(new_vmid).snapshot.post(
                snapname='baseline',
                description='Initial baseline snapshot for reimage'
            )
            logger.info(f"Created baseline snapshot for VM {new_vmid}")
        except Exception as e:
            logger.warning(f"Failed to create baseline snapshot for VM {new_vmid}: {e}")
        
        return True, new_vmid, f"Template converted to VM {safe_name}"
        
    except Exception as e:
        logger.exception(f"Failed to convert template to VM: {e}")
        return False, None, f"Conversion failed: {str(e)}"


def clone_template_for_class(template_vmid: int, node: str, name: str,
                             cluster_ip: str = None) -> Tuple[bool, Optional[int], str]:
    """Clone a template to create a working copy for a class.
    
    Args:
        template_vmid: VMID of the template to clone
        node: Node where template exists
        name: Name for the cloned VM
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Tuple of (success, new_vmid, message)
    """
    try:
        # Validate node parameter
        if not node:
            logger.error(f"clone_template_for_class: node parameter is empty or None")
            return False, None, "Template node is not set. Please ensure the template has a valid node."
        
        target_ip = cluster_ip or CLASS_CLUSTER_IP
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == target_ip:
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return False, None, f"Cluster not found for IP: {target_ip}"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        safe_name = sanitize_vm_name(name, fallback="clonetemplate")
        logger.info(f"Cloning template VMID {template_vmid} from node '{node}' to create '{safe_name}' (original requested name: '{name}')")
        
        # Find next available VMID - use cluster resources API for complete view
        used_vmids = set()
        try:
            # Use cluster resources API to get ALL VMs and containers across all nodes
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                used_vmids.add(r["vmid"])
            logger.info(f"Found {len(used_vmids)} existing VMIDs via cluster resources API")
        except Exception as e:
            logger.warning(f"Cluster resources API failed, falling back to per-node query: {e}")
            # Fallback: query each node individually
            for node_info in proxmox.nodes.get():
                node_name = node_info["node"]
                try:
                    # Get both QEMU VMs and LXC containers
                    for vm in proxmox.nodes(node_name).qemu.get():
                        used_vmids.add(vm["vmid"])
                    for ct in proxmox.nodes(node_name).lxc.get():
                        used_vmids.add(ct["vmid"])
                except Exception as node_error:
                    logger.warning(f"Failed to query node {node_name}: {node_error}")
        
        # Find next available VMID starting from 100
        new_vmid = 100
        while new_vmid in used_vmids:
            new_vmid += 1
        
        logger.info(f"Selected new VMID: {new_vmid} (next available after checking {len(used_vmids)} existing VMs)")
        
        # Clone the template (full clone, not linked)
        logger.info(f"Calling proxmox.nodes('{node}').qemu({template_vmid}).clone.post(newid={new_vmid}, name='{safe_name}', full=1)")
        
        try:
            proxmox.nodes(node).qemu(template_vmid).clone.post(
                newid=new_vmid,
                name=safe_name,
                full=1  # Full clone
            )
        except Exception as clone_error:
            # If clone fails due to VMID conflict, try once more with next VMID
            if "already exists" in str(clone_error).lower() or "vmid" in str(clone_error).lower():
                logger.warning(f"VMID {new_vmid} conflict detected, trying next VMID...")
                new_vmid += 1
                while new_vmid in used_vmids:
                    new_vmid += 1
                
                logger.info(f"Retrying clone with VMID {new_vmid}")
                proxmox.nodes(node).qemu(template_vmid).clone.post(
                    newid=new_vmid,
                    name=safe_name,
                    full=1
                )
            else:
                raise
        
        logger.info(f"Cloned template {template_vmid} → {new_vmid} ({safe_name}) on {node}")
        return True, new_vmid, f"Successfully cloned template '{safe_name}' to VMID {new_vmid}"
        
    except Exception as e:
        logger.exception(f"Failed to clone template: {e}")
        return False, None, f"Clone failed: {str(e)}"


def create_vm_shells(count: int, name_prefix: str, node: str, cluster_ip: str = None,
                    cpu_cores: int = 2, memory_mb: int = 2048) -> List[Dict[str, Any]]:
    """Create empty VM shells (VMs without disks) that can be populated later.
    
    This is much faster than full cloning and allows for progressive disk population.
    
    Args:
        count: Number of VM shells to create
        name_prefix: Prefix for VM names (e.g., "class-intro-linux")
        node: Target node for VMs
        cluster_ip: IP of the Proxmox cluster
        cpu_cores: Number of CPU cores per VM (default: 2)
        memory_mb: RAM in MB per VM (default: 2048)
        
    Returns:
        List of dicts with keys: vmid, name, node, success, error
    """
    results = []
    target_ip = cluster_ip or CLASS_CLUSTER_IP
    cluster_id = None
    
    for cluster in CLUSTERS:
        if cluster["host"] == target_ip:
            cluster_id = cluster["id"]
            break
    
    if not cluster_id:
        logger.error(f"Cluster not found for IP: {target_ip}")
        return []
    
    try:
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # Get all used VMIDs
        used_vmids = set()
        resources = proxmox.cluster.resources.get(type="vm")
        for r in resources:
            used_vmids.add(r["vmid"])
        
        logger.info(f"Creating {count} VM shells on node '{node}'...")
        
        for i in range(1, count + 1):
            # Find next available VMID
            new_vmid = 100
            while new_vmid in used_vmids:
                new_vmid += 1
            used_vmids.add(new_vmid)  # Reserve this VMID
            
            vm_name = sanitize_vm_name(f"{name_prefix}-{i}", fallback=f"vm-{new_vmid}")
            
            try:
                # Create VM shell with minimal config (no disks)
                proxmox.nodes(node).qemu.post(
                    vmid=new_vmid,
                    name=vm_name,
                    memory=memory_mb,
                    cores=cpu_cores,
                    sockets=1,
                    cpu='host',
                    ostype='l26',  # Linux 2.6+ kernel
                    net0='virtio,bridge=vmbr0',  # Network interface
                    # No disk attached - that comes later
                )
                
                # Fetch MAC address from VM config
                mac_address = None
                try:
                    import time
                    time.sleep(0.5)  # Brief delay to ensure config is written
                    vm_config = proxmox.nodes(node).qemu(new_vmid).config.get()
                    net0 = vm_config.get('net0', '')
                    # Parse MAC from net0 config (format: "virtio=XX:XX:XX:XX:XX:XX,bridge=vmbr0")
                    if '=' in net0:
                        parts = net0.split(',')
                        for part in parts:
                            if ':' in part and len(part.replace(':', '')) == 12:
                                # Found MAC address
                                mac_address = part.split('=')[-1] if '=' in part else part
                                break
                    logger.info(f"VM {new_vmid} MAC address: {mac_address}")
                except Exception as e:
                    logger.warning(f"Failed to fetch MAC address for VM {new_vmid}: {e}")
                
                logger.info(f"Created VM shell: {vm_name} (VMID {new_vmid})")
                results.append({
                    'vmid': new_vmid,
                    'name': vm_name,
                    'node': node,
                    'mac_address': mac_address,
                    'success': True,
                    'error': None
                })
                
            except Exception as e:
                logger.error(f"Failed to create VM shell {vm_name}: {e}")
                results.append({
                    'vmid': new_vmid,
                    'name': vm_name,
                    'node': node,
                    'success': False,
                    'error': str(e)
                })
        
        return results
        
    except Exception as e:
        logger.exception(f"Failed to create VM shells: {e}")
        return []


def populate_vm_shell_with_disk(vmid: int, node: str, template_vmid: int, template_node: str, 
                                cluster_ip: str = None) -> Tuple[bool, str]:
    """Populate an empty VM shell with a disk cloned from a template.
    
    Uses Proxmox's disk clone API to copy the template's disk directly to the VM shell.
    
    Args:
        vmid: VMID of the empty VM shell
        node: Node where the VM shell exists
        template_vmid: VMID of the template to clone disk from
        template_node: Node where the template exists
        cluster_ip: IP of the Proxmox cluster
        
    Returns:
        Tuple of (success, message)
    """
    target_ip = cluster_ip or CLASS_CLUSTER_IP
    cluster_id = None
    
    for cluster in CLUSTERS:
        if cluster["host"] == target_ip:
            cluster_id = cluster["id"]
            break
    
    if not cluster_id:
        return False, f"Cluster not found for IP: {target_ip}"
    
    try:
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # Get template disk configuration
        template_config = proxmox.nodes(template_node).qemu(template_vmid).config.get()
        
        # Find the boot disk (usually scsi0 or virtio0)
        disk_key = None
        disk_config = None
        for key in ['scsi0', 'virtio0', 'sata0', 'ide0']:
            if key in template_config:
                disk_key = key
                disk_config = template_config[key]
                break
        
        if not disk_key or not disk_config:
            return False, f"No disk found in template {template_vmid}"
        
        logger.info(f"Template disk config: {disk_key}={disk_config}")
        
        # Extract storage and format from disk config
        # Format: "storage:volume,size=XXG" or "storage:vm-100-disk-0"
        storage = disk_config.split(':')[0] if ':' in disk_config else 'local-lvm'
        
        # Use Proxmox's disk clone/copy API
        # The moveDisk endpoint can clone a disk from one VM to another
        logger.info(f"Cloning disk from template VM {template_vmid} to VM {vmid}")
        
        # Clone the template disk to the target VM using the disk copy operation
        # This directly copies the disk without creating a full VM clone
        proxmox.nodes(template_node).qemu(template_vmid).disk(disk_key).copy.post(
            target_vmid=vmid,
            target_storage=storage,
            target_disk=disk_key
        )
        
        logger.info(f"Successfully populated VM {vmid} with disk from template {template_vmid}")
        return True, f"Disk cloned successfully to VM {vmid}"
        
    except Exception as e:
        logger.exception(f"Failed to populate VM shell {vmid} with disk: {e}")
        return False, f"Disk population failed: {str(e)}"


def remove_vm_disk(vmid: int, node: str, cluster_ip: str = None) -> Tuple[bool, str]:
    """Remove the disk from a VM (detach and delete).
    
    Args:
        vmid: VMID of the VM
        node: Node where the VM exists
        cluster_ip: IP of the Proxmox cluster
        
    Returns:
        Tuple of (success, message)
    """
    target_ip = cluster_ip or CLASS_CLUSTER_IP
    cluster_id = None
    
    for cluster in CLUSTERS:
        if cluster["host"] == target_ip:
            cluster_id = cluster["id"]
            break
    
    if not cluster_id:
        return False, f"Cluster not found for IP: {target_ip}"
    
    try:
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # Get VM config to find the disk
        vm_config = proxmox.nodes(node).qemu(vmid).config.get()
        
        # Find the boot disk (usually scsi0 or virtio0)
        disk_key = None
        for key in ['scsi0', 'virtio0', 'sata0', 'ide0']:
            if key in vm_config:
                disk_key = key
                break
        
        if not disk_key:
            return False, f"No disk found in VM {vmid}"
        
        logger.info(f"Removing disk {disk_key} from VM {vmid}")
        
        # Remove the disk by setting it to "none" in the VM config
        # This detaches and deletes the disk
        proxmox.nodes(node).qemu(vmid).config.put(**{disk_key: 'none'})
        
        logger.info(f"Successfully removed disk from VM {vmid}")
        return True, f"Disk removed from VM {vmid}"
        
    except Exception as e:
        logger.exception(f"Failed to remove disk from VM {vmid}: {e}")
        return False, f"Disk removal failed: {str(e)}"


def copy_vm_disk(source_vmid: int, source_node: str, target_vmid: int, target_node: str, 
                 cluster_ip: str = None) -> Tuple[bool, str]:
    """Copy disk from source VM to target VM (target must have no disk or disk removed first).
    
    Args:
        source_vmid: VMID of the source VM
        source_node: Node where the source VM exists
        target_vmid: VMID of the target VM
        target_node: Node where the target VM exists
        cluster_ip: IP of the Proxmox cluster
        
    Returns:
        Tuple of (success, message)
    """
    target_ip = cluster_ip or CLASS_CLUSTER_IP
    cluster_id = None
    
    for cluster in CLUSTERS:
        if cluster["host"] == target_ip:
            cluster_id = cluster["id"]
            break
    
    if not cluster_id:
        return False, f"Cluster not found for IP: {target_ip}"
    
    try:
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # Get source VM disk configuration
        source_config = proxmox.nodes(source_node).qemu(source_vmid).config.get()
        
        # Find the boot disk (usually scsi0 or virtio0)
        disk_key = None
        disk_config = None
        for key in ['scsi0', 'virtio0', 'sata0', 'ide0']:
            if key in source_config:
                disk_key = key
                disk_config = source_config[key]
                break
        
        if not disk_key or not disk_config:
            return False, f"No disk found in source VM {source_vmid}"
        
        logger.info(f"Source VM disk config: {disk_key}={disk_config}")
        
        # Extract storage from disk config
        storage = disk_config.split(':')[0] if ':' in disk_config else 'local-lvm'
        
        logger.info(f"Copying disk from VM {source_vmid} to VM {target_vmid}")
        
        # Clone the source disk to the target VM
        proxmox.nodes(source_node).qemu(source_vmid).disk(disk_key).copy.post(
            target_vmid=target_vmid,
            target_storage=storage,
            target_disk=disk_key
        )
        
        logger.info(f"Successfully copied disk from VM {source_vmid} to VM {target_vmid}")
        return True, f"Disk copied from VM {source_vmid} to VM {target_vmid}"
        
    except Exception as e:
        logger.exception(f"Failed to copy disk from VM {source_vmid} to VM {target_vmid}: {e}")
        return False, f"Disk copy failed: {str(e)}"
