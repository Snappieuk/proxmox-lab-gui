# API routes package - exports for package consumers
# noqa: F401 comments are required because these are re-exports for external use
from app.routes.api.class_api import api_classes_bp as api_classes_bp  # noqa: F401
from app.routes.api.clusters import api_clusters_bp as api_clusters_bp  # noqa: F401
from app.routes.api.console import console_bp as console_bp  # noqa: F401
from app.routes.api.mappings import api_mappings_bp as api_mappings_bp  # noqa: F401
from app.routes.api.profile import api_profile_bp as api_profile_bp  # noqa: F401
from app.routes.api.rdp import api_rdp_bp as api_rdp_bp  # noqa: F401
from app.routes.api.ssh import api_ssh_bp as api_ssh_bp  # noqa: F401
from app.routes.api.users import api_users_bp as api_users_bp  # noqa: F401
from app.routes.api.vms import api_vms_bp as api_vms_bp  # noqa: F401
