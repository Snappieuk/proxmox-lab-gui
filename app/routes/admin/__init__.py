# Admin routes package - exports for package consumers
# noqa: F401 comments are required because these are re-exports for external use
from app.routes.admin.clusters import admin_clusters_bp as admin_clusters_bp  # noqa: F401
from app.routes.admin.diagnostics import admin_diagnostics_bp as admin_diagnostics_bp  # noqa: F401
from app.routes.admin.mappings import admin_mappings_bp as admin_mappings_bp  # noqa: F401
from app.routes.admin.users import admin_users_bp as admin_users_bp  # noqa: F401
from app.routes.admin.vm_class_mappings import admin_vm_mappings_bp as admin_vm_mappings_bp  # noqa: F401
