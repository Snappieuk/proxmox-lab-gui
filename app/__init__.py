#!/usr/bin/env python3
"""
Flask Application Factory

This module provides the create_app factory function for creating
configured Flask application instances.
"""

import logging
import os

from flask import Flask, session

from app.config import SECRET_KEY, CLUSTERS

# Initialize logging
from app.utils.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)


def create_app(config=None):
    """Create and configure the Flask application.
    
    Args:
        config: Optional configuration dictionary to override defaults
        
    Returns:
        Configured Flask application instance
    """
    # Create Flask app with template/static from app directory
    template_folder = os.path.join(os.path.dirname(__file__), 'templates')
    static_folder = os.path.join(os.path.dirname(__file__), 'static')
    
    app = Flask(__name__, 
                template_folder=template_folder,
                static_folder=static_folder)
    
    # Configure secret key
    app.secret_key = SECRET_KEY
    
    # Apply any additional config
    if config:
        app.config.update(config)
    
    # Configure logging
    logging.basicConfig(level=logging.INFO)
    
    # Initialize database (SQLAlchemy)
    from app.models import init_db
    init_db(app)
    logger.info("Database initialized")
    
    # Initialize WebSocket support
    try:
        from flask_sock import Sock
        sock = Sock(app)
        logger.info("WebSocket support ENABLED (flask-sock loaded)")
        
        # Initialize WebSocket routes
        from app.routes.api.ssh import init_websocket
        init_websocket(app, sock)
    except ImportError:
        logger.warning("WebSocket support DISABLED (flask-sock not available)")
    
    # Register blueprints
    from app.routes import register_blueprints
    register_blueprints(app)
    
    # Register context processor
    @app.context_processor
    def inject_admin_flag():
        """Inject admin flag and cluster info into all templates."""
        from app.services.user_manager import is_admin_user
        
        user = session.get("user")
        is_admin = is_admin_user(user) if user else False
        
        # Log admin status for debugging
        if user:
            app.logger.debug("inject_admin_flag: user=%s is_admin=%s", user, is_admin)
        
        # Inject cluster info for dropdown
        current_cluster = session.get("cluster_id", CLUSTERS[0]["id"])
        clusters = [{"id": c["id"], "name": c["name"]} for c in CLUSTERS]
        
        # Check if user has local account with role info
        local_user = None
        local_user_role = None
        if user:
            try:
                from app.services.class_service import get_user_by_username
                username = user.split('@')[0] if '@' in user else user
                local_user = get_user_by_username(username)
                if local_user:
                    local_user_role = local_user.role
            except Exception:
                pass
        
        return {
            "is_admin": is_admin,
            "clusters": clusters,
            "current_cluster": current_cluster,
            "local_user": local_user,
            "local_user_role": local_user_role,
        }
    
    # Start background IP scanner
    from app.services.proxmox_client import start_background_ip_scanner
    start_background_ip_scanner()
    
    logger.info("Flask app created successfully")
    return app
