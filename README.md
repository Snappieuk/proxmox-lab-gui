# Proxmox Lab GUI
Flask webapp providing a lab VM portal similar to Azure Labs for self-service student VM access.

## Features

- **Multi-cluster support**: Connect to multiple Proxmox clusters, switch between them in the UI
- **Database-first architecture**: VMInventory cache with background sync daemon for 100x faster page loads
- **Class-based management**: Teachers create classes, deploy VMs with QCOW2 overlay cloning, generate invite links
- **QCOW2 bulk cloning**: Instant VM deployment using overlay disks (100x faster than traditional cloning)
- **Student self-service**: Students join classes via invite, get auto-assigned VMs, start/stop/revert
- **Template spec caching**: Database stores template CPU/RAM/disk specs, eliminates repeated Proxmox queries
- **Class co-owners**: Teachers can grant other teachers full class management permissions
- **Progressive loading**: Fast page renders with async VM data fetching
- **Fast IP discovery**: Multi-tier system with ping+ARP verification (1-2 sec), background sync triggers
- **RDP downloads**: Generate .rdp files for Windows VM connections with fast IP verification
- **SSH terminal**: Web-based SSH terminal (optional, requires flask-sock)
- **Template publishing**: Teachers can publish VM changes to class template and propagate to all students

## Quick Start

### Development Setup (Testing Only)

**WARNING**: This code must run on a server with network access to Proxmox clusters. Local development is for testing only.

```bash
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env  # Edit with your Proxmox credentials
python3 run.py  # Runs on http://0.0.0.0:8080
```

### Production Deployment

**Option 1: Start script (standalone)**
```bash
chmod +x start.sh
./start.sh  # Activates venv, loads .env, runs Flask
```

**Option 2: Systemd service (recommended)**
```bash
# Edit proxmox-gui.service: update User, Group, WorkingDirectory paths
sudo cp proxmox-gui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now proxmox-gui
sudo systemctl status proxmox-gui
```

**Deploy updates:**
```bash
./deploy.sh  # Git pull, pip install, restart service or Flask
```

## Configuration

Create a `.env` file (or set environment variables):

```bash
# Proxmox cluster (can also use clusters.json for multi-cluster)
PVE_HOST=10.220.15.249
PVE_ADMIN_USER=root@pam
PVE_ADMIN_PASS=your_password
PVE_VERIFY=False  # SSL verification (set True for production certs)

# Admin users (comma-separated Proxmox usernames)
ADMIN_USERS=root@pam,admin@pve

# Performance tuning
VM_CACHE_TTL=300  # seconds (5min default, used as fallback)
ENABLE_IP_LOOKUP=True

# Background sync intervals (seconds)
BACKGROUND_SYNC_FULL_INTERVAL=600    # Full inventory sync every 10 minutes
BACKGROUND_SYNC_QUICK_INTERVAL=120   # Quick status check every 2 minutes
BACKGROUND_SYNC_CHECK_INTERVAL=60    # Loop check every 60 seconds

# ARP scanner settings
ARP_SUBNETS=10.220.15.255  # Broadcast address for your network
ENABLE_ARP_SCANNER=True

# SSH access for QCOW2 bulk cloning (required)
SSH_HOST=10.220.15.249
SSH_USER=root
SSH_PASSWORD=your_ssh_password
SSH_PORT=22

# Storage paths for QCOW2 cloning
QCOW2_TEMPLATE_PATH=/mnt/pve/TRUENAS-NFS/images
QCOW2_IMAGES_PATH=/mnt/pve/TRUENAS-NFS/images

# Session security
SECRET_KEY=your-secret-key-here  # Generate: python3 -c 'import secrets; print(secrets.token_hex(32))'
```

### Multi-Cluster Setup

Create `clusters.json` in project root:

```json
[
  {
    "id": "cluster1",
    "name": "Main Cluster",
    "host": "10.220.15.249",
    "user": "root@pam",
    "password": "password",
    "verify_ssl": false
  },
  {
    "id": "cluster2",
    "name": "Secondary Cluster",
    "host": "10.220.12.6",
    "user": "root@pam",
    "password": "password",
    "verify_ssl": false
  }
]
```

## Architecture

### Database-First Design

**VMInventory Table**: Single source of truth for all VM data displayed in GUI
- Stores: VM identity, status, IP address, resources, metadata, sync timestamps
- Updated by background sync daemon (not by API requests)
- Provides <100ms database queries vs 5-30 second Proxmox API calls
- **Result**: 100x faster page loads

**Background Sync Daemon** (`app/services/background_sync.py`):
- Auto-starts on application init, runs in separate thread
- Full inventory sync: Every 10 minutes (all VMs from all clusters)
- Quick sync: Every 2 minutes (status + IP for running VMs only)
- Immediate triggers: After VM start/stop operations (15 second delay for boot time)

**Performance Benefits**:
- Portal page loads in <100ms (database query)
- VM operations trigger immediate status updates (no waiting for sync)
- Background IP discovery after VM start (guest agent + ARP scan)
- No timeout storms from slow Proxmox API calls

See `DATABASE_FIRST_ARCHITECTURE.md` for detailed design documentation.

### QCOW2 Overlay Bulk Cloning

**Primary VM deployment method** for class-based labs:
- Uses QCOW2 overlay disks with backing-file references (copy-on-write)
- Creates VM shells via SSH `qm create` commands
- Attaches overlay disks instantly (no full disk copy)
- **Speed**: 25 student VMs deployed in <20 seconds (vs 54 minutes with traditional cloning)

**Why QCOW2 Overlays**:
- **100x faster**: Instant disk creation vs 2 minutes per full copy
- **No locking**: Template never locked, all VMs created in parallel
- **Storage efficient**: 25 VMs share 20GB base, overlays start at ~200KB
- **Complete isolation**: Each VM has independent overlay file, copy-on-write ensures changes are VM-specific
- **Safe**: Base template is read-only, cannot be deleted while overlays exist

**Requirements**:
- SSH access to Proxmox nodes (configured via environment variables)
- QCOW2-compatible storage backend (e.g., NFS, local)
- `paramiko` Python library for SSH connections

See `app/services/qcow2_bulk_cloner.py` for implementation details.

### Template Spec Caching

**Template Model** stores complete VM specifications:
- CPU: cores, sockets
- Memory: RAM in MB
- Disk: size, storage pool, path, format
- Network: bridge configuration
- OS: detected OS type
- Timestamp: last sync time

**Benefits**:
- Eliminates repeated Proxmox API queries for template specs
- Fast class creation (no waiting for template inspection)
- Consistent deployment (specs cached at registration time)

**Sync Endpoint**: `POST /api/templates/sync` refreshes specs from Proxmox

## Project Structure

```
app/
  config.py                    # Configuration (env vars, cluster list)
  models.py                    # SQLAlchemy ORM (User, Class, Template, VMAssignment, VMInventory)
  __init__.py                  # Flask app factory, background sync auto-start
  routes/
    auth.py                    # Login, logout, registration
    portal.py                  # Main VM dashboard
    classes.py                 # Class management UI
    admin/
      diagnostics.py           # Admin diagnostics, node info
      clusters.py              # Multi-cluster management
      users.py                 # Local user management
      vm_class_mappings.py     # VM-to-class assignment UI
    api/
      vms.py                   # VM list, start/stop/status operations
      rdp.py                   # RDP file generation with fast IP verification
      ssh.py                   # SSH WebSocket terminal
      classes.py               # Class CRUD API
      class_owners.py          # Class co-owner management
      class_template.py        # Template save/push/reimage operations
      publish_template.py      # Teacher-to-student VM propagation
      user_vm_assignments.py   # Direct VM assignments (no class)
      vm_class_mappings.py     # Bulk VM-to-class assignment
      templates.py             # Template query and sync endpoints
      sync.py                  # VMInventory sync control
      clusters.py              # Cluster switching
  services/
    background_sync.py         # Background daemon for VMInventory sync
    inventory_service.py       # VMInventory database operations
    proxmox_client.py          # Proxmox API integration, VM operations
    proxmox_service.py         # Multi-cluster connection manager
    qcow2_bulk_cloner.py       # QCOW2 overlay VM deployment (PRIMARY)
    class_vm_service.py        # High-level class VM workflow integration
    class_service.py           # Class CRUD, VM pool management, invites
    cluster_manager.py         # Cluster config persistence
    user_manager.py            # Authentication, admin detection
    arp_scanner.py             # Network scanning for IP discovery
    rdp_service.py             # RDP file generation
    ssh_service.py             # SSH WebSocket handler
    proxmox_operations.py      # Legacy VM operations (snapshot/revert still used)
  templates/                   # Jinja2 templates
  static/                      # CSS, JavaScript
  utils/                       # Logging, decorators, caching
run.py                         # Application entry point
start.sh                       # Start script (activates venv, runs Flask)
deploy.sh                      # Deployment script (git pull, restart)
restart.sh                     # Quick restart (no pull or install)
proxmox-gui.service            # Systemd service unit
debug_vminventory.py           # Debug VMInventory table
fix_vminventory.py             # Repair VMInventory records
migrate_db.py                  # Database schema migrations
```

## Class-Based Lab Workflow

1. **Admin setup**: Create local teacher accounts via `/admin/users` UI or database
2. **Register templates**: Admin/teacher registers Proxmox templates via `/admin/probe` or API
   - Optionally run `POST /api/templates/sync` to cache template specs in database
3. **Create class**: Teacher creates class at `/classes/create`, selects template, sets pool size
4. **Deploy VMs**: System automatically deploys teacher VM + class base template + student VMs using QCOW2 overlay cloning
   - Teacher VM: Full clone of original template for making changes
   - Class base template: Full clone of teacher VM (becomes base for student overlays)
   - Student VMs: QCOW2 overlays with backing-file reference to class base template
   - **Deployment time**: <20 seconds for 25 students (vs 54 minutes traditional cloning)
5. **Generate invite**: Teacher generates time-limited (7-day default) or never-expiring invite link
6. **Student enrollment**: Students visit invite link, auto-assigned first available VM (creates VMAssignment record)
7. **Lab usage**: Students see assigned VM in `/portal`, can start/stop/revert to baseline snapshot
8. **Template publishing** (optional): Teacher can publish changes from their VM → class base template → all student VMs
   - Uses QCOW2 rebasing to propagate disk changes
   - Long-running background operation
9. **Co-owner management** (optional): Teacher can add co-owners via `/api/classes/<id>/co-owners` for shared management

**Note**: All users (teachers and students) are local database accounts. Only admins need Proxmox accounts.

## Database

SQLite database at `app/lab_portal.db` (auto-created on first run):

- **users**: Local accounts (adminer/teacher/user roles), password hashing
- **classes**: Lab classes with invite tokens, template references
- **templates**: Proxmox template references with cached specs (CPU/RAM/disk/network)
- **vm_assignments**: VM-to-user mappings (class-based or direct)
- **vm_inventory**: **Cached VM data** (status, IP, resources) updated by background sync daemon
- **class_co_owners**: Many-to-many relationship for class co-ownership

**Migrations**: Use `migrate_db.py` to add missing columns without destroying data:
```bash
python3 migrate_db.py
```

## API Endpoints

### VM Operations
- `GET /api/vms` - Fetch VM list for current user (from VMInventory database)
- `POST /api/vm/<vmid>/start` - Start VM, trigger immediate status update + background IP discovery
- `POST /api/vm/<vmid>/stop` - Stop VM, update status immediately
- `GET /api/vm/<vmid>/status` - Get current VM status from database
- `GET /api/vm/<vmid>/ip` - Get VM IP address with fast verification
- `GET /rdp/<vmid>.rdp` - Download RDP file (fast ping+ARP verification)
- `GET /ssh/<vmid>` - WebSocket SSH terminal

### Class Management
- `GET /api/classes` - List classes for current user
- `POST /api/classes` - Create new class (auto-deploys VMs with QCOW2 cloning)
- `GET /api/classes/<id>` - Get class details
- `PUT /api/classes/<id>` - Update class
- `DELETE /api/classes/<id>` - Delete class and all VMs
- `POST /api/class/<id>/pool` - Add VMs to class pool (legacy, prefer auto-deployment)
- `POST /api/class/<id>/assign` - Assign VM to student
- `GET /api/classes/<id>/co-owners` - List class co-owners
- `POST /api/classes/<id>/co-owners` - Add co-owner
- `DELETE /api/classes/<id>/co-owners/<user_id>` - Remove co-owner

### Template Operations
- `GET /api/templates` - List registered templates with cached specs
- `GET /api/templates/by-name/<name>` - Look up template by name
- `GET /api/templates/stats` - Template usage statistics
- `POST /api/templates/sync` - Sync template specs from Proxmox to database
- `POST /api/classes/<id>/publish-template` - Publish teacher VM changes to all students

### VM Assignments
- `GET /api/user-vm-assignments` - List direct VM assignments (no class)
- `POST /api/user-vm-assignments` - Create direct assignment
- `DELETE /api/user-vm-assignments/<id>` - Remove assignment
- `GET /api/vm-class-mappings` - List VM-to-class mappings (admin only)
- `POST /api/vm-class-mappings` - Bulk assign VMs to class

### System Operations
- `GET /api/sync/status` - Background sync daemon status
- `POST /api/sync/trigger` - Manually trigger full inventory sync
- `GET /api/sync/inventory/summary` - VMInventory table statistics
- `POST /api/clusters/switch` - Change active cluster
- `GET /health` - Health check endpoint

## Authentication

**Simplified Authentication Model**:

1. **Proxmox auth**: Any user who can authenticate via Proxmox API is automatically an **admin**
   - Session stores `user@pve` (e.g., `root@pam`)
   - **Design principle**: Only admins should have Proxmox accounts
   
2. **Local auth**: SQLite users with password hashing (Werkzeug) for all non-admin users
   - Roles: `adminer` (full admin), `teacher` (create/manage classes), `user` (student)
   - Students and teachers use local accounts (no Proxmox access)

**Login flow**:
- Try Proxmox authentication first (grants admin access)
- If Proxmox auth fails, try local database authentication
- Session stores user identifier and role

**Admin detection**: `is_admin_user(userid)` returns True if:
- User authenticated via Proxmox API, OR
- Local user with `role='adminer'`

**Going forward**: No application users (students/teachers) should have Proxmox accounts. All managed via SQLite database.

## Performance & Caching

### Database-First Architecture
- **Primary cache**: VMInventory table (updated by background daemon)
- **Query speed**: <100ms vs 5-30 seconds for Proxmox API
- **Background sync**: Every 10 minutes (full), every 2 minutes (quick status)
- **Immediate updates**: VM start/stop operations update database instantly

### Fast IP Verification
- **RDP downloads**: Ping cached IP (1-2 sec), verify ARP MAC, fallback to full scan only if mismatch
- **IP discovery hierarchy**: VMInventory DB → guest agent → LXC interfaces → ARP scan → RDP port validation
- **Background trigger**: After VM start, daemon waits 15 seconds (boot time) then discovers IP

### Legacy Fallback Caches (minimal use)
- **VM list cache**: Module-level dict in `proxmox_client.py`, 5min TTL (used only when VMInventory unavailable)
- **IP lookup cache**: Separate 5min TTL for guest agent queries (prevents timeout storms)
- **ARP scanner cache**: 1hr TTL, background thread updates

## Troubleshooting

**Check health**: Visit `/health` endpoint (returns `{"status": "ok"}`)

**View logs**:
- Systemd: `sudo journalctl -u proxmox-gui -f`
- Standalone: Check terminal output (Flask logs to stdout)

**Admin access issues**:
- Verify successful Proxmox authentication OR local user `role='adminer'`
- Check logs for "inject_admin_flag" entries

**Slow page loads**:
- Check background sync status: `GET /api/sync/status`
- Verify VMInventory table populated: `python3 debug_vminventory.py`
- Manually trigger sync: `POST /api/sync/trigger`
- Check sync intervals in `.env` (default: 10min full, 2min quick)

**VM IP not appearing**:
- **QEMU VMs**: Requires guest agent installed and running
- **LXC containers**: Works when stopped, uses `.interfaces.get()` API
- **ARP scanner**: Verify `nmap` installed, background sync triggers IP discovery 15s after VM start
- **Fast verification**: RDP download pings cached IP, checks ARP MAC
- Check VMInventory: `SELECT vmid, status, ip_address FROM vm_inventory WHERE vmid = <vmid>`

**IP discovery issues**:
- Background sync auto-discovers IPs for running VMs every 2 minutes
- Manual trigger: Start VM, wait 15-20 seconds, refresh portal page
- Check ARP subnets configured correctly: `ARP_SUBNETS=10.220.15.255`
- Verify nmap installed: `which nmap` (required for ARP scanning)

**QCOW2 cloning issues**:
- Verify SSH access: `ssh root@<proxmox_host>`
- Check SSH credentials in `.env`: `SSH_HOST`, `SSH_USER`, `SSH_PASSWORD`
- Verify storage paths: `QCOW2_TEMPLATE_PATH`, `QCOW2_IMAGES_PATH`
- Check `paramiko` installed: `pip install paramiko`
- Review logs for SSH connection errors

**Background sync not running**:
- Check logs for "Starting background inventory sync daemon" message
- Verify no errors on app startup
- Manually trigger: `POST /api/sync/trigger`
- Check sync thread status: `GET /api/sync/status`

**Database errors**:
- Run migrations: `python3 migrate_db.py`
- Check for missing columns in VMInventory or Template tables
- Repair inventory: `python3 fix_vminventory.py`
- Debug inventory: `python3 debug_vminventory.py`

**Template spec caching**:
- Sync templates: `POST /api/templates/sync`
- Verify specs cached: `GET /api/templates` (check for cpu_cores, memory_mb, etc.)
- Re-register template if specs missing

## Development

**Manual testing workflow** (no automated tests):
1. Start app: `python3 run.py`
2. Login as admin (Proxmox credentials) or local user
3. Verify VMs appear in `/portal` (should load in <1 second)
4. Test start/stop buttons (check immediate status update)
5. Download RDP file for Windows VM (should be fast with ping verification)
6. Admin: verify `/admin/diagnostics`, `/admin/users`, `/admin/vm-class-mappings` accessible
7. Teacher: create class, verify QCOW2 cloning completes in <20 seconds for 25 VMs
8. Student: join class, verify VM assignment, test start/stop

**Database inspection**:
```bash
# SQLite CLI
sqlite3 app/lab_portal.db
sqlite> .tables
sqlite> SELECT * FROM vm_inventory LIMIT 5;
sqlite> SELECT * FROM templates;

# Python shell
python3
>>> from app import create_app
>>> from app.models import db, VMInventory, Template
>>> app = create_app()
>>> with app.app_context():
...     print(VMInventory.query.count())
...     print(Template.query.all())
```

**Performance profiling**:
- VMInventory queries: Should be <100ms
- Proxmox API calls: 5-30 seconds (avoid in request handlers)
- RDP download: 1-2 seconds with fast verification
- Class deployment: <20 seconds for 25 VMs with QCOW2 cloning

**Code organization**:
- Database operations → `app/services/inventory_service.py`
- Proxmox API calls → `app/services/proxmox_client.py`
- VM deployment → `app/services/qcow2_bulk_cloner.py`, `app/services/class_vm_service.py`
- Background tasks → `app/services/background_sync.py`
- API routes → `app/routes/api/*.py`
- Admin UI → `app/routes/admin/*.py`

## Additional Documentation

- `DATABASE_FIRST_ARCHITECTURE.md` - Detailed VMInventory and background sync design
- `CLASS_CO_OWNERS.md` - Class co-ownership feature documentation
- `.github/copilot-instructions.md` - Comprehensive architecture guide for developers
- `terraform/README.md` - Alternative Terraform-based VM deployment (slower than QCOW2)

## Contributing

This is an internal lab management tool. For architecture questions or feature requests, see the Copilot instructions.

## License

MIT
