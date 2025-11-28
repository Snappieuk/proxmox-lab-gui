# Proxmox Lab GUI
Flask webapp providing a lab VM portal similar to Azure Labs for self-service student VM access.

## Features

- **Multi-cluster support**: Connect to multiple Proxmox clusters, switch between them in the UI
- **Class-based management**: Teachers create classes, clone VMs from templates, generate invite links
- **Student self-service**: Students join classes via invite, get auto-assigned VMs, start/stop/revert
- **Legacy mappings**: Admin-managed user→VM assignments via JSON file (backwards compatible)
- **Progressive loading**: Fast page renders with async VM data fetching
- **IP discovery**: Multi-tier system (LXC interfaces → QEMU guest agent → ARP scanning)
- **RDP downloads**: Generate .rdp files for Windows VM connections
- **SSH terminal**: Web-based SSH terminal (optional, requires flask-sock)

## Quick Start

### Development Setup

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

# Admin group (Proxmox group whose members are admins)
ADMIN_GROUP=adminers

# Performance tuning
VM_CACHE_TTL=300  # seconds (5min)
ENABLE_IP_LOOKUP=True

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

## Project Structure

```
app/
  config.py           # Configuration (env vars, cluster list)
  models.py           # SQLAlchemy ORM (User, Class, Template, VMAssignment)
  __init__.py         # Flask app factory
  routes/
    auth.py           # Login, logout, registration
    portal.py         # Main VM dashboard
    classes.py        # Class management UI
    admin/            # Admin-only pages (mappings, diagnostics, clusters)
    api/              # REST API (VMs, RDP, SSH, mappings, classes)
  services/
    proxmox_client.py # Proxmox API integration, VM operations
    proxmox_service.py# Multi-cluster connection manager
    class_service.py  # Class CRUD, VM pool management, invites
    cluster_manager.py# Cluster config persistence
    user_manager.py   # Authentication, admin detection
    arp_scanner.py    # Network scanning for IP discovery
    rdp_service.py    # RDP file generation
    ssh_service.py    # SSH WebSocket handler
  templates/          # Jinja2 templates
  static/             # CSS, JavaScript
  utils/              # Logging, decorators, caching
run.py                # Application entry point
start.sh              # Start script (activates venv, runs Flask)
deploy.sh             # Deployment script (git pull, restart)
proxmox-gui.service   # Systemd service unit
```

## Class-Based Lab Workflow

1. **Admin setup**: Create local teacher accounts via DB or API
2. **Register templates**: Admin/teacher registers Proxmox templates in system
3. **Create class**: Teacher creates class, selects template, sets VM pool size
4. **Generate invite**: Teacher generates time-limited or never-expiring invite link
5. **Clone VMs**: Teacher adds VMs to pool (clones template N times)
6. **Student enrollment**: Students visit invite link, auto-assigned first available VM
7. **Lab usage**: Students start/stop VMs, revert to baseline snapshot

## Database

SQLite database at `app/lab_portal.db` (auto-created on first run):

- **users**: Local accounts (adminer/teacher/user roles)
- **classes**: Lab classes with invite tokens
- **templates**: Proxmox template references
- **vm_assignments**: Cloned VMs assigned to classes/users

## API Endpoints

- `GET /api/vms` - Fetch VM list for current user
- `POST /api/vm/<vmid>/start` - Start VM
- `POST /api/vm/<vmid>/stop` - Stop VM
- `GET /rdp/<vmid>.rdp` - Download RDP file
- `GET /api/mappings` - User-VM mappings (admin only)
- `GET /api/classes` - Class list
- `POST /api/class/<id>/pool` - Add VMs to class pool
- `POST /api/clusters/switch` - Change active cluster

## Authentication

Two authentication systems run in parallel:

1. **Proxmox auth**: Users login with Proxmox credentials, session stores `user@pve`
2. **Local auth**: SQLite users with roles for class system

Admin detection: user in `ADMIN_USERS` list OR member of `ADMIN_GROUP` OR local role='adminer'

## Caching

Three-tier caching for performance:

1. **VM list cache**: 5min TTL, invalidated on start/stop
2. **IP lookup cache**: 5min TTL, separate to avoid guest agent timeouts
3. **ARP scanner cache**: 1hr TTL, background thread updates

## Troubleshooting

**Check health**: Visit `/health` endpoint (returns `{"status": "ok"}`)

**View logs**:
- Systemd: `sudo journalctl -u proxmox-gui -f`
- Standalone: `tail -f /tmp/flask.log`

**Admin access issues**:
- Verify user in `ADMIN_USERS` or `ADMIN_GROUP`
- Check logs for "inject_admin_flag" entries

**IP discovery issues**:
- LXC: Works when stopped, uses `.interfaces.get()`
- QEMU: Requires running VM + guest agent installed
- ARP scanner: Requires nmap, root/CAP_NET_RAW for ARP probes

**Cache debugging**:
- Cache invalidated automatically on VM start/stop
- Manual: restart service or call `_invalidate_vm_cache()` in code

## Development

**No automated tests** - manual testing workflow:
1. Start app: `python3 run.py`
2. Login with Proxmox user
3. Verify VMs appear in `/portal`
4. Test start/stop buttons
5. Download RDP file for Windows VM
6. Admin: verify `/admin` and `/admin/mappings` accessible

**Database migrations** (no formal tool):
- Edit `app/models.py`
- In Python shell: `from app import create_app; from app.models import db; app = create_app(); app.app_context().__enter__(); db.drop_all(); db.create_all()`
- Production: manually ALTER TABLE or export/import data

## License

MIT
