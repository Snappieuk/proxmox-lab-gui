#!/usr/bin/env python3
"""
Proxmox operations for class-based lab management.

Provides functions for:
- Listing templates from cluster
- Cloning VMs from templates
- Converting VMs to templates
- VM snapshot/revert for fast restore
- VM deletion
"""

import logging
import time
from typing import Optional, List, Dict, Any, Tuple

from proxmox_client import get_proxmox_admin_for_cluster, CLUSTERS

logger = logging.getLogger(__name__)

# Target cluster IP for class templates (from problem statement)
CLASS_CLUSTER_IP = '10.220.15.249'


def get_cluster_id_for_ip(cluster_ip: str) -> Optional[str]:
    """Get cluster_id for a given cluster IP address."""
    for cluster in CLUSTERS:
        if cluster.get('host') == cluster_ip:
            return cluster['id']
    return None


def get_proxmox_for_ip(cluster_ip: str = CLASS_CLUSTER_IP):
    """Get Proxmox API client for a specific cluster IP.
    
    Defaults to the class management cluster (10.220.15.249).
    """
    cluster_id = get_cluster_id_for_ip(cluster_ip)
    if not cluster_id:
        # Fallback to first cluster if IP not found
        logger.warning("Cluster IP %s not found, using first cluster", cluster_ip)
        cluster_id = CLUSTERS[0]['id']
    return get_proxmox_admin_for_cluster(cluster_id)


def list_proxmox_templates(cluster_ip: str = CLASS_CLUSTER_IP) -> List[Dict[str, Any]]:
    """List all VM templates from a Proxmox cluster.
    
    Returns templates from cluster.resources where template=1.
    Only returns templates from the specified cluster (default: 10.220.15.249).
    """
    try:
        proxmox = get_proxmox_for_ip(cluster_ip)
        resources = proxmox.cluster.resources.get(type='vm') or []
        
        templates = []
        for r in resources:
            if r.get('template') == 1:
                templates.append({
                    'vmid': r.get('vmid'),
                    'name': r.get('name', f"template-{r.get('vmid')}"),
                    'node': r.get('node'),
                    'type': r.get('type', 'qemu'),  # qemu or lxc
                    'status': r.get('status', 'unknown'),
                    'maxmem': r.get('maxmem', 0),
                    'maxdisk': r.get('maxdisk', 0),
                    'maxcpu': r.get('maxcpu', 0),
                })
        
        # Sort by name
        templates.sort(key=lambda t: t['name'].lower())
        logger.info("Found %d templates on cluster %s", len(templates), cluster_ip)
        return templates
    
    except Exception as e:
        logger.exception("Failed to list templates from cluster %s: %s", cluster_ip, e)
        return []


def get_next_vmid(cluster_ip: str = CLASS_CLUSTER_IP) -> int:
    """Get the next available VMID from Proxmox cluster.
    
    Uses Proxmox API to get the next available ID. Falls back to a
    random ID in the 10000-99999 range if API fails.
    """
    import random
    
    try:
        proxmox = get_proxmox_for_ip(cluster_ip)
        result = proxmox.cluster.nextid.get()
        return int(result)
    except Exception as e:
        logger.exception("Failed to get next VMID from API: %s", e)
        # Fallback: generate random VMID in high range to avoid collisions
        # Using 10000-99999 range is less likely to collide with existing VMs
        fallback_vmid = random.randint(10000, 99999)
        logger.warning("Using fallback VMID: %d (should verify availability)", fallback_vmid)
        return fallback_vmid


def clone_vm_from_template(template_vmid: int, new_name: str, target_node: str = None,
                           cluster_ip: str = CLASS_CLUSTER_IP, full_clone: bool = True,
                           new_vmid: int = None) -> Tuple[Optional[int], str]:
    """Clone a VM from a template.
    
    Args:
        template_vmid: Source template VMID
        new_name: Name for the cloned VM
        target_node: Node to create VM on (defaults to template's node)
        cluster_ip: Proxmox cluster IP
        full_clone: If True, create independent clone. If False, linked clone.
        new_vmid: Optional specific VMID for new VM (auto-assigned if None)
    
    Returns: (new_vmid, message/error)
    """
    try:
        proxmox = get_proxmox_for_ip(cluster_ip)
        
        # Find the template
        resources = proxmox.cluster.resources.get(type='vm') or []
        template = None
        for r in resources:
            if r.get('vmid') == template_vmid and r.get('template') == 1:
                template = r
                break
        
        if not template:
            return None, f"Template {template_vmid} not found"
        
        source_node = template['node']
        target_node = target_node or source_node
        vmtype = template.get('type', 'qemu')
        
        # Get next VMID if not specified
        if new_vmid is None:
            new_vmid = get_next_vmid(cluster_ip)
        
        # Clone the VM
        if vmtype == 'lxc':
            # LXC container clone
            task = proxmox.nodes(source_node).lxc(template_vmid).clone.post(
                newid=new_vmid,
                hostname=new_name,
                target=target_node,
                full=1 if full_clone else 0
            )
        else:
            # QEMU VM clone
            task = proxmox.nodes(source_node).qemu(template_vmid).clone.post(
                newid=new_vmid,
                name=new_name,
                target=target_node,
                full=1 if full_clone else 0
            )
        
        # Wait for clone task to complete
        wait_for_task(proxmox, source_node, task)
        
        logger.info("Cloned template %d to VM %d (%s) on node %s", 
                    template_vmid, new_vmid, new_name, target_node)
        return new_vmid, f"VM {new_vmid} created from template"
    
    except Exception as e:
        logger.exception("Failed to clone template %d: %s", template_vmid, e)
        return None, f"Failed to clone template: {str(e)}"


def convert_vm_to_template(vmid: int, node: str, cluster_ip: str = CLASS_CLUSTER_IP) -> Tuple[bool, str]:
    """Convert a VM to a template.
    
    The VM must be stopped before conversion.
    
    Returns: (success, message/error)
    """
    try:
        proxmox = get_proxmox_for_ip(cluster_ip)
        
        # Check VM status
        try:
            status = proxmox.nodes(node).qemu(vmid).status.current.get()
            vm_status = status.get('status', 'unknown')
        except:
            # Try LXC
            try:
                status = proxmox.nodes(node).lxc(vmid).status.current.get()
                vm_status = status.get('status', 'unknown')
            except Exception as e:
                return False, f"VM {vmid} not found on node {node}"
        
        if vm_status != 'stopped':
            return False, f"VM must be stopped before converting to template (current: {vm_status})"
        
        # Convert to template
        try:
            # QEMU VM
            proxmox.nodes(node).qemu(vmid).template.post()
        except:
            # LXC container
            try:
                proxmox.nodes(node).lxc(vmid).template.post()
            except Exception as e:
                return False, f"Failed to convert to template: {str(e)}"
        
        logger.info("Converted VM %d to template on node %s", vmid, node)
        return True, f"VM {vmid} converted to template"
    
    except Exception as e:
        logger.exception("Failed to convert VM %d to template: %s", vmid, e)
        return False, f"Failed to convert to template: {str(e)}"


def create_vm_snapshot(vmid: int, node: str, snapshot_name: str = 'class_baseline',
                       description: str = None, cluster_ip: str = CLASS_CLUSTER_IP) -> Tuple[bool, str]:
    """Create a snapshot of a VM.
    
    Used by teachers to save a baseline state for student revert.
    
    Returns: (success, message/error)
    """
    try:
        proxmox = get_proxmox_for_ip(cluster_ip)
        
        # Try QEMU first
        try:
            task = proxmox.nodes(node).qemu(vmid).snapshot.post(
                snapname=snapshot_name,
                description=description or f"Class baseline snapshot created at {time.strftime('%Y-%m-%d %H:%M')}"
            )
            wait_for_task(proxmox, node, task)
            logger.info("Created snapshot '%s' for QEMU VM %d", snapshot_name, vmid)
            return True, f"Snapshot '{snapshot_name}' created"
        except Exception as qemu_err:
            # Try LXC
            try:
                task = proxmox.nodes(node).lxc(vmid).snapshot.post(
                    snapname=snapshot_name,
                    description=description or f"Class baseline snapshot"
                )
                wait_for_task(proxmox, node, task)
                logger.info("Created snapshot '%s' for LXC %d", snapshot_name, vmid)
                return True, f"Snapshot '{snapshot_name}' created"
            except Exception as lxc_err:
                return False, f"Failed to create snapshot: QEMU: {qemu_err}, LXC: {lxc_err}"
    
    except Exception as e:
        logger.exception("Failed to create snapshot for VM %d: %s", vmid, e)
        return False, f"Failed to create snapshot: {str(e)}"


def revert_vm_to_snapshot(vmid: int, node: str, snapshot_name: str = 'class_baseline',
                          cluster_ip: str = CLASS_CLUSTER_IP) -> Tuple[bool, str]:
    """Revert a VM to a snapshot.
    
    Used by students to restore their VM to teacher's baseline.
    This is fast because it uses Proxmox's snapshot system.
    
    Returns: (success, message/error)
    """
    try:
        proxmox = get_proxmox_for_ip(cluster_ip)
        
        # Try QEMU first
        try:
            task = proxmox.nodes(node).qemu(vmid).snapshot(snapshot_name).rollback.post()
            wait_for_task(proxmox, node, task)
            logger.info("Reverted QEMU VM %d to snapshot '%s'", vmid, snapshot_name)
            return True, f"VM reverted to '{snapshot_name}'"
        except Exception as qemu_err:
            # Try LXC
            try:
                task = proxmox.nodes(node).lxc(vmid).snapshot(snapshot_name).rollback.post()
                wait_for_task(proxmox, node, task)
                logger.info("Reverted LXC %d to snapshot '%s'", vmid, snapshot_name)
                return True, f"Container reverted to '{snapshot_name}'"
            except Exception as lxc_err:
                return False, f"Failed to revert: QEMU: {qemu_err}, LXC: {lxc_err}"
    
    except Exception as e:
        logger.exception("Failed to revert VM %d: %s", vmid, e)
        return False, f"Failed to revert: {str(e)}"


def delete_vm(vmid: int, node: str, cluster_ip: str = CLASS_CLUSTER_IP) -> Tuple[bool, str]:
    """Delete a VM from Proxmox.
    
    VM must be stopped before deletion.
    
    Returns: (success, message/error)
    """
    try:
        proxmox = get_proxmox_for_ip(cluster_ip)
        
        # Stop VM if running (try graceful shutdown first)
        try:
            # Try QEMU
            status = proxmox.nodes(node).qemu(vmid).status.current.get()
            if status.get('status') == 'running':
                proxmox.nodes(node).qemu(vmid).status.stop.post()
                time.sleep(2)  # Wait for stop
        except:
            try:
                # Try LXC
                status = proxmox.nodes(node).lxc(vmid).status.current.get()
                if status.get('status') == 'running':
                    proxmox.nodes(node).lxc(vmid).status.stop.post()
                    time.sleep(2)
            except:
                pass  # VM might not exist or already stopped
        
        # Delete VM
        try:
            # Try QEMU
            task = proxmox.nodes(node).qemu(vmid).delete()
            if task:
                wait_for_task(proxmox, node, task)
            logger.info("Deleted QEMU VM %d from node %s", vmid, node)
            return True, f"VM {vmid} deleted"
        except Exception as qemu_err:
            try:
                # Try LXC
                task = proxmox.nodes(node).lxc(vmid).delete()
                if task:
                    wait_for_task(proxmox, node, task)
                logger.info("Deleted LXC %d from node %s", vmid, node)
                return True, f"Container {vmid} deleted"
            except Exception as lxc_err:
                return False, f"Failed to delete: QEMU: {qemu_err}, LXC: {lxc_err}"
    
    except Exception as e:
        logger.exception("Failed to delete VM %d: %s", vmid, e)
        return False, f"Failed to delete VM: {str(e)}"


def start_class_vm(vmid: int, node: str, cluster_ip: str = CLASS_CLUSTER_IP) -> Tuple[bool, str]:
    """Start a class VM.
    
    Returns: (success, message/error)
    """
    try:
        proxmox = get_proxmox_for_ip(cluster_ip)
        
        # Try QEMU first
        try:
            proxmox.nodes(node).qemu(vmid).status.start.post()
            logger.info("Started QEMU VM %d on node %s", vmid, node)
            return True, f"VM {vmid} started"
        except Exception as qemu_err:
            # Try LXC
            try:
                proxmox.nodes(node).lxc(vmid).status.start.post()
                logger.info("Started LXC %d on node %s", vmid, node)
                return True, f"Container {vmid} started"
            except Exception as lxc_err:
                return False, f"Failed to start: QEMU: {qemu_err}, LXC: {lxc_err}"
    
    except Exception as e:
        logger.exception("Failed to start VM %d: %s", vmid, e)
        return False, f"Failed to start VM: {str(e)}"


def stop_class_vm(vmid: int, node: str, cluster_ip: str = CLASS_CLUSTER_IP) -> Tuple[bool, str]:
    """Stop a class VM.
    
    Returns: (success, message/error)
    """
    try:
        proxmox = get_proxmox_for_ip(cluster_ip)
        
        # Try QEMU first
        try:
            proxmox.nodes(node).qemu(vmid).status.shutdown.post()
            logger.info("Stopped QEMU VM %d on node %s", vmid, node)
            return True, f"VM {vmid} stopping"
        except Exception as qemu_err:
            # Try LXC
            try:
                proxmox.nodes(node).lxc(vmid).status.shutdown.post()
                logger.info("Stopped LXC %d on node %s", vmid, node)
                return True, f"Container {vmid} stopping"
            except Exception as lxc_err:
                return False, f"Failed to stop: QEMU: {qemu_err}, LXC: {lxc_err}"
    
    except Exception as e:
        logger.exception("Failed to stop VM %d: %s", vmid, e)
        return False, f"Failed to stop VM: {str(e)}"


def get_vm_status(vmid: int, node: str, cluster_ip: str = CLASS_CLUSTER_IP) -> Dict[str, Any]:
    """Get current status of a VM.
    
    Returns dict with status, ip, etc.
    """
    try:
        proxmox = get_proxmox_for_ip(cluster_ip)
        
        # Try QEMU first
        try:
            status = proxmox.nodes(node).qemu(vmid).status.current.get()
            return {
                'vmid': vmid,
                'status': status.get('status', 'unknown'),
                'type': 'qemu',
                'node': node,
                'name': status.get('name', f'vm-{vmid}'),
                'uptime': status.get('uptime', 0),
            }
        except:
            # Try LXC
            try:
                status = proxmox.nodes(node).lxc(vmid).status.current.get()
                return {
                    'vmid': vmid,
                    'status': status.get('status', 'unknown'),
                    'type': 'lxc',
                    'node': node,
                    'name': status.get('name', f'ct-{vmid}'),
                    'uptime': status.get('uptime', 0),
                }
            except:
                return {'vmid': vmid, 'status': 'unknown', 'error': 'VM not found'}
    
    except Exception as e:
        logger.exception("Failed to get status for VM %d: %s", vmid, e)
        return {'vmid': vmid, 'status': 'error', 'error': str(e)}


def wait_for_task(proxmox, node: str, task_id: str, timeout: int = 120) -> bool:
    """Wait for a Proxmox task to complete.
    
    Returns True if task completed successfully, False otherwise.
    """
    if not task_id:
        return True
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            task_status = proxmox.nodes(node).tasks(task_id).status.get()
            status = task_status.get('status', 'unknown')
            
            if status == 'stopped':
                # Check if task succeeded
                exitstatus = task_status.get('exitstatus', '')
                if exitstatus == 'OK':
                    return True
                else:
                    logger.warning("Task %s failed: %s", task_id, exitstatus)
                    return False
            
            # Still running, wait a bit
            time.sleep(1)
        
        except Exception as e:
            logger.warning("Error checking task status: %s", e)
            time.sleep(1)
    
    logger.warning("Task %s timed out after %d seconds", task_id, timeout)
    return False


def clone_vms_for_class(template_vmid: int, class_name: str, count: int,
                        target_node: str = None, cluster_ip: str = CLASS_CLUSTER_IP) -> List[Tuple[int, str]]:
    """Clone multiple VMs for a class pool.
    
    Args:
        template_vmid: Source template VMID
        class_name: Class name (used for VM naming)
        count: Number of VMs to create
        target_node: Node to create VMs on
        cluster_ip: Proxmox cluster IP
    
    Returns: List of (vmid, node) tuples for successfully created VMs
    """
    created_vms = []
    
    for i in range(count):
        vm_name = f"{class_name}-vm-{i+1:03d}"
        vmid, msg = clone_vm_from_template(
            template_vmid=template_vmid,
            new_name=vm_name,
            target_node=target_node,
            cluster_ip=cluster_ip
        )
        
        if vmid:
            # Get the node where VM was created
            status = get_vm_status(vmid, target_node or 'prox1', cluster_ip)
            node = status.get('node', target_node)
            created_vms.append((vmid, node))
            logger.info("Created VM %d (%s) for class pool", vmid, vm_name)
        else:
            logger.warning("Failed to create VM %s: %s", vm_name, msg)
    
    return created_vms


def update_class_baseline_snapshots(class_id: int, vmids: List[Tuple[int, str]],
                                    cluster_ip: str = CLASS_CLUSTER_IP) -> int:
    """Create/update baseline snapshots for all VMs in a class.
    
    Called when teacher 'pushes' updates to all students.
    
    Returns: Number of VMs successfully updated
    """
    success_count = 0
    
    for vmid, node in vmids:
        success, msg = create_vm_snapshot(
            vmid=vmid,
            node=node,
            snapshot_name='class_baseline',
            description=f"Class baseline updated at {time.strftime('%Y-%m-%d %H:%M')}",
            cluster_ip=cluster_ip
        )
        
        if success:
            success_count += 1
        else:
            logger.warning("Failed to update snapshot for VM %d: %s", vmid, msg)
    
    return success_count
