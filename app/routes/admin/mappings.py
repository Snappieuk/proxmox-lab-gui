#!/usr/bin/env python3
"""
Admin mappings routes blueprint.

Handles the mappings.json UI for admins to assign VMs to Proxmox users.
"""

import logging

from flask import Blueprint, render_template

from app.utils.decorators import admin_required

logger = logging.getLogger(__name__)

admin_mappings_bp = Blueprint('admin_mappings', __name__, url_prefix='/admin')


@admin_mappings_bp.route("/mappings", endpoint="admin_mappings")
@admin_required
def mappings():
    """
    Render the mappings management page.
    
    This page allows admins to assign VMs to Proxmox users via mappings.json.
    The actual data is loaded via AJAX from /api/mappings endpoint.
    """
    return render_template("mappings.html")
