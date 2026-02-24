#!/usr/bin/env python3
"""
Profile API routes blueprint.

Handles user profile operations like password changes.
"""

import logging

from flask import Blueprint, jsonify, request, session

from app.models import db
from app.utils.decorators import login_required

logger = logging.getLogger(__name__)

api_profile_bp = Blueprint('api_profile', __name__, url_prefix='/api/profile')


@api_profile_bp.route("/change-password", methods=["POST"])
@login_required
def change_password():
    """Change the current user's password.
    
    Body: {
        "current_password": "old_pass",
        "new_password": "new_pass"
    }
    
    Only works for local database users, not Proxmox users.
    """
    from app.utils.auth_helpers import get_current_user
    from app.services.user_manager import is_admin_user
    
    # Get session user
    session_user = session.get("user")
    if not session_user:
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    
    # Check if this is a Proxmox user (cannot change password here)
    if "@" in session_user or is_admin_user(session_user):
        return jsonify({
            "ok": False, 
            "error": "Proxmox users cannot change passwords through this interface"
        }), 403
    
    # Get local user from database
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "User not found"}), 404
    
    data = request.get_json() or {}
    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")
    
    if not current_password or not new_password:
        return jsonify({"ok": False, "error": "Current and new passwords are required"}), 400
    
    if len(new_password) < 6:
        return jsonify({"ok": False, "error": "New password must be at least 6 characters"}), 400
    
    # Verify current password
    if not user.check_password(current_password):
        return jsonify({"ok": False, "error": "Current password is incorrect"}), 401
    
    # Set new password
    try:
        user.set_password(new_password)
        db.session.commit()
        logger.info(f"User {user.username} changed their password")
        
        return jsonify({
            "ok": True,
            "message": "Password changed successfully"
        })
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Failed to change password for user {user.username}: {e}")
        return jsonify({"ok": False, "error": f"Failed to change password: {str(e)}"}), 500
