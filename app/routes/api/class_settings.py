""" 
API endpoints for class settings management.
"""
from flask import Blueprint, jsonify, request, session
from datetime import datetime
from app.models import db, Class, User, VMAssignment
from app.utils.decorators import login_required
from app.services.proxmox_service import get_proxmox_admin_for_cluster

bp = Blueprint('class_settings', __name__)


def require_teacher_or_admin(class_: Class, user_id: str) -> bool:
    """Check if user is teacher/admin for this class."""
    from app.services.proxmox_service import is_admin_user
    
    # Check if admin
    if is_admin_user(user_id):
        return True
    
    # Get local user
    local_user = User.query.filter_by(username=user_id).first()
    if not local_user:
        return False
    
    # Check if owner (primary teacher or co-owner)
    return class_.is_owner(local_user)


@bp.route('/api/classes/<int:class_id>/settings', methods=['GET'])
@login_required
def get_class_settings(class_id: int):
    """Get class settings."""
    user_id = session.get("user")
    
    class_ = Class.query.get(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check permissions
    if not require_teacher_or_admin(class_, user_id):
        return jsonify({"ok": False, "error": "Permission denied"}), 403
    
    return jsonify({
        "ok": True,
        "settings": {
            "auto_shutdown_enabled": class_.auto_shutdown_enabled or False,
            "auto_shutdown_cpu_threshold": class_.auto_shutdown_cpu_threshold or 20,
            "auto_shutdown_idle_minutes": class_.auto_shutdown_idle_minutes or 30,
            "restrict_hours": class_.restrict_hours or False,
            "hours_start": class_.hours_start or 0,
            "hours_end": class_.hours_end or 23,
            "max_usage_hours": class_.max_usage_hours or 0,
            "cpu_cores": class_.cpu_cores or 2,
            "memory_mb": class_.memory_mb or 4096
        }
    })


@bp.route('/api/classes/<int:class_id>/settings', methods=['POST'])
@login_required
def update_class_settings(class_id: int):
    """Update class settings."""
    user_id = session.get("user")
    
    class_ = Class.query.get(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check permissions
    if not require_teacher_or_admin(class_, user_id):
        return jsonify({"ok": False, "error": "Permission denied"}), 403
    
    data = request.get_json()
    
    # Auto-shutdown settings (optional)
    if 'auto_shutdown_enabled' in data:
        auto_shutdown_enabled = data.get('auto_shutdown_enabled', False)
        cpu_threshold = data.get('auto_shutdown_cpu_threshold', 20)
        idle_minutes = data.get('auto_shutdown_idle_minutes', 30)
        
        if not isinstance(auto_shutdown_enabled, bool):
            return jsonify({"ok": False, "error": "auto_shutdown_enabled must be boolean"}), 400
        
        if not isinstance(cpu_threshold, int) or cpu_threshold < 5 or cpu_threshold > 50:
            return jsonify({"ok": False, "error": "CPU threshold must be between 5 and 50"}), 400
        
        if not isinstance(idle_minutes, int) or idle_minutes < 5 or idle_minutes > 240:
            return jsonify({"ok": False, "error": "Idle minutes must be between 5 and 240"}), 400
        
        class_.auto_shutdown_enabled = auto_shutdown_enabled
        class_.auto_shutdown_cpu_threshold = cpu_threshold
        class_.auto_shutdown_idle_minutes = idle_minutes
    
    # Hour restriction settings (optional)
    if 'restrict_hours' in data:
        restrict_hours = data.get('restrict_hours', False)
        hours_start = data.get('hours_start', 0)
        hours_end = data.get('hours_end', 23)
        max_usage_hours = data.get('max_usage_hours', 0)
        
        if not isinstance(restrict_hours, bool):
            return jsonify({"ok": False, "error": "restrict_hours must be boolean"}), 400
        
        if not isinstance(hours_start, int) or hours_start < 0 or hours_start > 23:
            return jsonify({"ok": False, "error": "hours_start must be between 0 and 23"}), 400
        
        if not isinstance(hours_end, int) or hours_end < 0 or hours_end > 23:
            return jsonify({"ok": False, "error": "hours_end must be between 0 and 23"}), 400
        
        if not isinstance(max_usage_hours, int) or max_usage_hours < 0 or max_usage_hours > 500:
            return jsonify({"ok": False, "error": "max_usage_hours must be between 0 and 500"}), 400
        
        class_.restrict_hours = restrict_hours
        class_.hours_start = hours_start
        class_.hours_end = hours_end
        class_.max_usage_hours = max_usage_hours
    
    # VM Hardware specs (optional) - will trigger student VM recreation
    recreate_vms = False
    if 'cpu_cores' in data or 'memory_mb' in data:
        new_cpu = data.get('cpu_cores', class_.cpu_cores or 2)
        new_memory = data.get('memory_mb', class_.memory_mb or 4096)
        
        if not isinstance(new_cpu, int) or new_cpu < 1 or new_cpu > 32:
            return jsonify({"ok": False, "error": "CPU cores must be between 1 and 32"}), 400
        
        if not isinstance(new_memory, int) or new_memory < 512 or new_memory > 131072:
            return jsonify({"ok": False, "error": "Memory must be between 512MB and 128GB"}), 400
        
        # Check if specs actually changed
        if new_cpu != class_.cpu_cores or new_memory != class_.memory_mb:
            class_.cpu_cores = new_cpu
            class_.memory_mb = new_memory
            recreate_vms = True
    
    try:
        db.session.commit()
        
        # If VM specs changed, trigger recreation of student VMs in background
        if recreate_vms:
            import threading
            from app.services.class_vm_service import recreate_student_vms_with_new_specs
            
            thread = threading.Thread(
                target=recreate_student_vms_with_new_specs,
                args=(class_.id, class_.cpu_cores, class_.memory_mb),
                daemon=True
            )
            thread.start()
            
            message = "Settings updated. Student VMs are being recreated with new specs in the background."
        else:
            message = "Settings updated successfully"
        
        return jsonify({
            "ok": True,
            "message": message,
            "recreating_vms": recreate_vms,
            "settings": {
                "auto_shutdown_enabled": class_.auto_shutdown_enabled,
                "auto_shutdown_cpu_threshold": class_.auto_shutdown_cpu_threshold,
                "auto_shutdown_idle_minutes": class_.auto_shutdown_idle_minutes,
                "restrict_hours": class_.restrict_hours,
                "hours_start": class_.hours_start,
                "hours_end": class_.hours_end,
                "max_usage_hours": class_.max_usage_hours,
                "cpu_cores": class_.cpu_cores,
                "memory_mb": class_.memory_mb
            }
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"Database error: {str(e)}"}), 500


@bp.route('/api/vm/<int:vmid>/session-progress', methods=['GET'])
@login_required
def get_any_vm_session_progress(vmid: int):
    """Get VM session progress for any VM the user has access to."""
    user_id = session.get("user")
    
    # Get local user
    local_user = User.query.filter_by(username=user_id).first()
    if not local_user:
        return jsonify({"ok": False, "error": "User not found"}), 404
    
    # Find VM assignment for this user
    vm = VMAssignment.query.filter_by(
        proxmox_vmid=vmid,
        assigned_user_id=local_user.id
    ).first()
    
    if not vm or not vm.class_id:
        return jsonify({"ok": True, "has_restrictions": False})
    
    # Get class
    class_ = Class.query.get(vm.class_id)
    if not class_:
        return jsonify({"ok": True, "has_restrictions": False})
    
    # Check if class has any restrictions
    has_restrictions = (
        (class_.max_usage_hours or 0) > 0 or
        (class_.restrict_hours or False)
    )
    
    result = {
        "ok": True,
        "has_restrictions": has_restrictions,
        "status": "stopped",
        "uptime_minutes": 0,
        "max_usage_hours": class_.max_usage_hours or 0,
        "usage_hours": vm.usage_hours or 0.0,
        "restrict_hours": class_.restrict_hours or False,
        "hours_start": class_.hours_start or 0,
        "hours_end": class_.hours_end or 23,
        "within_hours": True,
        "class_name": class_.name
    }
    
    if not has_restrictions:
        return jsonify(result)
    
    # Get VM status and uptime from Proxmox
    try:
        cluster_id = class_.deployment_cluster
        if cluster_id:
            proxmox = get_proxmox_admin_for_cluster(int(cluster_id))
            node = vm.node
            
            if node:
                status_data = proxmox.nodes(node).qemu(vmid).status.current.get()
                result["status"] = status_data.get("status", "stopped")
                
                if result["status"] == "running":
                    uptime_seconds = status_data.get("uptime", 0)
                    result["uptime_minutes"] = uptime_seconds / 60
    except Exception:
        pass
    
    # Check if within allowed hours
    if class_.restrict_hours:
        current_hour = datetime.now().hour
        start = class_.hours_start or 0
        end = class_.hours_end or 23
        
        if start <= end:
            result["within_hours"] = start <= current_hour < end
        else:
            result["within_hours"] = current_hour >= start or current_hour < end
    
    return jsonify(result)


@bp.route('/api/classes/<int:class_id>/vm/<int:vmid>/session-progress', methods=['GET'])
@login_required
def get_vm_session_progress(class_id: int, vmid: int):
    """Get VM session progress for student (uptime vs limits)."""
    user_id = session.get("user")
    
    class_ = Class.query.get(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check if user has access to this VM in this class
    local_user = User.query.filter_by(username=user_id).first()
    if not local_user:
        return jsonify({"ok": False, "error": "User not found"}), 404
    
    vm = VMAssignment.query.filter_by(
        class_id=class_id,
        proxmox_vmid=vmid,
        assigned_user_id=local_user.id
    ).first()
    
    if not vm:
        return jsonify({"ok": False, "error": "VM not assigned to you"}), 403
    
    # Check if class has any restrictions
    has_restrictions = (
        (class_.max_usage_hours or 0) > 0 or
        (class_.restrict_hours or False)
    )
    
    result = {
        "ok": True,
        "has_restrictions": has_restrictions,
        "status": "stopped",
        "uptime_minutes": 0,
        "max_usage_hours": class_.max_usage_hours or 0,
        "usage_hours": vm.usage_hours or 0.0,
        "restrict_hours": class_.restrict_hours or False,
        "hours_start": class_.hours_start or 0,
        "hours_end": class_.hours_end or 23,
        "within_hours": True
    }
    
    if not has_restrictions:
        return jsonify(result)
    
    # Get VM status and uptime from Proxmox
    try:
        cluster_id = class_.deployment_cluster
        if cluster_id:
            proxmox = get_proxmox_admin_for_cluster(int(cluster_id))
            node = vm.node
            
            if node:
                status_data = proxmox.nodes(node).qemu(vmid).status.current.get()
                result["status"] = status_data.get("status", "stopped")
                
                if result["status"] == "running":
                    uptime_seconds = status_data.get("uptime", 0)
                    result["uptime_minutes"] = uptime_seconds / 60
    except Exception:
        # If we can't get status, just return defaults
        pass
    
    # Check if within allowed hours
    if class_.restrict_hours:
        current_hour = datetime.now().hour
        start = class_.hours_start or 0
        end = class_.hours_end or 23
        
        if start <= end:
            result["within_hours"] = start <= current_hour < end
        else:
            result["within_hours"] = current_hour >= start or current_hour < end
    
    return jsonify(result)


@bp.route('/api/classes/<int:class_id>/reset-usage/<int:vm_assignment_id>', methods=['POST'])
@login_required
def reset_vm_usage(class_id: int, vm_assignment_id: int):
    """Reset usage hours for a specific VM assignment."""
    user_id = session.get("user")
    
    class_ = Class.query.get(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check permissions
    if not require_teacher_or_admin(class_, user_id):
        return jsonify({"ok": False, "error": "Permission denied"}), 403
    
    vm = VMAssignment.query.get(vm_assignment_id)
    if not vm or vm.class_id != class_id:
        return jsonify({"ok": False, "error": "VM not found in this class"}), 404
    
    vm.usage_hours = 0.0
    vm.usage_last_reset = datetime.utcnow()
    
    try:
        db.session.commit()
        return jsonify({
            "ok": True,
            "message": f"Usage reset for {vm.vm_name or 'VM ' + str(vm.proxmox_vmid)}"
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"Database error: {str(e)}"}), 500


@bp.route('/api/classes/<int:class_id>/reset-all-usage', methods=['POST'])
@login_required
def reset_all_class_usage(class_id: int):
    """Reset usage hours for all VMs in the class."""
    user_id = session.get("user")
    
    class_ = Class.query.get(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check permissions
    if not require_teacher_or_admin(class_, user_id):
        return jsonify({"ok": False, "error": "Permission denied"}), 403
    
    reset_time = datetime.utcnow()
    count = 0
    
    for vm in class_.vm_assignments:
        if not vm.is_teacher_vm and not vm.is_template_vm:
            vm.usage_hours = 0.0
            vm.usage_last_reset = reset_time
            count += 1
    
    try:
        db.session.commit()
        return jsonify({
            "ok": True,
            "message": f"Usage reset for {count} student VMs"
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"Database error: {str(e)}"}), 500
