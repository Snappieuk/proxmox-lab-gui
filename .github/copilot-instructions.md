# Copilot Instructions for proxmox-lab-gui

## What this repo is
- A lightweight Flask webapp that provides a lab VM portal similar to Azure Labs.
- `rdp-gen/` contains the whole application: `app.py`, `auth.py`, `config.py`, `proxmox_client.py`, `cache.py`, templates and static assets.
- The app talks to a Proxmox API via `proxmoxer` using an admin service account defined in `rdp-gen/config.py`.

## Big picture architecture
- Single Flask app (no front-end framework) serving templates under `rdp-gen/templates/` and static files in `rdp-gen/static/`.
- `proxmox_client.py` is the Proxmox integration layer (server-side client) used by endpoints to query VMs and perform actions.
  - `get_all_vms()` returns a cached list of VMs.
  - `get_vms_for_user(user)` filters `get_all_vms()` using mappings from `mappings.json`.
  - `start_vm`, `shutdown_vm` call the Proxmox API actions using `proxmoxer`.
- `auth.py` uses a lightweight authentication method: it checks Proxmox 'pve' realm credentials by instantiating `ProxmoxAPI` with the provided user/password and calling `version.get()` to verify credentials.
- There is an in-process cache `ProxmoxCache(proxmox, ttl)` that expects a Proxmox client interface with `get_nodes`, `list_qemu`, `status_qemu` methods (see `cache.py`).
  - Note: `proxmox_admin` in `proxmox_client.py` is *not* the client used by `ProxmoxCache`; the code references a variable `proxmox` in `app.py` but it isn't defined in this repo. You may either supply a wrapper object implementing `get_nodes`, `list_qemu`, and `status_qemu`, or change `app.py` to pass a suitable client.
  - We now expose `proxmox_admin_wrapper` in `proxmox_client.py` for use with `ProxmoxCache`.

## Important files and patterns
- `rdp-gen/app.py` - Flask routes and UI rendering; this is the main entrypoint.
  - Routes: `/login`, `/logout`, `/portal`, `/admin`, `/admin/mappings`, `/rdp/<vmid>.rdp`, `/api/*` routes for start/stop and listing VMs.
  - Uses `login_required` decorator from `auth.py` for route protection.
- `rdp-gen/proxmox_client.py` - Proxmox API interface used by the app; includes caching and user mapping logic.
- `rdp-gen/config.py` - Secrets and configuration (hardcoded defaults in repo; in production consider environment injection or `./config_override.py`).
- `rdp-gen/mappings.json` - Stores per-user VM mappings; `set_user_vm_mapping()` updates this file.
  - Mapping file path is the value of `MAPPINGS_FILE` from `config.py` and is referenced relative to the working dir — expect it to be created under the `rdp-gen` folder when running locally.
- Templates under `rdp-gen/templates/` — use Jinja2 standard patterns.

## Project conventions / patterns
- No external process/service discovery: this repo expects a reachable Proxmox host and a working admin account configured in `config.py`.
- Minimal/no tests in the repo. Keep changes small and test locally against a Proxmox setup or by mocking `ProxmoxAPI`.
- When updating logic that queries Proxmox, be mindful of the cache in `proxmox_client.py` and the `ProxmoxCache` in `cache.py`.
- Admin vs non-admin behavior:
  - Admins are defined in `ADMIN_USERS` or members of `ADMIN_GROUP` in `config.py`.
  - Admins can see and manage all VMs, while non-admins only view VMs mapped in the `mappings.json` file.
    - Caution: `is_admin_user` is defined twice in `proxmox_client.py`. The second (cached) implementation will override the first; prefer to keep only the cached variant if you modify this logic.

## Examples / Useful snippets
- Return only allowed VMs for a user: `get_vms_for_user(user)` in `proxmox_client.py`.
- Start/Stop VM from Flask route:
  - `start_vm(vm)` — proxmox action: `nodes(node).qemu(vmid).status.start.post()`
  - `shutdown_vm(vm)` — proxmox action: `nodes(node).qemu(vmid).status.shutdown.post()`
- RDP file generation uses `build_rdp(ip)` in `proxmox_client.py` and served from `app.py` at `/rdp/<vmid>.rdp`.

## Developer workflows (local dev & quick checks)
- Run the app locally using the built-in Flask server (development only):

```bash
cd rdp-gen
python3 app.py
```
- The app runs on 0.0.0.0:8080; useful for quick testing with proxmox API access.
- Install dependencies in a venv; this repo uses `proxmoxer`:

```bash
python3 -m venv venv
source venv/bin/activate
pip install proxmoxer flask
```

- There are no automated tests present — test functionality manually:
  - Create `mappings.json` entries for users if testing user access.
  - Update `config.py` or use a local override for `PVE_HOST` and credentials.
  - For development without a Proxmox instance, you can mock `ProxmoxAPI` calls in tests by implementing a fixture that matches `get_nodes`, `list_qemu`, and `status_qemu` methods.

## Security & secrets
- `rdp-gen/config.py` contains hardcoded credentials in the repo. Do NOT commit production credentials to the repo.
- Prefer injecting secrets via environment variables or an external config file in CI/deployment.

## What to watch for / gotchas
- `ProxmoxCache` expects a minimal client interface with `get_nodes`, `list_qemu`, `status_qemu`. The app uses `ProxmoxCache(proxmox, ttl=5)` but there is no `proxmox` variable defined in the repo — the app imports it but does not define a proxmox client object directly. Search for a missing connector if you run into an import error.
  - Quick fix: Use `ProxmoxCache(proxmox_client.proxmox_admin_wrapper, ttl)` where `proxmox_admin_wrapper` is a tiny wrapper class around `proxmox_admin` exposing `get_nodes`, `list_qemu`, and `status_qemu`.
- `mappings.json` usage: absent file is treated as empty; `set_user_vm_mapping()` writes/creates it.
- Admin list membership may be represented in two formats by Proxmox: `members` arrays of strings or dicts — helper `_user_in_group` handles both.

## Common tasks and how-to snippets
- Add a new field to the VM cards: update `proxmox_client.get_all_vms()` to include the field (e.g. `guest_os`), then update `index.html` to show it.
- Add a new API endpoint: create a new Flask route in `app.py`, use `@login_required`, call helper functions in `proxmox_client.py` and return JSON responses.
- Add robust error handling when calling proxmox: wrap calls to `proxmox_admin` then return appropriate flask `abort` or JSON error responses.

## Housekeeping for AI agents
- Preserve `config.py` defaults when editing; the repo stores default host and creds which are likely for local development only.
- For any change that touches `proxmox_client.py`, ensure the caching and mapping logic are preserved.
- When suggesting code, use existing helper functions (`get_all_vms`, `get_vms_for_user`, `find_vm_for_user`) instead of re-implementing VM filtering.
- The UI uses simple CSS and toggles; server-side changes should keep JSON field names stable (`vmid`, `status`, `ip`, `category`).

---

If anything here is unclear or you want additional `AGENTS.md`-style detailed instructions (e.g. for running integration tests or for CI), tell me what specific areas to expand and I'll update the file accordingly.