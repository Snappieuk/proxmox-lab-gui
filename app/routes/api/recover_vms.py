#!/usr/bin/env python3
"""VM recovery API - add cached VMs back to classes."""

import logging
from flask import Blueprint, jsonify, request
from app.models import db, VMAssignment, Class
from app.utils.decorators import admin_required
from app.services.proxmox_service import get_all_vms

logger = logging.getLogger(__name__)

recover_vms_bp = Blueprint('recover_vms', __name__, url_prefix='/api/recover-vms')


@recover_vms_bp.route("/vms", methods=["GET"])
@admin_required
def get_cached_vms():
    """Get all cached VMs (including those in other classes)."""
    try:
        all_vms = get_all_vms()
        
        # Augment VMs with assignment info from database
        for vm in all_vms:
            vmid = vm.get('vmid')
            # Look up the most recent/relevant assignment for this VM
            assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
            if assignment:
                vm['class_id'] = assignment.class_id  # None if direct assignment
                vm['assigned_user_id'] = assignment.assigned_user_id
            else:
                vm['class_id'] = None
                vm['assigned_user_id'] = None
        
        # Return all VMs - user can add any VM to any class
        # (allows moving VMs between classes or adding duplicates if needed)
        return jsonify({
            "ok": True,
            "vms": all_vms,
            "total": len(all_vms)
        })
    except Exception as e:
        logger.exception("Failed to get cached VMs")
        return jsonify({"ok": False, "error": str(e)}), 500


@recover_vms_bp.route("/add-to-class", methods=["POST"])
@admin_required
def add_vms_to_class():
    """Add selected VMs to a class."""
    try:
        data = request.get_json()
        class_id = data.get('class_id')
        vmids = data.get('vmids', [])
        
        if not class_id:
            return jsonify({"ok": False, "error": "class_id required"}), 400
        
        if not vmids:
            return jsonify({"ok": False, "error": "At least one VM required"}), 400
        
        # Verify class exists
        cls = Class.query.get(class_id)
        if not cls:
            return jsonify({"ok": False, "error": f"Class {class_id} not found"}), 404
        
        # Get VM info from cache
        all_vms = get_all_vms()
        vm_map = {vm['vmid']: vm for vm in all_vms}
        
        added = 0
        skipped = 0
        errors = []
        
        for vmid in vmids:
            try:
                # Check if already exists in this class
                existing = VMAssignment.query.filter_by(
                    proxmox_vmid=vmid,
                    class_id=class_id
                ).first()
                
                if existing:
                    skipped += 1
                    continue
                
                # Get VM info
                vm = vm_map.get(vmid)
                if not vm:
                    errors.append(f"VMID {vmid} not found in Proxmox")
                    continue
                
                # FIRST: Check if assignment exists for this VMID (regardless of class)
                # If so, UPDATE it instead of creating a duplicate
                any_assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
                
                if any_assignment:
                    # Update existing assignment
                    any_assignment.class_id = class_id
                    any_assignment.assigned_user_id = None  # Reset to unassigned pool VM
                    any_assignment.status = 'available'
                    any_assignment.vm_name = vm.get('name')
                    any_assignment.node = vm.get('node')
                    any_assignment.mac_address = vm.get('mac_address')
                    any_assignment.cached_ip = vm.get('ip')
                    db.session.add(any_assignment)
                else:
                    # Create new assignment only if none exists
                    assignment = VMAssignment(
                        proxmox_vmid=vmid,
                        class_id=class_id,
                        assigned_user_id=None,  # Unassigned pool VM
                        status='available',
                        vm_name=vm.get('name'),
                        node=vm.get('node'),
                        mac_address=vm.get('mac_address'),
                        cached_ip=vm.get('ip')
                    )
                    db.session.add(assignment)
                
                added += 1
            
            except Exception as e:
                logger.exception(f"Failed to add VMID {vmid}")
                errors.append(f"VMID {vmid}: {str(e)}")
                skipped += 1
        
        db.session.commit()
        
        return jsonify({
            "ok": True,
            "added": added,
            "skipped": skipped,
            "errors": errors
        })
    
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to add VMs to class")
        return jsonify({"ok": False, "error": str(e)}), 500
