#!/usr/bin/env python3
"""
Fix Mismatched VMs - Recreate VMs with proper config cloned from class-base VM

This script identifies and fixes VMs that were created without proper hardware specs,
EFI disks, TPM disks, or other configuration options by deleting and recreating them
with the correct configuration cloned from the class-base VM.

Usage:
    python fix_mismatched_vms.py --class-id <CLASS_ID> --vmids <VMID1,VMID2,...>
    python fix_mismatched_vms.py --class-id <CLASS_ID> --check-all

Examples:
    # Fix specific VMs
    python fix_mismatched_vms.py --class-id 123 --vmids 53523,53524,53525
    
    # Check all VMs in a class and identify mismatched ones
    python fix_mismatched_vms.py --class-id 123 --check-all
    
    # Fix all mismatched VMs in a class (auto-detected)
    python fix_mismatched_vms.py --class-id 123 --fix-all
"""

import sys
import os
import logging
import argparse
from typing import List, Dict, Optional

# Add app directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from app import create_app
from app.models import Class, VMAssignment, db
from app.services.ssh_executor import get_pooled_ssh_executor_from_config
from app.services.vm_template import create_overlay_vm
from app.services.class_vm_service import get_vmid_for_class_vm
from app.config import PROXMOX_STORAGE_NAME, DEFAULT_VM_IMAGES_PATH

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def check_vm_config(ssh_executor, vmid: int, reference_vmid: int) -> Dict[str, any]:
    """
    Compare VM configuration with reference VM (class-base).
    
    Returns dict with:
        - matched: bool (True if config matches)
        - missing_features: list of missing features
        - details: dict of config comparison
    """
    result = {
        'matched': True,
        'missing_features': [],
        'details': {}
    }
    
    # Get both VM configs
    vm_config_cmd = f"qm config {vmid}"
    ref_config_cmd = f"qm config {reference_vmid}"
    
    exit_code, vm_config_out, _ = ssh_executor.execute(vm_config_cmd, check=False)
    if exit_code != 0:
        result['matched'] = False
        result['missing_features'].append('VM_NOT_FOUND')
        return result
    
    exit_code, ref_config_out, _ = ssh_executor.execute(ref_config_cmd, check=False)
    if exit_code != 0:
        result['matched'] = False
        result['missing_features'].append('REFERENCE_VM_NOT_FOUND')
        return result
    
    # Parse configs
    vm_config = {}
    ref_config = {}
    
    for line in vm_config_out.split('\n'):
        if ':' in line:
            key, value = line.split(':', 1)
            vm_config[key.strip()] = value.strip()
    
    for line in ref_config_out.split('\n'):
        if ':' in line:
            key, value = line.split(':', 1)
            ref_config[key.strip()] = value.strip()
    
    # Check critical features
    critical_keys = ['cores', 'memory', 'sockets', 'cpu', 'machine', 'bios', 'scsihw']
    
    for key in critical_keys:
        vm_val = vm_config.get(key, 'MISSING')
        ref_val = ref_config.get(key, 'MISSING')
        result['details'][key] = {'vm': vm_val, 'reference': ref_val, 'match': vm_val == ref_val}
        
        if vm_val != ref_val:
            result['matched'] = False
            result['missing_features'].append(f'{key.upper()}_MISMATCH')
    
    # Check for EFI disk
    if 'efidisk0' in ref_config:
        if 'efidisk0' not in vm_config:
            result['matched'] = False
            result['missing_features'].append('MISSING_EFI_DISK')
            result['details']['efidisk0'] = {'vm': 'MISSING', 'reference': ref_config['efidisk0'], 'match': False}
        else:
            result['details']['efidisk0'] = {'vm': vm_config['efidisk0'], 'reference': ref_config['efidisk0'], 'match': True}
    
    # Check for TPM disk
    if 'tpmstate0' in ref_config:
        if 'tpmstate0' not in vm_config:
            result['matched'] = False
            result['missing_features'].append('MISSING_TPM_STATE')
            result['details']['tpmstate0'] = {'vm': 'MISSING', 'reference': ref_config['tpmstate0'], 'match': False}
        else:
            result['details']['tpmstate0'] = {'vm': vm_config['tpmstate0'], 'reference': ref_config['tpmstate0'], 'match': True}
    
    return result


def fix_vm(ssh_executor, class_obj: Class, vmid: int, class_base_vmid: int, 
           node: str, dry_run: bool = False) -> bool:
    """
    Fix a mismatched VM by deleting and recreating it with proper config.
    
    Args:
        ssh_executor: SSH connection to Proxmox
        class_obj: Class object
        vmid: VMID to fix
        class_base_vmid: Reference VM to clone config from
        node: Node where VM should be created
        dry_run: If True, only show what would be done
        
    Returns:
        True if successful, False otherwise
    """
    # Get VM assignment to preserve metadata
    assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
    if not assignment:
        logger.error(f"No VMAssignment found for VMID {vmid}")
        return False
    
    vm_name = assignment.vm_name
    assigned_user_id = assignment.assigned_user_id
    
    logger.info(f"{'[DRY RUN] ' if dry_run else ''}Fixing VM {vmid} ({vm_name})")
    
    if dry_run:
        logger.info(f"  Would delete VM {vmid}")
        logger.info(f"  Would recreate with config from class-base VM {class_base_vmid}")
        logger.info(f"  Would preserve assignment to user {assigned_user_id}")
        return True
    
    # Step 1: Stop VM if running
    logger.info(f"  Checking VM {vmid} status...")
    status_cmd = f"qm status {vmid}"
    exit_code, status_out, _ = ssh_executor.execute(status_cmd, check=False)
    if exit_code == 0 and 'running' in status_out.lower():
        logger.info(f"  Stopping VM {vmid}...")
        ssh_executor.execute(f"qm stop {vmid}", timeout=30, check=False)
        import time
        time.sleep(5)
    
    # Step 2: Delete VM
    logger.info(f"  Deleting VM {vmid}...")
    delete_cmd = f"qm destroy {vmid} --purge"
    exit_code, _, stderr = ssh_executor.execute(delete_cmd, timeout=60, check=False)
    if exit_code != 0:
        logger.error(f"  Failed to delete VM {vmid}: {stderr}")
        return False
    
    logger.info(f"  VM {vmid} deleted successfully")
    
    # Step 3: Recreate VM with proper config
    logger.info(f"  Recreating VM {vmid} with config from class-base VM {class_base_vmid}...")
    
    # Get class prefix for template path
    class_prefix = vm_name.rsplit('-', 1)[0]  # Extract prefix from name like "classname-student-23"
    if '-student-' in vm_name:
        class_prefix = vm_name.split('-student-')[0]
    elif '-teacher' in vm_name:
        class_prefix = vm_name.replace('-teacher', '')
    
    # Determine template VMID if template-based class
    template_vmid = None
    if class_obj.template:
        template_vmid = class_obj.template.vmid
    
    # Build paths
    if template_vmid:
        base_qcow2_path = f"/mnt/pve/{PROXMOX_STORAGE_NAME}/templates/{class_prefix}-t{template_vmid}-base.qcow2"
        class_base_disk_path = f"/mnt/pve/{PROXMOX_STORAGE_NAME}/images/{class_base_vmid}/vm-{class_base_vmid}-disk-0.qcow2"
    else:
        class_base_disk_path = None
    
    # Create VM with config cloning
    success, error, new_mac = create_overlay_vm(
        ssh_executor=ssh_executor,
        vmid=vmid,
        name=vm_name,
        base_qcow2_path=class_base_disk_path if class_base_disk_path else None,
        node=node,
        template_vmid=class_base_vmid,  # Clone config from class-base VM
    )
    
    if not success:
        logger.error(f"  Failed to recreate VM {vmid}: {error}")
        return False
    
    logger.info(f"  VM {vmid} recreated successfully with MAC {new_mac}")
    
    # Step 4: Update VMAssignment record
    assignment.mac_address = new_mac
    assignment.node = node
    db.session.commit()
    logger.info(f"  VMAssignment updated for VM {vmid}")
    
    return True


def main():
    parser = argparse.ArgumentParser(
        description='Fix mismatched VMs by recreating them with proper config',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--class-id', type=int, required=True, help='Class ID')
    parser.add_argument('--vmids', type=str, help='Comma-separated list of VMIDs to fix (e.g., 53523,53524,53525)')
    parser.add_argument('--check-all', action='store_true', help='Check all VMs in class and identify mismatched ones')
    parser.add_argument('--fix-all', action='store_true', help='Automatically fix all mismatched VMs in class')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without making changes')
    
    args = parser.parse_args()
    
    # Create Flask app context
    app = create_app()
    
    with app.app_context():
        # Get class
        class_obj = Class.query.get(args.class_id)
        if not class_obj:
            logger.error(f"Class {args.class_id} not found")
            return 1
        
        logger.info(f"Working with class: {class_obj.name} (ID: {class_obj.id})")
        
        # Get class-base VMID
        class_base_vmid = class_obj.vmid_prefix * 100 + 99
        logger.info(f"Reference VM (class-base): {class_base_vmid}")
        
        # Get cluster connection info
        cluster_ip = None
        if class_obj.template and class_obj.template.cluster_ip:
            cluster_ip = class_obj.template.cluster_ip
        else:
            cluster_ip = "10.220.15.249"  # Default cluster
        
        # Connect to SSH
        logger.info(f"Connecting to cluster {cluster_ip}...")
        ssh_executor = get_pooled_ssh_executor_from_config(cluster_ip)
        if not ssh_executor:
            logger.error("Failed to connect to cluster")
            return 1
        
        ssh_executor.connect()
        
        # Verify class-base VM exists
        check_cmd = f"qm config {class_base_vmid} 2>/dev/null"
        exit_code, _, _ = ssh_executor.execute(check_cmd, check=False)
        if exit_code != 0:
            logger.error(f"Class-base VM {class_base_vmid} not found! Cannot use as reference.")
            return 1
        
        logger.info(f"✓ Class-base VM {class_base_vmid} found")
        
        try:
            if args.check_all or args.fix_all:
                # Check all student VMs in class
                logger.info("Scanning all student VMs in class...")
                assignments = VMAssignment.query.filter_by(
                    class_id=args.class_id,
                    is_teacher_vm=False,
                    is_template_vm=False
                ).all()
                
                mismatched_vms = []
                
                for assignment in assignments:
                    vmid = assignment.proxmox_vmid
                    logger.info(f"\nChecking VM {vmid} ({assignment.vm_name})...")
                    
                    result = check_vm_config(ssh_executor, vmid, class_base_vmid)
                    
                    if result['matched']:
                        logger.info(f"  ✓ VM {vmid} matches reference configuration")
                    else:
                        logger.warning(f"  ✗ VM {vmid} has mismatched configuration:")
                        for feature in result['missing_features']:
                            logger.warning(f"    - {feature}")
                        mismatched_vms.append((vmid, assignment))
                
                logger.info(f"\n{'='*60}")
                logger.info(f"Summary: {len(mismatched_vms)} mismatched VMs found out of {len(assignments)} total")
                logger.info(f"{'='*60}\n")
                
                if mismatched_vms:
                    logger.info("Mismatched VMs:")
                    for vmid, assignment in mismatched_vms:
                        logger.info(f"  - {vmid} ({assignment.vm_name})")
                    
                    if args.fix_all:
                        logger.info(f"\n{'='*60}")
                        logger.info(f"Fixing {len(mismatched_vms)} mismatched VMs...")
                        logger.info(f"{'='*60}\n")
                        
                        success_count = 0
                        fail_count = 0
                        
                        for vmid, assignment in mismatched_vms:
                            if fix_vm(ssh_executor, class_obj, vmid, class_base_vmid, 
                                     assignment.node, dry_run=args.dry_run):
                                success_count += 1
                            else:
                                fail_count += 1
                        
                        logger.info(f"\n{'='*60}")
                        logger.info(f"Results: {success_count} fixed, {fail_count} failed")
                        logger.info(f"{'='*60}")
                else:
                    logger.info("All VMs have matching configurations!")
            
            elif args.vmids:
                # Fix specific VMs
                vmid_list = [int(vmid.strip()) for vmid in args.vmids.split(',')]
                logger.info(f"Fixing {len(vmid_list)} VMs: {vmid_list}")
                
                success_count = 0
                fail_count = 0
                
                for vmid in vmid_list:
                    assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
                    if not assignment:
                        logger.error(f"VM {vmid} not found in database")
                        fail_count += 1
                        continue
                    
                    logger.info(f"\nChecking VM {vmid} ({assignment.vm_name})...")
                    result = check_vm_config(ssh_executor, vmid, class_base_vmid)
                    
                    if result['matched']:
                        logger.info(f"  ✓ VM {vmid} already matches reference configuration (skipping)")
                        continue
                    
                    logger.warning(f"  ✗ VM {vmid} has mismatched configuration:")
                    for feature in result['missing_features']:
                        logger.warning(f"    - {feature}")
                    
                    if fix_vm(ssh_executor, class_obj, vmid, class_base_vmid, 
                             assignment.node, dry_run=args.dry_run):
                        success_count += 1
                    else:
                        fail_count += 1
                
                logger.info(f"\n{'='*60}")
                logger.info(f"Results: {success_count} fixed, {fail_count} failed")
                logger.info(f"{'='*60}")
            
            else:
                logger.error("Must specify either --vmids, --check-all, or --fix-all")
                return 1
        
        finally:
            ssh_executor.disconnect()
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
