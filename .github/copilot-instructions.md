# Copilot Instructions for proxmox-lab-gui

## What this repo is
A lightweight Flask webapp providing a lab VM portal similar to Azure Labs. Think "self-service VM access for students" – users log in with Proxmox credentials, see their assigned VMs, and can start/stop them or download RDP files. All code lives in `rdp-gen/` (Flask app + templates + Proxmox integration). No frontend framework, just vanilla JS + progressive loading.

## Architecture patterns

**Single-page progressive loading**:
- Routes like `/portal` and `/admin` render skeleton HTML shells
- JavaScript immediately calls `/api/vms` to fetch VM data and populate cards client-side
- Avoids slow initial page loads while Proxmox API is queried
- See `templates/index.html` for fetch pattern, `app.py` for API routes

**Two-tier caching strategy** (`proxmox_client.py` + `cache.py`):
- Module-level `_vm_cache_data` dict (TTL from `VM_CACHE_TTL`, default 120s) caches full VM list
- Separate `_ip_cache` (hardcoded 5min TTL) for guest agent IP lookups – **critical** to avoid timeout storms
- `ProxmoxCache` class wraps `ProxmoxAdminWrapper` for threadsafe bulk fetching
- Cache invalidation: automatic on VM start/stop via `_invalidate_vm_cache()`

**Authentication & authorization** (`auth.py`):
- User logs in with Proxmox username/password → attempts `ProxmoxAPI(...).version.get()` to validate
- On success, session stores full userid (`student1@pve`)
- `@login_required` checks session, `@admin_required` also checks `is_admin_user(userid)`
- Admins defined by: (a) `ADMIN_USERS` list in config OR (b) membership in `ADMIN_GROUP` Proxmox group

**User-VM mapping** (`mappings.json`):
- Non-admin users only see VMs explicitly mapped to their userid in JSON file
- Admins see ALL VMs across all nodes (subject to `VALID_NODES` filter if set)
- Admin UI at `/admin/mappings` lets you assign/unassign VMs to users
- File format: `{"user@pve": [100, 101, 102], "student2@pve": [103]}`

## Key files

**`rdp-gen/app.py`** (Flask entrypoint, ~410 lines):
- All routes: `/login`, `/register`, `/logout`, `/portal`, `/admin`, `/admin/mappings`, `/admin/probe`, `/health`
- API endpoints: `/api/vms`, `/api/mappings`, `/api/vm/<vmid>/start`, `/api/vm/<vmid>/stop`
- RDP download: `/rdp/<vmid>.rdp` (triggers browser download with Content-Disposition header)
- Helper: `require_user()` returns current userid or aborts 401 (use instead of raw `session.get("user")`)
- Decorator: `admin_required` = `login_required` + `is_admin_user()` check
- Runs on 0.0.0.0:8080 via `app.run()` with threaded mode for development and production

**`rdp-gen/proxmox_client.py`** (Proxmox integration, ~680 lines):
- Singletons: `proxmox_admin` (ProxmoxAPI instance), `proxmox_admin_wrapper` (for cache interface)
- Core functions: `get_all_vms()`, `get_vms_for_user(user, search)`, `find_vm_for_user(user, vmid)`
- VM operations: `start_vm(vm)`, `shutdown_vm(vm)` – handle both QEMU and LXC types
- RDP generation: `build_rdp(vm)` – raises `ValueError` if VM has no IP (caller must catch)
- Category guessing: `_guess_category(vm)` – "win" in name → windows, LXC → linux, else → other
- IP lookups: LXC via `.interfaces.get()` (fast, works when stopped), QEMU via guest agent (slow, requires running VM)
- User management: `create_pve_user(username, password)` for self-service registration
- Admin detection: `is_admin_user(userid)` checks `ADMIN_USERS` list + `ADMIN_GROUP` membership
- Diagnostics: `probe_proxmox()` returns node/resource summary dict (used by `/admin/probe` route)

**`rdp-gen/config.py`**:
- **ALL settings use `os.getenv()` with hardcoded defaults** – never commit prod secrets!
- Proxmox API: `PVE_HOST`, `PVE_ADMIN_USER`, `PVE_ADMIN_PASS`, `PVE_VERIFY` (SSL verification)
- Node filtering: `VALID_NODES` (comma-separated list, or None for all nodes)
- Admin privileges: `ADMIN_USERS` (comma-separated), `ADMIN_GROUP` (Proxmox group name)
- Performance: `VM_CACHE_TTL` (seconds, default 120), `ENABLE_IP_LOOKUP` (bool, default True)
- Persistence: `MAPPINGS_FILE` (path to JSON file, defaults to `rdp-gen/mappings.json`)
- Session: `SECRET_KEY` (Flask session signing, change in production!)

**`rdp-gen/cache.py`** (`ProxmoxCache` class):
- Threadsafe caching wrapper with `threading.Lock()` and TTL expiration
- Requires client implementing: `get_nodes()`, `list_qemu(node)`, `status_qemu(node, vmid)`
- Used by `app.py` on startup: `cache = ProxmoxCache(proxmox_admin_wrapper, ttl=VM_CACHE_TTL)`
- **Note**: `proxmox_client.py` functions use separate module-level cache, not this class

**`rdp-gen/auth.py`**:
- `authenticate_proxmox_user(username, password)` – attempts Proxmox API call to validate credentials
- `@login_required` decorator – checks `session.get("user")`, redirects to login if missing
- `current_user()` – returns userid from session or None

**Production deployment files**:
- `start.sh` – Activates venv, loads `.env`, runs Flask app.py directly with threaded mode
- `deploy.sh` – Deployment script: detects systemd service or standalone Flask, restarts
- `proxmox-gui.service` – Systemd unit file running app.py directly (edit User/Group/paths, then `systemctl enable/start`)
- `.env.example` – Template for local `.env` file (gitignored)

## Developer workflows

**Local dev setup**:
```bash
cd /workspaces/proxmox-lab-gui
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Edit with your Proxmox credentials
cd rdp-gen && python3 app.py  # Runs on http://0.0.0.0:8080
```

**Production deployment**:
```bash
# Quick start with Flask
./start.sh  # Activates venv, loads .env, runs app.py directly with threaded mode

# Or via systemd (recommended)
sudo cp proxmox-gui.service /etc/systemd/system/
# Edit service file: update User, Group, and all /path/to/proxmox-lab-gui
sudo systemctl daemon-reload
sudo systemctl enable --now proxmox-gui
sudo systemctl status proxmox-gui

# Deploy after git pull
./deploy.sh  # Auto-detects systemd or standalone Flask, restarts
```

**Debugging tips**:
- Check `/health` endpoint – returns `{"status": "ok"}` if app is running
- Use `/admin/probe` (admin only) – shows Proxmox node summary, resource counts, API latency
- Run `python3 rdp-gen/proxmox_probe.py` – CLI tool dumps probe output as JSON
- Session debugging: `app.logger.info()` logs to stdout/stderr (visible in `journalctl -u proxmox-gui -f`)
- Admin access issues: verify `is_admin_user(userid)` returns True, check logs for "inject_admin_flag" entries

**Testing without Proxmox** (no test suite exists):
- `mock_client.py` removed (not maintained) – implement your own stub if needed
- Minimal stub: class with `get_nodes()` → `[{"node": "prox1"}]`, `list_qemu(node)` → `[{...vm dict...}]`, `status_qemu(node, vmid)` → `{...status...}`
- Swap `proxmox_admin_wrapper` in `app.py` with your stub instance

**Environment config priority**:
1. Export env vars: `export PVE_HOST=10.220.15.249`
2. Or create `.env` file (gitignored): `PVE_HOST=10.220.15.249`
3. Fallback to hardcoded defaults in `config.py` (insecure for prod!)

**No automated tests** – manual testing workflow:
- Start app, login with Proxmox user, verify VMs appear in `/portal`
- Test start/stop buttons, check Proxmox UI for VM status changes
- Download RDP file for Windows VM, verify connection works
- Admin: login as admin user, verify `/admin` and `/admin/mappings` accessible

## Security & secrets
- `config.py` has hardcoded defaults for local dev (10.220.15.249, password!)
- **NEVER commit production credentials** - use environment variables
- Sessions use `SECRET_KEY` from config (change in production)
- Proxmox API calls use SSL verification controlled by `PVE_VERIFY` (default False for dev)

## Critical gotchas

**Dual caching layers** (easy to confuse):
- `ProxmoxCache` class in `cache.py` – used minimally, mostly for app startup warmup
- Module-level `_vm_cache_data` dict in `proxmox_client.py` – **primary cache** for all `/api/vms` calls
- IP lookups have separate `_ip_cache` (5min TTL) to avoid guest agent timeout storms
- Cache invalidation: `start_vm()` and `shutdown_vm()` call `_invalidate_vm_cache()` to clear module-level cache

**Admin group membership API quirks**:
- Proxmox returns `members` as either `["user@pve", ...]` OR `[{"userid": "user@pve"}, ...]` (two formats!)
- `_user_in_group()` handles both, checks all realm variants (user@pam, user@pve, user without realm)
- Set `ADMIN_GROUP=None` to disable group-based admin (rely only on `ADMIN_USERS` list)

**LXC vs QEMU differences** (both exposed as "VMs"):
- LXC: `nodes(node).lxc(vmid)`, IP via `.interfaces.get()` (fast, works when stopped)
- QEMU: `nodes(node).qemu(vmid)`, IP via `.agent.get()` (slow, requires running VM + guest agent installed)
- Unified in code via `type` field: "lxc" or "qemu" (see `_build_vm_dict()` in `proxmox_client.py`)

**RDP file generation failures**:
- `build_rdp(vm)` raises `ValueError` if VM has no IP address (stopped VMs, missing guest agent)
- `app.py` catches this in `/rdp/<vmid>.rdp` route, renders `error.html` with 503 status
- Frontend: `index.html` hides RDP button if `vm.ip` is falsy or placeholder string

**mappings.json path resolution**:
- Defaults to `rdp-gen/mappings.json` (relative to `proxmox_client.py`)
- Override with `MAPPINGS_FILE` env var (absolute path recommended for production)
- Auto-created on first write if missing – parent dir must exist!

**SSL verification disabled by default** (`PVE_VERIFY=False`):
- Required for self-signed Proxmox certs in dev/lab environments
- Suppresses urllib3 warnings via `urllib3.disable_warnings()` in `app.py` startup
- Production: use proper certs and set `PVE_VERIFY=True`

**Session secret key hardcoded** (`SECRET_KEY="change-me-session-key"`):
- Default in `config.py` is **insecure** – sessions can be forged!
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
1. In `proxmox_client.py` `_build_vm_dict()`, extract from `raw` dict: `cores = raw.get("maxcpu")`
2. Add to return dict: `"cores": cores`
3. In `index.html`, add display: `<div>Cores: {{ vm.cores }}</div>`

**Debug admin access**:
- Check `is_admin_user(user)` returns True (logs to INFO level)
- Verify user in `ADMIN_USERS` list or member of `ADMIN_GROUP` Proxmox group
- Use `/admin/probe` endpoint (admin-only) to see node/resource diagnostics

**Manually test authentication**:
```python
from auth import authenticate_proxmox_user
result = authenticate_proxmox_user("student1", "password")
print(result)  # Should return "student1@pve" or None
```
