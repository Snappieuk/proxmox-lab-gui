#!/usr/bin/env python3
"""
Portal routes blueprint.

Main dashboard and portal views.
"""

import logging

from flask import Blueprint, jsonify, redirect, render_template, url_for

from app.utils.decorators import current_user, login_required

logger = logging.getLogger(__name__)

portal_bp = Blueprint('portal', __name__)


@portal_bp.route("/")
def root():
    """Root redirect to portal or login."""
    if current_user():
        return redirect(url_for("portal.portal"))
    return redirect(url_for("auth.login"))


@portal_bp.route("/portal")
@login_required
def portal():
    """Main portal view."""
    from app.services.user_manager import require_user
    require_user()
    
    # Fast path: render empty page immediately for progressive loading
    # VMs will be loaded via /api/vms after page renders
    return render_template(
        "index_improved.html",
        progressive_load=True,
        windows_vms=[],
        linux_vms=[],
        lxc_containers=[],
        other_vms=[],
        message=None,
    )


@portal_bp.route("/health")
def health():
    """Health check endpoint."""
    return jsonify({"ok": True})
