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
        
        # Get Depl0y URL if available
        depl0y_url = app.config.get('DEPL0Y_URL')
        
        return {
            "is_admin": is_admin,
            "clusters": clusters,
            "current_cluster": current_cluster,
            "local_user": local_user,
            "local_user_role": local_user_role,
            "depl0y_url": depl0y_url,
        }
    
    # Start background IP scanner
    from app.services.proxmox_client import start_background_ip_scanner
    start_background_ip_scanner()
    
    # Start background VM inventory sync
    from app.services.background_sync import start_background_sync
    start_background_sync(app)
    logger.info("Background VM inventory sync started")
    
    # Ensure templates are replicated across all nodes at startup (disabled by default)
    from app.config import ENABLE_TEMPLATE_REPLICATION
    if ENABLE_TEMPLATE_REPLICATION:
        def _replicate_templates_startup():
            import time
            time.sleep(2)
            try:
                with app.app_context():
                    from app.services.proxmox_operations import replicate_templates_to_all_nodes
                    logger.info("Starting template replication check across nodes...")
                    replicate_templates_to_all_nodes()
                    logger.info("Template replication check completed")
            except Exception as e:
                logger.warning(f"Template replication startup failed: {e}")

        import threading
        threading.Thread(target=_replicate_templates_startup, daemon=True).start()
    else:
        logger.info("Template replication at startup disabled")
    
    # Initialize Depl0y companion service (background)
    from app.config import ENABLE_DEPL0Y, DEPL0Y_URL as CUSTOM_DEPL0Y_URL
    
    if CUSTOM_DEPL0Y_URL:
        # Use manually configured Depl0y URL
        app.config['DEPL0Y_URL'] = CUSTOM_DEPL0Y_URL
        logger.info(f"Depl0y configured at {CUSTOM_DEPL0Y_URL}")
    elif ENABLE_DEPL0Y:
        # Auto-install and start Depl0y
        try:
            from app.services.depl0y_manager import initialize_depl0y, get_depl0y_url, _detect_host_ip, DEPL0Y_PORT
            # Start in background; don't set placeholder URL
            initialize_depl0y(background=True)
            # Poll for a short time to get actual URL
            import time
            depl_url = None
            for _ in range(12):
                url = get_depl0y_url()
                if url:
                    depl_url = url
                    break
                time.sleep(1)
            
            # If still not available, use the expected URL (service may still be starting)
            if not depl_url:
                host_ip = _detect_host_ip()
                if DEPL0Y_PORT == 80:
                    depl_url = f"http://{host_ip}"
                else:
                    depl_url = f"http://{host_ip}:{DEPL0Y_PORT}"
                logger.info(f"Depl0y starting in background, using expected URL: {depl_url}")
            
            app.config['DEPL0Y_URL'] = depl_url
            if depl_url:
                logger.info(f"Depl0y service at {depl_url}")
        except Exception as e:
            logger.warning(f"Depl0y initialization failed: {e}")
            app.config['DEPL0Y_URL'] = None
    else:
        app.config['DEPL0Y_URL'] = None
        logger.info("Depl0y integration disabled")
    
    logger.info("Flask app created successfully")
    return app
