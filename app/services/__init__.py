# Services package - re-exports for convenient imports
# noqa: F401 comments indicate these are intentional re-exports
# DELETED: cluster_manager imports (duplicate of proxmox_service)
from app.services.proxmox_service import (
    get_current_cluster as get_current_cluster,  # noqa: F401
    get_current_cluster_id as get_current_cluster_id,  # noqa: F401
    get_proxmox_admin as get_proxmox_admin,  # noqa: F401
    get_proxmox_admin_for_cluster as get_proxmox_admin_for_cluster,  # noqa: F401
    switch_cluster as switch_cluster,  # noqa: F401
)
from app.services.user_manager import (
    authenticate_proxmox_user as authenticate_proxmox_user,  # noqa: F401
    create_pve_user as create_pve_user,  # noqa: F401
    get_pve_users as get_pve_users,  # noqa: F401
    is_admin_user as is_admin_user,  # noqa: F401
    require_user as require_user,  # noqa: F401
)
