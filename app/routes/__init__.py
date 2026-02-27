# Routes package - contains Flask blueprints

# Import blueprints for easy access
from app.routes.admin import admin_bp
from app.routes.api.class_api import api_classes_bp
from app.routes.api.class_owners import api_class_owners_bp
from app.routes.api.class_import import class_import_bp
from app.routes.api.class_settings import bp as api_class_settings_bp
from app.routes.api.health import health_bp
from app.routes.api.class_template import api_class_template_bp
from app.routes.api.class_ip_refresh import class_ip_refresh_bp
from app.routes.api.clusters import api_clusters_bp
from app.routes.api.console import console_bp
from app.routes.api.mappings import api_mappings_bp
from app.routes.api.profile import api_profile_bp
from app.routes.api.recover_vms import recover_vms_bp
from app.routes.api.publish_template import api_publish_template_bp
from app.routes.api.rdp import api_rdp_bp
from app.routes.api.resources import resources_bp
from app.routes.api.snapshots import api_snapshots_bp
from app.routes.api.ssh import api_ssh_bp
from app.routes.api.sync import sync_bp as api_sync_bp
from app.routes.api.templates import bp as api_templates_bp
from app.routes.api.template_migrate import api_template_migrate_bp
from app.routes.api.user_vm_assignments import api_user_vm_assignments_bp
from app.routes.api.users import api_users_bp
from app.routes.api.vm_builder import vm_builder_bp
from app.routes.api.vm_class_mappings import api_vm_class_mappings_bp
from app.routes.api.vms import api_vms_bp
from app.routes.auth import auth_bp
from app.routes.classes import classes_bp
from app.routes.portal import portal_bp
from app.routes.template_builder import template_builder_bp


def register_blueprints(app):
    """Register all blueprints with the Flask app."""
    from flask import render_template, request
    
    app.register_blueprint(auth_bp)
    app.register_blueprint(portal_bp)
    app.register_blueprint(classes_bp)
    app.register_blueprint(template_builder_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(api_vms_bp)
    app.register_blueprint(api_mappings_bp)
    app.register_blueprint(recover_vms_bp)
    app.register_blueprint(api_clusters_bp)
    app.register_blueprint(api_rdp_bp)
    app.register_blueprint(api_ssh_bp)
    app.register_blueprint(api_classes_bp)
    app.register_blueprint(api_class_template_bp)
    app.register_blueprint(class_import_bp)
    app.register_blueprint(api_class_settings_bp)
    app.register_blueprint(console_bp)
    app.register_blueprint(class_ip_refresh_bp)
    app.register_blueprint(api_templates_bp)
    app.register_blueprint(api_template_migrate_bp)
    app.register_blueprint(api_sync_bp)
    app.register_blueprint(api_snapshots_bp)
    app.register_blueprint(api_vm_class_mappings_bp)
    app.register_blueprint(api_class_owners_bp)
    app.register_blueprint(api_user_vm_assignments_bp)
    app.register_blueprint(api_users_bp)
    app.register_blueprint(api_profile_bp)
    app.register_blueprint(api_publish_template_bp)
    app.register_blueprint(vm_builder_bp)
    app.register_blueprint(resources_bp)
    app.register_blueprint(health_bp)
    
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Registered vm_builder_bp with URL prefix: {vm_builder_bp.url_prefix}")
    logger.info(f"VM Builder routes: {[rule.rule for rule in app.url_map.iter_rules() if 'vm-builder' in rule.rule]}")
    
    # Register 404 handler for undefined routes
    @app.errorhandler(404)
    def page_not_found(e):
        """Handle 404 errors with styled page."""
        try:
            logger.warning(f"404 - Page not found: {request.path}")
            return render_template('error.html', 
                                 error_title="Page Not Found",
                                 error_message=f"The page '{request.path}' does not exist.",
                                 back_url="/portal"), 404
        except Exception as ex:
            logger.error(f"Error in 404 handler: {ex}")
            # Fallback to simple response
            return f"<h1>404 - Page Not Found</h1><p>The page '{request.path}' does not exist.</p><a href='/portal'>Return to Portal</a>", 404
    
    # Register 405 handler for wrong HTTP methods
    @app.errorhandler(405)
    def method_not_allowed(e):
        """Handle 405 errors (wrong HTTP method)."""
        try:
            logger.warning(f"405 - Method not allowed: {request.method} {request.path}")
            valid_methods = ', '.join(e.valid_methods) if hasattr(e, 'valid_methods') else 'Unknown'
            return render_template('error.html',
                                 error_title="Method Not Allowed",
                                 error_message=f"The {request.method} method is not allowed for '{request.path}'. Available methods: {valid_methods}",
                                 back_url="/portal"), 405
        except Exception as ex:
            logger.error(f"Error in 405 handler: {ex}")
            # Fallback to simple response
            return f"<h1>405 - Method Not Allowed</h1><p>The {request.method} method is not allowed for '{request.path}'.</p><a href='/portal'>Return to Portal</a>", 405
