# Copilot Instructions for proxmox-lab-gui

## What this repo is
A Flask webapp providing a lab VM portal similar to Azure Labs for self-service student VM access.

**Architecture**: Modular blueprint-based Flask application with all code under `app/` directory. Uses SQLite for class management, Proxmox API for VM control, VMInventory database-first caching, and VM shell + disk copy deployment strategy.

**Entry point**: `run.py` - imports `create_app()` factory from `app/` package and runs Flask with threaded mode.

**Deployment model**: This code is NOT run locally or in a codespace. It is pushed to a remote Proxmox server (typically via git) and runs there as a systemd service or standalone Flask process. The server has direct network access to Proxmox clusters.

**Performance architecture**: Database-first design with background sync daemon. All VM reads (<100ms) come from VMInventory table, never directly from slow Proxmox API (5-30s). Background sync updates database every 5min (full) and 30sec (quick status).

**Shell environment**: This repo is deployed on Linux servers but development may occur in Windows PowerShell environments. When generating terminal commands for deployment/maintenance, assume Linux bash. For local development on Windows, use PowerShell syntax with `;` for command chaining (never `&&`).

## Current state & architecture decisions (READ THIS FIRST!)
- **VMInventory + background_sync system**: **Core architecture** - database-first design where all VM reads come from `VMInventory` table. Background sync daemon updates DB every 5min (full) and 30sec (quick). Provides 100x faster page loads vs direct Proxmox API calls. Code in `app/__init__.py` (lines 158-161), `app/routes/api/vms.py`, `app/services/inventory_service.py`, `app/services/background_sync.py`.
- **Template sync daemon**: Background daemon syncs templates to database with full spec caching (CPU/RAM/disk/OS). Full sync every 30min, quick verification every 5min. Enables instant template dropdown loading (<100ms vs 5-30s). Code in `app/services/template_sync.py`, started in `app/__init__.py` (lines 185-187).
- **Auto-shutdown service**: Monitors VMs in classes with auto-shutdown enabled, shuts down VMs with CPU below threshold for configured duration. Also enforces hour restrictions. Code in `app/services/auto_shutdown_service.py`, started in `app/__init__.py` (lines 163-165). Class settings stored in database (`auto_shutdown_enabled`, `auto_shutdown_cpu_threshold`, `auto_shutdown_idle_minutes`, `restrict_hours`, `allowed_hours_start`, `allowed_hours_end`).
- **VM specs management**: Teachers can modify VM hardware specs (CPU cores and RAM) via class settings. Changes trigger automatic recreation of all student VMs with new specs. Function `recreate_student_vms_with_new_specs()` in `class_vm_service.py` deletes old student VMs and creates new ones with updated hardware while preserving overlay structure. API endpoint: `/api/classes/<id>/settings` (POST with `cpu_cores` and `memory_mb` fields).
- **Progressive UI loading**: Frontend shows "Starting..." badge for VMs that are running but have no IP yet. Only transitions to "Running" after IP is discovered. Polls at 2s and 10s intervals after VM start, background sync triggers IP discovery 15s after start operation.
- **IP discovery workflow**: Multi-tier hierarchy - VMInventory DB cache → Proxmox guest agent `network-get-interfaces` API → LXC `.interfaces.get()` → ARP scanner with nmap → RDP port 3389 validation. Background thread triggers immediate IP sync 15 seconds after VM start (allows boot time before network check).
- **VM Deployment strategy** (Config cloning + disk overlays - NEVER use qm clone):
  - **Location**: All disk operations in `app/services/class_vm_service.py` using direct SSH `qm` and `qemu-img` commands
  - **Critical rule**: NEVER use `qm clone` for any VM creation - always use config cloning + disk overlay approach
  - **Config cloning** (`app/services/vm_config_clone.py`): **NEW PRIMARY METHOD**
    - Copies entire VM config file from template to student VM (65+ settings guaranteed identical)
    - Modifies VM-specific fields only: disk paths, EFI/TPM paths, MAC address, VM name
    - Function: `clone_vm_config()` - reads `/etc/pve/qemu-server/{template}.conf`, modifies, writes new config
    - Helper: `check_template_has_efi_tpm()` - detects if template uses EFI/TPM for UEFI boot (Windows 11 requirement)
    - EFI/TPM disk creation: If template has EFI/TPM, creates raw disk files (528K for EFI, 4M for TPM) with proper permissions
    - Path rewriting: Updates all disk paths (main disk, EFI disk, TPM state) to point to new VM's directories
    - Preserves all options: efitype, pre-enrolled-keys, version, disk options, network options - everything from template
  - **Workflow with template**:
    1. Export template disk: `qemu-img convert -O qcow2` creates base QCOW2 file from template
    2. Teacher VM: Overlay disk created first, then `clone_vm_config()` copies entire config from template with disk path updates
    3. Student VMs: `qemu-img create -f qcow2 -F qcow2 -b <base>` creates overlay, then `clone_vm_config()` copies config
    4. EFI/TPM handling: If template has EFI/TPM, creates raw disk files and updates paths in config file
  - **Workflow without template**:
    1. Teacher/class-base/student VMs: `create_vm_shell()` from `vm_core.py` creates empty VMs with hardware specs
  - **Why config cloning**: Guarantees 100% identical configuration (CPU, memory, BIOS, UEFI, TPM, boot order, VGA, all 65+ settings). Faster and more reliable than individual setting application. Critical for Windows 11 VMs with UEFI+TPM requirements.
  - **Template push**: Export updated template disk to new class-base QCOW2, recreate student overlays with `clone_vm_config()`
  - **Key modules**: `vm_config_clone.py` (config copying), `vm_template.py` (overlay creation), `vm_core.py` (shell creation), `class_vm_service.py` (orchestration)
  - **SSH helper**: `SSHExecutor` from `ssh_executor.py` provides SSH connection wrapper and connection pooling
- **Class co-owners feature**: Teachers can grant other teachers/admins full class management permissions. Uses many-to-many relationship via `class_co_owners` association table. Check permissions with `Class.is_owner(user)` method.
- **Template publishing workflow**: Teachers can publish VM changes to class template, then propagate to all student VMs. Located at `app/routes/api/publish_template.py` and `app/routes/api/class_template.py`. Deletes old student VMs and recreates fresh ones from updated template using disk copy approach (same as initial class creation).
- **User VM assignments**: All VM→user mappings stored in database via VMAssignment table. Direct assignments (no class) use `class_id=NULL`. Located at `app/routes/api/user_vm_assignments.py`. NO JSON files used.
- **Authentication model**: **Proxmox users = Admins only**. Going forward, no application users (students/teachers) should have Proxmox accounts. All non-admin users managed via SQLite database.
- **Build VM from Scratch feature**: Teachers can create VMs from ISO images with hardware configuration. ISO-based deployment (no cloud-init). VM automatically created on node where ISO is located. Service in `app/services/vm_deployment_service.py`, API at `app/routes/api/vm_builder.py`, UI in template_builder modal. ISO files synced on startup via `trigger_iso_sync_all_clusters()`. Creates VMAssignment with `class_id=NULL` to show VMs in "My Template VMs" section.
- **Console/VNC access**: Web-based console via noVNC (static files in `app/static/novnc/`). WebSocket proxy in `app/routes/api/console.py` bridges browser ↔ Proxmox VNC websocket. Routes: `/console/<vmid>` (noVNC UI), `/api/console/<vmid>/proxy` (WebSocket). Requires `flask-sock` package. Auto-refreshes 5 minutes before VNC ticket expiration (2-hour lifetime) and immediately on ticket expiration errors. Optimized for installer performance with compression=0 and quality=9 for best responsiveness. Note: `vnc_proxy.py` exists as legacy/alternative implementation - use `console.py` for new development.
- **SSH connection pooling**: Reusable SSH connections via `_connection_pool` in `app/services/ssh_executor.py`. Cleanup handler registered in `app/__init__.py` to close all connections on shutdown. Critical for performance - avoids SSH handshake overhead on every disk operation.

## Architecture patterns
**Database-first user access control**:
- **VMAssignment table**: All VM→user mappings stored in database. Uses `assigned_user_id` FK to User table, `class_id` FK to Class table (NULL for direct assignments).
- **Permission check**: `_resolve_user_owned_vmids()` function queries VMAssignment table, returns set of accessible VMIDs.
- **Admin bypass**: Admins see all VMs (permission check skipped).
- Both class-based assignments and direct assignments use same VMAssignment table, differentiated by `class_id` field.
- Function `_resolve_user_owned_vmids()` in `app/routes/api/vms.py` handles all permission logic.
- All systems query Proxmox clusters via `app/services/proxmox_service.py` and render same templates.

**Database-first cluster management** (`app/models.py` Cluster model + `app/config.py`):
- **Cluster table**: Stores all Proxmox cluster connection configurations (host, port, user, password, verify_ssl, is_active, is_default)
- **Migration**: `migrate_db.py` automatically migrates existing `clusters.json` to database on first run
- **Runtime loading**: `load_clusters_from_db()` loads active clusters from database, falls back to clusters.json if DB unavailable
- **Legacy support**: clusters.json still supported for backwards compatibility, but database is preferred
- **Admin UI**: Clusters managed via `/admin/clusters` route (add/edit/delete/activate/deactivate)
- Session stores `cluster_id` – user switches clusters via dropdown in UI
- Each cluster gets separate `ProxmoxAPI` connection (cached in `_proxmox_connections` dict)
- `get_proxmox_admin()` returns connection for current session cluster
- **Key quirk**: `get_proxmox_admin_for_cluster(id)` bypasses session (used in parallel fetches)

**Progressive loading pattern** (avoids slow page renders):
- Routes like `/portal` render skeleton HTML with empty VM cards
- JavaScript immediately calls `/api/vms` to fetch data and populate cards client-side
- Prevents timeout on initial page load while Proxmox API is queried (can take 30s+ with many VMs)
- See `templates/index.html` for fetch pattern, `app/routes/api/vms.py` for API

**Database-first caching architecture**:
1. **VMInventory table** (`app/models.py`): Single source of truth for all VM data shown in GUI. Stores identity, status, network, resources, metadata, and sync tracking.
2. **Background sync daemon** (`app/services/background_sync.py`): Auto-starts on app init, keeps VMInventory synchronized with Proxmox. Full sync every 5min, quick sync every 30sec for running VMs.
3. **Inventory service** (`app/services/inventory_service.py`): Database operations layer - `persist_vm_inventory()`, `fetch_vm_inventory()`, `update_vm_status()`.
4. **Performance benefit**: Database queries <100ms vs Proxmox API 5-30 seconds = **100x faster page loads**. GUI never blocked by slow Proxmox API.
5. **VM list cache** (legacy fallback in `app/services/proxmox_service.py`): Module-level `_vm_cache_data` dict with 5min TTL. Used only when VMInventory unavailable.
6. **IP lookup cache** (`_ip_cache` in `app/services/proxmox_service.py`): Separate 5min TTL for guest agent IP queries – **critical** to avoid timeout storms (guest agent calls are SLOW).

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
  vm_class_mappings.py → /admin/vm-class-mappings (manage VM↔class assignments)
api/
  vms.py         → /api/vms (fetch VM list), /api/vm/<vmid>/start|stop|status|ip
  class_api.py   → /api/classes (CRUD), /api/class/<id>/pool, /api/class/<id>/assign
  class_owners.py → /api/classes/<id>/co-owners (GET/POST/DELETE co-owner management)
  class_template.py → /api/class/<id>/template/* (reimage, save, push operations)
  publish_template.py → /api/classes/<id>/publish-template (teacher→student VM propagation)
  user_vm_assignments.py → /api/user-vm-assignments (direct VM assignments, no class)
  vm_class_mappings.py → /api/vm-class-mappings (bulk assign VMs to classes)
  vm_builder.py  → /api/vm-builder/build (build VM from scratch with ISO), /isos (list ISOs from all nodes)
  rdp.py         → /rdp/<vmid>.rdp (download RDP file)
  ssh.py         → /ssh/<vmid> (WebSocket terminal, requires flask-sock)
  console.py     → /console/<vmid> (noVNC web console), /api/console/<vmid>/proxy (WebSocket)
  vnc_proxy.py   → /api/vnc/<vmid>/proxy (alternative VNC WebSocket proxy)
  mappings.py    → /api/mappings (DEPRECATED - migrated to database, kept for legacy compat)
  clusters.py    → /api/clusters/switch (change active cluster)
  templates.py   → /api/templates (list templates, by-name lookup, node distribution, stats)
  sync.py        → /api/sync/status|trigger|inventory/summary (VMInventory sync control)
  class_settings.py → /api/classes/<id>/settings (GET/PUT auto-shutdown, hour restrictions)
```


## Key files

**`run.py`** (Application entry point, ~30 lines):
- Imports `create_app` factory from `app/` package, runs Flask app with `threaded=True`
- Disables SSL warnings if `PVE_VERIFY=False` (before any imports)

**`app/__init__.py`** (Application factory, ~215 lines):
- `create_app()` function: configures Flask, initializes SQLAlchemy (`models.init_db()`), registers blueprints
- WebSocket support: conditionally imports `flask-sock` (gracefully degrades if missing)
- Context processor: injects `is_admin`, `clusters`, `current_cluster`, `local_user` into all templates
- Templates/static served from `app/templates/` and `app/static/` directories
- **Background services startup** (critical initialization order):
  1. Database initialization (`init_db()`)
  2. WebSocket/flask-sock setup (SSH terminal, VNC console)
  3. Background IP scanner (`start_background_ip_scanner()`)
  4. Background VM inventory sync (`start_background_sync()` - line 158-161)
  5. Auto-shutdown daemon (`start_auto_shutdown_daemon()` - line 163-165)
  6. ISO sync trigger (`trigger_iso_sync_all_clusters()`)
  7. SSH connection pool cleanup handler (`atexit.register()`)
  8. Template sync daemon (`start_template_sync_daemon()` - line 185-187)

**`app/models.py`** (SQLAlchemy ORM, ~550 lines):
- `Cluster`: **Database-first cluster config** - stores Proxmox cluster connection info (host, port, user, password, verify_ssl, is_active, is_default). Replaces clusters.json for persistent cluster management.
- `User`: Local accounts with roles (`adminer`/`teacher`/`user`), password hashing via Werkzeug
- `Class`: Lab classes created by teachers, linked to `Template`, has `join_token` with expiry
- `Template`: References Proxmox template VMIDs, can be cluster-wide or class-specific
- `VMAssignment`: Tracks deployed VMs (created via disk export + overlay) assigned to classes/users, status field (`available`/`assigned`/`deleting`)
- `VMInventory`: **Database-first cache** - stores all VM metadata (identity, status, network, resources). Updated by background sync daemon. Single source of truth for GUI.
- `init_db(app)`: Sets up SQLite at `app/lab_portal.db`, creates tables on first run

**`app/services/proxmox_operations.py`** (VM operations & template management, ~2500 lines):
- Template management: `list_proxmox_templates()` (scans clusters for template VMs)
- **DEPRECATED**: `clone_vms_for_class()` (~800 lines) - old approach using `qm clone` (slow, inefficient), replaced by disk export + overlay approach in `class_vm_service.py`. Should be removed in future cleanup. DO NOT USE.
- VM lifecycle: `start_class_vm()`, `stop_class_vm()`, `delete_vm()`, `get_vm_status()`
- Snapshot operations: `create_vm_snapshot()`, `revert_vm_to_snapshot()` – still used
- Template conversion: `convert_vm_to_template()` (converts VM → template)
- Multi-cluster aware: Takes `cluster_ip` param, looks up cluster by IP in `CLUSTERS` config

**`app/services/vm_config_clone.py`** (VM config cloning, ~325 lines):
- **Purpose**: Copy Proxmox VM config files directly for 100% identical VMs
- **Core architecture**: Reads `/etc/pve/qemu-server/{source}.conf`, modifies VM-specific fields, writes to new config file
- **Key functions**:
  - `check_template_has_efi_tpm()`: Detects if template has EFI/TPM disks configured (lines 17-70)
  - `clone_vm_config()`: Main function - copies entire config file with path/MAC modifications (lines 82-305)
  - `generate_mac_address()`: Creates random MAC address in Proxmox format (02:xx:xx:xx:xx:xx)
- **Fields modified during cloning**:
  1. VM name - updates to destination VM name
  2. Template flag - removed (line starts with `template:`)
  3. Main disk path - updates to point to overlay disk (scsi0/virtio0/sata0/ide0)
  4. EFI disk path - rewrites to `{storage}:{vmid}/vm-{vmid}-disk-1.raw` with all options preserved
  5. TPM state path - rewrites to `{storage}:{vmid}/vm-{vmid}-disk-2.raw` with all options preserved
  6. MAC address - generates new unique address for net0
  7. VGA settings - explicitly preserved for VirtIO-GPU support
- **Path rewriting logic** (lines 193-221):
  - Splits disk lines on first comma to separate path from options
  - Preserves ALL options after comma (disk-cache, IO thread, efitype, pre-enrolled-keys, version, etc.)
  - Uses string manipulation instead of regex for precise option preservation
  - Example: `efidisk0: STORAGE:126/base-126-disk-0.qcow2,efitype=4m,pre-enrolled-keys=1` → `efidisk0: STORAGE:42809/vm-42809-disk-1.raw,efitype=4m,pre-enrolled-keys=1`
- **SSH hop support**: Can read template config from different node via SSH tunnel (lines 127-135)
- **Used by**: `vm_template.create_overlay_vm()` when `template_vmid` parameter provided

**`app/services/vm_template.py`** (Template export and overlay creation, ~1037 lines):
- **Purpose**: Template disk export and overlay VM creation with full config cloning
- **Key functions**:
  - `export_template_to_qcow2()`: Exports template disk to QCOW2 base file (lines 87-230)
  - `create_overlay_vm()`: Creates VM with overlay disk + cloned config (lines 328-520) - **PRIMARY VM CREATION METHOD**
  - `create_overlay_disk()`: Creates QCOW2 overlay with backing file (lines 664-710)
- **Config cloning integration** (lines 393-490):
  - When `template_vmid` provided, calls `clone_vm_config()` instead of manual setting application
  - Detects EFI/TPM requirements via `check_template_has_efi_tpm()`
  - Creates EFI disk (528K raw) and TPM disk (4M raw) if template has them
  - Sets proper permissions (chmod 600, chown root:root) on all disk files
  - Verifies disk creation via `ls -lh` and config file inspection
- **EFI/TPM disk creation** (lines 418-460):
  - EFI: `qemu-img create -f raw {path} 528K` - required for UEFI boot
  - TPM: `qemu-img create -f raw {path} 4M` - required for Windows 11 TPM 2.0
  - Both disks created in VM directory: `{storage_path}/{vmid}/vm-{vmid}-disk-N.raw`
  - Path format in config: `efidisk0: STORAGE:VMID/vm-VMID-disk-1.raw,efitype=4m,pre-enrolled-keys=1`
- **Verification logging** (lines 468-490):
  - Reads created config file to verify EFI/TPM lines present
  - Logs errors if expected disks missing from config
  - Critical for debugging UEFI boot issues in Windows VMs

**`app/services/ssh_executor.py`** (SSH connection pooling, ~329 lines):
- **Purpose**: Thread-safe SSH connection pooling for Proxmox node operations
- **Core classes**:
  - `SSHConnectionPool`: Manages reusable paramiko SSH connections (lines 23-118)
  - `SSHExecutor`: Wraps connection for command execution (lines 120-280)
- **Connection pooling** (lines 23-118):
  - Thread-safe connection reuse to avoid SSH handshake overhead
  - Automatic reconnection on connection failure
  - Connection validation before reuse
  - Module-level `_connection_pool` for global access
- **Key functions**:
  - `get_pooled_ssh_executor()`: Returns executor from connection pool (lines 169-199)
  - `get_pooled_ssh_executor_from_config()`: Creates executor from cluster config with error handling (lines 301-329)
  - `get_ssh_executor_from_config()`: Legacy non-pooled executor creation (lines 283-299)
  - `cleanup_connection_pool()`: Closes all pooled connections (lines 201-213)
- **Cleanup integration**: `atexit.register()` in `app/__init__.py` ensures connections closed on shutdown
- **Error handling** (lines 315-329): Catches import errors and connection failures, logs detailed error messages
- **Used by**: All VM operations in `class_vm_service.py`, `vm_template.py`, `vm_core.py` for `qm` and `qemu-img` commands

**`app/services/class_vm_service.py`** (Class VM service workflows, ~2225 lines):
- **Primary VM creation service**: Orchestrates class VM deployment workflows using config cloning + disk overlays
  - **Modular design** (imports from canonical modules):
    - `export_template_to_qcow2`: Imported from `vm_template` - exports template disk using `qemu-img convert`
    - `create_overlay_vm`: Imported from `vm_template` - creates VM with overlay disk and cloned config
    - `wait_for_vm_stopped`: Imported from `vm_core` - polls VM status until stopped
    - `create_vm_shell`: Imported from `vm_core` - creates empty VM shell for template-less workflows
    - Wrapper functions: `get_next_available_vmid()`, `get_vm_mac_address()`, `sanitize_vm_name()` delegate to canonical `vm_utils` implementations
  - **Template-based workflow** (uses config cloning):
    1. Export template disk to base QCOW2: `export_template_to_qcow2()`
    2. Create teacher VM: `create_overlay_vm(template_vmid=X)` - clones full config + creates overlay disk + EFI/TPM disks
    3. Create student VMs: `create_overlay_vm(template_vmid=X)` for each student - identical config to template
  - **Template-less workflow** (manual shell creation):
    1. Create empty VM shells with hardware specs: `create_vm_shell()` from `vm_core.py`
    2. No config cloning, manually specify CPU/RAM/disk parameters
  - **Key functions**: 
    - `deploy_class_vms()`: Entry point - orchestrates VM creation and VMInventory update (lines 800-900)
    - `create_class_vms()`: Core logic - handles template/no-template workflows, creates all VMs (lines 920-1400)
    - `recreate_student_vms_from_template()`: Recreate student VMs from template (used in template push) (lines 1600-1800)
    - `get_vm_mac_address()`: Parses `qm config` output to extract MAC from net0 line
    - `save_and_deploy_class_vms()`: Updates student VMs by rebasing overlays on new base
  - **EFI/TPM support**: Automatically detects and creates EFI/TPM disks when template has them (Windows 11 UEFI boot)
  - **SSH execution**: Uses `get_pooled_ssh_executor_from_config()` for connection-pooled command execution
- **VMAssignment management**: Creates database records for all VMs with vm_name and mac_address
- **Critical fixes (Feb 2026)**:
  - Fixed undefined variable `template_node` → `template_node_name` (4 occurrences)
  - Student VMs now use `create_overlay_vm(template_vmid=X)` instead of manual shell creation
  - Template-less VMs use `create_vm_shell()` instead of raw `qm create` commands

**`app/services/class_service.py`** (Class management logic, ~560 lines):
- User CRUD: `create_local_user()`, `authenticate_local_user()`, `update_user_role()`
- Class CRUD: `create_class()`, `update_class()`, `delete_class()`, `get_classes_for_teacher()`
- VM pool management: `add_vms_to_class_pool()` (exports template + creates overlays), `remove_vm_from_class()` (deletes VM)
- Student enrollment: `join_class_via_token()` (auto-assigns unassigned VM), `generate_class_invite()`
- Template registration: `register_template()` (scans Proxmox for templates, saves to DB)

**`app/services/vm_deployment_service.py`** (VM builder service, ~490 lines):
- **Build VM from Scratch**: Creates VMs from ISO images with hardware configuration
- **ISO-only deployment**: No cloud-init, automatic node detection from ISO location
- **Key functions**:
  - `build_vm_from_scratch()`: Main entry point - orchestrates VM creation with ISO
  - `_find_node_with_iso()`: Scans all nodes to locate ISO file
  - `_deploy_vm_with_iso()`: Creates VM with ISO attached, unified for Linux/Windows
  - `_ensure_virtio_iso()`: Downloads/checks VirtIO drivers ISO for Windows VMs
  - `list_available_isos()`: Returns available ISO images from all nodes with location info
- **Automatic node detection**: VM created on node where ISO exists (no manual selection)
- **Windows support**: VirtIO drivers ISO attachment, UEFI configuration, manual installation
- **Template conversion**: Can convert created VMs to templates
- **Hardware options**: CPU, memory, disk, network, BIOS, boot order, SCSI controller, etc.

**`app/services/proxmox_service.py`** (Proxmox integration, ~1800 lines):
- **Singletons**: `proxmox_admin` (legacy single cluster), `proxmox_admin_wrapper` (cache adapter)
- **Core VM functions**: `get_all_vms()` (fetch all VMs from database), `get_vms_for_user()` (user-specific view), `find_vm_for_user()` (permission check)
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
- **Admin privileges**: Proxmox users are automatically admins. Local admin users have role='adminer'. Admin group management is optional supplementary control via Proxmox groups.
- **Performance**: `VM_CACHE_TTL` (300s), `PROXMOX_CACHE_TTL` (30s), `ENABLE_IP_LOOKUP` (bool), `ENABLE_IP_PERSISTENCE` (slow, disabled by default)
- **Persistence files**: `CLUSTER_CONFIG_FILE` (JSON for cluster definitions), `VM_CACHE_FILE` (deprecated), `IP_CACHE_FILE` (deprecated)
- **ARP scanner**: `ARP_SUBNETS` (broadcast addresses, default `["10.220.15.255"]` for 10.220.8.0/21 network)
- **Template replication**: `ENABLE_TEMPLATE_REPLICATION` (bool, default False) – auto-replicate templates at startup

**`app/services/arp_scanner.py`** (ARP-based IP discovery, ~740 lines):
- `discover_ips_via_arp(vm_mac_map, subnets, background=True)`: Runs nmap ARP probe, matches MAC→IP
- `get_arp_table()`: Parses `ip neigh` or `/proc/net/arp` output, returns `{mac: ip}` dict
- `check_rdp_ports(ips, timeout=1)`: Parallel TCP port 3389 check (validates Windows VM connectivity)
- **Background mode**: Spawns thread, returns cached results immediately (avoids blocking API responses)
- **Root requirement**: `nmap -sn -PR` (ARP ping) needs root or `CAP_NET_RAW` capability. Falls back to ICMP ping without root.
- **Cache**: `_arp_cache` (1hr TTL), `_rdp_hosts_cache` (RDP-enabled IPs), invalidated on VM operations

**`app/services/auto_shutdown_service.py`** (Auto-shutdown daemon, ~321 lines):
- **Auto-shutdown**: Monitors VMs in classes with `auto_shutdown_enabled=True`, shuts down VMs with CPU below threshold for configured duration
- **Hour restrictions**: Enforces `restrict_hours` - shuts down VMs outside allowed time windows
- **Key functions**:
  - `start_auto_shutdown_daemon(app)`: Starts background daemon thread
  - `check_and_shutdown_idle_vms()`: Main loop - checks all restricted classes
  - `check_class_vms(class_)`: Enforces restrictions for specific class
  - `_get_vm_cpu_usage()`: Queries Proxmox RRD data for CPU metrics
- **Settings** (Class model): `auto_shutdown_enabled`, `auto_shutdown_cpu_threshold` (default 20%), `auto_shutdown_idle_minutes` (default 30), `restrict_hours`, `allowed_hours_start`, `allowed_hours_end`
- **Check interval**: 300 seconds (5 minutes)
- **Idle tracking**: `vm_idle_tracker` dict tracks idle state per VM (cluster, class_id, idle_since, low_cpu_checks)
- **API endpoint**: `/api/classes/<id>/settings` (GET/PUT class restrictions)

**`app/services/template_sync.py`** (Template sync daemon, ~270 lines):
- **Background daemon**: Syncs Proxmox templates to database with full spec caching (CPU/RAM/disk/OS)
- **Sync intervals**: Full sync every 30min, quick verification every 5min
- **Key functions**:
  - `start_template_sync_daemon(app)`: Starts background daemon thread
  - `sync_templates_from_proxmox()`: Full sync - queries all clusters, caches specs
  - `quick_template_verification()`: Fast check - verifies templates still exist
- **Database storage**: Template table with cluster/node/vmid, specs (cores, memory, disk_size), OS detection
- **Performance benefit**: Template dropdown loads instantly (<100ms vs 5-30s Proxmox API calls)
- **API endpoints**: `/api/sync/templates/status`, `/api/sync/templates/trigger` (admin only)

**`app/utils/caching.py`** (Cache utilities, ~170 lines):
- `ThreadSafeCache`: In-memory dict with TTL, thread lock, `get()`/`set()`/`invalidate()` methods
- `PersistentCache`: JSON-backed cache with same interface, survives restarts, used for VM/IP persistence

**Production deployment files**:
- `start.sh` – Activates venv, loads `.env`, runs `python3 run.py`
- `deploy.sh` – Git pull, pip install, restart systemd service or standalone Flask
- `restart.sh` – Quick restart (no git pull or pip install)
- `clear_cache.sh` – Clears VM cache files
- `proxmox-gui.service` – Systemd unit (edit User/Group/paths, then `systemctl enable/start`)
  
**Utility scripts**:
- `migrate_db.py` – Database migration script (creates tables, adds columns, migrates clusters.json to database)

**Removed/deprecated**:
- `terraform/` directory – Removed, Terraform deployment strategy no longer used
- `app/services/terraform_service.py` – Deleted, functionality replaced by disk copy approach
- `app/services/qcow2_bulk_cloner.py` – Deleted, refactored into `ssh_executor.py` (SSH wrapper only)
- `tests/test_qcow2_bulk_cloner.py` – Deleted, tested non-existent module
- `add_cluster.py` – Removed, clusters now managed via web UI at `/admin/clusters`
- `check_mac_addresses.py` – Removed, functionality replaced by VMInventory database queries
- `migrate_config_imports.py` – Removed, one-time migration completed
- `debug_vminventory.py` – Removed, use database queries or `/api/sync/status` instead
- `fix_vminventory.py` – Removed, background sync handles inventory repairs automatically

**Code quality updates (Feb 2026)**:
- Fixed all ruff linting errors (E722, E712, E402, F401, F821, F841 rules)
- Resolved hostname_service.py syntax errors (indentation issues in try/except blocks)
- Fixed undefined variable references in proxmox_operations.py and template_migration_service.py
- Updated user_manager.py admin group functions to accept optional admin_group parameter instead of using undefined module constants
- Removed unused imports and variables throughout codebase (hostname_service, template_migration_service, vm_utils, tests)
- All 46 ruff errors resolved, codebase now passes strict linting checks

**Code deduplication completed (2024)**:
- Removed 100-line duplicate `export_template_to_qcow2()` from `class_vm_service.py` → now imports from `vm_template`
- Removed 50-line duplicate `wait_for_vm_stopped()` from `class_vm_service.py` → now imports from `vm_core`
- Removed 80-line duplicate `_get_vm_mac()` from `proxmox_client.py` → now calls `vm_utils.get_vm_mac_address_api()`
- Wrapper functions in `class_vm_service.py` properly delegate to canonical implementations in `vm_utils`
- Total reduction: ~230 lines of duplicate code eliminated

**Frontend patterns**:
- **Progressive loading**: Routes render skeleton HTML, JavaScript fetches data via `/api/vms` (prevents timeout on initial page load)
- **Async patterns**: Use `async/await` for all fetch operations, error handling with try/catch blocks
- **Status polling**: After VM power operations, poll at 2s and 10s intervals to update status badges
- **Example**: See `app/templates/index.html` functions: `loadVMs()`, `refreshVmStatus()`, `sendPowerAction()`
- **Error handling**: API routes return `{"ok": false, "error": "message"}` format, frontend shows alerts

**API response patterns**:
- Success: `return jsonify({"ok": True, "data": ...}), 200`
- Client error: `return jsonify({"ok": False, "error": "message"}), 400/404`
- Server error: `return jsonify({"ok": False, "error": str(e)}), 500`
- All API routes use `@login_required` or `@admin_required` decorators from `app/utils/decorators.py`
- Use `require_user()` to get current username, `is_admin_user(user)` to check admin privileges

**Database patterns**:
- SQLAlchemy ORM with declarative models in `app/models.py`
- Relationships: `Class` ↔ `User` (teacher), `Class` ↔ `Template`, `VMAssignment` ↔ `User` (assigned_user)
- Many-to-many: `class_enrollments` (students), `class_co_owners` (co-teachers)
- Always use `db.session.commit()` after modifications, wrap in try/except with `db.session.rollback()` on error
- Query patterns: `User.query.filter_by(username=x).first()`, `Class.query.get(id)`, `VMAssignment.query.filter_by(class_id=x).all()`

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
5. **Deploy VMs**: Teacher adds VMs to pool via disk export + overlay workflow (creates teacher + class-base QCOW2 + student overlay VMs in background)
6. **Student enrollment**: Students visit invite link, auto-assigned first available VM in class pool (creates VMAssignment record)
7. **Student access**: Student sees assigned VM in `/portal`, can start/stop/revert to baseline snapshot
8. **Template publishing**: Teacher can publish changes from their VM → class template → all student VMs via `/api/classes/<id>/publish-template`
9. **Co-owner management**: Teacher can add co-owners (other teachers) via `/api/classes/<id>/co-owners` for shared class management
**Note**: All users (teachers and students) are local database accounts. No Proxmox accounts needed except for admins.

**Database migrations** (`migrate_db.py`):
- **Usage**: Run `python3 migrate_db.py` to apply schema migrations and migrate data
- **Features**:
  - Creates missing tables (e.g., clusters table for database-first architecture)
  - Adds missing columns to existing tables (ALTERs non-destructively)
  - Migrates clusters.json → database (one-time migration, preserves JSON as backup)
  - Safe to run multiple times (checks existing schema before applying changes)
- **For development**: Edit `app/models.py` and add new columns to `CRITICAL_MIGRATIONS` list in `migrate_db.py`
- **For production**: Run `migrate_db.py` after deploying code changes (non-destructive)
- **Nuclear option** (dev only): Drop and recreate all tables:
  ```python
  from app import create_app
  from app.models import db
  app = create_app()
  with app.app_context():
      db.drop_all()  # WARNING: destroys data!
      db.create_all()
  ```

**Debugging tips**:
- Check `/health` endpoint – returns `{"status": "ok"}` if app is running
- Use `/admin` (admin only) – shows Proxmox node summary, resource counts, API latency
- Run `python3 -c "from app.services import proxmox_client; print(proxmox_client.probe_proxmox())"` – Test Proxmox connectivity
- Session debugging: `app.logger.info()` logs to stdout/stderr (visible in `journalctl -u proxmox-gui -f`)
- Admin access issues: verify `is_admin_user(userid)` returns True, check logs for "inject_admin_flag" entries
- Cache debugging: check `_vm_cache_data` age, manually call `_invalidate_vm_cache()` if stale
- ARP scanner: verify nmap installed, run with `sudo` for ARP probe access, check `_arp_cache` status
- VMInventory issues: query database directly or visit `/api/sync/status` to see last sync times and stats
- Background sync status: visit `/api/sync/status` to see last sync times and stats

**Testing without Proxmox** (no test suite exists):
- Create mock `ProxmoxAPI` class with methods: `version.get()`, `nodes().qemu().status.current.get()`, etc.
- Swap `proxmox_admin` in `app/services/proxmox_service.py` or inject mock into `app/services/proxmox_service.py`
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
- Module-level `_vm_cache_data` dict in `app/services/proxmox_service.py` – **primary cache** for all `/api/vms` calls
- IP lookups have separate `_ip_cache` (5min TTL) to avoid guest agent timeout storms
- Cache invalidation: `start_vm()` and `shutdown_vm()` call `_invalidate_vm_cache()` to clear module-level cache

**Admin group membership API** (Proxmox group management):
- `add_user_to_admin_group(user: str, admin_group: str = None)`: Adds user to Proxmox admin group. If `admin_group` not provided, uses first configured admin group from `get_all_admin_groups()`.
- `remove_user_from_admin_group(user: str, admin_group: str = None)`: Removes user from Proxmox admin group. Requires `admin_group` parameter (auto-detects if None).
- These functions manage Proxmox group membership for admin access control
- **Design principle**: Any Proxmox authentication = admin. Group membership management is for supplementary Proxmox access control.

**LXC vs QEMU differences** (both exposed as "VMs"):
- LXC: `nodes(node).lxc(vmid)`, IP via `.interfaces.get()` (fast, works when stopped)
- QEMU: `nodes(node).qemu(vmid)`, IP via `.agent.get()` (slow, requires running VM + guest agent installed)
- Unified in code via `type` field: "lxc" or "qemu" (see `_build_vm_dict()` in `app/services/proxmox_service.py`)

**RDP file generation failures**:
- `build_rdp(vm)` raises `ValueError` if VM has no IP address (stopped VMs, missing guest agent)
- `app.py` catches this in `/rdp/<vmid>.rdp` route, renders `error.html` with 503 status

**SQLAlchemy session caching with manual database edits** (CRITICAL for assignment visibility):
- **Issue**: When VM assignments are manually edited outside Flask (direct SQL, database tools), SQLAlchemy's session cache doesn't detect these changes. Queries return stale cached objects instead of fresh database data.
- **Manifests as**: Manual assignments not showing in UI, student VMs showing as "Unassigned" when they should be assigned, database changes not visible.
- **Root cause**: SQLAlchemy uses an identity map (session cache) - when you query an object already in cache, it returns the cached instance without checking the database.
- **Fix**: Call `db.session.expire_all()` before querying assignments in routes that may have external edits:
  - `app/routes/classes.py` `view_class()` - line 111 (added Feb 2026)
  - `app/routes/api/class_api.py` `list_class_vms()` - line 787 (added Feb 2026)
- **Performance**: expire_all() is fast (<1ms) - just clears Python dicts. Database queries use connection pooling/caching.
- **Alternative approaches**: Use eager loading with `.options(joinedload(VMAssignment.assigned_user))` or `db.session.refresh(assignment)` for specific objects.
- **Related fix**: See `SQLALCHEMY_SESSION_CACHING_FIX.md` and `test_session_caching_fix.py` for detailed documentation and verification script.

**No JSON files used for data storage**:
- All user/class/VM data in SQLite database (`app/lab_portal.db`)
- Configuration in environment variables or `clusters.json` (cluster definitions only)
- No mappings.json, no vm_cache.json, no ip_cache.json (deprecated files may still exist but are not used)

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
1. In `app/services/proxmox_service.py` `_build_vm_dict()`, extract from `raw` dict: `cores = raw.get("maxcpu")`
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

**Add database column** (safe, non-destructive):
1. Edit `app/models.py`, add column: `new_field = db.Column(db.String(100))`
2. Add to `CRITICAL_MIGRATIONS` list in `migrate_db.py` (for ALTER TABLE)
3. Run `python3 migrate_db.py` - safe to run multiple times
4. Never use `db.drop_all()` in production - data loss!

**Frontend: Add new VM action button**:
1. In `index.html`, add button in VM card actions section
2. Call async function with `await fetch("/api/vm/<vmid>/action", {method: "POST"})`
3. Handle response: show alert on error, refresh VM status on success
4. Use `refreshSingleVMStatus(vmid)` to update single VM without full page reload

## Key troubleshooting patterns

**VM not appearing in portal**:
1. Check VMInventory: Query database or visit `/api/sync/status` - is VM in database?
2. Check VMAssignment: Does user have assignment record? Query `VMAssignment.query.filter_by(assigned_user_id=user.id).all()`
3. Force sync: Visit `/api/sync/trigger?type=full` to trigger background sync
4. Check cluster: Is VM on active cluster? Session has correct `cluster_id`?

**IP not showing for VM**:
1. Check VMInventory: Query database or visit `/api/sync/status` - is IP in database?
2. Guest agent: QEMU VMs need guest agent installed and running
3. ARP scan: Background sync uses ARP as fallback - check `nmap` installed, has root access
4. Force discovery: Start VM, wait 15 seconds for boot, background sync triggers IP discovery
5. LXC containers: IP discovery works even when stopped via `.interfaces.get()` API

**Background sync not running**:
1. Check `/api/sync/status` - shows last sync times and stats
2. Check logs: `journalctl -u proxmox-gui -f | grep background_sync`
3. Restart app: `sudo systemctl restart proxmox-gui` or `./restart.sh`
4. Manual trigger: Visit `/api/sync/trigger?type=full` to force sync

**Slow page loads despite database-first architecture**:
1. Check if route queries Proxmox API directly - should only query database
2. Background sync might be overloaded - check sync intervals in config
3. Database query inefficiency - add indexes for frequent lookups
4. Frontend making too many API calls - use progressive loading pattern

**Class VM deployment fails**:
1. SSH access: Verify SSH credentials in `.env` (SSH_HOST, SSH_USER, SSH_PASSWORD)
2. Storage paths: Check QCOW2_TEMPLATE_PATH and QCOW2_IMAGES_PATH exist on Proxmox server
3. Template export: Look for "qemu-img convert" errors in logs
4. Overlay creation: Check "qemu-img create -f qcow2 -F qcow2 -b" errors
5. Never use `qm clone` - always use disk export + overlay approach

## Project-specific conventions

**Naming patterns**:
- Database models: PascalCase (`Class`, `VMAssignment`, `VMInventory`)
- Service functions: snake_case (`create_class_vms()`, `fetch_vm_inventory()`)
- API routes: kebab-case URLs (`/api/vm-builder/build`, `/api/user-vm-assignments`)
- Blueprint prefixes: descriptive (`api_vms_bp`, `admin_users_bp`, `auth_bp`)
- Private functions: leading underscore (`_resolve_user_owned_vmids()`, `_build_vm_dict()`)

**Import organization**:
```python
#!/usr/bin/env python3
"""Module docstring."""

# Standard library imports
import logging
import os
from datetime import datetime

# Third-party imports
from flask import Blueprint, jsonify, request
from werkzeug.security import generate_password_hash

# Local application imports
from app.models import User, Class, VMAssignment
from app.services.proxmox_client import get_all_vms
from app.utils.decorators import login_required
```

**Logging patterns**:
- Use module-level logger: `logger = logging.getLogger(__name__)`
- Log levels: DEBUG (verbose), INFO (normal flow), WARNING (recoverable issues), ERROR (failures)
- Log before expensive operations: `logger.info(f"Syncing {len(vms)} VMs to database")`
- Log exceptions with context: `logger.error(f"Failed to start VM {vmid}: {str(e)}")`

**Error handling in API routes**:
```python
@api_bp.route("/api/example", methods=["POST"])
@login_required
def api_example():
    try:
        user = require_user()
        data = request.get_json()
        
        # Validate inputs
        if not data.get("required_field"):
            return jsonify({"ok": False, "error": "Required field missing"}), 400
        
        # Perform operation
        result = some_service_function(data)
        
        return jsonify({"ok": True, "data": result}), 200
        
    except ValueError as e:
        logger.warning(f"Validation error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return jsonify({"ok": False, "error": "Internal server error"}), 500
```

**Database transaction patterns**:
```python
from app.models import db, Class

try:
    # Make changes
    new_class = Class(name="Example", teacher_id=user.id)
    db.session.add(new_class)
    db.session.commit()
    
    return new_class
    
except Exception as e:
    db.session.rollback()
    logger.error(f"Database error: {e}")
    raise
```

**Frontend fetch patterns** (from `app/templates/index.html`):
```javascript
async function loadVMs() {
    try {
        const response = await fetch('/api/vms');
        const data = await response.json();
        
        if (!data.ok) {
            showError(data.error || 'Failed to load VMs');
            return;
        }
        
        // Process data.data (array of VMs)
        renderVMCards(data.data);
        
    } catch (error) {
        console.error('Error loading VMs:', error);
        showError('Network error - please try again');
    }
}
```

**Configuration precedence** (highest to lowest):
1. Environment variables (`.env` file or exported)
2. Database (Cluster table for cluster configs)
3. JSON files (legacy `clusters.json` for backwards compatibility)
4. Hardcoded defaults in `app/config.py` (insecure, dev only)

**When to use each data source**:
- **VMInventory table**: All VM status/metadata reads in frontend
- **Proxmox API**: Only during background sync, VM operations (start/stop/create/delete)
- **VMAssignment table**: All user-to-VM permission checks
- **Cluster table**: All cluster connection configurations (replaces clusters.json)

**Performance guidelines**:
- NEVER query Proxmox API in request handlers (use VMInventory table)
- NEVER synchronous loops over VMs calling Proxmox API (use background sync)
- Cache expensive lookups (IP discovery, MAC addresses) with TTL
- Use database indexes for frequently queried fields (vmid, cluster_id, assigned_user_id)
- Background threads for slow operations (ARP scan, template export)

**Security practices**:
- All routes require `@login_required` or `@admin_required` decorators
- Never trust client input - validate all JSON request data
- Use `require_user()` to get authenticated username from session
- Check `is_admin_user(user)` before admin operations
- Password hashing via `werkzeug.security.generate_password_hash()` with scrypt
- Session secret must be changed in production (`SECRET_KEY` env var)

