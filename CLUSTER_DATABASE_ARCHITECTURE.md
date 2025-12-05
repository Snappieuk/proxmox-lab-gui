# Database-First Cluster Architecture

## Overview

Starting with this commit, Proxmox cluster configurations are stored in the **database** (`clusters` table) rather than hardcoded in `config.py` or stored in `clusters.json`.

This aligns with the database-first architecture principle used throughout the application (VMInventory, VMAssignment, Class management, etc.).

## Architecture

### Database Schema

The `Cluster` model in `app/models.py` stores:

```python
class Cluster(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cluster_id = db.Column(db.String(50), unique=True, nullable=False)  # Internal identifier
    name = db.Column(db.String(120), nullable=False)                   # Display name
    host = db.Column(db.String(255), nullable=False)                   # IP or hostname
    port = db.Column(db.Integer, default=8006)                         # Proxmox API port
    user = db.Column(db.String(80), nullable=False)                    # Proxmox username
    password = db.Column(db.String(256), nullable=False)               # Proxmox password
    verify_ssl = db.Column(db.Boolean, default=False)                  # SSL verification
    is_default = db.Column(db.Boolean, default=False)                  # Default cluster
    is_active = db.Column(db.Boolean, default=True)                    # Enable/disable
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

### Migration from clusters.json

The `migrate_db.py` script automatically migrates existing `clusters.json` configuration to the database:

```bash
python3 migrate_db.py
```

**Migration behavior:**
- Reads `app/clusters.json` if it exists
- Creates `clusters` table if missing
- Inserts each cluster into database (skips if already exists)
- Preserves JSON file as backup (not deleted automatically)
- Safe to run multiple times (idempotent)

### Runtime Loading

The `config.py` module provides `load_clusters_from_db()` function:

```python
def load_clusters_from_db():
    """Load cluster configuration from database.
    
    Returns list of cluster dicts compatible with CLUSTERS format.
    Falls back to clusters.json if database is unavailable.
    """
    try:
        from app.models import Cluster
        clusters = Cluster.query.filter_by(is_active=True).all()
        if clusters:
            return [c.to_dict() for c in clusters]
    except Exception:
        # Database not available - fall back to JSON
        pass
    
    # Fallback: Load from clusters.json (legacy)
    if os.path.exists(CLUSTER_CONFIG_FILE):
        with open(CLUSTER_CONFIG_FILE, 'r') as f:
            return json.load(f)
    
    # Ultimate fallback: hardcoded defaults
    return CLUSTERS
```

**Loading order:**
1. **Database** (preferred): Query `Cluster.query.filter_by(is_active=True)`
2. **clusters.json** (legacy): Load from file if database unavailable
3. **Hardcoded defaults** (fallback): Use `CLUSTERS` list in `config.py`

## Admin UI

Clusters can be managed via the admin interface at `/admin/clusters`:

- **Add cluster**: Create new cluster configuration
- **Edit cluster**: Update connection details, credentials
- **Activate/Deactivate**: Enable or disable clusters without deleting
- **Set default**: Mark cluster as default for new users
- **Delete**: Remove cluster configuration (with confirmation)

## Benefits

### 1. **Runtime Configuration Changes**
- Add/edit/delete clusters without editing code or restarting app
- Enable/disable clusters dynamically
- Change credentials via admin UI

### 2. **Security**
- Credentials stored in database (can be encrypted at rest)
- No need to commit cluster passwords to git
- Admin-only access to cluster management

### 3. **Consistency**
- Aligns with database-first architecture (VMInventory, VMAssignment, etc.)
- Single source of truth for all configuration
- Audit trail via `created_at` and `updated_at` timestamps

### 4. **Multi-tenancy Ready**
- Future: per-user or per-class cluster assignments
- Track which clusters are used by which classes
- Cluster-specific resource quotas

## Backwards Compatibility

**Legacy support maintained:**
- `clusters.json` still works if database unavailable
- Hardcoded `CLUSTERS` list in `config.py` still works as ultimate fallback
- Existing code using `CLUSTERS` list continues to work (now loads from DB)

**Migration path:**
1. Run `python3 migrate_db.py` to create table and migrate JSON
2. Verify clusters appear in `/admin/clusters`
3. Test cluster switching and VM operations
4. Optionally backup and remove `app/clusters.json` (or keep as fallback)

## Developer Notes

### Adding Clusters Programmatically

```python
from app.models import Cluster, db
from app import create_app

app = create_app()
with app.app_context():
    cluster = Cluster(
        cluster_id='cluster3',
        name='Test Cluster',
        host='10.220.20.10',
        port=8006,
        user='root@pam',
        password='secure-password',
        verify_ssl=False,
        is_default=False,
        is_active=True
    )
    db.session.add(cluster)
    db.session.commit()
```

### Querying Clusters

```python
from app.models import Cluster

# Get all active clusters
active_clusters = Cluster.query.filter_by(is_active=True).all()

# Get default cluster
default_cluster = Cluster.query.filter_by(is_default=True).first()

# Get specific cluster
cluster = Cluster.query.filter_by(cluster_id='cluster1').first()
```

### Converting to Legacy Format

```python
# For compatibility with existing code expecting dict format
cluster_dict = cluster.to_dict()  # Includes password (sensitive)
safe_dict = cluster.to_safe_dict()  # Excludes password (for API responses)
```

## Security Considerations

### Password Storage

**Current state:**
- Passwords stored as plaintext in database
- Database file protected by filesystem permissions

**Future enhancements:**
- Encrypt passwords at rest (e.g., using Fernet symmetric encryption)
- Store encryption key in environment variable or separate keyfile
- Decrypt passwords only when creating ProxmoxAPI connections

### Access Control

**Current state:**
- Only admin users can access `/admin/clusters`
- Cluster passwords excluded from API responses via `to_safe_dict()`

**Future enhancements:**
- Audit log for cluster configuration changes
- Per-cluster access control (which users/classes can use which clusters)
- Read-only cluster credentials for non-admin operations

## Testing

### Manual Testing

```bash
# 1. Migrate existing clusters.json
python3 migrate_db.py

# 2. Check database contents
sqlite3 app/lab_portal.db "SELECT cluster_id, name, host, is_active FROM clusters;"

# 3. Test admin UI
# Navigate to /admin/clusters in browser
# Add, edit, activate/deactivate clusters

# 4. Test cluster switching
# Login to portal, switch cluster via dropdown
# Verify VMs load from correct cluster
```

### Automated Testing (Future)

```python
def test_cluster_crud():
    """Test cluster create/read/update/delete operations."""
    cluster = Cluster(cluster_id='test1', name='Test', host='10.0.0.1', ...)
    db.session.add(cluster)
    db.session.commit()
    
    assert Cluster.query.filter_by(cluster_id='test1').first() is not None
    
    cluster.is_active = False
    db.session.commit()
    
    assert len(Cluster.query.filter_by(is_active=True).all()) == 0
```

## Migration Checklist

- [x] Create `Cluster` model in `app/models.py`
- [x] Add `CLUSTER_TABLE_SCHEMA` to `migrate_db.py`
- [x] Implement `migrate_clusters_from_json()` function
- [x] Add `load_clusters_from_db()` to `app/config.py`
- [x] Update copilot instructions with database-first architecture
- [ ] Update admin UI to manage clusters (add/edit/delete)
- [ ] Test cluster migration on production database
- [ ] Add password encryption for cluster credentials
- [ ] Add audit logging for cluster configuration changes

## See Also

- `DATABASE_FIRST_ARCHITECTURE.md` - Overview of database-first design principles
- `CLASS_CO_OWNERS.md` - Example of database-first feature implementation
- `app/models.py` - Database schema definitions
- `migrate_db.py` - Migration script
