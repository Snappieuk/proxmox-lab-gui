#!/usr/bin/env python3
"""
API endpoints for managing class co-owners.
"""

import logging

from flask import Blueprint, jsonify, request, session

from app.models import User, db
from app.services.class_service import (
    get_class_by_id,
    get_user_by_id,
    get_user_by_username,
)
from app.utils.decorators import login_required

logger = logging.getLogger(__name__)

api_class_owners_bp = Blueprint('api_class_owners', __name__, url_prefix='/api')


@api_class_owners_bp.route("/classes/<int:class_id>/co-owners", methods=["GET"])
@login_required
def get_class_co_owners(class_id: int):
    """Get list of co-owners for a class."""
    username = session.get('user')
    if not username:
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    # Remove @pve suffix if present
    if '@' in username:
        username = username.split('@')[0]
    user = get_user_by_username(username)
    if not user:
        return jsonify({"ok": False, "error": "User not found"}), 401
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check permissions (must be owner or admin)
    if not user.is_adminer and not class_.is_owner(user):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    co_owners = [
        {
            'id': co.id,
            'username': co.username,
            'role': co.role
        }
        for co in class_.co_owners.all()
    ]
    
    return jsonify({
        "ok": True,
        "co_owners": co_owners,
        "primary_teacher": {
            'id': class_.teacher.id,
            'username': class_.teacher.username
        } if class_.teacher else None
    })


@api_class_owners_bp.route("/classes/<int:class_id>/co-owners", methods=["POST"])
@login_required
def add_class_co_owner(class_id: int):
    """Add a co-owner to a class."""
    username = session.get('user')
    if not username:
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    # Remove @pve suffix if present
    if '@' in username:
        username = username.split('@')[0]
    user = get_user_by_username(username)
    if not user:
        return jsonify({"ok": False, "error": "User not found"}), 401
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check permissions (must be primary teacher or admin)
    if not user.is_adminer and class_.teacher_id != user.id:
        return jsonify({"ok": False, "error": "Only the primary teacher or admin can add co-owners"}), 403
    
    data = request.get_json()
    username = data.get('username')
    user_id = data.get('user_id')
    
    # Find user by username or ID
    co_owner = None
    if user_id:
        co_owner = get_user_by_id(user_id)
    elif username:
        co_owner = get_user_by_username(username)
    
    if not co_owner:
        return jsonify({"ok": False, "error": "User not found"}), 404
    
    # Check if already primary teacher
    if co_owner.id == class_.teacher_id:
        return jsonify({"ok": False, "error": "User is already the primary teacher"}), 400
    
    # Check if already a co-owner
    if co_owner in class_.co_owners.all():
        return jsonify({"ok": False, "error": "User is already a co-owner"}), 409
    
    # Add co-owner
    try:
        class_.co_owners.append(co_owner)
        db.session.commit()
        logger.info(f"Added {co_owner.username} as co-owner to class {class_.name}")
        
        return jsonify({
            "ok": True,
            "message": f"{co_owner.username} added as co-owner",
            "co_owner": {
                'id': co_owner.id,
                'username': co_owner.username,
                'role': co_owner.role
            }
        })
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Failed to add co-owner: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_class_owners_bp.route("/classes/<int:class_id>/co-owners/<int:user_id>", methods=["DELETE"])
@login_required
def remove_class_co_owner(class_id: int, user_id: int):
    """Remove a co-owner from a class."""
    username = session.get('user')
    if not username:
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    # Remove @pve suffix if present
    if '@' in username:
        username = username.split('@')[0]
    user = get_user_by_username(username)
    if not user:
        return jsonify({"ok": False, "error": "User not found"}), 401
    
    class_ = get_class_by_id(class_id)
    if not class_:
        return jsonify({"ok": False, "error": "Class not found"}), 404
    
    # Check permissions (must be primary teacher or admin)
    if not user.is_adminer and class_.teacher_id != user.id:
        return jsonify({"ok": False, "error": "Only the primary teacher or admin can remove co-owners"}), 403
    
    co_owner = get_user_by_id(user_id)
    if not co_owner:
        return jsonify({"ok": False, "error": "User not found"}), 404
    
    # Check if is a co-owner
    if co_owner not in class_.co_owners.all():
        return jsonify({"ok": False, "error": "User is not a co-owner of this class"}), 400
    
    # Remove co-owner
    try:
        class_.co_owners.remove(co_owner)
        db.session.commit()
        logger.info(f"Removed {co_owner.username} as co-owner from class {class_.name}")
        
        return jsonify({
            "ok": True,
            "message": f"{co_owner.username} removed as co-owner"
        })
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Failed to remove co-owner: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_class_owners_bp.route("/users/teachers", methods=["GET"])
@login_required
def get_teachers_list():
    """Get list of all users who can be teachers (teachers and adminers)."""
    username = session.get('user')
    if not username:
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    # Remove @pve suffix if present
    if '@' in username:
        username = username.split('@')[0]
    user = get_user_by_username(username)
    if not user:
        return jsonify({"ok": False, "error": "User not found"}), 401
    
    # Get all users with teacher or adminer role
    teachers = User.query.filter(
        (User.role == 'teacher') | (User.role == 'adminer')
    ).order_by(User.username).all()
    
    teachers_list = [
        {
            'id': t.id,
            'username': t.username,
            'role': t.role
        }
        for t in teachers
    ]
    
    return jsonify({
        "ok": True,
        "teachers": teachers_list
    })
