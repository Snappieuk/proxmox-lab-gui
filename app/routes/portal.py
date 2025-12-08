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


@portal_bp.route("/profile")
@login_required
def profile():
    """User profile and settings page."""
    from app.utils.auth_helpers import get_current_user
    from app.services.user_manager import is_admin_user
    
    user = get_current_user()
    if not user:
        return redirect(url_for("auth.login"))
    
    # Check if this is a Proxmox user (has @pve or @pam suffix or is in admin list)
    from flask import session
    session_user = session.get("user", "")
    is_proxmox_user = "@" in session_user or is_admin_user(session_user)
    
    return render_template(
        "profile.html",
        user=user,
        is_proxmox_user=is_proxmox_user
    )
