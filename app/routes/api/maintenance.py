#!/usr/bin/env python3
"""Admin maintenance API endpoints for cleanup operations."""

import logging
from flask import Blueprint, jsonify, request
from app.models import db, VMAssignment, Class
from app.utils.decorators import admin_required
from sqlalchemy import func

logger = logging.getLogger(__name__)

maintenance_bp = Blueprint('maintenance', __name__, url_prefix='/api/admin/maintenance')


@maintenance_bp.route("/health", methods=["GET"])
@admin_required
def maintenance_health():
    """Simple health check endpoint."""
    return jsonify({"ok": True, "status": "maintenance API is working"})


@maintenance_bp.route("/assignments/stats", methods=["GET"])
@admin_required
def get_assignment_stats():
    """Get statistics about VM assignments."""
    try:
        total = VMAssignment.query.count()
        
        # VMs in classes (class_id is NOT NULL)
        in_classes = VMAssignment.query.filter(
            VMAssignment.class_id.isnot(None)
        ).count()
        
        # VMs assigned to users (assigned_user_id is NOT NULL)
        assigned_to_users = VMAssignment.query.filter(
            VMAssignment.assigned_user_id.isnot(None)
        ).count()
        
        # Find duplicate groups
        duplicates = db.session.query(
            VMAssignment.proxmox_vmid,
            VMAssignment.class_id,
            func.count(VMAssignment.id).label('count')
        ).group_by(
            VMAssignment.proxmox_vmid,
            VMAssignment.class_id
        ).having(func.count(VMAssignment.id) > 1).count()
        
        return jsonify({
            "ok": True,
            "stats": {
                "total": total,
                "in_classes": in_classes,
                "assigned_to_users": assigned_to_users,
                "duplicate_groups": duplicates
            }
        })
    except Exception as e:
        logger.exception("Failed to get assignment stats")
        return jsonify({"ok": False, "error": str(e)}), 500


@maintenance_bp.route("/assignments/unassigned", methods=["GET"])
@admin_required
def get_unassigned_assignments():
    """
    List orphaned direct assignments (no user AND no class).
    
    Note: Class pool VMs (with class_id but no user) are NOT shown here
    as they are not orphaned - they're legitimate unassigned pool VMs.
    """
    try:
        # Only show DIRECT assignments that are orphaned (no user AND no class)
        orphaned = VMAssignment.query.filter(
            VMAssignment.assigned_user_id.is_(None),
            VMAssignment.class_id.is_(None)
        ).all()
        
        result = []
        for assign in orphaned:
            result.append({
                'id': assign.id,
                'vmid': assign.proxmox_vmid,
                'class_id': assign.class_id,
                'class_name': "DIRECT",
                'status': assign.status,
                'assigned_user_id': assign.assigned_user_id
            })
        
        return jsonify({
            "ok": True,
            "unassigned": result,
            "count": len(result)
        })
    except Exception as e:
        logger.exception("Failed to get orphaned assignments")
        return jsonify({"ok": False, "error": str(e)}), 500


@maintenance_bp.route("/assignments/duplicates", methods=["GET"])
@admin_required
def get_duplicate_assignments():
    """List duplicate assignments (same VM in same class)."""
    try:
        duplicates_query = db.session.query(
            VMAssignment.proxmox_vmid,
            VMAssignment.class_id,
            func.count(VMAssignment.id).label('count')
        ).group_by(
            VMAssignment.proxmox_vmid,
            VMAssignment.class_id
        ).having(func.count(VMAssignment.id) > 1).all()
        
        result = []
        for vmid, class_id, count in duplicates_query:
            assignments = VMAssignment.query.filter_by(
                proxmox_vmid=vmid,
                class_id=class_id
            ).order_by(VMAssignment.id).all()
            
            cls_name = None
            if class_id:
                cls = Class.query.get(class_id)
                cls_name = cls.name if cls else "UNKNOWN"
            
            assignment_list = []
            for assign in assignments:
                user_name = assign.assigned_user.username if assign.assigned_user else None
                assignment_list.append({
                    'id': assign.id,
                    'user_id': assign.assigned_user_id,
                    'username': user_name or "UNASSIGNED",
                    'status': assign.status
                })
            
            result.append({
                'vmid': vmid,
                'class_id': class_id,
                'class_name': cls_name or "DIRECT",
                'count': count,
                'assignments': assignment_list
            })
        
        return jsonify({
            "ok": True,
            "duplicates": result,
            "count": len(result)
        })
    except Exception as e:
        logger.exception("Failed to get duplicate assignments")
        return jsonify({"ok": False, "error": str(e)}), 500


@maintenance_bp.route("/assignments/delete-unassigned", methods=["POST"])
@admin_required
def delete_unassigned_assignments():
    """
    Delete orphaned direct assignments (no user, no class).
    
    IMPORTANT: This does NOT delete class pool VMs that are unassigned!
    Class pool VMs (with class_id but no assigned_user) are legitimate
    unassigned VMs waiting for students to join.
    """
    try:
        # Only delete DIRECT assignments that are orphaned (no user AND no class)
        orphaned = VMAssignment.query.filter(
            VMAssignment.assigned_user_id.is_(None),
            VMAssignment.class_id.is_(None)
        ).all()
        count = len(orphaned)
        
        for assign in orphaned:
            db.session.delete(assign)
        
        db.session.commit()
        logger.info(f"Deleted {count} orphaned direct assignments (no class, no user)")
        
        return jsonify({
            "ok": True,
            "message": f"Deleted {count} orphaned assignments",
            "deleted": count
        })
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to delete orphaned assignments")
        return jsonify({"ok": False, "error": str(e)}), 500


@maintenance_bp.route("/assignments/delete/<int:assignment_id>", methods=["DELETE"])
@admin_required
def delete_assignment(assignment_id: int):
    """Delete a specific assignment by ID."""
    try:
        assign = VMAssignment.query.get(assignment_id)
        if not assign:
            return jsonify({"ok": False, "error": "Assignment not found"}), 404
        
        vmid = assign.proxmox_vmid
        cls_id = assign.class_id
        user_name = assign.assigned_user.username if assign.assigned_user else "UNASSIGNED"
        
        db.session.delete(assign)
        db.session.commit()
        
        logger.info(f"Deleted assignment {assignment_id}: VM {vmid} from user {user_name}")
        
        return jsonify({
            "ok": True,
            "message": f"Deleted assignment {assignment_id}",
            "deleted": {
                'id': assignment_id,
                'vmid': vmid,
                'class_id': cls_id,
                'user': user_name
            }
        })
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Failed to delete assignment {assignment_id}")
        return jsonify({"ok": False, "error": str(e)}), 500


@maintenance_bp.route("/recover-vms/scan", methods=["GET"])
@admin_required
def scan_recoverable_vms():
    """Scan Proxmox for VMs that match class ID patterns."""
    try:
        logger.info("Starting VM recovery scan")
        
        from app.services.proxmox_service import get_all_vms
        
        # Get all VMs from Proxmox
        try:
            all_vms = get_all_vms(skip_ips=True)
            logger.info(f"Found {len(all_vms)} VMs in Proxmox")
        except Exception as e:
            logger.error(f"Failed to get VMs from Proxmox: {e}")
            raise
        
        # Get all classes
        classes = Class.query.all()
        logger.info(f"Found {len(classes)} classes")
        
        # Log all VMIDs for debugging
        all_vmids = [vm['vmid'] for vm in all_vms]
        logger.info(f"All VMIDs: {sorted(all_vmids)}")
        
        # Find VMs matching class ID patterns
        # Pattern: class_id=5 → look for VMIDs starting with 50 (50001, 50002, etc)
        recoverable = {}
        diagnostics = {
            'all_vms': len(all_vms),
            'all_classes': len(classes),
            'all_vmids': sorted(all_vmids),
            'class_patterns_checked': []
        }
        
        for cls in classes:
            class_id = cls.id
            class_prefix = str(class_id).zfill(2)  # "05" for class 5
            
            matching_vms = []
            
            for vm in all_vms:
                vmid = vm['vmid']
                vmid_str = str(vmid)
                
                # Check if VMID starts with class ID pattern
                if vmid_str.startswith(class_prefix) and len(vmid_str) >= 5:
                    # Also check it's not already assigned to this class
                    existing = VMAssignment.query.filter_by(
                        proxmox_vmid=vmid,
                        class_id=class_id
                    ).first()
                    
                    if not existing:
                        matching_vms.append({
                            'vmid': vmid,
                            'name': vm.get('name', 'unknown'),
                            'status': vm.get('status', 'unknown'),
                            'node': vm.get('node', 'unknown'),
                            'type': vm.get('type', 'qemu')
                        })
            
            # Log this class's pattern check
            pattern_check = {
                'class_id': class_id,
                'class_name': cls.name,
                'pattern': class_prefix,
                'matching_count': len(matching_vms),
                'matching_vmids': [vm['vmid'] for vm in matching_vms]
            }
            diagnostics['class_patterns_checked'].append(pattern_check)
            logger.info(f"Class {class_id} pattern '{class_prefix}': found {len(matching_vms)} VMs")
            
            if matching_vms:
                recoverable[str(class_id)] = {
                    'class_name': cls.name,
                    'vms': matching_vms,
                    'count': len(matching_vms)
                }
        
        total_recoverable = sum(d['count'] for d in recoverable.values())
        logger.info(f"Found {total_recoverable} total recoverable VMs")
        
        return jsonify({
            "ok": True,
            "recoverable": recoverable,
            "total_recoverable": total_recoverable,
            "diagnostics": diagnostics
        })
    
    except Exception as e:
        logger.exception("Failed to scan recoverable VMs")
        return jsonify({"ok": False, "error": f"Scan error: {str(e)}"}), 500


@maintenance_bp.route("/recover-vms/list-all-vms", methods=["GET"])
@admin_required
def list_all_proxmox_vms():
    """List all VMs in Proxmox for diagnostic purposes."""
    try:
        from app.services.proxmox_service import get_all_vms
        
        all_vms = get_all_vms(skip_ips=True)
        
        vm_list = []
        for vm in all_vms:
            vm_list.append({
                'vmid': vm['vmid'],
                'name': vm.get('name', 'unknown'),
                'status': vm.get('status', 'unknown'),
                'node': vm.get('node', 'unknown'),
                'type': vm.get('type', 'qemu'),
                'in_db': bool(VMAssignment.query.filter_by(proxmox_vmid=vm['vmid']).first())
            })
        
        # Sort by VMID
        vm_list.sort(key=lambda x: x['vmid'])
        
        return jsonify({
            "ok": True,
            "total_vms": len(vm_list),
            "vms": vm_list
        })
    
    except Exception as e:
        logger.exception("Failed to list VMs")
        return jsonify({"ok": False, "error": str(e)}), 500


@maintenance_bp.route("/recover-vms/manually-add", methods=["POST"])
@admin_required
def manually_add_vms_to_class():
    """Manually add VMs to a class."""
    try:
        data = request.get_json()
        class_id = data.get('class_id')
        vmids = data.get('vmids', [])  # List of VM IDs to add
        
        if not class_id or not vmids:
            return jsonify({"ok": False, "error": "class_id and vmids required"}), 400
        
        # Verify class exists
        cls = Class.query.get(class_id)
        if not cls:
            return jsonify({"ok": False, "error": f"Class {class_id} not found"}), 404
        
        # Get VM names from Proxmox
        from app.services.proxmox_service import get_all_vms
        all_vms = get_all_vms(skip_ips=True)
        vm_map = {vm['vmid']: vm for vm in all_vms}
        
        added = 0
        skipped = 0
        
        for vmid in vmids:
            # Check if already assigned to this class
            existing = VMAssignment.query.filter_by(
                proxmox_vmid=vmid,
                class_id=class_id
            ).first()
            
            if existing:
                logger.info(f"VM {vmid} already assigned to class {class_id}, skipping")
                skipped += 1
                continue
            
            # Get VM info
            vm_info = vm_map.get(vmid)
            if not vm_info:
                logger.warning(f"VM {vmid} not found in Proxmox")
                continue
            
            try:
                assign = VMAssignment(
                    class_id=class_id,
                    proxmox_vmid=vmid,
                    vm_name=vm_info.get('name'),
                    node=vm_info.get('node'),
                    status='available',
                    assigned_user_id=None
                )
                db.session.add(assign)
                added += 1
                logger.info(f"Added VM {vmid} ({vm_info.get('name')}) to class {class_id}")
            except Exception as e:
                logger.error(f"Failed to add VM {vmid}: {e}")
                continue
        
        db.session.commit()
        
        return jsonify({
            "ok": True,
            "message": f"Added {added} VMs to class {cls.name}",
            "added": added,
            "skipped": skipped
        })
    
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to manually add VMs")
        return jsonify({"ok": False, "error": str(e)}), 500

@admin_required
def recover_deleted_vms():
    """Recover deleted class VM assignments from Proxmox."""
    try:
        logger.info("Starting VM recovery")
        
        from app.services.proxmox_service import get_all_vms
        
        # Scan for recoverable VMs
        try:
            all_vms = get_all_vms(skip_ips=True)
        except Exception as e:
            logger.error(f"Failed to get VMs: {e}")
            raise
        
        classes = Class.query.all()
        
        recovered = 0
        
        for cls in classes:
            class_id = cls.id
            class_prefix = str(class_id).zfill(2)
            
            for vm in all_vms:
                vmid = vm['vmid']
                vmid_str = str(vmid)
                
                # Check if VMID matches class pattern
                if vmid_str.startswith(class_prefix) and len(vmid_str) >= 5:
                    # Check it's not already assigned
                    existing = VMAssignment.query.filter_by(
                        proxmox_vmid=vmid,
                        class_id=class_id
                    ).first()
                    
                    if not existing:
                        try:
                            assign = VMAssignment(
                                class_id=class_id,
                                proxmox_vmid=vmid,
                                vm_name=vm.get('name'),
                                node=vm.get('node'),
                                status='available',
                                assigned_user_id=None
                            )
                            db.session.add(assign)
                            recovered += 1
                        except Exception as e:
                            logger.error(f"Failed to recover VM {vmid}: {e}")
                            continue
        
        db.session.commit()
        logger.info(f"Successfully recovered {recovered} VMs")
        
        return jsonify({
            "ok": True,
            "message": f"Successfully recovered {recovered} VMs to classes",
            "recovered": recovered
        })
    
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to recover VMs")
        return jsonify({"ok": False, "error": f"Recovery error: {str(e)}"}), 500
