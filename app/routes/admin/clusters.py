#!/usr/bin/env python3
"""
Admin Clusters routes blueprint.

Handles cluster configuration and settings.
"""

import logging

from flask import Blueprint, render_template

from app.utils.decorators import admin_required

logger = logging.getLogger(__name__)

admin_clusters_bp = Blueprint('admin_clusters', __name__, url_prefix='/admin')


@admin_clusters_bp.route("/settings")
@admin_required
def admin_settings():
    """Settings page for cluster configuration."""
    return render_template("settings.html")
