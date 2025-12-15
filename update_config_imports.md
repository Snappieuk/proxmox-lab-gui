# Config.py to Settings Service Migration Guide

## Summary
We're migrating from hardcoded `app/config.py` to database-first architecture using `app/services/settings_service.py`.

## Completed Updates
✅ `app/config.py` - Replaced with minimal version (SECRET_KEY + cache TTLs only)
✅ `app/services/settings_service.py` - Created with all getter functions
✅ `app/services/proxmox_service.py` - Updated to use `get_clusters_from_db()` and `settings_service`
✅ `app/services/proxmox_client.py` - Updated to remove most config imports
✅ `app/__init__.py` - Partially updated (line 14 done, line 193 pending)

## Remaining Updates Needed

### Critical Files (Must Update Before Running)

1. **app/services/user_manager.py** (16 lines) - Uses ADMIN_GROUP, ADMIN_USERS, CLUSTERS
   - Line 16: Remove `from app.config import ADMIN_GROUP, ADMIN_USERS, CLUSTERS`
   - Update `_get_admin_group_members_cached()` to accept admin_group parameter
   - Update `is_admin_user()` to call `get_all_admin_groups()` from settings_service
   - Update all ADMIN_GROUP references to use parameter from settings

2. **app/__init__.py** (line 193) - Uses ENABLE_TEMPLATE_REPLICATION
   - Replace `from app.config import ENABLE_TEMPLATE_REPLICATION` with:
     ```python
     from app.models import SystemSettings
     enable_template_replication = SystemSettings.get('enable_template_replication', 'false') == 'true'
     if enable_template_replication:
     ```

### API Routes (8 files)

Replace `from app.config import CLUSTERS` with `from app.services.proxmox_service import get_clusters_from_db`
Replace `CLUSTERS` usage with `get_clusters_from_db()` calls

- `app/routes/api/vms.py` (3 locations: lines 14, 91, 276)
- `app/routes/api/class_api.py` (4 locations: lines 166, 427, 633, 649)
- `app/routes/api/class_template.py` (7 locations: lines 95, 170, 218, 303, 370, 443, 526)
  - Line 218 also imports ARP_SUBNETS - use `get_arp_subnets(cluster_dict)` from settings_service
- `app/routes/api/class_ip_refresh.py` (2 locations: lines 59, 137)
  - Line 137 uses ARP_SUBNETS - use `get_arp_subnets(cluster_dict)`
- `app/routes/api/templates.py` (line 8)
- `app/routes/api/template_migrate.py` (line 115)
- `app/routes/api/clusters.py` (line 13) - Can probably be removed entirely
- `app/routes/api/vnc_proxy.py` (line 14)

### Service Files (7 files)

- `app/services/class_vm_service.py` (4 locations: lines 344, 1102, 1389, 1633)
- `app/services/class_service.py` (2 locations: lines 540, 867)
- `app/services/proxmox_operations.py` (2 locations: lines 29, 1730)
  - Line 1730 uses ARP_SUBNETS
- `app/services/vm_deployment_service.py` (line 21)
- `app/services/template_sync.py` (line 13)
- `app/services/cluster_manager.py` (line 13) - Uses CLUSTER_CONFIG_FILE, CLUSTERS
- `app/services/mappings_service.py` (line 91) - Uses CLUSTERS, VALID_NODES

### Other Files

- `app/routes/auth.py` (line 12)
- `tests/test_cached_vms.py` (line 158) - Uses PROXMOX_CACHE_TTL

## Migration Pattern

### For CLUSTERS imports:
```python
# OLD:
from app.config import CLUSTERS
cluster = next((c for c in CLUSTERS if c['id'] == cluster_id), None)

# NEW:
from app.services.proxmox_service import get_clusters_from_db
clusters = get_clusters_from_db()
cluster = next((c for c in clusters if c['id'] == cluster_id), None)
```

### For per-cluster settings:
```python
# OLD:
from app.config import ARP_SUBNETS, VM_CACHE_TTL

# NEW:
from app.services.proxmox_service import get_clusters_from_db
from app.services.settings_service import get_arp_subnets, get_vm_cache_ttl

clusters = get_clusters_from_db()
cluster_dict = next((c for c in clusters if c.get("id") == cluster_id), {})
arp_subnets = get_arp_subnets(cluster_dict)
cache_ttl = get_vm_cache_ttl(cluster_dict)
```

### For admin settings:
```python
# OLD:
from app.config import ADMIN_USERS, ADMIN_GROUP

# NEW:
from app.services.settings_service import get_all_admin_users, get_all_admin_groups

admin_users = get_all_admin_users()  # Aggregated from all clusters
admin_groups = get_all_admin_groups()
```

### For SystemSettings:
```python
# OLD:
from app.config import ENABLE_TEMPLATE_REPLICATION

# NEW:
from app.models import SystemSettings
enable_template_replication = SystemSettings.get('enable_template_replication', 'false') == 'true'
```

## Database Migration

Before running the application, execute:
```bash
python migrate_db.py
```

This will:
- Create `system_settings` table
- Add new columns to `clusters` table (qcow2_template_path, qcow2_images_path, etc.)
- Insert default SystemSettings values

## Testing Checklist

After migration:
1. ✅ Run `migrate_db.py` successfully
2. ⬜ Application starts without import errors
3. ⬜ Cluster management UI works (/admin/clusters)
4. ⬜ Settings page shows all cluster fields
5. ⬜ VM operations work (start/stop/create)
6. ⬜ Class creation with template works
7. ⬜ Admin access control works
8. ⬜ IP discovery works with per-cluster ARP subnets

## Rollback Plan

If migration fails:
1. Restore `config.py` from `config.py.backup`
2. Revert changes to updated files
3. Run previous version

config.py.backup contains the full original configuration.
