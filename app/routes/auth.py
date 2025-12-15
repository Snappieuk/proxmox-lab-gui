#!/usr/bin/env python3
"""
Authentication routes blueprint.

Handles login, logout, and user registration.
"""

import logging

from flask import Blueprint, redirect, render_template, request, session, url_for

from app.services.proxmox_service import get_clusters_from_db

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

        # Try Proxmox authentication first (returns full user@realm format)
        full_user = authenticate_proxmox_user(username, password)
        
        if full_user:
            # Proxmox user authenticated - full_user already has realm suffix
            logger.info("Proxmox user logged in: %s", full_user)
        else:
            # If Proxmox auth fails, try local account authentication
            from app.services.class_service import authenticate_local_user
            local_user = authenticate_local_user(username, password)
            if local_user:
                # Local user authenticated - use their username without realm suffix
                full_user = username
                logger.info("Local user logged in: %s", username)
            else:
                error = "Invalid username or password."
        
        if full_user:
            session["user"] = full_user
            # Initialize cluster selection (default to first cluster)
            if "cluster_id" not in session:
                session["cluster_id"] = get_clusters_from_db()[0]["id"]
            logger.info("user logged in: %s", full_user)
            next_url = request.args.get("next") or url_for("portal.portal")
            return redirect(next_url)

    return render_template("login.html", error=error)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    """Handle user registration - creates LOCAL database accounts only."""
    from app.services.class_service import create_local_user
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
            # Create local database user (NOT Proxmox user)
            user, error_msg = create_local_user(username, password, role='user')
            if user:
                success = f"Account created successfully! You can now sign in as {username}"
                logger.info("New local user registered: %s", username)
            else:
                error = error_msg or "Failed to create account"

    return render_template("register.html", error=error, success=success)


@auth_bp.route("/logout")
def logout():
    """Handle user logout."""
    
    # Note: We can't use the decorator here since we need to redirect to login
    # But we should still check if logged in
    if session.get("user"):
        session.pop("user", None)
    return redirect(url_for("auth.login"))
