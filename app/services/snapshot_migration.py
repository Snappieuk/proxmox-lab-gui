#!/usr/bin/env python3
"""
Snapshot migration utility for linked clone classes.

Helps retroactively add baseline snapshots to existing linked clone VMs
that were created before the automatic snapshot feature was implemented.

This is useful for updating old linked clone deployments to support reimage functionality.
"""

import logging
from typing import Tuple

from app.models import Class, VMAssignment
from app.services.ssh_executor import get_pooled_ssh_executor_from_config
from app.services.vm_core import create_snapshot_ssh

logger = logging.getLogger(__name__)


def create_baseline_snapshots_for_class(class_id: int) -> Tuple[bool, str, dict]:
    """
    Retroactively create baseline snapshots for all VMs in a linked clone class.
    
    This is useful for existing classes created before automatic snapshot support.
    After running this, reimaging will work for these VMs.
    
    Args:
        class_id: Database ID of the class
        
    Returns:
        (success: bool, message: str, stats: dict with created/failed counts)
    """
    try:
        class_ = Class.query.get(class_id)
        if not class_:
            return False, f"Class {class_id} not found", {}
        
        # Only works for linked clone classes
        if getattr(class_, 'deployment_method', 'config_clone') != 'linked_clone':
            return False, f"Class {class_.name} is not a linked clone class", {}
        
        logger.info(f"Creating baseline snapshots for linked clone class {class_.name}")
        
        # Get all VMs in this class
        vms = VMAssignment.query.filter_by(class_id=class_id, is_template_vm=False).all()
        
        if not vms:
            return True, f"No VMs found in class {class_.name}", {"created": 0, "failed": 0}
        
        # Get cluster config from first VM's template or from config
        cluster_ip = class_.template.cluster_ip if class_.template else None
        
        if not cluster_ip:
            from app.services.proxmox_service import get_clusters_from_db
            clusters = get_clusters_from_db()
            if clusters:
                cluster_ip = clusters[0]["host"]
            else:
                return False, "No clusters configured", {}
        
        # Find cluster config
        from app.config import CLUSTERS
        cluster_config = None
        for c in CLUSTERS:
            if c.get('host') == cluster_ip:
                cluster_config = c
                break
        
        if not cluster_config:
            return False, f"Cluster {cluster_ip} not found in configuration", {}
        
        # Get SSH executor
        try:
            ssh_executor = get_pooled_ssh_executor_from_config(cluster_config)
        except Exception as e:
            return False, f"Failed to connect to cluster: {str(e)}", {}
        
        # Create snapshots for each VM
        stats = {"created": 0, "failed": 0, "skipped": 0}
        errors = []
        
        for vm in vms:
            vmid = vm.proxmox_vmid
            vm_name = vm.vm_name or f"VM {vmid}"
            
            try:
                # Check if baseline snapshot already exists
                list_snap_cmd = f"qm listsnapshot {vmid}"
                exit_code, stdout, stderr = ssh_executor.execute(list_snap_cmd, timeout=10, check=False)
                
                has_baseline = False
                if exit_code == 0:
                    for line in stdout.split('\n'):
                        if 'baseline' in line.lower():
                            has_baseline = True
                            break
                
                if has_baseline:
                    logger.info(f"VM {vmid} ({vm_name}) already has baseline snapshot - skipping")
                    stats["skipped"] += 1
                    continue
                
                # Create baseline snapshot
                logger.info(f"Creating baseline snapshot for VM {vmid} ({vm_name})")
                
                snap_success, snap_error = create_snapshot_ssh(
                    ssh_executor=ssh_executor,
                    vmid=vmid,
                    snapname="baseline",
                    description="Retroactively created baseline snapshot for reimage functionality",
                    include_ram=False
                )
                
                if snap_success:
                    logger.info(f"Created baseline snapshot for VM {vmid} ({vm_name})")
                    stats["created"] += 1
                else:
                    logger.error(f"Failed to create baseline snapshot for VM {vmid}: {snap_error}")
                    errors.append(f"{vm_name} (VMID {vmid}): {snap_error}")
                    stats["failed"] += 1
            
            except Exception as e:
                logger.error(f"Error processing VM {vmid}: {e}")
                errors.append(f"{vm_name} (VMID {vmid}): {str(e)}")
                stats["failed"] += 1
        
        # Build response message
        message = f"Snapshot migration complete for {class_.name}: "
        message += f"{stats['created']} created, {stats['failed']} failed, {stats['skipped']} skipped"
        
        if errors:
            message += "\n\nErrors:\n" + "\n".join(errors)
        
        success = stats["failed"] == 0
        return success, message, stats
    
    except Exception as e:
        logger.exception(f"Error in snapshot migration: {e}")
        return False, f"Snapshot migration failed: {str(e)}", {}


def create_baseline_snapshots_for_all_linked_clones() -> Tuple[bool, str, dict]:
    """
    Retroactively create baseline snapshots for ALL linked clone classes.
    
    Use with caution as this processes all classes.
    
    Returns:
        (success: bool, message: str, stats: dict with totals)
    """
    try:
        # Find all linked clone classes
        linked_clone_classes = Class.query.filter_by(deployment_method='linked_clone').all()
        
        if not linked_clone_classes:
            return True, "No linked clone classes found", {"classes": 0, "vms_created": 0, "vms_failed": 0}
        
        logger.info(f"Creating baseline snapshots for {len(linked_clone_classes)} linked clone classes")
        
        total_stats = {"classes": 0, "vms_created": 0, "vms_failed": 0, "vms_skipped": 0}
        all_errors = []
        
        for class_ in linked_clone_classes:
            success, message, stats = create_baseline_snapshots_for_class(class_.id)
            
            total_stats["classes"] += 1
            total_stats["vms_created"] += stats.get("created", 0)
            total_stats["vms_failed"] += stats.get("failed", 0)
            total_stats["vms_skipped"] += stats.get("skipped", 0)
            
            if not success:
                all_errors.append(f"Class {class_.name}: {message}")
            else:
                logger.info(f"Class {class_.name}: {message}")
        
        # Build overall response
        message = (
            f"Snapshot migration complete for all linked clone classes:\n\n"
            f"Classes processed: {total_stats['classes']}\n"
            f"Snapshots created: {total_stats['vms_created']}\n"
            f"Snapshots failed: {total_stats['vms_failed']}\n"
            f"Already had snapshots: {total_stats['vms_skipped']}"
        )
        
        if all_errors:
            message += "\n\nErrors:\n" + "\n".join(all_errors)
        
        success = total_stats["vms_failed"] == 0
        return success, message, total_stats
    
    except Exception as e:
        logger.exception(f"Error in batch snapshot migration: {e}")
        return False, f"Batch snapshot migration failed: {str(e)}", {}
