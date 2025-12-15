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
    
    # Check if initial setup is needed
    clusters = get_clusters_from_db()
    if not clusters:
        return redirect(url_for("auth.setup"))
    
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
            # Debug: Verify Flask app has secret_key configured
            from flask import current_app
            if not current_app.secret_key:
                logger.error("CRITICAL: Flask app.secret_key is None at login!")
                error = "Server configuration error - please contact administrator"
            else:
                try:
                    session["user"] = full_user
                    # Initialize cluster selection (default to first cluster if available)
                    if "cluster_id" not in session:
                        clusters = get_clusters_from_db()
                        if clusters:
                            session["cluster_id"] = clusters[0]["id"]
                        else:
                            logger.warning("No clusters configured in database")
                    logger.info("user logged in: %s", full_user)
                    next_url = request.args.get("next") or url_for("portal.portal")
                    return redirect(next_url)
                except RuntimeError as e:
                    logger.error(f"Session error during login: {e}", exc_info=True)
                    error = f"Login failed: {str(e)}"

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


@auth_bp.route("/setup", methods=["GET", "POST"])
def setup():
    """First-time setup page - add initial cluster configuration."""
    from app.models import db, Cluster
    
    # Check if clusters already exist
    clusters = get_clusters_from_db()
    if clusters:
        # Already configured, redirect to login
        return redirect(url_for("auth.login"))
    
    error = None
    success = None
    
    if request.method == "POST":
        try:
            cluster_id = request.form.get("cluster_id", "").strip()
            name = request.form.get("name", "").strip()
            host = request.form.get("host", "").strip()
            port = request.form.get("port", "8006")
            user = request.form.get("user", "").strip()
            password = request.form.get("password", "")
            verify_ssl = request.form.get("verify_ssl") == "on"
            
            # Optional settings
            qcow2_template_path = request.form.get("qcow2_template_path", "/mnt/pve/templates").strip()
            qcow2_images_path = request.form.get("qcow2_images_path", "/mnt/pve/images").strip()
            admin_group = request.form.get("admin_group", "adminers").strip()
            
            # Validate required fields
            if not all([cluster_id, name, host, user, password]):
                error = "Please fill in all required fields."
            else:
                # Create cluster
                cluster = Cluster(
                    cluster_id=cluster_id,
                    name=name,
                    host=host,
                    port=int(port),
                    user=user,
                    password=password,
                    verify_ssl=verify_ssl,
                    is_default=True,  # First cluster is always default
                    is_active=True,
                    qcow2_template_path=qcow2_template_path,
                    qcow2_images_path=qcow2_images_path,
                    admin_group=admin_group,
                    enable_ip_lookup=True,
                    enable_ip_persistence=False,
                    vm_cache_ttl=300,
                )
                
                db.session.add(cluster)
                db.session.commit()
                
                logger.info(f"Initial cluster setup completed: {name}")
                success = f"Cluster '{name}' configured successfully! You can now login with your Proxmox credentials."
                
        except Exception as e:
            db.session.rollback()
            logger.error(f"Setup failed: {e}", exc_info=True)
            error = f"Failed to save cluster configuration: {str(e)}"
    
    return render_template("setup.html", error=error, success=success)


@auth_bp.route("/logout")
def logout():
    """Handle user logout."""
    
    # Note: We can't use the decorator here since we need to redirect to login
    # But we should still check if logged in
    if session.get("user"):
        session.pop("user", None)
    return redirect(url_for("auth.login"))
