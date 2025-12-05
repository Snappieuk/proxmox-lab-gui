#!/usr/bin/env python3
"""
Template Publishing API.

Allows teachers to publish changes from their VM to the class template,
then propagate to all student VMs.
"""

import logging
import threading

from flask import Blueprint, current_app, jsonify

from app.models import Class, VMAssignment
from app.utils.decorators import login_required

logger = logging.getLogger(__name__)

api_publish_template_bp = Blueprint('api_publish_template', __name__, url_prefix='/api')


@api_publish_template_bp.route("/classes/<int:class_id>/publish-template", methods=["POST"])
@login_required
def publish_template(class_id: int):
    """
    Publish teacher's VM changes to class template and all student VMs.
    
    Workflow:
    1. Copy teacher VM disk → class template VM
    2. Copy class template VM disk → all student VMs
    
    This is a long-running operation that runs in the background.
    """
    from app.services.user_manager import require_user
    
    username = require_user()
    
    try:
        # Get class
        class_obj = Class.query.get(class_id)
        if not class_obj:
            return jsonify({"ok": False, "error": "Class not found"}), 404
        
        # Check if user is teacher or co-owner
        from app.models import User
        user = User.query.filter_by(username=username).first()
        if not user:
            return jsonify({"ok": False, "error": "User not found"}), 404
        
        if not class_obj.is_owner(user):
            return jsonify({"ok": False, "error": "Only class owners can publish template changes"}), 403
        
        # Get teacher VM and template VM
        teacher_vm = VMAssignment.query.filter_by(
            class_id=class_id,
            is_teacher_vm=True
        ).first()
        
        template_vm = VMAssignment.query.filter_by(
            class_id=class_id,
            is_template_vm=True
        ).first()
        
        if not teacher_vm:
            return jsonify({"ok": False, "error": "No teacher VM found for this class"}), 404
        
        if not template_vm:
            return jsonify({"ok": False, "error": "No template VM found for this class"}), 404
        
        # Get all student VMs
        student_vms = VMAssignment.query.filter_by(
            class_id=class_id,
            is_teacher_vm=False,
            is_template_vm=False
        ).all()
        
        if len(student_vms) == 0:
            return jsonify({"ok": False, "error": "No student VMs found to publish to"}), 404
        
        # Start background publishing process
        app = current_app._get_current_object()
        
        def _publish_in_background():
            try:
                app_ctx = app.app_context()
                app_ctx.push()
                
                from app.services.proxmox_operations import copy_vm_disk, remove_vm_disk
                
                logger.info(f"Publishing template for class {class_id}: teacher VM {teacher_vm.proxmox_vmid} → template VM {template_vm.proxmox_vmid} → {len(student_vms)} student VMs")
                
                # Step 1: Copy teacher VM disk to template VM
                logger.info(f"Step 1: Copying disk from teacher VM {teacher_vm.proxmox_vmid} to template VM {template_vm.proxmox_vmid}")
                
                # Remove template VM disk first
                success = remove_vm_disk(
                    vmid=template_vm.proxmox_vmid,
                    node=template_vm.node,
                    cluster_ip="10.220.15.249"
                )
                
                if not success:
                    logger.error(f"Failed to remove template VM {template_vm.proxmox_vmid} disk")
                    return
                
                # Copy teacher disk to template
                success, msg = copy_vm_disk(
                    source_vmid=teacher_vm.proxmox_vmid,
                    source_node=teacher_vm.node,
                    target_vmid=template_vm.proxmox_vmid,
                    target_node=template_vm.node,
                    cluster_ip="10.220.15.249"
                )
                
                if not success:
                    logger.error(f"Failed to copy teacher VM disk to template: {msg}")
                    return
                
                logger.info(f"Successfully updated template VM {template_vm.proxmox_vmid} with teacher's changes")
                
                # Step 2: Copy template VM disk to all student VMs
                logger.info(f"Step 2: Copying template VM {template_vm.proxmox_vmid} disk to {len(student_vms)} student VMs")
                
                for student_vm in student_vms:
                    try:
                        # Remove student VM disk first
                        success = remove_vm_disk(
                            vmid=student_vm.proxmox_vmid,
                            node=student_vm.node,
                            cluster_ip="10.220.15.249"
                        )
                        
                        if not success:
                            logger.error(f"Failed to remove student VM {student_vm.proxmox_vmid} disk")
                            continue
                        
                        # Copy template disk to student
                        success, msg = copy_vm_disk(
                            source_vmid=template_vm.proxmox_vmid,
                            source_node=template_vm.node,
                            target_vmid=student_vm.proxmox_vmid,
                            target_node=student_vm.node,
                            cluster_ip="10.220.15.249"
                        )
                        
                        if success:
                            logger.info(f"Updated student VM {student_vm.proxmox_vmid}: {msg}")
                        else:
                            logger.error(f"Failed to update student VM {student_vm.proxmox_vmid}: {msg}")
                    
                    except Exception as e:
                        logger.exception(f"Error updating student VM {student_vm.proxmox_vmid}: {e}")
                
                logger.info(f"Template publishing complete for class {class_id}")
                
            except Exception as e:
                logger.exception(f"Failed to publish template for class {class_id}: {e}")
            finally:
                try:
                    app_ctx.pop()
                except Exception:
                    pass
        
        # Start background thread
        threading.Thread(target=_publish_in_background, daemon=True).start()
        
        return jsonify({
            "ok": True,
            "message": f"Publishing template changes to {len(student_vms)} student VMs in background",
            "teacher_vmid": teacher_vm.proxmox_vmid,
            "template_vmid": template_vm.proxmox_vmid,
            "student_count": len(student_vms)
        })
        
    except Exception as e:
        logger.exception(f"Failed to publish template for class {class_id}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
