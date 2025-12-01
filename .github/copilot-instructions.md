# Copilot Instructions for proxmox-lab-gui

## What this repo is
A Flask webapp providing a lab VM portal similar to Azure Labs for self-service student VM access.

**Architecture**: Modular blueprint-based Flask application with all code under `app/` directory. Uses SQLite for class management, Proxmox API for VM control, VMInventory database-first caching, and Terraform for VM deployment.

**Entry point**: `run.py` - imports `create_app()` factory from `app/` package and runs Flask with threaded mode.

**Deployment model**: This code is NOT run locally or in a codespace. It is pushed to a remote Proxmox server (typically via git) and runs there as a systemd service or standalone Flask process. The server has direct network access to Proxmox clusters.

**Performance architecture**: Database-first design with background sync daemon. All VM reads (<100ms) come from VMInventory table, never directly from slow Proxmox API (5-30s). Background sync updates database every 5min (full) and 30sec (quick status).

## Current state & architecture decisions (READ THIS FIRST!)
- **VMInventory + background_sync system**: **Core architecture** - database-first design where all VM reads come from `VMInventory` table (see `DATABASE_FIRST_ARCHITECTURE.md`). Background sync daemon updates DB every 5min (full) and 30sec (quick). Provides 100x faster page loads vs direct Proxmox API calls. Code in `app/__init__.py` (lines 137-139), `app/routes/api/vms.py`, `app/services/inventory_service.py`, `app/services/background_sync.py`.
- **Progressive UI loading**: Frontend shows "Starting..." badge for VMs that are running but have no IP yet. Only transitions to "Running" after IP is discovered. Polls at 2s and 10s intervals after VM start, background sync triggers IP discovery 15s after start operation.
- **IP discovery workflow**: Multi-tier hierarchy - VMInventory DB cache → Proxmox guest agent `network-get-interfaces` API → LXC `.interfaces.get()` → ARP scanner with nmap → RDP port 3389 validation. Background thread triggers immediate IP sync 15 seconds after VM start (allows boot time before network check).
- **Terraform VM deployment**: Primary deployment method (see `terraform/README.md`) replaces legacy Python cloning. Provides 10x speed improvement, parallel deployment, automatic VMID allocation, idempotency. Located at `app/services/terraform_service.py`.
- **Legacy Python cloning**: Old `clone_vms_for_class()` function (~800 lines in `app/services/proxmox_operations.py`) exists but is NOT used - Terraform handles all VM deployment. May be removed in future cleanup.
- **Mappings.json system**: **DEPRECATED - Should be migrated to database**. Currently reads `app/mappings.json` to map Proxmox users → VMIDs. This should be replaced with database-backed VMAssignment records. The User model already has `vm_assignments` relationship - mappings.json is redundant.
- **Authentication model**: **Proxmox users = Admins only**. Going forward, no application users (students/teachers) should have Proxmox accounts. All non-admin users managed via SQLite database.

## Architecture patterns
**Dual system coexistence** (user access - MIGRATION IN PROGRESS):
- **Legacy mappings.json system**: **DEPRECATED** - Admins manually map Proxmox users → VMIDs in JSON file at `app/mappings.json`. Read by `_resolve_user_owned_vmids()` function. Should be migrated to VMAssignment table in database.
- **Class-based system**: Teachers create classes linked to Proxmox templates, VMs auto-cloned and assigned to students via invite links. SQLite-backed (`app/lab_portal.db`). Uses VMAssignment table.
- **Future state**: Only class-based system. All VM→user mappings in database via VMAssignment records. No JSON files. Function `_resolve_user_owned_vmids()` should only query database.
- Both systems query same Proxmox clusters via `app/services/proxmox_client.py` and render same templates. User sees combined VM list (mappings.json + class assignments). Function `_resolve_user_owned_vmids()` merges both sources.

**Multi-cluster support** (`app/config.py` + `app/services/cluster_manager.py`):
- `CLUSTERS` list defines multiple Proxmox clusters (can load from `clusters.json` for persistence)
- Session stores `cluster_id` – user switches clusters via dropdown in UI
- Each cluster gets separate `ProxmoxAPI` connection (cached in `_proxmox_connections` dict)
- `get_proxmox_admin()` returns connection for current session cluster
- **Key quirk**: `get_proxmox_admin_for_cluster(id)` bypasses session (used in parallel fetches)

**Progressive loading pattern** (avoids slow page renders):
- Routes like `/portal` render skeleton HTML with empty VM cards
- JavaScript immediately calls `/api/vms` to fetch data and populate cards client-side
- Prevents timeout on initial page load while Proxmox API is queried (can take 30s+ with many VMs)
- See `templates/index.html` for fetch pattern, `app/routes/api/vms.py` for API

**Database-first caching architecture** (see `DATABASE_FIRST_ARCHITECTURE.md`):
1. **VMInventory table** (`app/models.py`): Single source of truth for all VM data shown in GUI. Stores identity, status, network, resources, metadata, and sync tracking.
2. **Background sync daemon** (`app/services/background_sync.py`): Auto-starts on app init, keeps VMInventory synchronized with Proxmox. Full sync every 5min, quick sync every 30sec for running VMs.
3. **Inventory service** (`app/services/inventory_service.py`): Database operations layer - `persist_vm_inventory()`, `fetch_vm_inventory()`, `update_vm_status()`.
4. **Performance benefit**: Database queries <100ms vs Proxmox API 5-30 seconds = **100x faster page loads**. GUI never blocked by slow Proxmox API.
5. **VM list cache** (legacy fallback in `app/services/proxmox_client.py`): Module-level `_vm_cache_data` dict with 5min TTL. Used only when VMInventory unavailable.
6. **IP lookup cache** (`_ip_cache` in `app/services/proxmox_client.py`): Separate 5min TTL for guest agent IP queries – **critical** to avoid timeout storms (guest agent calls are SLOW).

**IP discovery hierarchy** (most complex subsystem):
1. **VMInventory database cache**: Check database first (updated by background sync every 30s for running VMs)
2. **Proxmox guest agent**: For QEMU VMs, call `proxmox.nodes(node).qemu(vmid).agent('network-get-interfaces').get()` (requires running VM + guest agent installed)
3. **LXC interfaces API**: For containers, fast via `.interfaces.get()` API (works even when stopped)
4. **ARP scanner fallback** (`app/services/arp_scanner.py`): Uses `nmap -sn -PR` (requires root/CAP_NET_RAW) to populate kernel ARP table, matches VM MAC → IP. Cached 1hr, runs in background thread.
5. **RDP port scan**: Optional validation that discovered IP has port 3389 open (Windows VMs + Linux with xrdp)
6. **Background trigger on VM start**: After starting a VM, daemon thread waits 15 seconds (boot time) then triggers immediate background sync to discover IP address

**Note**: IP discovery timing is critical for UX. After VM start: immediate database status update → 15s delay → background sync (guest agent + ARP scan) → 2s and 10s UI polls pick up discovered IP.

**Authentication & authorization** (simplified model):
- **Proxmox auth**: Any user who can authenticate via Proxmox API is automatically considered an **admin**. No student/teacher accounts should be created in Proxmox.
- **Local auth** (`app/services/class_service.py`): SQLite users with password hashing (Werkzeug) for all non-admin users. Three roles: `adminer` (full admin), `teacher` (create/manage classes), `user` (student).
- Login flow: Try Proxmox auth first (grants admin), then local auth fallback (grants role from database).
- Admin detection: `is_admin_user(userid)` checks (a) successful Proxmox authentication OR (b) local user role == 'adminer'.
- **Design principle**: Going forward, NO users created in the application should have accounts in Proxmox. Proxmox is admin-only.

**Blueprint-based routing** (`app/routes/__init__.py`):
```
auth.py          → /login, /register, /logout
portal.py        → /portal (main VM dashboard)
classes.py       → /classes, /classes/create, /classes/join/<token>
admin/
  mappings.py    → /admin/mappings (legacy JSON editor)
  clusters.py    → /admin/clusters (add/edit Proxmox clusters)
  diagnostics.py → /admin (probe nodes, view system info)
  users.py       → /admin/users (manage local users/roles)
api/
  vms.py         → /api/vms (fetch VM list), /api/vm/<vmid>/start|stop|status|ip
  classes.py     → /api/classes (CRUD), /api/class/<id>/pool, /api/class/<id>/assign
  rdp.py         → /rdp/<vmid>.rdp (download RDP file)
  ssh.py         → /ssh/<vmid> (WebSocket terminal, requires flask-sock)
  mappings.py    → /api/mappings (CRUD for mappings.json)
  clusters.py    → /api/clusters/switch (change active cluster)
  templates.py   → /api/templates (list templates, by-name lookup, node distribution, stats)
  sync.py        → /api/sync/status|trigger|inventory/summary (VMInventory sync control)
```


## Key files

**`run.py`** (Application entry point, ~30 lines):
- Imports `create_app` factory from `app/` package, runs Flask app with `threaded=True`
- Disables SSL warnings if `PVE_VERIFY=False` (before any imports)

**`app/__init__.py`** (Application factory, ~110 lines):
- `create_app()` function: configures Flask, initializes SQLAlchemy (`models.init_db()`), registers blueprints
- WebSocket support: conditionally imports `flask-sock` (gracefully degrades if missing)
- Context processor: injects `is_admin`, `clusters`, `current_cluster`, `local_user` into all templates
- Templates/static served from `app/templates/` and `app/static/` directories

**`app/models.py`** (SQLAlchemy ORM, ~300 lines):
- `User`: Local accounts with roles (`adminer`/`teacher`/`user`), password hashing via Werkzeug
- `Class`: Lab classes created by teachers, linked to `Template`, has `join_token` with expiry
- `Template`: References Proxmox template VMIDs, can be cluster-wide or class-specific
- `VMAssignment`: Tracks cloned VMs assigned to classes/users, status field (`available`/`assigned`/`deleting`)
- `VMInventory`: **Database-first cache** - stores all VM metadata (identity, status, network, resources). Updated by background sync daemon. Single source of truth for GUI.
- `init_db(app)`: Sets up SQLite at `app/lab_portal.db`, creates tables on first run

**`app/services/proxmox_operations.py`** (VM cloning & template operations, ~410 lines):
- Template management: `list_proxmox_templates()` (scans clusters for template VMs)
- **VM cloning (UNUSED)**: `clone_vm_from_template()`, `clone_vms_for_class()` – replaced by Terraform, not imported anywhere
- VM lifecycle: `start_class_vm()`, `stop_class_vm()`, `delete_vm()`, `get_vm_status()`
- Snapshot operations: `create_vm_snapshot()`, `revert_vm_to_snapshot()` – still used
- Template conversion: `convert_vm_to_template()` (converts VM → template)
- **Template replication**: `replicate_templates_to_all_nodes()` – still useful for multi-node clusters
- Multi-cluster aware: Takes `cluster_ip` param, looks up cluster by IP in `CLUSTERS` config

**`app/services/terraform_service.py`** (NEW - Terraform-based VM deployment, ~480 lines):
- **Primary deployment method**: `deploy_class_vms()` – deploys teacher + student VMs in parallel using Terraform
- **Node selection**: `_get_best_nodes()` – queries Proxmox for least-loaded nodes
- **Workspace isolation**: Each class gets isolated Terraform workspace in `terraform/classes/<class_id>/`
- **State management**: `get_class_vms()` reads Terraform state, `destroy_class_vms()` removes all VMs
- **Idempotent**: Safe to re-run deployment (creates only missing VMs)
- **Benefits**: 10x faster than Python cloning, automatic VMID allocation, built-in error handling

**`app/services/class_service.py`** (Class management logic, ~560 lines):
- User CRUD: `create_local_user()`, `authenticate_local_user()`, `update_user_role()`
- Class CRUD: `create_class()`, `update_class()`, `delete_class()`, `get_classes_for_teacher()`
- VM pool management: `add_vms_to_class_pool()` (clones template), `remove_vm_from_class()` (deletes VM)
- Student enrollment: `join_class_via_token()` (auto-assigns unassigned VM), `generate_class_invite()`
- Template registration: `register_template()` (scans Proxmox for templates, saves to DB)

**`app/services/proxmox_client.py`** (Proxmox integration, ~1800 lines):
- **Singletons**: `proxmox_admin` (legacy single cluster), `proxmox_admin_wrapper` (cache adapter)
- **Core VM functions**: `get_all_vms()` (fetch all VMs, applies mappings.json filter), `get_vms_for_user()` (user-specific view), `find_vm_for_user()` (permission check)
- **VM operations**: `start_vm(vm)`, `shutdown_vm(vm)` – handle both QEMU and LXC, invalidate cache after operation
- **IP discovery**: `_lookup_vm_ip(node, vmid, vmtype)` – tries Proxmox guest agent `network-get-interfaces` API (QEMU) or `.interfaces.get()` (LXC), falls back to ARP scan
- **RDP generation**: `build_rdp(vm)` – raises `ValueError` if VM has no IP (caller must catch)
- **Admin operations**: `create_pve_user()`, `is_admin_user()`, `_user_in_group()` (handles dual membership format quirk)
- **Caching**: Module-level `_vm_cache_data`, `_ip_cache` dicts with `_invalidate_vm_cache()` function

**`app/services/proxmox_service.py`** (Multi-cluster proxy, ~180 lines):
- `get_proxmox_admin()`: Returns `ProxmoxAPI` instance for current session cluster (lazy init + thread-safe)
- `get_proxmox_admin_for_cluster(cluster_id)`: Returns connection for specific cluster (no session dependency)
- `_proxmox_connections` dict: Caches one `ProxmoxAPI` per cluster (cleared on cluster config change)
- `get_current_cluster()`: Reads `session["cluster_id"]` or defaults to first cluster in `CLUSTERS` list

**`app/services/cluster_manager.py`** (~80 lines):
- `save_cluster_config(clusters)`: Persists to `clusters.json`, updates runtime `CLUSTERS` list, clears connections
- `invalidate_cluster_cache()`: Clears short-lived cluster resources cache (not VM cache, confusingly named)

**`app/config.py`** (Configuration, ~105 lines):
- **Multi-cluster**: `CLUSTERS` list (id, name, host, user, password, verify_ssl) – loads from `clusters.json` if exists
- **Legacy single-cluster**: `PVE_HOST`, `PVE_ADMIN_USER`, `PVE_ADMIN_PASS` (still used as defaults)
- **Admin privileges**: `ADMIN_USERS` (comma-separated), `ADMIN_GROUP` (Proxmox group name, default "adminers")
- **Performance**: `VM_CACHE_TTL` (300s), `PROXMOX_CACHE_TTL` (30s), `ENABLE_IP_LOOKUP` (bool), `ENABLE_IP_PERSISTENCE` (slow, disabled by default)
- **Persistence files**: `MAPPINGS_FILE` (JSON), `CLUSTER_CONFIG_FILE` (JSON), `IP_CACHE_FILE` (JSON), `VM_CACHE_FILE` (JSON)
- **ARP scanner**: `ARP_SUBNETS` (broadcast addresses, default `["10.220.15.255"]` for 10.220.8.0/21 network)
- **Template replication**: `ENABLE_TEMPLATE_REPLICATION` (bool, default False) – auto-replicate templates at startup

**`app/services/arp_scanner.py`** (ARP-based IP discovery, ~740 lines):
- `discover_ips_via_arp(vm_mac_map, subnets, background=True)`: Runs nmap ARP probe, matches MAC→IP
- `get_arp_table()`: Parses `ip neigh` or `/proc/net/arp` output, returns `{mac: ip}` dict
- `check_rdp_ports(ips, timeout=1)`: Parallel TCP port 3389 check (validates Windows VM connectivity)
- **Background mode**: Spawns thread, returns cached results immediately (avoids blocking API responses)
- **Root requirement**: `nmap -sn -PR` (ARP ping) needs root or `CAP_NET_RAW` capability. Falls back to ICMP ping without root.
- **Cache**: `_arp_cache` (1hr TTL), `_rdp_hosts_cache` (RDP-enabled IPs), invalidated on VM operations

**`app/utils/caching.py`** (Cache utilities, ~170 lines):
- `ThreadSafeCache`: In-memory dict with TTL, thread lock, `get()`/`set()`/`invalidate()` methods
- `PersistentCache`: JSON-backed cache with same interface, survives restarts, used for VM/IP persistence

**Production deployment files**:
- `start.sh` – Activates venv, loads `.env`, runs `python3 run.py`
- `deploy.sh` – Git pull, pip install, restart systemd service or standalone Flask
- `restart.sh` – Quick restart (no git pull or pip install)
- `clear_cache.sh` – Clears VM cache files
- `proxmox-gui.service` – Systemd unit (edit User/Group/paths, then `systemctl enable/start`)
- **Terraform config** (`terraform/` directory - see `terraform/README.md`):
  - `main.tf` – VM resource definitions (QEMU VMs with linked/full clones)
  - `variables.tf` – Input variables (cluster connection, VM specs, node list)
  - `terraform.tfvars.example` – Example configuration
  - `classes/` – Per-class Terraform workspaces (isolated state)
  
**Utility scripts**:
- `debug_vminventory.py` – Query VMInventory table directly (for debugging sync issues)
- `fix_vminventory.py` – Repair VMInventory table (remove stale records, fix sync errors)


## Developer workflows

**Local dev setup**:
```bash
# WARNING: This is for testing code only, NOT for production use
# The app needs to run on a server with network access to Proxmox clusters
cd /workspaces/proxmox-lab-gui
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env  # Edit with your Proxmox credentials
python3 run.py  # Runs on http://0.0.0.0:8080
```

**Production deployment**:
```bash
# On the remote Proxmox server (not local machine):

# Quick start with Flask
./start.sh  # Activates venv, loads .env, runs run.py with threaded mode

# Or via systemd (recommended)
sudo cp proxmox-gui.service /etc/systemd/system/
# Edit service file: update User, Group, and all /path/to/proxmox-lab-gui
sudo systemctl daemon-reload
sudo systemctl enable --now proxmox-gui
sudo systemctl status proxmox-gui

# Deploy after git pull (on remote server)
./deploy.sh  # Auto-detects systemd or standalone Flask, restarts
```

**Development workflow**:
```bash
# Local development (testing only - won't connect to real Proxmox):
cd /workspaces/proxmox-lab-gui
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env  # Edit with your Proxmox credentials
python3 run.py  # Runs on http://0.0.0.0:8080

# Push to remote server for actual deployment:
git add . && git commit -m "Changes" && git push
# SSH to remote server, git pull, and restart service
```

**Class-based lab workflow**:
1. **Initial setup**: Admin creates local teacher accounts via `/admin/users` UI or `create_local_user(username, password, role='teacher')`
2. **Register templates**: Teacher visits `/admin/probe` to see available templates, registers them via API or DB
3. **Create class**: Teacher creates class at `/classes/create`, selects template, sets pool size
4. **Generate invite**: Teacher generates never-expiring or time-limited invite link (7-day default)
5. **Clone VMs**: Teacher adds VMs to pool via Terraform (deploys teacher + student VMs in parallel)
6. **Student enrollment**: Students visit invite link, auto-assigned first available VM in class pool (creates VMAssignment record)
7. **Student access**: Student sees assigned VM in `/portal`, can start/stop/revert to baseline snapshot
**Note**: All users (teachers and students) are local database accounts. No Proxmox accounts needed except for admins.

**Database migrations** (no formal tool, manual process):
- Schema changes: edit `app/models.py`, then in Python shell:
  ```python
  from app import create_app
  from app.models import db
  app = create_app()
  with app.app_context():
      db.drop_all()  # WARNING: destroys data!
      db.create_all()
  ```
- For production: manually ALTER TABLE or export/import data

**Debugging tips**:
- Check `/health` endpoint – returns `{"status": "ok"}` if app is running
- Use `/admin` (admin only) – shows Proxmox node summary, resource counts, API latency
- Run `python3 -c "from app.services import proxmox_client; print(proxmox_client.probe_proxmox())"` – Test Proxmox connectivity
- Session debugging: `app.logger.info()` logs to stdout/stderr (visible in `journalctl -u proxmox-gui -f`)
- Admin access issues: verify `is_admin_user(userid)` returns True, check logs for "inject_admin_flag" entries
- Cache debugging: check `_vm_cache_data` age, manually call `_invalidate_vm_cache()` if stale
- ARP scanner: verify nmap installed, run with `sudo` for ARP probe access, check `_arp_cache` status

**Testing without Proxmox** (no test suite exists):
- Create mock `ProxmoxAPI` class with methods: `version.get()`, `nodes().qemu().status.current.get()`, etc.
- Swap `proxmox_admin` in `app/services/proxmox_client.py` or inject mock into `app/services/proxmox_service.py`
- Return fixture VM dicts: `{"vmid": 100, "name": "test-vm", "status": "running", "node": "mock1", "type": "qemu"}`

**Environment config priority**:
1. Export env vars: `export PVE_HOST=10.220.15.249`
2. Or create `.env` file (gitignored): `PVE_HOST=10.220.15.249`
3. Or edit `clusters.json` for multi-cluster persistence
4. Fallback to hardcoded defaults in `app/config.py` (insecure for prod!)

**No automated tests** – manual testing workflow:
- Start app, login with Proxmox user, verify VMs appear in `/portal`
- Test start/stop buttons, check Proxmox UI for VM status changes
- Download RDP file for Windows VM, verify connection works
- Admin: login as admin user, verify `/admin` and `/admin/mappings` accessible
- Classes: create local account, create class as teacher, generate invite, join as student


## Security & secrets
- `config.py` has hardcoded defaults for local dev (10.220.15.249, password!)
- **NEVER commit production credentials** - use environment variables
- Sessions use `SECRET_KEY` from config (change in production)
- Proxmox API calls use SSL verification controlled by `PVE_VERIFY` (default False for dev)

## Critical gotchas

**Dual caching layers** (easy to confuse):
- `ProxmoxCache` class in `cache.py` – used minimally, mostly for app startup warmup
- Module-level `_vm_cache_data` dict in `app/services/proxmox_client.py` – **primary cache** for all `/api/vms` calls
- IP lookups have separate `_ip_cache` (5min TTL) to avoid guest agent timeout storms
- Cache invalidation: `start_vm()` and `shutdown_vm()` call `_invalidate_vm_cache()` to clear module-level cache

**Admin group membership API quirks** (DEPRECATED):
- Legacy code checks `ADMIN_USERS` list and `ADMIN_GROUP` Proxmox group membership
- **Going forward**: Any Proxmox authentication = admin. Group membership checks unnecessary.
- Set `ADMIN_GROUP=None` to disable (or remove this logic entirely)

**LXC vs QEMU differences** (both exposed as "VMs"):
- LXC: `nodes(node).lxc(vmid)`, IP via `.interfaces.get()` (fast, works when stopped)
- QEMU: `nodes(node).qemu(vmid)`, IP via `.agent.get()` (slow, requires running VM + guest agent installed)
- Unified in code via `type` field: "lxc" or "qemu" (see `_build_vm_dict()` in `app/services/proxmox_client.py`)

**RDP file generation failures**:
- `build_rdp(vm)` raises `ValueError` if VM has no IP address (stopped VMs, missing guest agent)
- `app.py` catches this in `/rdp/<vmid>.rdp` route, renders `error.html` with 503 status
**mappings.json path resolution** (DEPRECATED - MIGRATE TO DATABASE):
- File at `app/mappings.json` currently read by `_resolve_user_owned_vmids()` to determine VM access
- **Should be removed**: All VM assignments should use VMAssignment table in database
- User model already has `vm_assignments` relationship - JSON file is redundant
- Migration path: Create VMAssignment records for any mappings in JSON, then delete JSON file and related code
- Override with `MAPPINGS_FILE` env var (absolute path recommended for production)
- Auto-created on first write if missing – parent dir must exist!

**SSL verification disabled by default** (`PVE_VERIFY=False`):
- Required for self-signed Proxmox certs in dev/lab environments
- Suppresses urllib3 warnings via `urllib3.disable_warnings()` in `app.py` startup
- Production: use proper certs and set `PVE_VERIFY=True`

**Session secret key hardcoded** (`SECRET_KEY="change-me-session-key"`):
- Default in `app/config.py` is **insecure** – sessions can be forged!
- Production: generate random key: `python3 -c 'import secrets; print(secrets.token_hex(32))'`
- Set via `SECRET_KEY` env var or `.env` file


## Common tasks

**Add new API endpoint**:
```python
@app.route("/api/vm/<int:vmid>/reboot", methods=["POST"])
@login_required
def api_vm_reboot(vmid: int):
    user = require_user()
    vm = find_vm_for_user(user, vmid)
    if not vm:
        return jsonify({"ok": False, "error": "VM not found"}), 404
    # Call proxmox_admin.nodes(vm["node"]).qemu(vmid).status.reboot.post()
    return jsonify({"ok": True})
```

**Add VM metadata field**:
1. In `app/services/proxmox_client.py` `_build_vm_dict()`, extract from `raw` dict: `cores = raw.get("maxcpu")`
2. Add to return dict: `"cores": cores`
3. In `index.html`, add display: `<div>Cores: {{ vm.cores }}</div>`

**Admin detection**:
- Check `is_admin_user(user)` returns True (logs to INFO level)
- Admin if: (a) authenticated via Proxmox OR (b) local user with role='adminer'
- **Design principle**: Any Proxmox user is automatically admin. No ADMIN_USERS list or ADMIN_GROUP checks needed.

**Manually test authentication**:
```python
from app.services.user_manager import authenticate_proxmox_user
result = authenticate_proxmox_user("student1", "password")
print(result)  # Should return "student1@pve" or None
```
