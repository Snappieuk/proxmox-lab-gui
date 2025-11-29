# Template Database Tracking

This document describes the template tracking system that stores all template information in the database.

## Overview

The system automatically tracks all Proxmox templates across all nodes and clusters in a SQLite database. This enables efficient querying of template locations without repeatedly querying the Proxmox API.

## Database Schema

The `Template` model includes:

- **Basic fields:**
  - `id`: Primary key
  - `name`: Template name
  - `proxmox_vmid`: VMID in Proxmox
  - `cluster_ip`: Which cluster it belongs to
  - `node`: Which node it's on

- **Replica tracking fields:**
  - `is_replica`: `True` if this is a replica of another template
  - `source_vmid`: Original template VMID (for replicas)
  - `source_node`: Original template node (for replicas)
  - `last_verified_at`: When this template was last confirmed to exist

- **Class tracking fields:**
  - `is_class_template`: `True` if this is a base template created for a class
  - `class_id`: Foreign key to the class (if applicable)

- **Unique constraint:** `(cluster_ip, node, proxmox_vmid)` - ensures no duplicate entries

## How Templates Are Registered

### 1. Startup Replication (Automatic)

When the app starts, `replicate_templates_to_all_nodes()` runs in a background thread:

1. Scans all nodes in all clusters for templates
2. Registers original templates in the database
3. Clones missing templates to other nodes (replicas)
4. Converts cloned VMs to templates
5. Registers replicas in the database with `is_replica=True` and source information

This ensures:
- All templates exist on all nodes (for efficient linked clones)
- Database contains complete template inventory
- Replicas are tracked with their source template

### 2. Class Creation (On-Demand)

When a teacher creates a class with precreated VMs:

1. First VM cloned from original template → Teacher's editable VM (stays as VM)
2. Second VM cloned from original template → Base template for class
3. Base template converted to template and registered in database with:
   - `is_class_template=True`
   - `class_id=<class_id>`
   - `is_replica=True`
   - `source_vmid=<original_template_vmid>`
4. Student VMs cloned as linked clones from the base template

### 3. Manual Registration (Optional)

You can manually register templates using the migration script:

```bash
python migrate_templates.py
```

This will:
- Create/update database schema
- Scan all Proxmox clusters for templates
- Register all found templates in the database
- Display statistics

## API Endpoints

### List All Templates
```http
GET /api/templates
```

Optional query parameters:
- `cluster_ip`: Filter by cluster IP
- `cluster_id`: Filter by cluster ID
- `node`: Filter by node name
- `vmid`: Filter by specific VMID
- `is_replica`: Filter replicas (true/false)
- `is_class_template`: Filter class templates (true/false)
- `class_id`: Filter by class ID

Example:
```bash
curl http://localhost:8080/api/templates?cluster_ip=10.220.15.249&node=pve1
```

Response:
```json
[
  {
    "id": 1,
    "name": "ubuntu-22.04-template",
    "proxmox_vmid": 111,
    "cluster_ip": "10.220.15.249",
    "node": "pve1",
    "is_replica": false,
    "source_vmid": null,
    "source_node": null,
    "is_class_template": false,
    "class_id": null,
    "last_verified_at": "2024-01-15T10:30:00"
  }
]
```

### Get Templates by Name
```http
GET /api/templates/by-name/<template_name>
```

Returns all instances of a template across nodes.

Example:
```bash
curl http://localhost:8080/api/templates/by-name/ubuntu-22.04-template
```

### Get Template Node Distribution
```http
GET /api/templates/nodes?vmid=<vmid>
```

Returns which nodes have this template (original + replicas).

Example:
```bash
curl http://localhost:8080/api/templates/nodes?vmid=111
```

Response:
```json
{
  "template_vmid": 111,
  "template_name": "ubuntu-22.04-template",
  "cluster_ip": "10.220.15.249",
  "nodes": {
    "pve1": {
      "vmid": 111,
      "name": "ubuntu-22.04-template",
      "is_original": true,
      "last_verified_at": "2024-01-15T10:30:00"
    },
    "pve2": {
      "vmid": 201,
      "name": "ubuntu-22.04-template-replica",
      "is_original": false,
      "last_verified_at": "2024-01-15T10:35:00"
    }
  }
}
```

### Get Template Statistics
```http
GET /api/templates/stats
```

Returns counts and distribution statistics.

Example:
```bash
curl http://localhost:8080/api/templates/stats?cluster_ip=10.220.15.249
```

Response:
```json
{
  "total_templates": 15,
  "original_templates": 5,
  "replica_templates": 8,
  "class_templates": 2,
  "templates_by_node": {
    "pve1": 8,
    "pve2": 7
  }
}
```

## Benefits

1. **Performance**: Query database instead of Proxmox API for template locations
2. **Reliability**: Centralized source of truth for template inventory
3. **Efficiency**: Student clones use nearest template replica (same node)
4. **Tracking**: Know which templates are replicas and their sources
5. **Class isolation**: Track which templates were created for specific classes

## Migration Guide

If you have an existing installation, run the migration script to populate the database:

```bash
# Navigate to project root
cd /path/to/proxmox-lab-gui

# Activate virtual environment
source venv/bin/activate  # Linux/Mac
# or
.\venv\Scripts\activate   # Windows

# Run migration
python migrate_templates.py
```

The migration will:
1. Add new columns to the Template table
2. Scan all clusters for templates
3. Replicate templates to all nodes (if missing)
4. Register all templates in the database
5. Display statistics

## Querying from Code

```python
from app.models import Template

# Get all templates on a specific node
templates = Template.query.filter_by(
    cluster_ip="10.220.15.249",
    node="pve1"
).all()

# Get original templates (not replicas or class templates)
originals = Template.query.filter_by(
    is_replica=False,
    is_class_template=False
).all()

# Get all replicas of a template
replicas = Template.query.filter_by(
    source_vmid=111,
    is_replica=True
).all()

# Get templates for a class
class_templates = Template.query.filter_by(
    class_id=5
).all()
```

## Troubleshooting

### Templates not showing in database

Run the migration script:
```bash
python migrate_templates.py
```

### Replicas not created

Check logs for errors during startup:
```bash
# If using systemd
sudo journalctl -u proxmox-gui -f

# If running standalone
# Check terminal output for "Template replication startup" messages
```

### Template verification failed

Check that:
1. Template VMs exist in Proxmox
2. Template flag is set (template=1 in config)
3. Permissions allow querying VM configs
4. Network connectivity to Proxmox API is working

### Database out of sync

Re-run the migration script to rescan and update:
```bash
python migrate_templates.py
```

Or restart the app (replication runs on startup):
```bash
sudo systemctl restart proxmox-gui
```
