#!/usr/bin/env python3
"""
Admin route for VM:Class mappings.

Allows admins/teachers to manually add existing VMs to classes without auto-assignment.
"""

import logging
from flask import Blueprint, render_template, session

from app.utils.decorators import login_required
from app.services.user_manager import is_admin_user

logger = logging.getLogger(__name__)

admin_vm_mappings_bp = Blueprint('admin_vm_mappings', __name__, url_prefix='/admin')


@admin_vm_mappings_bp.route("/vm-class-mappings")
@login_required
def vm_class_mappings():
    """Admin page for managing VM to Class mappings."""
    user = session.get("user")
    if not is_admin_user(user):
        from flask import abort
        abort(403)
    
    return render_template("admin/vm_class_mappings.html")
