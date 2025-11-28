#!/usr/bin/env python3
"""
Authentication routes blueprint.

Handles login, logout, and user registration.
"""

import logging

from flask import Blueprint, render_template, request, redirect, url_for, session

# Import path setup (adds rdp-gen to sys.path)
import app.utils.paths

from config import CLUSTERS

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Handle user login."""
    from app.services.user_manager import authenticate_proxmox_user
    from app.utils.decorators import current_user
    
    if current_user():
        # Already logged in — redirect to portal
        return redirect(url_for("portal.portal"))

    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        full_user = authenticate_proxmox_user(username, password)
        if not full_user:
            error = "Invalid username or password."
        else:
            session["user"] = full_user
            # Initialize cluster selection (default to first cluster)
            if "cluster_id" not in session:
                session["cluster_id"] = CLUSTERS[0]["id"]
            logger.info("user logged in: %s", full_user)
            next_url = request.args.get("next") or url_for("portal.portal")
            return redirect(next_url)

    return render_template("login.html", error=error)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    """Handle user registration."""
    from app.services.user_manager import create_pve_user
    from app.utils.decorators import current_user
    
    if current_user():
        # Already logged in — redirect to portal
        return redirect(url_for("portal.portal"))

    error = None
    success = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")

        if password != password_confirm:
            error = "Passwords do not match."
        else:
            success_flag, error_msg = create_pve_user(username, password)
            if success_flag:
                success = f"Account created successfully! You can now sign in as {username}@pve"
                logger.info("New user registered: %s@pve", username)
            else:
                error = error_msg

    return render_template("register.html", error=error, success=success)


@auth_bp.route("/logout")
def logout():
    """Handle user logout."""
    from app.utils.decorators import login_required
    
    # Note: We can't use the decorator here since we need to redirect to login
    # But we should still check if logged in
    if session.get("user"):
        session.pop("user", None)
    return redirect(url_for("auth.login"))
