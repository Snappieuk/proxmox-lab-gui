#!/usr/bin/env python3
"""
Flask Application Factory

This module provides the create_app factory function for creating
configured Flask application instances.
"""

import logging
import os

from flask import Flask, session

from app.config import CLUSTERS, SECRET_KEY

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
        
        # Initialize WebSocket routes
        from app.routes.api.ssh import init_websocket
        init_websocket(app, sock)
        logger.info("WebSocket support ENABLED (flask-sock loaded)")
    except ImportError as e:
        logger.warning(f"WebSocket support DISABLED (flask-sock not available): {e}")
    except Exception as e:
        logger.error(f"WebSocket initialization failed: {e}", exc_info=True)
    
    # Register blueprints
    from app.routes import register_blueprints
    register_blueprints(app)
    
    # Global error handler for API routes to return JSON instead of HTML
    @app.errorhandler(Exception)
    def handle_api_error(error):
        """Return JSON for API errors instead of HTML."""
        from flask import jsonify, request

        # Log all errors with the URL that caused them
        logger.error(f"Error on {request.method} {request.path}: {error}", exc_info=True)
        
        # Only apply to /api/ routes
        if request.path.startswith('/api/'):
            # Get HTTP status code if available
            status_code = getattr(error, 'code', 500)
            
            return jsonify({
                'ok': False,
                'error': str(error),
                'type': type(error).__name__
            }), status_code
        
        # For non-API routes, use default Flask error handling
        raise error
    
    # Add favicon route to prevent 404 errors
    @app.route('/favicon.ico')
    def favicon():
        """Favicon endpoint - return 204 No Content to prevent errors."""
        from flask import make_response
        response = make_response('', 204)
        response.headers['Cache-Control'] = 'public, max-age=86400'
        return response
    
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
    
    # Start background VM inventory sync
    from app.services.background_sync import start_background_sync
    start_background_sync(app)
    logger.info("Background VM inventory sync started")
    
    # Start auto-shutdown daemon
    from app.services.auto_shutdown_service import start_auto_shutdown_daemon
    start_auto_shutdown_daemon(app)
    logger.info("Auto-shutdown daemon started")
    
    # Register cleanup handler for SSH connection pool
    import atexit
    from app.services.ssh_executor import _connection_pool
    
    def cleanup_ssh_pool():
        """Close all SSH connections on app shutdown."""
        logger.info("Closing SSH connection pool...")
        _connection_pool.close_all()
    
    atexit.register(cleanup_ssh_pool)
    logger.info("SSH connection pool cleanup handler registered")
    
    # Start background template sync (database-first architecture)
    from app.services.template_sync import start_template_sync_daemon
    start_template_sync_daemon(app)
    logger.info("Background template sync started")
    
    # Ensure templates are replicated across all nodes at startup (disabled by default)
    from app.config import ENABLE_TEMPLATE_REPLICATION
    if ENABLE_TEMPLATE_REPLICATION:
        def _replicate_templates_startup():
            import time
            time.sleep(2)
            try:
                with app.app_context():
                    from app.services.proxmox_operations import (
                        replicate_templates_to_all_nodes,
                    )
                    logger.info("Starting template replication check across nodes...")
                    replicate_templates_to_all_nodes()
                    logger.info("Template replication check completed")
            except Exception as e:
                logger.warning(f"Template replication startup failed: {e}")

        import threading
        threading.Thread(target=_replicate_templates_startup, daemon=True).start()
    else:
        logger.info("Template replication at startup disabled")
    
    # Depl0y integration removed
    
    logger.info("Flask app created successfully")
    return app
