# Proxmox Lab GUI
Flask webapp providing a lab VM portal similar to Azure Labs for self-service student VM access.

## Features

### Core Architecture
- **Database-first multi-cluster**: All cluster configurations stored in database, managed via web UI (no .env files needed)
- **VMInventory caching**: Background sync daemons cache all VM data for 100x faster page loads (<100ms vs 5-30s Proxmox API)
- **Template sync daemon**: Automatic background sync of templates with full spec caching (CPU/RAM/disk/OS), updates every 30min
- **ISO sync daemon**: Caches ISO images from configured storage pools for instant VM builder access
- **Progressive UI**: Skeleton HTML + async API calls prevent timeout on slow Proxmox responses

### Class Management
- **Class-based labs**: Teachers create classes, deploy VMs using disk overlay strategy, generate invite links
- **Disk overlay deployment**: QCOW2 copy-on-write overlays for instant VM deployment (100x faster than `qm clone`)
- **Class import**: Scan existing Proxmox VMs by VMID pattern and import as classes (teacher VM: xxxxx00, base VM: xxxxx99, students: xxxxx01-98)
- **Template publishing**: Teachers can modify their VM, save to class template, and push updates to all student VMs
- **Class co-owners**: Grant other teachers full class management permissions (many-to-many relationship)
- **Auto-shutdown**: Configure idle CPU thresholds and time restrictions per class to save resources
- **Single-node override**: Deploy entire class to specific node (bypasses load balancing)

### Student Experience
- **Self-service portal**: Students join classes via invite links, get auto-assigned VMs from pool
- **VM controls**: Start/stop VMs, download RDP files, access web console and SSH terminal
- **Revert to baseline**: Students can reset VMs to original snapshot state
- **Progressive status**: UI shows "Starting..." badge until IP discovered (2s/10s polling + background sync)

### VM Management
- **VM Builder**: Create VMs from ISO images with custom hardware specs (CPU, RAM, disk, network)
- **Multi-tier IP discovery**: Guest agent → LXC API → ARP scanner with nmap → RDP port validation
- **RDP generation**: Automatic .rdp file creation with IP validation (Windows VMs + Linux with xrdp)
- **Web console**: Embedded noVNC with automatic ticket refresh and compression optimization
- **SSH terminal**: Web-based SSH access (requires flask-sock package)
- **Direct VM assignments**: Assign VMs to users outside of class context (for template VMs)

## Quick Start

### ⚡ One-Command Installation (Recommended)

Install and auto-start with systemd in one command:

```bash
# Download and run installer
curl -sSL https://raw.githubusercontent.com/Snappieuk/proxmox-lab-gui/main/install.sh | sudo bash

# Or if you prefer wget
wget -qO- https://raw.githubusercontent.com/Snappieuk/proxmox-lab-gui/main/install.sh | sudo bash
```

**What it does:**
- ✅ Installs all system dependencies (Python, git, nmap, etc.)
- ✅ Clones repository to `/opt/proxmox-lab-gui`
- ✅ Creates Python virtual environment
- ✅ Installs Python dependencies
- ✅ Initializes database with migrations
- ✅ Creates systemd service for auto-start
- ✅ Starts the service automatically

**After installation:**
1. Access the web UI: `http://your-ip:8080`
2. Login as admin (any Proxmox user initially)
3. Configure clusters at: `http://your-ip:8080/admin/settings`
4. Add your Proxmox clusters with credentials
5. Create teacher/student accounts or classes

**Note:** No .env configuration needed! All cluster credentials are managed through the web UI.

### 🔄 One-Command Update

Update to the latest version:

```bash
sudo bash /opt/proxmox-lab-gui/update.sh
```

**What it does:**
- ✅ Creates automatic backup (database + config)
- ✅ Stops the service
- ✅ Pulls latest code from git
- ✅ Updates Python dependencies
- ✅ Runs database migrations
- ✅ Restarts the service
- ✅ Keeps last 5 backups

### Development Setup (Testing Only)

**WARNING**: This code must run on a server with network access to Proxmox clusters. Local development is for testing only.

```bash
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 migrate_db.py  # Initialize database
python3 run.py  # Runs on http://0.0.0.0:8080
# Then configure clusters via web UI at http://localhost:8080/admin/settings
```

### Manual Production Deployment

**Option 1: Start script (standalone)**
```bash
chmod +x start.sh
./start.sh  # Activates venv, runs Flask
```

**Option 2: Systemd service (manual setup)**
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

### Database-First Architecture

**All cluster configurations are managed through the web UI** - no `.env` or `clusters.json` files needed!

1. **First-time setup**: After installation, visit `http://your-ip:8080`
2. **Add clusters**: Go to Settings → Add your Proxmox clusters with credentials
3. **Configure storage**: Set SSH credentials and storage paths for each cluster
4. **Start using**: Create classes, assign VMs, manage users - all through the web interface

### Optional: Environment Variables (.env)

Only required if you need to override defaults. Most settings are managed per-cluster in the web UI.

Create `.env` file (optional):

```bash
# SSH access for disk operations (can also be configured per-cluster in web UI)
SSH_HOST=10.220.15.249
SSH_USER=root
SSH_PASSWORD=your_ssh_password
SSH_PORT=22

# Storage paths for VM disk operations
QCOW2_TEMPLATE_PATH=/mnt/pve/TRUENAS-NFS/images
QCOW2_IMAGES_PATH=/mnt/pve/TRUENAS-NFS/images

# Network settings (optional - defaults work for most setups)
ARP_SUBNETS=10.220.15.255  # Broadcast address for your network

# Session security (optional - auto-generated if not provided)
SECRET_KEY=your-secret-key-here  # Generate: python3 -c 'import secrets; print(secrets.token_hex(32))'
```

**Note**: 
- Cluster credentials are stored in database, managed via web UI (no `.env` needed)
- SSH credentials can be set per-cluster in web UI or via `.env` as global default
- Session SECRET_KEY is auto-generated on first run if not provided

## Architecture

### Database-First Design

**VMInventory Table**: Single source of truth for all VM data displayed in GUI
- Stores: VM identity, status, IP address, resources, metadata, sync timestamps
- Updated by background sync daemon (not by API requests)
- Provides <100ms database queries vs 5-30 second Proxmox API calls
- **Result**: 100x faster page loads

**Template Table**: Cached template data with comprehensive specs
- Stores: Template identity, cluster/node location, CPU/RAM/disk specs, OS type, network config
- Updated by template sync daemon (not by API requests)
- Enables instant template loading without Proxmox API calls
- **Result**: Cluster switching and template browsing <100ms vs 5-30 seconds

**Background Sync Daemons**:
- **VM Sync** (`app/services/background_sync.py`):
  - Auto-starts on application init, runs in separate thread
  - Full inventory sync: Every 5 minutes (all VMs from all clusters)
  - Quick sync: Every 30 seconds (status + IP for running VMs only)
  - Immediate triggers: After VM start/stop operations (15 second delay for boot time)
  
- **Template Sync** (`app/services/template_sync.py`):
  - Auto-starts on application init, runs in separate thread
  - Full template sync: Every 30 minutes (all templates with specs from all clusters)
  - Quick verification: Every 5 minutes (confirm templates still exist)
  - Manual trigger: `POST /api/sync/templates/trigger` (admin only)

**Performance Benefits**:
- Portal page loads in <100ms (database query)
- Template dropdown populates instantly (<100ms vs 5-30 seconds)
- Cluster switching instant (templates already cached per cluster)
- VM operations trigger immediate status updates (no waiting for sync)
- Background IP discovery after VM start (guest agent + ARP scan)
- No timeout storms from slow Proxmox API calls

See `DATABASE_FIRST_ARCHITECTURE.md` and `TEMPLATE_SYNC_ARCHITECTURE.md` for detailed design documentation.

### VM Deployment Strategy

**Disk export + overlay approach** for class-based labs (NEVER uses `qm clone`):
- **Workflow with template**:
  1. Export template disk: `qemu-img convert -O qcow2` creates base QCOW2 file from template
  2. Teacher VM: Empty shell (`qm create`) + copy-on-write overlay pointing to base QCOW2
  3. Class-base VM: Reference QCOW2 file used as backing file for student overlays
  4. Student VMs: `qemu-img create -f qcow2 -F qcow2 -b <base>` creates copy-on-write overlays

- **Workflow without template**:
  - Teacher/class-base/student VMs: `qm create --scsi0 STORAGE:SIZE` creates empty shells with disks

**Why disk export + overlays (not qm clone)**:
- **100x faster**: Instant disk creation vs 2 minutes per full copy
- **No locking**: Template never locked, all VMs created in parallel
- **Storage efficient**: 25 VMs share 20GB base, overlays start at ~200KB
- **Complete isolation**: Each VM has independent overlay file, copy-on-write ensures changes are VM-specific
- **Safe**: Base template is read-only, cannot be deleted while overlays exist

**Requirements**:
- SSH access to Proxmox nodes (configured via environment variables)
- QCOW2-compatible storage backend (e.g., NFS, local)
- `paramiko` Python library for SSH connections

**Deployment Time**: 25 student VMs deployed in <20 seconds (vs 54 minutes with traditional cloning)

See `app/services/class_vm_service.py` for implementation details.

### Multi-Cluster & Template Management

**Cluster Selection**:
- UI dropdown allows switching between configured clusters
- Templates automatically filter by selected cluster (database query)
- Nodes dynamically load based on selected cluster
- Templates from one cluster cannot be used on another cluster

**Template Sync Daemon**:
- Scans all clusters for templates (QEMU VMs with `template=1`)
- Caches complete specs in database: CPU, RAM, disk, OS type, network bridge
- Syncs every 30 minutes (full), every 5 minutes (verification)
- UI loads templates instantly from database (<100ms)

**Single-Node Deployment Override**:
- Optional: Deploy entire class to specific node (bypasses load balancing)
- Useful for testing, resource isolation, or maintenance scenarios
- Configured per-class via UI dropdown

**Template Publishing Workflow**:
- Teacher makes changes to their VM
- Publishes changes → updates class base template
- System recreates student VMs with fresh overlays from updated base
- Long-running background operation with progress tracking

See `TEMPLATE_SYNC_ARCHITECTURE.md` for detailed template sync design.

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
    template_sync.py           # Background daemon for Template sync
    inventory_service.py       # VMInventory database operations
    proxmox_client.py          # Proxmox API integration, VM operations
    proxmox_service.py         # Multi-cluster connection manager
    class_vm_service.py        # Class VM deployment workflows (disk export + overlay)
    class_service.py           # Class CRUD, VM pool management, invites
    cluster_manager.py         # Cluster config persistence
    user_manager.py            # Authentication, admin detection
    arp_scanner.py             # Network scanning for IP discovery
    rdp_service.py             # RDP file generation
    ssh_service.py             # SSH WebSocket handler
    ssh_executor.py            # SSH command execution helper
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
2. **Template sync**: Background daemon automatically syncs templates from all clusters to database
   - Full sync every 30 minutes (fetches all specs)
   - Quick verification every 5 minutes
   - Manual trigger: `POST /api/sync/templates/trigger`
3. **Create class**: Teacher creates class at `/classes/create`
   - Select deployment cluster (determines available templates)
   - Select template from cluster-specific list
   - Optionally select single node for deployment (overrides load balancing)
   - Set pool size (number of student VMs)
4. **Deploy VMs**: System automatically deploys VMs using disk export + overlay strategy
   - Teacher VM: Empty shell + overlay from exported template disk
   - Class base: Reference QCOW2 used as backing file for student overlays
   - Student VMs: Copy-on-write overlays referencing class base
   - **Deployment time**: <20 seconds for 25 students
   - Progress tracking: Real-time updates with percentage and current VM
5. **Generate invite**: Teacher generates time-limited (7-day default) or never-expiring invite link
6. **Student enrollment**: Students visit invite link, auto-assigned first available VM (creates VMAssignment record)
7. **Lab usage**: Students see assigned VM in `/portal`, can start/stop/revert to baseline snapshot
   - Portal loads in <100ms (reads from VMInventory database)
   - Start/stop operations update database immediately
   - Background sync discovers IP address 15 seconds after VM start
8. **Template publishing** (optional): Teacher can publish changes from their VM → class base template → all student VMs
   - Recreates student VMs with fresh overlays from updated base
   - Long-running background operation with progress tracking
9. **Co-owner management** (optional): Teacher can add co-owners via `/api/classes/<id>/co-owners` for shared management

**Note**: All users (teachers and students) are local database accounts. Only admins need Proxmox accounts.

## Database

SQLite database at `app/lab_portal.db` (auto-created on first run):

- **users**: Local accounts (adminer/teacher/user roles), password hashing
- **classes**: Lab classes with invite tokens, template references, deployment cluster/node overrides
- **templates**: Proxmox template references with cached specs (CPU/RAM/disk/network/OS), synced by background daemon
- **vm_assignments**: VM-to-user mappings (class-based or direct), includes VM name and MAC address
- **vm_inventory**: **Cached VM data** (status, IP, resources) updated by background sync daemon
- **class_co_owners**: Many-to-many relationship for class co-ownership
- **class_enrollments**: Many-to-many relationship for student class enrollment

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
- `GET /api/classes/templates?cluster_id={id}` - List templates for cluster (from database)
- `GET /api/templates` - List all registered templates with cached specs
- `GET /api/templates/by-name/<name>` - Look up template by name
- `GET /api/templates/nodes` - Get nodes where template exists
- `GET /api/templates/stats` - Template usage statistics
- `POST /api/sync/templates/trigger` - Manually trigger template sync (admin only)
- `GET /api/sync/templates/status` - Template database statistics
- `POST /api/classes/<id>/publish-template` - Publish teacher VM changes to all students

### VM Assignments
- `GET /api/user-vm-assignments` - List direct VM assignments (no class)
- `POST /api/user-vm-assignments` - Create direct assignment
- `DELETE /api/user-vm-assignments/<id>` - Remove assignment
- `GET /api/vm-class-mappings` - List VM-to-class mappings (admin only)
- `POST /api/vm-class-mappings` - Bulk assign VMs to class

### System Operations
- `GET /api/sync/status` - VMInventory sync daemon status
- `POST /api/sync/trigger` - Manually trigger full inventory sync (admin only)
- `GET /api/sync/inventory/summary` - VMInventory table statistics
- `POST /api/sync/templates/trigger` - Manually trigger template sync (admin only)
- `GET /api/sync/templates/status` - Template database statistics
- `POST /api/clusters/switch` - Change active cluster
- `GET /api/clusters` - List all configured clusters
- `GET /api/clusters/{id}/nodes` - Get nodes for specific cluster
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
- **VMInventory cache**: All VM data stored in database, updated by background daemon
  - Query speed: <100ms vs 5-30 seconds for Proxmox API
  - Full sync every 5 minutes, quick sync every 30 seconds
  - Immediate updates after VM start/stop operations
  
- **Template cache**: All template data stored in database, updated by background daemon
  - Query speed: <100ms vs 5-30 seconds for Proxmox API
  - Full sync every 30 minutes (with specs), quick verification every 5 minutes
  - Cluster filtering at database level (instant cluster switching)

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
- Check sync intervals in `.env` (default: 5min full, 30sec quick)

**Templates not appearing**:
- Check template sync status: `GET /api/sync/templates/status`
- Manually trigger template sync: `POST /api/sync/templates/trigger`
- Verify templates in database: `SELECT * FROM templates`
- Check cluster filter: Templates tied to specific cluster_ip

**Cluster switching shows wrong templates**:
- Restart app to reload template sync daemon
- Clear browser cache (templates may be cached client-side)
- Manually trigger template sync: `POST /api/sync/templates/trigger`
- Verify cluster_ip in templates table matches cluster host

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

**Deployment node/cluster override not working**:
- Verify `deployment_cluster` and `deployment_node` columns exist in classes table
- Run migration: `python3 migrate_db.py`
- Check class record in database: `SELECT deployment_cluster, deployment_node FROM classes WHERE id = <class_id>`
- Review logs for "Deploying to specific cluster" or "Deploying to specific node" messages

**Background sync not running**:
- Check logs for "Starting background inventory sync daemon" and "Background template sync started" messages
- Verify no errors on app startup
- Manually trigger: `POST /api/sync/trigger` (VMs) or `POST /api/sync/templates/trigger` (templates)
- Check sync thread status: `GET /api/sync/status`

**Database errors**:
- Run migrations: `python3 migrate_db.py`
- Check for missing columns in VMInventory, Template, or Classes tables
- Repair inventory: `python3 fix_vminventory.py`
- Debug inventory: `python3 debug_vminventory.py`

**Template spec caching**:
- Templates auto-sync every 30 minutes (background daemon)
- Manual sync: `POST /api/sync/templates/trigger`
- Verify specs cached: `GET /api/sync/templates/status`
- Check database: `SELECT cpu_cores, memory_mb, os_type FROM templates WHERE proxmox_vmid = <vmid>`

## Development

**Manual testing workflow** (no automated tests):
1. Start app: `python3 run.py`
2. Login as admin (Proxmox credentials) or local user
3. Verify VMs appear in `/portal` (should load in <1 second from database)
4. Test start/stop buttons (check immediate status update)
5. Download RDP file for Windows VM (should be fast with ping verification)
6. Admin: verify `/admin/diagnostics`, `/admin/users`, `/admin/vm-class-mappings` accessible
7. Teacher: create class with cluster/node selection, verify deployment completes in <20 seconds for 25 VMs
8. Student: join class, verify VM assignment, test start/stop
9. Test cluster switching: Select different cluster, verify templates reload instantly

**Database inspection**:
```bash
# SQLite CLI
sqlite3 app/lab_portal.db
sqlite> .tables
sqlite> SELECT * FROM vm_inventory LIMIT 5;
sqlite> SELECT * FROM templates;
sqlite> SELECT name, deployment_cluster, deployment_node FROM classes;

# Python shell
python3
>>> from app import create_app
>>> from app.models import db, VMInventory, Template, Class
>>> app = create_app()
>>> with app.app_context():
...     print(f"VMs: {VMInventory.query.count()}")
...     print(f"Templates: {Template.query.count()}")
...     print(Template.query.all())
```

**Performance profiling**:
- VMInventory queries: Should be <100ms
- Template queries: Should be <100ms
- Proxmox API calls: 5-30 seconds (avoid in request handlers)
- RDP download: 1-2 seconds with fast verification
- Class deployment: <20 seconds for 25 VMs with disk overlay strategy
- Cluster switching: <100ms (templates loaded from database)

**Code organization**:
- Database operations → `app/services/inventory_service.py`
- Proxmox API calls → `app/services/proxmox_service.py`
- VM deployment → `app/services/class_vm_service.py` (uses SSH executor)
- Template sync → `app/services/template_sync.py`
- Background tasks → `app/services/background_sync.py`, `app/services/template_sync.py`
- API routes → `app/routes/api/*.py`
- Admin UI → `app/routes/admin/*.py`

## Additional Documentation

- `DATABASE_FIRST_ARCHITECTURE.md` - Detailed VMInventory and background sync design
- `TEMPLATE_SYNC_ARCHITECTURE.md` - Detailed template sync daemon and database-first design
- `CLASS_CO_OWNERS.md` - Class co-ownership feature documentation
- `.github/copilot-instructions.md` - Comprehensive architecture guide for developers

## Contributing

This is an internal lab management tool. For architecture questions or feature requests, see the Copilot instructions.

## License

MIT
