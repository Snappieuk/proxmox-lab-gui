#!/usr/bin/env python3
"""
Template Builder routes blueprint.

Allows teachers to create and manage VM templates for their classes.
"""

import logging

from flask import Blueprint, jsonify, render_template, session

from app.utils.decorators import login_required

logger = logging.getLogger(__name__)

template_builder_bp = Blueprint('template_builder', __name__, url_prefix='/template-builder')


@template_builder_bp.route("")
@login_required
def template_builder_page():
    """Template builder page - teachers and admins only."""
    from app.services.user_manager import require_user, is_admin_user
    from app.services.class_service import get_user_by_username
    
    user = require_user()
    
    # Check if user is teacher or admin
    is_admin = is_admin_user(user)
    
    # Check local user role
    username = user.split('@')[0] if '@' in user else user
    local_user = get_user_by_username(username)
    
    if not is_admin and (not local_user or local_user.role not in ['teacher', 'adminer']):
        return render_template(
            "error.html",
            message="Access denied. Template Builder is only accessible to teachers and administrators."
        ), 403
    
    return render_template(
        "template_builder/index.html",
        user=user,
        is_admin=is_admin,
        local_user=local_user
    )
