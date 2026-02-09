#!/usr/bin/env python3
"""
Fix Mismatched VMs - Recreate VMs with proper config cloned from class-base VM

This script automatically scans all classes and identifies VMs that were created 
without proper hardware specs, EFI disks, TPM disks, or other configuration options.
It can recreate them with the correct configuration cloned from the class-base VM.

Usage:
    python fix_mismatched_vms.py                    # Scan all classes
    python fix_mismatched_vms.py --class-id <ID>    # Check specific class only
    python fix_mismatched_vms.py --fix-all          # Auto-fix all mismatched VMs
    python fix_mismatched_vms.py --dry-run          # Preview changes without executing

Examples:
    # Scan all classes and show mismatched VMs
    python fix_mismatched_vms.py
    
    # Check only a specific class
    python fix_mismatched_vms.py --class-id 123
    
    # Fix all mismatched VMs across all classes (with preview)
    python fix_mismatched_vms.py --fix-all --dry-run
    
    # Actually fix all mismatched VMs
    python fix_mismatched_vms.py --fix-all
"""

import sys
import os
import logging
import argparse
from typing import Dict, List, Tuple

# Add app directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from app import create_app
from app.models import Class, VMAssignment, db
from app.services.ssh_executor import get_pooled_ssh_executor_from_config
from app.services.vm_template import create_overlay_vm

# Get storage name from environment (same as services use)
PROXMOX_STORAGE_NAME = os.getenv("PROXMOX_STORAGE_NAME", "TRUENAS-NFS")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def check_vm_config(ssh_executor, vmid: int, reference_vmid: int) -> Dict:
    """
    Compare VM configuration with reference VM (class-base).
    
    Returns dict with:
        - matched: bool (True if config matches)
        - missing_features: list of missing features
        - details: dict of config comparison
        - disk_format: str (qcow2, raw, or unknown)
    """
    result = {
        'matched': True,
        'missing_features': [],
        'details': {},
        'disk_format': 'unknown'
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
    
    # Check disk format (verify main disk is QCOW2 overlay, not raw)
    disk_path = None
    for key in vm_config:
        if key in ['scsi0', 'virtio0', 'sata0', 'ide0']:
            # Extract path from config (format: "STORAGE:PATH,options...")
            disk_config = vm_config[key]
            if ':' in disk_config:
                disk_path = disk_config.split(':')[1].split(',')[0].strip()
                break
    
    if disk_path:
        # Check actual file format on disk
        full_disk_path = f"/mnt/pve/{PROXMOX_STORAGE_NAME}/images/{disk_path}"
        qemu_info_cmd = f"qemu-img info {full_disk_path} 2>/dev/null | grep 'file format:'"
        exit_code, format_out, _ = ssh_executor.execute(qemu_info_cmd, check=False)
        if exit_code == 0 and 'file format:' in format_out:
            if 'qcow2' in format_out:
                result['disk_format'] = 'qcow2'
            elif 'raw' in format_out:
                result['disk_format'] = 'raw'
                result['matched'] = False
                result['missing_features'].append('WRONG_DISK_FORMAT_RAW')
        
        # Also check if it has a backing file (should be overlay)
        backing_cmd = f"qemu-img info {full_disk_path} 2>/dev/null | grep 'backing file:'"
        exit_code, backing_out, _ = ssh_executor.execute(backing_cmd, check=False)
        if exit_code == 0 and 'backing file:' in backing_out:
            result['details']['has_backing_file'] = True
        else:
            result['details']['has_backing_file'] = False
            if result['disk_format'] == 'qcow2':
                result['matched'] = False
                result['missing_features'].append('MISSING_BACKING_FILE')
    
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
    
    # Build paths - use class-base disk as backing file for student VMs
    class_base_disk_path = f"/mnt/pve/{PROXMOX_STORAGE_NAME}/images/{class_base_vmid}/vm-{class_base_vmid}-disk-0.qcow2"
    
    # Verify class-base disk exists and is qcow2
    verify_cmd = f"qemu-img info {class_base_disk_path} 2>/dev/null | head -5"
    exit_code, info_out, _ = ssh_executor.execute(verify_cmd, check=False, timeout=10)
    if exit_code != 0:
        logger.error(f"  Class-base disk not found: {class_base_disk_path}")
        return False
    
    if 'qcow2' not in info_out:
        logger.error("  Class-base disk is not QCOW2 format!")
        return False
    
    logger.info(f"  Verified class-base disk: {class_base_disk_path} (QCOW2)")
    
    # Create VM with config cloning from class-base VM
    # This will:
    # 1. Create QCOW2 overlay disk with class-base as backing file
    # 2. Clone entire config (hardware, EFI, TPM, options) from class-base
    success, error, new_mac = create_overlay_vm(
        ssh_executor=ssh_executor,
        vmid=vmid,
        name=vm_name,
        base_qcow2_path=class_base_disk_path,
        node=node,
        template_vmid=class_base_vmid,  # Clone ALL config from class-base VM
    )
    
    if not success:
        logger.error(f"  Failed to recreate VM {vmid}: {error}")
        return False
    
    logger.info(f"  VM {vmid} recreated successfully with MAC {new_mac}")
    
    # Verify disk was created as QCOW2 overlay
    new_disk_path = f"/mnt/pve/{PROXMOX_STORAGE_NAME}/images/{vmid}/vm-{vmid}-disk-0.qcow2"
    verify_cmd = f"qemu-img info {new_disk_path} 2>/dev/null | grep -E '(file format|backing file)'"
    exit_code, verify_out,_ = ssh_executor.execute(verify_cmd, check=False, timeout=10)
    if exit_code == 0:
        logger.info(f"  Disk verification: {verify_out.strip()}")
        if 'qcow2' not in verify_out:
            logger.warning("  WARNING: Disk is not QCOW2 format!")
        if 'backing file' not in verify_out:
            logger.warning("  WARNING: Disk has no backing file (not an overlay)!")
    
    # Step 4: Update VMAssignment record
    assignment.mac_address = new_mac
    assignment.node = node
    db.session.commit()
    logger.info(f"  VMAssignment updated for VM {vmid}")
    
    return True


def scan_class(ssh_executor, class_obj: Class) -> Tuple[List[Tuple[int, VMAssignment]], int]:
    """
    Scan all student VMs in a class and identify mismatched ones.
    
    Returns:
        Tuple of (list of mismatched (vmid, assignment) tuples, total student count)
    """
    # Get class-base VMID
    class_base_vmid = class_obj.vmid_prefix * 100 + 99
    
    # Verify class-base VM exists
    check_cmd = f"qm config {class_base_vmid} 2>/dev/null"
    exit_code, _, _ = ssh_executor.execute(check_cmd, check=False)
    if exit_code != 0:
        logger.warning(f"Class-base VM {class_base_vmid} not found for class '{class_obj.name}'! Skipping.")
        return [], 0
    
    # Check all student VMs in class
    assignments = VMAssignment.query.filter_by(
        class_id=class_obj.id,
        is_teacher_vm=False,
        is_template_vm=False
    ).all()
    
    if not assignments:
        logger.info(f"  No student VMs found in class '{class_obj.name}'")
        return [], 0
    
    mismatched_vms = []
    
    for assignment in assignments:
        vmid = assignment.proxmox_vmid
        
        result = check_vm_config(ssh_executor, vmid, class_base_vmid)
        
        if not result['matched']:
            mismatched_vms.append((vmid, assignment, result['missing_features']))
    
    return mismatched_vms, len(assignments)


def main():
    parser = argparse.ArgumentParser(
        description='Fix mismatched VMs by recreating them with proper config',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--class-id', type=int, help='Check only this specific class (default: scan all classes)')
    parser.add_argument('--fix-all', action='store_true', help='Automatically fix all mismatched VMs')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without making changes')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show detailed progress for each VM')
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Create Flask app context
    app = create_app()
    
    with app.app_context():
        # Get classes to check
        if args.class_id:
            classes = [Class.query.get(args.class_id)]
            if not classes[0]:
                logger.error(f"Class {args.class_id} not found")
                return 1
            logger.info(f"Checking class: {classes[0].name} (ID: {classes[0].id})")
        else:
            classes = Class.query.filter(Class.vmid_prefix.isnot(None)).all()
            if not classes:
                logger.info("No classes with VMs found")
                return 0
            logger.info(f"Scanning {len(classes)} classes for mismatched VMs...\n")
        
        # Track all mismatched VMs across all classes
        all_mismatched = {}  # class_id -> list of (vmid, assignment, features)
        
        # Scan each class
        for class_obj in classes:
            if not class_obj.vmid_prefix:
                logger.debug(f"Skipping class '{class_obj.name}' - no VMID prefix allocated")
                continue
            
            # Get cluster connection info
            cluster_ip = None
            if class_obj.template and class_obj.template.cluster_ip:
                cluster_ip = class_obj.template.cluster_ip
            else:
                cluster_ip = "10.220.15.249"  # Default cluster
            
            logger.info(f"\n{'='*70}")
            logger.info(f"Class: {class_obj.name} (ID: {class_obj.id}, Prefix: {class_obj.vmid_prefix})")
            logger.info(f"{'='*70}")
            
            # Connect to SSH
            ssh_executor = get_pooled_ssh_executor_from_config(cluster_ip)
            if not ssh_executor:
                logger.error(f"Failed to connect to cluster {cluster_ip} - skipping class")
                continue
            
            try:
                ssh_executor.connect()
                
                # Scan this class
                mismatched_vms, total_students = scan_class(ssh_executor, class_obj)
                
                if mismatched_vms:
                    all_mismatched[class_obj.id] = (class_obj, mismatched_vms)
                    logger.warning(f"  ✗ Found {len(mismatched_vms)} mismatched VMs out of {total_students} total:")
                    for vmid, assignment, features in mismatched_vms:
                        logger.warning(f"    - VM {vmid} ({assignment.vm_name}): {', '.join(features)}")
                else:
                    if total_students > 0:
                        logger.info(f"  ✓ All {total_students} student VMs match class-base configuration")
                    
            except Exception as e:
                logger.error(f"Error scanning class '{class_obj.name}': {e}")
            finally:
                ssh_executor.disconnect()
        
        # Print summary
        logger.info(f"\n{'='*70}")
        logger.info("SUMMARY")
        logger.info(f"{'='*70}")
        
        if not all_mismatched:
            logger.info("✓ No mismatched VMs found across all classes!")
            return 0
        
        total_mismatched = sum(len(vms) for _, vms in all_mismatched.values())
        logger.warning(f"Found {total_mismatched} mismatched VMs across {len(all_mismatched)} classes:\n")
        
        for class_id, (class_obj, mismatched_vms) in all_mismatched.items():
            logger.warning(f"Class '{class_obj.name}' (ID: {class_id}):")
            logger.warning(f"  {len(mismatched_vms)} VMs need fixing:")
            for vmid, assignment, features in mismatched_vms:
                logger.warning(f"    - {vmid} ({assignment.vm_name})")
        
        # Fix VMs if requested
        if args.fix_all:
            logger.info(f"\n{'='*70}")
            logger.info(f"{'[DRY RUN] ' if args.dry_run else ''}FIXING MISMATCHED VMs")
            logger.info(f"{'='*70}\n")
            
            total_success = 0
            total_failed = 0
            
            for class_id, (class_obj, mismatched_vms) in all_mismatched.items():
                logger.info(f"\nFixing class '{class_obj.name}'...")
                
                # Get cluster connection
                cluster_ip = None
                if class_obj.template and class_obj.template.cluster_ip:
                    cluster_ip = class_obj.template.cluster_ip
                else:
                    cluster_ip = "10.220.15.249"
                
                ssh_executor = get_pooled_ssh_executor_from_config(cluster_ip)
                if not ssh_executor:
                    logger.error(f"Failed to connect to cluster {cluster_ip}")
                    total_failed += len(mismatched_vms)
                    continue
                
                try:
                    ssh_executor.connect()
                    class_base_vmid = class_obj.vmid_prefix * 100 + 99
                    
                    for vmid, assignment, features in mismatched_vms:
                        if fix_vm(ssh_executor, class_obj, vmid, class_base_vmid,
                                 assignment.node, dry_run=args.dry_run):
                            total_success += 1
                        else:
                            total_failed += 1
                            
                except Exception as e:
                    logger.error(f"Error fixing VMs in class '{class_obj.name}': {e}")
                    total_failed += len(mismatched_vms)
                finally:
                    ssh_executor.disconnect()
            
            logger.info(f"\n{'='*70}")
            logger.info(f"RESULTS: {total_success} fixed, {total_failed} failed")
            logger.info(f"{'='*70}")
        else:
            logger.info("\nTo fix these VMs, run: python fix_mismatched_vms.py --fix-all")
            if not args.dry_run:
                logger.info("To preview changes first: python fix_mismatched_vms.py --fix-all --dry-run")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
