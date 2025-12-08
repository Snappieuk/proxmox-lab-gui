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
    from app.services.proxmox_client import is_admin_user
    
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
            "max_session_minutes": class_.max_session_minutes or 0
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
        max_session_minutes = data.get('max_session_minutes', 0)
        
        if not isinstance(restrict_hours, bool):
            return jsonify({"ok": False, "error": "restrict_hours must be boolean"}), 400
        
        if not isinstance(hours_start, int) or hours_start < 0 or hours_start > 23:
            return jsonify({"ok": False, "error": "hours_start must be between 0 and 23"}), 400
        
        if not isinstance(hours_end, int) or hours_end < 0 or hours_end > 23:
            return jsonify({"ok": False, "error": "hours_end must be between 0 and 23"}), 400
        
        if not isinstance(max_session_minutes, int) or max_session_minutes < 0 or max_session_minutes > 1440:
            return jsonify({"ok": False, "error": "max_session_minutes must be between 0 and 1440"}), 400
        
        class_.restrict_hours = restrict_hours
        class_.hours_start = hours_start
        class_.hours_end = hours_end
        class_.max_session_minutes = max_session_minutes
    
    try:
        db.session.commit()
        return jsonify({
            "ok": True,
            "message": "Settings updated successfully",
            "settings": {
                "auto_shutdown_enabled": class_.auto_shutdown_enabled,
                "auto_shutdown_cpu_threshold": class_.auto_shutdown_cpu_threshold,
                "auto_shutdown_idle_minutes": class_.auto_shutdown_idle_minutes,
                "restrict_hours": class_.restrict_hours,
                "hours_start": class_.hours_start,
                "hours_end": class_.hours_end,
                "max_session_minutes": class_.max_session_minutes
            }
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"Database error: {str(e)}"}), 500


@bp.route('/api/vm/<int:vmid>/session-progress', methods=['GET'])
@login_required
def get_any_vm_session_progress(vmid: int):
    """Get VM session progress for any VM the user has access to."""
    user_id = session.get("user")\n    \n    # Get local user\n    local_user = User.query.filter_by(username=user_id).first()\n    if not local_user:\n        return jsonify({\"ok\": False, \"error\": \"User not found\"}), 404\n    \n    # Find VM assignment for this user\n    vm = VMAssignment.query.filter_by(\n        proxmox_vmid=vmid,\n        assigned_user_id=local_user.id\n    ).first()\n    \n    if not vm or not vm.class_id:\n        return jsonify({\"ok\": True, \"has_restrictions\": False})\n    \n    # Get class\n    class_ = Class.query.get(vm.class_id)\n    if not class_:\n        return jsonify({\"ok\": True, \"has_restrictions\": False})\n    \n    # Check if class has any restrictions\n    has_restrictions = (\n        (class_.max_session_minutes or 0) > 0 or\n        (class_.restrict_hours or False)\n    )\n    \n    result = {\n        \"ok\": True,\n        \"has_restrictions\": has_restrictions,\n        \"status\": \"stopped\",\n        \"uptime_minutes\": 0,\n        \"max_session_minutes\": class_.max_session_minutes or 0,\n        \"restrict_hours\": class_.restrict_hours or False,\n        \"hours_start\": class_.hours_start or 0,\n        \"hours_end\": class_.hours_end or 23,\n        \"within_hours\": True,\n        \"class_name\": class_.name\n    }\n    \n    if not has_restrictions:\n        return jsonify(result)\n    \n    # Get VM status and uptime from Proxmox\n    try:\n        cluster_id = class_.deployment_cluster\n        if cluster_id:\n            proxmox = get_proxmox_admin_for_cluster(int(cluster_id))\n            node = vm.node\n            \n            if node:\n                status_data = proxmox.nodes(node).qemu(vmid).status.current.get()\n                result[\"status\"] = status_data.get(\"status\", \"stopped\")\n                \n                if result[\"status\"] == \"running\":\n                    uptime_seconds = status_data.get(\"uptime\", 0)\n                    result[\"uptime_minutes\"] = uptime_seconds / 60\n    except Exception as e:\n        pass\n    \n    # Check if within allowed hours\n    if class_.restrict_hours:\n        current_hour = datetime.now().hour\n        start = class_.hours_start or 0\n        end = class_.hours_end or 23\n        \n        if start <= end:\n            result[\"within_hours\"] = start <= current_hour < end\n        else:\n            result[\"within_hours\"] = current_hour >= start or current_hour < end\n    \n    return jsonify(result)\n\n\n@bp.route('/api/classes/<int:class_id>/vm/<int:vmid>/session-progress', methods=['GET'])
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
        (class_.max_session_minutes or 0) > 0 or
        (class_.restrict_hours or False)
    )
    
    result = {
        "ok": True,
        "has_restrictions": has_restrictions,
        "status": "stopped",
        "uptime_minutes": 0,
        "max_session_minutes": class_.max_session_minutes or 0,
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
    except Exception as e:
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
