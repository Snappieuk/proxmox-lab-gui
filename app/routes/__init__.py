# Routes package - contains Flask blueprints

# Import blueprints for easy access
from app.routes.auth import auth_bp
from app.routes.portal import portal_bp
from app.routes.classes import classes_bp
from app.routes.admin.mappings import admin_mappings_bp
from app.routes.admin.clusters import admin_clusters_bp
from app.routes.admin.diagnostics import admin_diagnostics_bp
from app.routes.admin.users import admin_users_bp
from app.routes.admin.vm_class_mappings import admin_vm_mappings_bp
from app.routes.api.vms import api_vms_bp
from app.routes.api.mappings import api_mappings_bp
from app.routes.api.clusters import api_clusters_bp
from app.routes.api.rdp import api_rdp_bp
from app.routes.api.ssh import api_ssh_bp
from app.routes.api.class_api import api_classes_bp
from app.routes.api.class_template import api_class_template_bp
from app.routes.api.templates import bp as api_templates_bp
from app.routes.api.sync import sync_bp as api_sync_bp
from app.routes.api.vm_class_mappings import api_vm_class_mappings_bp
from app.routes.api.class_owners import api_class_owners_bp
from app.routes.api.user_vm_assignments import api_user_vm_assignments_bp
from app.routes.api.publish_template import api_publish_template_bp


def register_blueprints(app):
    """Register all blueprints with the Flask app."""
    app.register_blueprint(auth_bp)
    app.register_blueprint(portal_bp)
    app.register_blueprint(classes_bp)
    app.register_blueprint(admin_mappings_bp)
    app.register_blueprint(admin_clusters_bp)
    app.register_blueprint(admin_diagnostics_bp)
    app.register_blueprint(admin_users_bp)
    app.register_blueprint(admin_vm_mappings_bp)
    app.register_blueprint(api_vms_bp)
    app.register_blueprint(api_mappings_bp)
    app.register_blueprint(api_clusters_bp)
    app.register_blueprint(api_rdp_bp)
    app.register_blueprint(api_ssh_bp)
    app.register_blueprint(api_classes_bp)
    app.register_blueprint(api_class_template_bp)
    app.register_blueprint(api_templates_bp)
    app.register_blueprint(api_sync_bp)
    app.register_blueprint(api_vm_class_mappings_bp)
    app.register_blueprint(api_class_owners_bp)
    app.register_blueprint(api_user_vm_assignments_bp)
    app.register_blueprint(api_publish_template_bp)
