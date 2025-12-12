# Cluster Management System

## Overview

The Proxmox Lab GUI now features a **database-first cluster management system** that allows you to dynamically configure and manage multiple Proxmox clusters without editing configuration files or restarting the application.

## Key Features

### 🎛️ Dynamic Configuration
- **No more hard-coded clusters**: All cluster configurations are stored in the database
- **No more hard-coded passwords**: Credentials managed securely through the admin UI
- **Live updates**: Changes take effect immediately without application restart
- **Web-based management**: Configure everything through `/admin/settings` UI

### ⚙️ Per-Cluster Options

Each cluster can be configured with:

#### Connection Settings
- **Cluster ID**: Unique identifier (e.g., `cluster1`, `prod-cluster`)
- **Display Name**: Human-readable name shown in UI
- **Proxmox Host**: IP address or hostname
- **API Port**: Proxmox API port (default: 8006)
- **Admin User**: Proxmox user account (e.g., `root@pam`)
- **Password**: Proxmox password (stored in database, never exposed in API)
- **SSL Verification**: Enable/disable SSL certificate verification

#### Deployment Toggles
- **Allow VM Deployment**: Enable creating and deploying VMs to this cluster
- **Template Synchronization**: Sync templates from this cluster to the database
- **ISO Synchronization**: Sync ISO images from this cluster
- **Auto-Shutdown Monitoring**: Monitor and auto-shutdown idle VMs on this cluster

#### Management Options
- **Active/Inactive**: Enable or disable the cluster
- **Default Cluster**: Set as default for new users and deployments
- **Priority (0-100)**: Deployment priority (higher = preferred for new VMs)

#### Storage Configuration
- **Default Storage**: Default storage pool for VM disks (e.g., `local-lvm`)
- **Template Storage**: Storage for template exports (e.g., `local`)

#### Metadata
- **Description**: Optional notes about the cluster

## Migration from clusters.json

### Automatic Migration

On first startup after upgrading, run the migration script:

```bash
python migrate_db.py
```

This will:
1. Create the `clusters` table with all new columns
2. Import existing clusters from `clusters.json` to the database
3. Preserve `clusters.json` as a backup (not used after migration)

### Manual Migration

If you need to manually migrate:

1. **Backup your clusters.json**:
   ```bash
   cp app/clusters.json app/clusters.json.backup
   ```

2. **Run migration**:
   ```bash
   python migrate_db.py
   ```

3. **Verify in UI**:
   - Visit `/admin/settings`
   - Confirm all clusters appear
   - Test connections using "Test Connection" button

4. **Remove clusters.json** (optional):
   ```bash
   # After confirming everything works
   rm app/clusters.json
   ```

## Using the Settings UI

### Accessing Settings
1. Login as an admin user
2. Navigate to `/admin/settings`
3. View all configured clusters

### Adding a Cluster

1. Click "Add Cluster" button
2. Fill in required fields:
   - Cluster ID (unique identifier)
   - Display Name
   - Proxmox Host
   - Admin User
   - Password
3. Configure toggles:
   - Enable/disable deployment options
   - Set as active/default cluster
4. (Optional) Configure advanced settings:
   - Priority
   - Storage pools
   - Description
5. Click "Save Cluster"
6. (Optional) Click "Test Connection" to verify

### Editing a Cluster

1. Click "Edit" on the cluster card
2. Update any fields (leave password blank to keep existing)
3. Click "Save Cluster"

### Deleting a Cluster

1. Click "Delete" on the cluster card
2. Confirm deletion
3. Note: Cannot delete the last cluster

### Testing Connection

1. Edit a cluster
2. Click "Test Connection" button
3. Verify:
   - Connection successful
   - Proxmox version displayed
   - Node count correct

## API Endpoints

All cluster management is done through REST API endpoints:

### GET `/api/cluster-config`
**Auth**: Admin only  
**Returns**: List of all clusters (without passwords)

```json
{
  "ok": true,
  "clusters": [
    {
      "id": "cluster1",
      "db_id": 1,
      "name": "Main Cluster",
      "host": "10.220.15.249",
      "port": 8006,
      "user": "root@pam",
      "is_active": true,
      "is_default": true,
      "allow_vm_deployment": true,
      "allow_template_sync": true,
      "allow_iso_sync": true,
      "auto_shutdown_enabled": false,
      "priority": 80,
      "default_storage": "local-lvm",
      "template_storage": "local"
    }
  ]
}
```

### POST `/api/cluster-config`
**Auth**: Admin only  
**Creates**: New cluster

**Request**:
```json
{
  "cluster_id": "cluster2",
  "name": "Secondary Cluster",
  "host": "10.220.12.6",
  "port": 8006,
  "user": "root@pam",
  "password": "secure-password",
  "is_active": true,
  "allow_vm_deployment": true,
  "priority": 50
}
```

### PUT `/api/cluster-config/<db_id>`
**Auth**: Admin only  
**Updates**: Existing cluster

**Request**: Same as POST, but password is optional (omit to keep existing)

### DELETE `/api/cluster-config/<db_id>`
**Auth**: Admin only  
**Deletes**: Cluster configuration

### POST `/api/cluster-config/<db_id>/test`
**Auth**: Admin only  
**Tests**: Connection to cluster

**Response**:
```json
{
  "ok": true,
  "message": "Connection successful",
  "version": "7.4",
  "nodes": 3,
  "node_names": ["pve1", "pve2", "pve3"]
}
```

## Architecture Changes

### Database Schema

New columns in `clusters` table:
- `allow_vm_deployment` (BOOLEAN, default: 1)
- `allow_template_sync` (BOOLEAN, default: 1)
- `allow_iso_sync` (BOOLEAN, default: 1)
- `auto_shutdown_enabled` (BOOLEAN, default: 0)
- `priority` (INTEGER, default: 50)
- `default_storage` (VARCHAR(100), nullable)
- `template_storage` (VARCHAR(100), nullable)
- `description` (TEXT, nullable)

### Code Changes

#### `app/models.py`
- Extended `Cluster` model with new fields
- Updated `to_dict()` and `to_safe_dict()` methods
- Added `db_id` to safe dict for API operations

#### `app/routes/api/clusters.py`
- Converted from JSON-based to database-based management
- Added CRUD endpoints (GET, POST, PUT, DELETE)
- Added connection test endpoint

#### `app/services/proxmox_service.py`
- Added `get_clusters_from_db()` function
- Updated all cluster lookups to use database
- Support for both cluster_id strings and numeric database IDs

#### `app/config.py`
- Added migration warnings for legacy `clusters.json`
- Database takes precedence over JSON file

#### `app/__init__.py`
- Context processor loads clusters from database
- Cluster dropdown populated dynamically

#### `migrate_db.py`
- Added new cluster columns to migration
- Updated cluster table schema

## Deployment Priority

The `priority` field (0-100) determines which cluster is preferred when deploying new VMs:

- **100**: Highest priority - always preferred
- **75-99**: High priority
- **50-74**: Normal priority (default: 50)
- **25-49**: Low priority
- **0-24**: Lowest priority - only used if others unavailable

Example use case:
- Production cluster: priority 90
- Development cluster: priority 60  
- Testing cluster: priority 30

When creating a new class, the system will prefer the production cluster unless it's disabled or full.

## Toggles Explained

### Allow VM Deployment
**Purpose**: Control whether new VMs can be deployed to this cluster  
**Use case**: Disable when cluster is full or under maintenance  
**Effect**: Class creation and VM builder will skip this cluster

### Template Synchronization
**Purpose**: Include this cluster's templates in the template sync daemon  
**Use case**: Disable for test clusters with temporary templates  
**Effect**: Templates won't appear in class creation or VM builder dropdowns

### ISO Synchronization
**Purpose**: Include this cluster's ISOs in the ISO sync daemon  
**Use case**: Disable for clusters with slow storage  
**Effect**: ISOs won't appear in VM builder ISO dropdown

### Auto-Shutdown Monitoring
**Purpose**: Enable auto-shutdown service to monitor VMs on this cluster  
**Use case**: Enable for student labs, disable for production  
**Effect**: Auto-shutdown daemon will check VMs on this cluster every 5 minutes

## Security Considerations

### Password Storage
- Passwords are stored in the database (plaintext in SQLite)
- **Production recommendation**: Encrypt passwords or use Proxmox API tokens
- Never expose passwords in API responses (use `to_safe_dict()`)

### Access Control
- All cluster management endpoints require admin authentication
- Students and teachers cannot view or modify cluster configurations
- Only Proxmox-authenticated users or local users with `adminer` role can access

### SSL Verification
- Disabled by default for self-signed certificates in lab environments
- **Production recommendation**: Use valid SSL certificates and enable verification

## Troubleshooting

### Clusters not appearing in dropdown
1. Check database: `sqlite3 app/lab_portal.db "SELECT * FROM clusters;"`
2. Verify `is_active = 1`
3. Check logs for database errors
4. Run `python migrate_db.py` to ensure schema is current

### Connection test fails
1. Verify host IP is reachable: `ping <host>`
2. Verify API port is open: `telnet <host> 8006`
3. Check credentials are correct
4. Check Proxmox is running: `systemctl status pve-cluster`
5. Verify SSL settings match (verify_ssl true/false)

### Changes not taking effect
1. Check browser console for API errors
2. Verify cache invalidation: clusters are reloaded on every request
3. Check Proxmox connections are cleared: restart application if needed

### Migration issues
1. Backup database: `cp app/lab_portal.db app/lab_portal.db.backup`
2. Run migration with verbose output: `python migrate_db.py`
3. Check for error messages about missing columns
4. If migration fails, restore backup and try again

## Best Practices

### Cluster Organization
- Use descriptive cluster IDs: `prod-cluster`, `dev-cluster`, `student-cluster`
- Set meaningful display names: "Production (10.220.15.249)"
- Add descriptions: "Production cluster - no student access"

### Priority Assignment
- Production: 80-100
- Development: 50-79  
- Testing: 20-49
- Decommissioned: 0-19 (or disable)

### Storage Configuration
- Set `default_storage` for faster VM deployment
- Use `template_storage` for template exports (fast storage preferred)
- Leave blank to use Proxmox defaults

### Maintenance Mode
To take a cluster offline for maintenance:
1. Set "Active" toggle to OFF
2. VMs won't be deployed to this cluster
3. Existing VMs remain accessible to users
4. Re-enable when maintenance complete

## Example Configurations

### Development Cluster
```yaml
Cluster ID: dev-cluster
Name: Development Cluster
Host: 10.220.12.6
Priority: 60
Allow VM Deployment: ✓
Allow Template Sync: ✓
Allow ISO Sync: ✓
Auto-Shutdown: ✓ (save resources)
Default Storage: local-lvm
```

### Production Cluster
```yaml
Cluster ID: prod-cluster
Name: Production Cluster
Host: 10.220.15.249
Priority: 90
Allow VM Deployment: ✓
Allow Template Sync: ✓
Allow ISO Sync: ✗ (use dedicated ISO server)
Auto-Shutdown: ✗ (always available)
Default Storage: ceph-storage
Template Storage: nfs-templates
```

### Read-Only Cluster (Template Source)
```yaml
Cluster ID: template-cluster
Name: Template Repository
Host: 10.220.20.10
Priority: 10
Allow VM Deployment: ✗
Allow Template Sync: ✓
Allow ISO Sync: ✗
Auto-Shutdown: ✗
```

## Future Enhancements

Potential improvements for future versions:

1. **Password Encryption**: Encrypt passwords in database using Fernet or similar
2. **API Token Support**: Support Proxmox API tokens instead of passwords
3. **Health Monitoring**: Display cluster health status (CPU, RAM, storage)
4. **Resource Tracking**: Show VMs deployed per cluster
5. **Backup Configuration**: Export/import cluster configurations
6. **Cluster Groups**: Group clusters by environment (prod, dev, test)
7. **RBAC**: Per-cluster access control for teachers
8. **Audit Log**: Track cluster configuration changes
