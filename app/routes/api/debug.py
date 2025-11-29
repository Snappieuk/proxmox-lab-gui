#!/usr/bin/env python3
"""
Debug API endpoint for troubleshooting admin detection.
"""

import logging
from flask import Blueprint, jsonify, session

from app.utils.decorators import login_required
from app.services.proxmox_client import is_admin_user, _user_in_group, _get_admin_group_members_cached
from app.config import ADMIN_USERS, ADMIN_GROUP

logger = logging.getLogger(__name__)

debug_bp = Blueprint('api_debug', __name__, url_prefix='/api/debug')


@debug_bp.route('/admin-status', methods=['GET'])
@login_required
def admin_status():
    """
    Returns detailed admin detection information for current user.
    """
    user = session.get('user')
    if not user:
        return jsonify({"error": "Not logged in"}), 401
    
    # Get admin status
    is_admin = is_admin_user(user)
    
    # Check group membership
    in_admin_group = False
    group_members = []
    if ADMIN_GROUP:
        try:
            group_members = _get_admin_group_members_cached()
            in_admin_group = _user_in_group(user, ADMIN_GROUP)
        except Exception as e:
            logger.error(f"Failed to check admin group: {e}")
    
    # Check admin users list
    in_admin_list = user in ADMIN_USERS
    
    # Generate user variants for debugging
    variants = {user}
    if "@" in user:
        name, realm = user.split("@", 1)
        variants.add(f"{name}@pam")
        variants.add(f"{name}@pve")
    else:
        variants.add(f"{user}@pve")
        variants.add(f"{user}@pam")
    
    return jsonify({
        "current_user": user,
        "is_admin": is_admin,
        "checks": {
            "in_admin_users_list": in_admin_list,
            "in_admin_group": in_admin_group,
        },
        "config": {
            "admin_users": ADMIN_USERS,
            "admin_group": ADMIN_GROUP,
        },
        "group_data": {
            "members": group_members,
            "user_variants_checked": list(variants),
        }
    })
