#!/usr/bin/env python3
"""
Proxmox Operations - VM cloning, template management, and snapshots.

This module provides higher-level operations for class-based VM management.
"""

import logging
import re
from typing import List, Dict, Tuple, Any, Optional

from app.services.proxmox_service import get_proxmox_admin_for_cluster
from app.config import CLUSTERS

logger = logging.getLogger(__name__)

# Default cluster IP for class operations (restricted to single cluster)
CLASS_CLUSTER_IP = "10.220.15.249"


def sanitize_vm_name(name: str, fallback: str = "vm") -> str:
    """Sanitize a proposed VM name to satisfy Proxmox (DNS-style) constraints.

    Rules applied (approx RFC 1123 hostname subset):
    - Lowercase all characters
    - Allow only a-z, 0-9, hyphen; replace others with '-'
    - Collapse consecutive hyphens
    - Trim leading/trailing hyphens
    - Ensure starts/ends with alphanumeric (prepend/append fallback tokens if needed)
    - Limit length to 63 chars
    - If result empty, use fallback
    """
    if not isinstance(name, str):
        name = str(name) if name is not None else ""
    original = name
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name)
    name = name.strip('-')
    if not name:
        name = fallback
    # Ensure starts with alphanumeric
    if not name[0].isalnum():
        name = f"{fallback}-{name}" if name else fallback
    # Ensure ends with alphanumeric
    if not name[-1].isalnum():
        name = f"{name}0"
    # Truncate to 63 characters
    if len(name) > 63:
        name = name[:63].rstrip('-') or fallback
    if name != original:
        logger.info(f"sanitize_vm_name: '{original}' -> '{name}'")
    return name
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
                           wait_until_complete: bool = True, wait_timeout_sec: int = 900) -> Tuple[bool, str]:
    """Clone a VM from a template.
    
    Args:
        template_vmid: VMID of the template to clone
        new_vmid: VMID for the new VM
        name: Name for the new VM
        node: Node to create the VM on
        cluster_ip: IP of the Proxmox cluster (restricted to 10.220.15.249)
    
    Returns:
        Tuple of (success, message)
    """
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

        safe_name = sanitize_vm_name(name)

        import time
        attempt = 0
        while True:
            try:
                clone_result = proxmox.nodes(node).qemu(template_vmid).clone.post(
                    newid=new_vmid,
                    name=safe_name,
                    full=1  # Full clone (not linked)
                )
                logger.info(f"Cloned template {template_vmid} to {new_vmid} ({safe_name}) on {node} (attempt {attempt+1})")
                break
            except Exception as clone_err:
                msg = str(clone_err).lower()
                # Typical locked error: 'VM is locked (clone)' – wait and retry
                if ('locked' in msg or 'busy' in msg) and attempt < max_retries - 1:
                    attempt += 1
                    logger.warning(f"Template VMID {template_vmid} locked/busy, retrying in {retry_delay}s (attempt {attempt}/{max_retries})")
                    time.sleep(retry_delay)
                    continue
                logger.exception(f"Clone failed for template {template_vmid} after {attempt+1} attempts: {clone_err}")
                return False, f"Clone failed: {clone_err}"

        # Optional: Poll task status until finished (stopped: OK) or timeout
        if wait_until_complete:
            try:
                upid = None
                if isinstance(clone_result, dict):
                    upid = clone_result.get('upid')
                # Some setups may return a string UPID directly
                if isinstance(clone_result, str) and 'UPID' in clone_result:
                    upid = clone_result

                if upid:
                    logger.info(f"Polling clone task {upid} until completion (timeout {wait_timeout_sec}s)")
                    waited = 0
                    poll_interval = 4
                    while waited < wait_timeout_sec:
                        try:
                            status = proxmox.nodes(node).tasks(upid).status.get()
                            # Status fields: status (e.g., 'stopped'), exitstatus (e.g., 'OK'), id
                            st = status.get('status')
                            exitst = status.get('exitstatus')
                            if st == 'stopped' and (exitst is None or exitst == 'OK'):
                                logger.info(f"Clone task {upid} completed: status={st}, exitstatus={exitst}")
                                break
                        except Exception as e:
                            logger.debug(f"Task status poll error for {upid}: {e}")
                            # Continue polling; transient errors can occur
                        time.sleep(poll_interval)
                        waited += poll_interval
                    if waited >= wait_timeout_sec:
                        logger.warning(f"Clone task {upid} did not complete within {wait_timeout_sec}s; continuing")
                else:
                    logger.warning("Clone result did not include UPID; skipping task polling")
            except Exception as e:
                logger.warning(f"Failed to poll clone task completion: {e}")

            # Additionally, poll VM status until no 'lock' and status available
            try:
                waited = 0
                poll_interval = 4
                while waited < wait_timeout_sec:
                    try:
                        current = proxmox.nodes(node).qemu(new_vmid).status.current.get()
                        lock = current.get('lock')
                        # Accept when lock cleared; VM may be stopped after clone
                        if not lock:
                            break
                    except Exception:
                        pass
                    time.sleep(poll_interval)
                    waited += poll_interval
            except Exception as e:
                logger.debug(f"VM status poll error post-clone: {e}")
        
        # Create baseline snapshot for reimage functionality
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
                        cluster_ip: str = None, task_id: str = None, per_vm_retry: int = 5, 
                        concurrent: bool = True, max_workers: int = 3,
                        include_template_vm: bool = True, smart_placement: bool = True) -> List[Dict[str, Any]]:
    """Clone multiple VMs from a template for a class.
    
    Args:
        template_vmid: VMID of the template
        node: Fallback node if smart placement disabled or fails
        count: Number of student VMs to clone
        name_prefix: Prefix for VM names (will append number)
        cluster_ip: IP of the Proxmox cluster (restricted to 10.220.15.249)
        task_id: Optional task ID for progress tracking
        concurrent: If True, clone VMs in parallel (default: True)
        max_workers: Maximum parallel clones (default: 3)
        include_template_vm: If True, create a reference VM from template (default: True)
        smart_placement: If True, distribute VMs across nodes based on resources (default: True)
    
    Returns:
        List of dicts with keys: vmid, name, node, is_template_vm (bool)
    """
    from app.services.clone_progress import update_clone_progress
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from app.services.node_selector import select_best_node, distribute_vms_across_nodes
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
        
        next_vmid = 200  # Start from 200 for class VMs
        
        # Smart node selection: distribute VMs across nodes based on resources
        node_assignments = []
        if smart_placement:
            logger.info(f"Using smart placement with dynamic re-scoring to distribute {count + (1 if include_template_vm else 0)} VMs across cluster")
            total_vms = (1 if include_template_vm else 0) + count
            
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
                                } except Exception:
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
            
            # Get node assignments with dynamic re-scoring based on estimated resources
            node_assignments = distribute_vms_across_nodes(
                cluster_id, 
                total_vms,
                estimated_ram_mb=estimated_ram_mb,
                estimated_cpu_cores=estimated_cores
            )
            
            if not node_assignments:
                logger.warning("Smart placement failed, falling back to single node")
                node_assignments = [node] * total_vms
        else:
            logger.info(f"Smart placement disabled, using node: {node}")
            total_vms = (1 if include_template_vm else 0) + count
            node_assignments = [node] * total_vms
        
        assignment_index = 0  # Track which node to use next
        
        # Step 1: Create template reference VM if requested
        template_vm_info = None
        if include_template_vm:
            # Find VMID for template VM
            while next_vmid in used_vmids:
                next_vmid += 1
            
            template_vm_name = f"{name_prefix}-Template"
            template_vm_name = sanitize_vm_name(template_vm_name, fallback="classvm-template")
            template_vm_node = node_assignments[assignment_index]
            assignment_index += 1
            
            logger.info(f"Creating template reference VM: {template_vm_name} (VMID {next_vmid}) on node {template_vm_node}")
            if task_id:
                update_clone_progress(task_id, current_vm=template_vm_name)
            
            success, msg = clone_vm_from_template(
                template_vmid=template_vmid,
                new_vmid=next_vmid,
                name=template_vm_name,
                node=template_vm_node,
                cluster_ip=cluster_ip,
                max_retries=per_vm_retry
            )
            
            if success:
                template_vm_info = {
                    "vmid": next_vmid,
                    "name": template_vm_name,
                    "node": template_vm_node,
                    "is_template_vm": True  # Mark as the template reference VM
                }
                created_vms.append(template_vm_info)
                used_vmids.add(next_vmid)
                next_vmid += 1
                logger.info(f"Created template reference VM {template_vm_name} (VMID {next_vmid-1}) on {template_vm_node}")
                # Wait for Proxmox clone lock to clear before proceeding with student clones
                try:
                    proxmox = get_proxmox_admin_for_cluster(cluster_id)
                    check_node = template_vm_node
                    check_vmid = template_vm_info["vmid"]
                    # Poll status for a short period to detect lock clearing
                    max_wait_seconds = 120
                    interval = 4
                    waited = 0
                    while waited < max_wait_seconds:
                        try:
                            status = proxmox.nodes(check_node).qemu(check_vmid).status.current.get()
                            # When cloning finishes, status returns with no 'lock' or lock != 'clone'
                            lock = status.get('lock')
                            if not lock or lock != 'clone':
                                logger.info(f"Template VM {check_vmid} lock cleared after {waited}s")
                                break
                        except Exception as e:
                            logger.debug(f"Polling template VM status error: {e}")
                            break
                        time.sleep(interval)
                        waited += interval
                    if waited >= max_wait_seconds:
                        logger.warning(f"Template VM {check_vmid} still locked after {max_wait_seconds}s; proceeding with student clones")
                except Exception as e:
                    logger.warning(f"Failed to poll template VM lock state: {e}")
                
                if task_id:
                    update_clone_progress(task_id, completed=1)
            else:
                logger.error(f"Failed to create template VM: {msg}")
                if task_id:
                    update_clone_progress(task_id, failed=1, error=msg)
        
        # Step 2: Prepare student VM specifications
        vm_specs = []
        for i in range(count):
            # Find next available VMID
            while next_vmid in used_vmids:
                next_vmid += 1
            
            vm_name = f"{name_prefix}-{i+1}"
            vm_name = sanitize_vm_name(vm_name, fallback="classvm")
            vm_node = node_assignments[assignment_index]
            assignment_index += 1
            
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
                
                success, msg = clone_vm_from_template(
                    template_vmid=template_vmid,
                    new_vmid=spec["vmid"],
                    name=spec["name"],
                    node=spec["node"],  # Use assigned node from smart placement
                    cluster_ip=cluster_ip,
                    max_retries=per_vm_retry
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
                # Submit all clone jobs
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
                
                success, msg = clone_vm_from_template(
                    template_vmid=template_vmid,
                    new_vmid=spec["vmid"],
                    name=spec["name"],
                    node=spec["node"],  # Use assigned node from smart placement
                    cluster_ip=cluster_ip,
                    max_retries=per_vm_retry
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
        proxmox.nodes(node).qemu(vmid).status.start.post()
        
        logger.info(f"Started VM {vmid} on {node}")
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
        proxmox.nodes(node).qemu(vmid).status.shutdown.post()
        
        logger.info(f"Stopped VM {vmid} on {node}")
        return True, "VM stopped successfully"
        
    except Exception as e:
        logger.exception(f"Failed to stop VM {vmid}: {e}")
        return False, f"Stop failed: {str(e)}"


def get_vm_status(vmid: int, node: str = None, cluster_ip: str = None) -> Dict[str, Any]:
    """Get VM status.
    
    Args:
        vmid: VM ID
        node: Node name (optional, will be auto-discovered if not provided)
        cluster_ip: IP of the Proxmox cluster
    
    Returns:
        Dict with VM status info (just the status string, not full object)
    """
    try:
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            return "unknown"
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # If node not provided, find it from cluster resources
        if not node or node == "qemu" or node == "None":
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                if r.get('vmid') == vmid:
                    node = r.get('node')
                    break
        
        if not node:
            return "unknown"
        
        status = proxmox.nodes(node).qemu(vmid).status.current.get()
        
        # Return just the status string (running/stopped) not the full object
        return status.get("status", "unknown")
        
    except Exception as e:
        logger.exception(f"Failed to get VM status: {e}")
        return "unknown"


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
    """Delete a VM.
    
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
        proxmox.nodes(node).qemu(vmid).delete()
        
        logger.info(f"Deleted VM {vmid} on {node}")
        return True, "VM deleted successfully"
        
    except Exception as e:
        logger.exception(f"Failed to delete VM {vmid}: {e}")
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
        proxmox.nodes(node).qemu(vmid).template.post()
        
        logger.info(f"Converted VM {vmid} to template on {node}")
        return True, "VM converted to template"
        
    except Exception as e:
        logger.exception(f"Failed to convert VM to template: {e}")
        return False, f"Conversion failed: {str(e)}"


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
