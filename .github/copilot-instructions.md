# Copilot Instructions for proxmox-lab-gui

## What this repo is
- A lightweight Flask webapp providing a lab VM portal similar to Azure Labs
- `rdp-gen/` contains the entire application: Flask app, templates, static assets, and Proxmox integration
- Talks to Proxmox VE API via `proxmoxer` using an admin service account from `config.py`

## Big picture architecture

**Single Flask app (no frontend framework)** serving Jinja2 templates with progressive loading:
- `/portal` and `/admin` render empty page shells, VMs load via client-side `fetch("/api/vms")`
- `proxmox_client.py` is the core integration layer with **two-tier caching**:
  - Module-level `_vm_cache_data` (TTL from `VM_CACHE_TTL`) for full VM list
  - Separate `_ip_cache` (5min TTL) for guest agent IP lookups to avoid hammering slow APIs
- **ProxmoxAdminWrapper** bridges `proxmoxer.ProxmoxAPI` to `ProxmoxCache` interface
  - `get_nodes()`, `list_qemu(node)`, `status_qemu(node, vmid)`
  - Combines both QEMU VMs and LXC containers in `list_qemu()` 
  - App uses `cache = ProxmoxCache(proxmox_admin_wrapper, ttl=VM_CACHE_TTL)` in `app.py`

**Authentication pattern** (`auth.py`):
- Validates Proxmox 'pve' realm users by attempting `ProxmoxAPI(...).version.get()` with provided credentials
- Session stores full userid like `student1@pve`
- `@login_required` decorator protects routes, `@admin_required` checks admin status

**User-VM mapping** (`mappings.json`):
- Non-admins only see VMs listed in `mappings.json` for their userid
- Admins (in `ADMIN_USERS` list or `ADMIN_GROUP` Proxmox group) see all VMs
- `set_user_vm_mapping(user, [vmids])` persists to JSON file

## Important files and patterns

**`rdp-gen/app.py`** - Flask routes and main entrypoint:
- Routes: `/login`, `/register`, `/logout`, `/portal`, `/admin`, `/admin/mappings`, `/rdp/<vmid>.rdp`
- API routes: `/api/vms` (GET), `/api/vm/<vmid>/start` (POST), `/api/vm/<vmid>/stop` (POST)
- `require_user()` helper aborts with 401 if no session
- `admin_required` decorator wraps `login_required` and checks `is_admin_user()`
- **Progressive loading**: portal renders empty shell, JavaScript fetches `/api/vms` and populates dynamically

**`rdp-gen/proxmox_client.py`** - Proxmox API interface (680 lines):
- `proxmox_admin` = `ProxmoxAPI(...)` singleton for admin operations
- `proxmox_admin_wrapper` = `ProxmoxAdminWrapper(proxmox_admin)` for `ProxmoxCache`
- `get_all_vms()` returns cached list with category guessing (`_guess_category`)
  - Windows detection: name contains "win" → category "windows"
  - All LXC → "linux", other QEMU → "linux", fallback → "other"
- IP address lookup:
  - LXC: uses `.lxc(vmid).interfaces.get()` API (fast, works when stopped)
  - QEMU: uses `.qemu(vmid).agent.get("network-get-interfaces")` (requires running VM + guest agent)
- `start_vm(vm)` / `shutdown_vm(vm)` handle both qemu and lxc types, call `_invalidate_vm_cache()`
- `build_rdp(vm)` generates .rdp file content, raises `ValueError` if no IP
- `get_vms_for_user(user, search)` filters by admin status + mappings, supports search by name/vmid/ip
- `create_pve_user(username, password)` for self-registration (validates username/password, creates user@pve)

**`rdp-gen/config.py`** - Configuration with env var overrides:
- All settings read via `os.getenv()` with hardcoded defaults (DO NOT commit production secrets)
- `PVE_HOST`, `PVE_ADMIN_USER`, `PVE_ADMIN_PASS`, `PVE_VERIFY` for Proxmox API
- `VALID_NODES` (comma-separated) restricts which nodes are queried
- `ADMIN_USERS` (comma-separated) + `ADMIN_GROUP` define admin privileges
- `VM_CACHE_TTL` (default 120s) controls cache expiration
- `ENABLE_IP_LOOKUP` (default True) toggles guest agent IP queries

**`rdp-gen/cache.py`** - `ProxmoxCache` class (50 lines):
- Expects client with `get_nodes()`, `list_qemu(node)`, `status_qemu(node, vmid)` interface
- Threadsafe with `threading.Lock()`, refreshes on TTL expiration
- Bulk fetches nodes → VMs per node → statuses (cheap)
- Used by `app.py` but NOT by `proxmox_client.py` functions (they use module-level cache)

**`rdp-gen/templates/index.html`** - Main portal UI:
- Progressive loading: skeleton → `fetch("/api/vms")` → populate VM cards
- VM cards grouped by category: windows, linux, lxc, other
- Start/Stop buttons call `/api/vm/<vmid>/{start|stop}`, refresh VM card on success
- RDP download button only shown for Windows VMs with IP addresses

## Developer workflows

**Local development**:
```bash
cd rdp-gen
python3 -m venv venv
source venv/bin/activate
pip install -r ../requirements.txt
# Configure via env vars or .env file (see .env.example)
export PVE_HOST=10.220.15.249
export PVE_ADMIN_USER=root@pam
export PVE_ADMIN_PASS=secret
python3 app.py  # Runs on 0.0.0.0:8080
```

**Testing without Proxmox**:
- No mock client maintained (`mock_client.py` removed)
- Implement minimal wrapper: `get_nodes()`, `list_qemu(node)`, `status_qemu(node, vmid)` returning dummy data
- Substitute `proxmox_admin_wrapper` in `app.py` for local testing

**Adding VM fields**:
1. Update `_build_vm_dict()` in `proxmox_client.py` to extract new field from raw VM data
2. Modify `index.html` template to display the field in VM cards
3. Cache invalidation happens automatically on start/stop/shutdown

**Environment configuration**:
- Use `.env` file (gitignored) or export env vars
- See `.env.example` for all available settings
- Production: inject secrets via container env or systemd EnvironmentFile

## Security & secrets
- `config.py` has hardcoded defaults for local dev (10.220.15.249, password!)
- **NEVER commit production credentials** - use environment variables
- Sessions use `SECRET_KEY` from config (change in production)
- Proxmox API calls use SSL verification controlled by `PVE_VERIFY` (default False for dev)

## Critical gotchas

**Dual caching layers**:
- `ProxmoxCache` in `cache.py` (used by app startup, not extensively)
- Module-level `_vm_cache_data` in `proxmox_client.py` (primary cache for all endpoints)
- IP lookups have separate `_ip_cache` (5min TTL) to avoid guest agent timeout storms

**Admin group membership formats**:
- Proxmox API returns `members` as either `["user@pve", ...]` OR `[{"userid": "user@pve"}, ...]`
- `_user_in_group()` handles both formats, checks all realm variants (user@pam, user@pve)

**LXC vs QEMU differences**:
- LXC: `nodes(node).lxc(vmid)`, IP lookup via `.interfaces.get()` (works when stopped)
- QEMU: `nodes(node).qemu(vmid)`, IP lookup via `.agent.get()` (requires running + guest agent)
- Both handled transparently by `type` field ("lxc" or "qemu") in VM dict

**RDP file generation**:
- Returns `ValueError` if VM has no IP address (common for stopped VMs or missing guest agent)
- `app.py` catches this and renders `error.html` template with 503 status
- Template in `index.html` hides RDP button if `vm.ip` is falsy or "<ip>"

**mappings.json location**:
- Path from `MAPPINGS_FILE` env var, defaults to `rdp-gen/mappings.json`
- Created on first write if missing
- Format: `{"user@pve": [vmid1, vmid2], ...}`

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
