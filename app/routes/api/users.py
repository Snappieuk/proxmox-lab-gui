#!/usr/bin/env python3
"""
Users API routes blueprint.

Handles user listing and management endpoints.
"""

import logging

from flask import Blueprint, jsonify

from app.models import User
from app.utils.decorators import login_required
from app.utils.auth_helpers import get_current_user

logger = logging.getLogger(__name__)

api_users_bp = Blueprint('api_users', __name__, url_prefix='/api/users')


@api_users_bp.route("/teachers", methods=["GET"])
@login_required
def list_teachers():
    """Get list of all teachers and admins.
    
    Used for co-owner selection and other teacher-related features.
    Only accessible by teachers and admins.
    """
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    
    if not user.is_teacher and not user.is_adminer:
        return jsonify({"ok": False, "error": "Teacher or admin privileges required"}), 403
    
    # Get all users with teacher or adminer role
    teachers = User.query.filter(
        (User.role == 'teacher') | (User.role == 'adminer')
    ).order_by(User.username).all()
    
    teachers_list = [
        {
            "id": t.id,
            "username": t.username,
            "role": t.role
        }
        for t in teachers
    ]
    
    return jsonify({
        "ok": True,
        "teachers": teachers_list
    })
