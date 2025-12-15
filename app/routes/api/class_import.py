#!/usr/bin/env python3
"""
Class import API - Import existing classes from Proxmox VM naming patterns.

Scans Proxmox for VMs matching naming conventions (e.g., ABC00-01, ABC-teacher, ABC-base)
and automatically creates classes with proper VM assignments.
"""

import logging
import re
from typing import Dict, List, Tuple

from flask import Blueprint, jsonify, request

from app.models import Class, User, VMAssignment, db
from app.services.inventory_service import fetch_vm_inventory
from app.services.user_manager import is_admin_user, require_user
from app.utils.decorators import admin_required, login_required

logger = logging.getLogger(__name__)

class_import_bp = Blueprint('class_import', __name__, url_prefix='/api/class-import')


@class_import_bp.route("/scan", methods=["GET"])
@login_required
def api_scan_importable_classes():
    """Scan Proxmox for VMs matching importable class naming patterns.
    
    Looks for patterns like:
    - ABC00-01, ABC00-02, ABC00-03 (student VMs with prefix + 00-XX numbering)
    - ABC-teacher (teacher VM)
    - ABC-base (class base VM)
    
    Returns:
        Groups of VMs that could be imported as classes
    """
    try:
        user = require_user()
        
        # Only admins and teachers can import classes
        is_admin = is_admin_user(user)
        username = user.split('@')[0] if '@' in user else user
        from app.services.class_service import get_user_by_username
        local_user = get_user_by_username(username)
        
        if not is_admin and (not local_user or local_user.role not in ['teacher', 'adminer']):
            return jsonify({"ok": False, "error": "Access denied"}), 403
        
        cluster_id = request.args.get('cluster_id')
        
        # Get all VMs from inventory
        vms = fetch_vm_inventory(cluster_id=cluster_id)
        
        logger.info(f"Scanning {len(vms)} VMs for importable classes")
        if vms:
            logger.info(f"Sample VMs (VMID: Name): {[(vm.get('vmid'), vm.get('name')) for vm in vms[:5]]}")
        
        # Group VMs by potential class prefix
        potential_classes, unmatched = _analyze_vm_naming_patterns(vms)
        
        logger.info(f"Found {len(potential_classes)} potential class groups")
        
        return jsonify({
            "ok": True,
            "potential_classes": potential_classes,
            "total_groups": len(potential_classes),
            "total_vms_scanned": len(vms),
            "sample_vms": [{"vmid": vm.get('vmid'), "name": vm.get('name')} for vm in vms[:10]],
            "unmatched_count": len(unmatched),
            "unmatched_samples": [{"vmid": vm['vmid'], "name": vm['name']} for vm in unmatched[:10]]
        })
        
    except Exception as e:
        logger.exception("Failed to scan importable classes")
        return jsonify({"ok": False, "error": str(e)}), 500


@class_import_bp.route("/import", methods=["POST"])
@login_required
def api_import_class():
    """Import a class from existing Proxmox VMs.
    
    Request body:
    {
        "class_name": "ABC Class",
        "prefix": "ABC",
        "teacher_vmid": 100,
        "base_vmid": 101,
        "student_vmids": [102, 103, 104],
        "cluster_id": "cluster1"
    }
    """
    try:
        user = require_user()
        
        # Only admins and teachers can import classes
        is_admin = is_admin_user(user)
        username = user.split('@')[0] if '@' in user else user
        from app.services.class_service import get_user_by_username
        local_user = get_user_by_username(username)
        
        if not is_admin and (not local_user or local_user.role not in ['teacher', 'adminer']):
            return jsonify({"ok": False, "error": "Access denied"}), 403
        
        data = request.get_json()
        logger.info(f"Import class request data: {data}")
        
        class_name = data.get("class_name")
        prefix = data.get("prefix")
        teacher_vmid = data.get("teacher_vmid")
        base_vmid = data.get("base_vmid")
        student_vmids = data.get("student_vmids", [])
        cluster_id = data.get("cluster_id")  # Can be None - will use current cluster
        
        logger.info(f"Parsed fields - class_name: {class_name}, prefix: {prefix}, teacher_vmid: {teacher_vmid}, base_vmid: {base_vmid}, student_vmids: {student_vmids}, cluster_id: {cluster_id}")
        
        if not class_name or not prefix:
            logger.warning(f"Missing required fields - class_name: {class_name}, prefix: {prefix}")
            return jsonify({"ok": False, "error": "Missing required fields: class_name and prefix"}), 400
        
        # Get teacher user ID
        teacher_id = local_user.id if local_user else None
        if not teacher_id:
            return jsonify({"ok": False, "error": "Teacher user not found"}), 400
        
        # Create class
        new_class = Class(
            name=class_name,
            description=f"Imported from Proxmox VMs with prefix '{prefix}'",
            teacher_id=teacher_id,
            template_id=None,  # No template for imported classes
            join_token=None,
            token_expires_at=None
        )
        db.session.add(new_class)
        db.session.flush()  # Get class ID
        
        # Get VM details from inventory
        all_vmids = []
        if teacher_vmid:
            all_vmids.append(teacher_vmid)
        if base_vmid:
            all_vmids.append(base_vmid)
        all_vmids.extend(student_vmids)
        
        vms = fetch_vm_inventory(vmids=all_vmids, cluster_id=cluster_id)
        vm_map = {vm['vmid']: vm for vm in vms}
        
        # Create VM assignments
        assignments_created = 0
        
        # Teacher VM
        if teacher_vmid and teacher_vmid in vm_map:
            vm = vm_map[teacher_vmid]
            assignment = VMAssignment(
                class_id=new_class.id,
                proxmox_vmid=teacher_vmid,
                vm_name=vm.get('name'),
                node=vm.get('node'),
                mac_address=vm.get('mac'),
                assigned_user_id=teacher_id,
                status='assigned',
                vm_role='teacher'
            )
            db.session.add(assignment)
            assignments_created += 1
        
        # Base VM
        if base_vmid and base_vmid in vm_map:
            vm = vm_map[base_vmid]
            assignment = VMAssignment(
                class_id=new_class.id,
                proxmox_vmid=base_vmid,
                vm_name=vm.get('name'),
                node=vm.get('node'),
                mac_address=vm.get('mac'),
                assigned_user_id=None,
                status='available',
                vm_role='base'
            )
            db.session.add(assignment)
            assignments_created += 1
        
        # Student VMs
        for vmid in student_vmids:
            if vmid in vm_map:
                vm = vm_map[vmid]
                assignment = VMAssignment(
                    class_id=new_class.id,
                    proxmox_vmid=vmid,
                    vm_name=vm.get('name'),
                    node=vm.get('node'),
                    mac_address=vm.get('mac'),
                    assigned_user_id=None,
                    status='available',
                    vm_role='student'
                )
                db.session.add(assignment)
                assignments_created += 1
        
        db.session.commit()
        
        logger.info(f"Imported class '{class_name}' with {assignments_created} VMs by user {user}")
        
        return jsonify({
            "ok": True,
            "class_id": new_class.id,
            "assignments_created": assignments_created,
            "message": f"Successfully imported class '{class_name}' with {assignments_created} VMs"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to import class")
        return jsonify({"ok": False, "error": str(e)}), 500


def _analyze_vm_naming_patterns(vms: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Analyze VM IDs to find potential class groupings.
    
    VMID Pattern (not VM name):
    - Teacher VM: {3digits}00 (e.g., VMID 12300)
    - Base VM: {3digits}99 (e.g., VMID 12399)
    - Student VMs: {3digits}01-98 (e.g., VMID 12301, 12302, etc.)
    
    VM names are separate and not used for pattern matching.
    
    Args:
        vms: List of VM dictionaries from inventory
        
    Returns:
        Tuple of (potential_classes, unmatched_vms)
    """
    # Group VMs by VMID prefix (first 3 digits)
    groups = {}
    unmatched_vms = []  # Track VMs that didn't match any pattern
    
    for vm in vms:
        vm_name = vm.get('name', '')
        vmid = vm.get('vmid')
        
        if not vmid:
            continue
        
        vmid_str = str(vmid)
        
        # VMID must be 5 digits (3 random + 2 identifier)
        if len(vmid_str) != 5 or not vmid_str.isdigit():
            unmatched_vms.append({'vmid': vmid, 'name': vm_name})
            continue
        
        # Extract prefix (first 3 digits) and suffix (last 2 digits)
        prefix = vmid_str[:3]
        suffix = vmid_str[3:5]
        
        matched = False
        
        # Teacher VM: suffix is "00"
        if suffix == "00":
            matched = True
            group_key = prefix
            
            if group_key not in groups:
                groups[group_key] = {
                    'prefix': prefix,
                    'classname': vm_name or f"Class {prefix}",  # Use VM name as class name suggestion
                    'teacher_vm': None,
                    'base_vm': None,
                    'student_vms': []
                }
            groups[group_key]['teacher_vm'] = {
                'vmid': vmid,
                'name': vm_name,
                'node': vm.get('node'),
                'status': vm.get('status')
            }
        
        # Base VM: suffix is "99"
        elif suffix == "99":
            matched = True
            group_key = prefix
            
            if group_key not in groups:
                groups[group_key] = {
                    'prefix': prefix,
                    'classname': vm_name or f"Class {prefix}",
                    'teacher_vm': None,
                    'base_vm': None,
                    'student_vms': []
                }
            groups[group_key]['base_vm'] = {
                'vmid': vmid,
                'name': vm_name,
                'node': vm.get('node'),
                'status': vm.get('status')
            }
        
        # Student VM: suffix is "01" through "98"
        elif suffix.isdigit() and 1 <= int(suffix) <= 98:
            matched = True
            group_key = prefix
            
            if group_key not in groups:
                groups[group_key] = {
                    'prefix': prefix,
                    'classname': vm_name or f"Class {prefix}",
                    'teacher_vm': None,
                    'base_vm': None,
                    'student_vms': []
                }
            groups[group_key]['student_vms'].append({
                'vmid': vmid,
                'name': vm_name,
                'node': vm.get('node'),
                'status': vm.get('status'),
                'suffix': suffix  # Track student number
            })
        
        # Track unmatched VMs
        if not matched:
            unmatched_vms.append({
                'vmid': vmid,
                'name': vm_name
            })
    
    # Convert to list and filter out groups with no student VMs
    result = []
    for group_key, group in groups.items():
        if len(group['student_vms']) > 0:
            # Sort student VMs by VMID
            group['student_vms'].sort(key=lambda x: x['vmid'])
            group['student_count'] = len(group['student_vms'])
            # Use teacher VM name as suggested class name, or default to "Class {prefix}"
            if group['teacher_vm']:
                suggested = group['teacher_vm']['name'] or f"Class {group['prefix']}"
            else:
                suggested = f"Class {group['prefix']}"
            group['suggested_name'] = suggested.replace('-', ' ').replace('_', ' ').title()
            result.append(group)
    
    # Sort by prefix
    result.sort(key=lambda x: x['prefix'])
    
    # Log unmatched VMs for debugging
    if unmatched_vms:
        logger.info(f"Found {len(unmatched_vms)} VMs that didn't match VMID pattern (not 5 digits or suffix not 00/01-98/99): {[(vm['vmid'], vm['name']) for vm in unmatched_vms][:10]}")
    
    return result, unmatched_vms
