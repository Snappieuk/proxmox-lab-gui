#!/usr/bin/env python3

# Disable SSL warnings before any imports that might trigger them
import os
if os.getenv("PVE_VERIFY", "False").lower() not in ("true", "1", "yes"):
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from functools import wraps

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

# Optional WebSocket support for SSH terminal
try:
    from flask_sock import Sock
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    logger = __import__('logging').getLogger(__name__)
    logger.warning("flask-sock not installed, WebSocket SSH terminal disabled")

from config import SECRET_KEY, VM_CACHE_TTL
from auth import login_required, current_user, authenticate_proxmox_user
from proxmox_client import (
    get_all_vms,
    get_vms_for_user,
    get_vm_ip,
    find_vm_for_user,
    is_admin_user,
    build_rdp,
    start_vm,
    shutdown_vm,
    get_user_vm_map,
    set_user_vm_mapping,
    get_pve_users,
    probe_proxmox,
    create_pve_user,
    verify_vm_ip,
)

import re
import logging
import json

app = Flask(__name__)
app.secret_key = SECRET_KEY

if WEBSOCKET_AVAILABLE:
    sock = Sock(app)


def require_user() -> str:
    """Return current user or abort with 401.

    This helps the type checker and centralizes the session -> username conversion.
    """
    user = current_user()
    if not user:
        abort(401)
    return user


@app.context_processor
def inject_admin_flag():
    user = session.get("user")
    is_admin = is_admin_user(user) if user else False
    # Log admin status for debugging (INFO level to be visible)
    if user:
        app.logger.info("inject_admin_flag: user=%s is_admin=%s", user, is_admin)
    return {"is_admin": is_admin}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        user = require_user()
        if not is_admin_user(user):
            abort(403)
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        # Already logged in — redirect to portal
        return redirect(url_for("portal"))

    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        full_user = authenticate_proxmox_user(username, password)
        if not full_user:
            error = "Invalid username or password."
        else:
            session["user"] = full_user
            app.logger.info("user logged in: %s", full_user)
            next_url = request.args.get("next") or url_for("portal")
            return redirect(next_url)

    return render_template("login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user"):
        # Already logged in — redirect to portal
        return redirect(url_for("portal"))

    error = None
    success = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")

        if password != password_confirm:
            error = "Passwords do not match."
        else:
            success_flag, error_msg = create_pve_user(username, password)
            if success_flag:
                success = f"Account created successfully! You can now sign in as {username}@pve"
                app.logger.info("New user registered: %s@pve", username)
            else:
                error = error_msg

    return render_template("register.html", error=error, success=success)


@app.route("/logout")
@login_required
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Main dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def root():
    if current_user():
        return redirect(url_for("portal"))
    return redirect(url_for("login"))


@app.route("/portal")
@login_required
def portal():
    user = require_user()
    
    # Fast path: render empty page immediately for progressive loading
    # VMs will be loaded via /api/vms after page renders
    return render_template(
        "index.html",
        progressive_load=True,
        windows_vms=[],
        linux_vms=[],
        lxc_containers=[],
        other_vms=[],
        message=None,
    )


@app.route("/health")
def health():
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin: user ↔ VM mappings
# ---------------------------------------------------------------------------

@app.route("/admin/mappings", methods=["GET", "POST"])
@admin_required
def admin_mappings():
    user = require_user()
    message = request.args.get('message')
    error = request.args.get('error')
    selected_user = None

    if request.method == "POST":
        # Just selecting a user to view/edit
        selected_user = (request.form.get("user") or "").strip()

    # Progressive loading - return minimal data on GET
    if request.method == "GET" and not request.args.get('user'):
        return render_template(
            "mappings.html",
            user=user,
            progressive_load=True,
            message=message,
            error=error,
        )
    
    # If user is selected via query param (from redirect), use that
    if request.args.get('user'):
        selected_user = request.args.get('user')
    
    # POST request or user selected - load full data
    mapping = get_user_vm_map()
    users = get_pve_users()
    all_vms = get_all_vms()
    
    # Get admin group info
    from proxmox_client import get_admin_group_members, ADMIN_GROUP, ADMIN_USERS
    current_admins = list(set(ADMIN_USERS + get_admin_group_members()))
    protected_admins = ADMIN_USERS  # Users in ADMIN_USERS config can't be removed via UI
    
    # Create vmid to name mapping for display
    vm_id_to_name = {v["vmid"]: v.get("name", f"vm-{v['vmid']}") for v in all_vms}
    all_vm_ids = sorted({v["vmid"] for v in all_vms})

    return render_template(
        "mappings.html",
        user=user,
        progressive_load=False,
        mapping=mapping,
        users=users,
        all_vm_ids=all_vm_ids,
        vm_id_to_name=vm_id_to_name,
        message=message,
        error=error,
        selected_user=selected_user,
        admin_group=ADMIN_GROUP,
        current_admins=current_admins,
        protected_admins=protected_admins,
    )


@app.route("/admin/mappings/update-vms", methods=["POST"])
@admin_required
def admin_mappings_update_vms():
    """Update VM assignments for selected user."""
    user = require_user()
    selected_user = (request.form.get("user") or "").strip()
    vmids_str = (request.form.get("vmids") or "").strip()
    
    if not selected_user:
        return redirect(url_for("admin_mappings", error="User is required"))
    
    try:
        if vmids_str:
            parts = re.split(r"[,\s]+", vmids_str)
            vmids = [int(p) for p in parts if p]
        else:
            vmids = []
        set_user_vm_mapping(selected_user, vmids)
        return redirect(url_for("admin_mappings", user=selected_user, message=f"Updated VM assignments for {selected_user}"))
    except ValueError:
        return redirect(url_for("admin_mappings", user=selected_user, error="VM IDs must be integers"))
    except Exception as e:
        app.logger.exception("Failed to update VM mappings for %s", selected_user)
        return redirect(url_for("admin_mappings", user=selected_user, error=f"Failed to update: {str(e)}"))


@app.route("/admin/mappings/unassign-form", methods=["POST"])
@admin_required
def admin_mappings_unassign_form():
    """Unassign a specific VM from selected user (form-based)."""
    user = require_user()
    selected_user = (request.form.get("user") or "").strip()
    vmid_str = (request.form.get("vmid") or "").strip()
    
    if not selected_user or not vmid_str:
        return redirect(url_for("admin_mappings", user=selected_user, error="User and VM ID are required"))
    
    try:
        vmid = int(vmid_str)
        mapping = get_user_vm_map()
        current_vms = mapping.get(selected_user, [])
        
        if vmid in current_vms:
            current_vms.remove(vmid)
            set_user_vm_mapping(selected_user, current_vms)
            return redirect(url_for("admin_mappings", user=selected_user, message=f"Removed VM {vmid} from {selected_user}"))
        else:
            return redirect(url_for("admin_mappings", user=selected_user, error=f"VM {vmid} not assigned to {selected_user}"))
    except ValueError:
        return redirect(url_for("admin_mappings", user=selected_user, error="Invalid VM ID"))
    except Exception as e:
        app.logger.exception("Failed to unassign VM %s from %s", vmid_str, selected_user)
        return redirect(url_for("admin_mappings", user=selected_user, error=f"Failed to unassign: {str(e)}"))


@app.route("/admin/mappings/grant-admin-form", methods=["POST"])
@admin_required
def admin_mappings_grant_admin_form():
    """Grant admin privileges to selected user (form-based)."""
    user = require_user()
    selected_user = (request.form.get("user") or "").strip()
    
    if not selected_user:
        return redirect(url_for("admin_mappings", error="User is required"))
    
    try:
        from proxmox_client import add_user_to_admin_group, ADMIN_GROUP
        if not ADMIN_GROUP:
            return redirect(url_for("admin_mappings", user=selected_user, error="ADMIN_GROUP not configured"))
        
        add_user_to_admin_group(selected_user)
        return redirect(url_for("admin_mappings", user=selected_user, message=f"Added {selected_user} to admin group"))
    except Exception as e:
        app.logger.exception("Failed to add %s to admin group", selected_user)
        return redirect(url_for("admin_mappings", user=selected_user, error=f"Failed to grant admin: {str(e)}"))


@app.route("/admin/mappings/remove-admin-form", methods=["POST"])
@admin_required
def admin_mappings_remove_admin_form():
    """Remove admin privileges from selected user (form-based)."""
    user = require_user()
    selected_user = (request.form.get("user") or "").strip()
    
    if not selected_user:
        return redirect(url_for("admin_mappings", error="User is required"))
    
    try:
        from proxmox_client import remove_user_from_admin_group, ADMIN_GROUP, ADMIN_USERS
        
        # Check if user is in protected list
        if selected_user in ADMIN_USERS:
            return redirect(url_for("admin_mappings", user=selected_user, error=f"{selected_user} is a protected admin (in ADMIN_USERS config)"))
        
        if not ADMIN_GROUP:
            return redirect(url_for("admin_mappings", user=selected_user, error="ADMIN_GROUP not configured"))
        
        remove_user_from_admin_group(selected_user)
        return redirect(url_for("admin_mappings", user=selected_user, message=f"Removed {selected_user} from admin group"))
    except Exception as e:
        app.logger.exception("Failed to remove %s from admin group", selected_user)
        return redirect(url_for("admin_mappings", user=selected_user, error=f"Failed to remove admin: {str(e)}"))


@app.route("/admin/mappings/unassign", methods=["POST"])
@admin_required
def api_unassign_vm():
    """Remove a single VM from a user's mapping."""
    try:
        data = request.get_json()
        user = data.get("user")
        vmid = int(data.get("vmid"))
        
        if not user or not vmid:
            return jsonify({"ok": False, "error": "Missing user or vmid"}), 400
        
        # Get current mapping
        mapping = get_user_vm_map()
        
        if user not in mapping:
            return jsonify({"ok": False, "error": "User has no mappings"}), 404
        
        # Remove the vmid from user's list
        if vmid in mapping[user]:
            mapping[user].remove(vmid)
            
            # If user has no more VMs, remove the user entry entirely
            if not mapping[user]:
                del mapping[user]
            
            # Save updated mapping
            set_user_vm_mapping(user, mapping.get(user, []))
            
            app.logger.info("Admin %s removed VM %d from user %s", require_user(), vmid, user)
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": "VM not in user's mappings"}), 404
            
    except Exception as e:
        app.logger.exception("Failed to unassign VM")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/mappings/add-admin", methods=["POST"])
@admin_required
def add_admin():
    """Add a user to the admin group."""
    try:
        user_to_add = request.form.get("user", "").strip()
        
        if not user_to_add:
            return redirect(url_for("admin_mappings"))
        
        from proxmox_client import add_user_to_admin_group
        success = add_user_to_admin_group(user_to_add)
        
        if success:
            app.logger.info("Admin %s added %s to admin group", require_user(), user_to_add)
        else:
            app.logger.error("Failed to add %s to admin group", user_to_add)
        
        return redirect(url_for("admin_mappings"))
    except Exception as e:
        app.logger.exception("Failed to add admin")
        return redirect(url_for("admin_mappings"))


@app.route("/admin/mappings/remove-admin", methods=["POST"])
@admin_required
def remove_admin():
    """Remove a user from the admin group."""
    try:
        data = request.get_json()
        user_to_remove = data.get("user", "").strip()
        
        if not user_to_remove:
            return jsonify({"ok": False, "error": "Missing user"}), 400
        
        # Don't allow removing users from ADMIN_USERS config
        from proxmox_client import ADMIN_USERS, remove_user_from_admin_group
        if user_to_remove in ADMIN_USERS:
            return jsonify({"ok": False, "error": "Cannot remove users defined in ADMIN_USERS config"}), 400
        
        success = remove_user_from_admin_group(user_to_remove)
        
        if success:
            app.logger.info("Admin %s removed %s from admin group", require_user(), user_to_remove)
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": "Failed to remove user from admin group"}), 500
            
    except Exception as e:
        app.logger.exception("Failed to remove admin")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin")
@admin_required
def admin_view():
    user = require_user()
    
    # Fast path: render empty page immediately for progressive loading
    # VMs will be loaded via /api/vms after page renders
    probe = probe_proxmox()
    return render_template(
        "index.html",
        progressive_load=True,
        windows_vms=[],
        linux_vms=[],
        lxc_containers=[],
        other_vms=[],
        user=user,
        message=None,
        probe=probe,
    )


@app.route("/admin/probe")
@admin_required
def admin_probe():
    """Return Proxmox diagnostics as JSON (admin-only)."""
    info = probe_proxmox()
    return jsonify(info)


# ---------------------------------------------------------------------------
# JSON API: VM list + power actions
# ---------------------------------------------------------------------------

@app.route("/api/vms")
@login_required
def api_vms():
    """
    Get VMs for current user.
    
    Uses cached VM data - switching between "My Machines" and "Admin View" 
    doesn't trigger new Proxmox API calls. The cache includes user mappings,
    so filtering is just a list comprehension, not a new API query.
    
    Query parameters:
    - skip_ips: Skip IP lookups for fast initial load
    - force_refresh: Bypass cache and fetch fresh data from Proxmox
    - search: Filter VMs by name/VMID/IP
    """
    user = require_user()
    # Check if we should skip IPs for fast initial load
    skip_ips = request.args.get('skip_ips', 'false').lower() == 'true'
    force_refresh = request.args.get('force_refresh', 'false').lower() == 'true'
    search = request.args.get('search', '')
    
    try:
        vms = get_vms_for_user(user, search=search or None, skip_ips=skip_ips, force_refresh=force_refresh)
        return jsonify(vms)
    except Exception as e:
        app.logger.exception("Failed to get VMs for user %s", user)
        return jsonify({
            "error": True,
            "message": f"Failed to load VMs: {str(e)}",
            "details": "Check if Proxmox server is reachable and credentials are correct"
        }), 500


@app.route("/api/vm/<int:vmid>/ip")
@login_required
def api_vm_ip(vmid: int):
    """Get IP address for a specific VM (lazy loading)."""
    user = require_user()
    try:
        vm = find_vm_for_user(user, vmid)
        if not vm:
            return jsonify({"error": "VM not found or not accessible"}), 404
        
        # First check if we have a scan status (from background ARP scan)
        from arp_scanner import get_scan_status
        scan_status = get_scan_status(vmid)
        
        if scan_status:
            # If status is an IP address (contains dots), return it
            if '.' in scan_status and scan_status.count('.') == 3:
                return jsonify({
                    "vmid": vmid,
                    "ip": scan_status,
                    "status": "complete"
                })
            # If scan failed or not found, fall through to direct lookup below
        
        # Direct IP lookup (guest agent/interfaces)
        # This is the fallback if ARP scan hasn't found it yet or failed
        ip = get_vm_ip(vmid, vm["node"], vm["type"])
        
        # If we found an IP via direct lookup, return it
        if ip:
            return jsonify({
                "vmid": vmid,
                "ip": ip,
                "status": "complete"
            })
        
        # No IP found - return the scan status if we have one, otherwise generic message
        return jsonify({
            "vmid": vmid,
            "ip": None,
            "status": scan_status if scan_status else "not_found"
        })
    except Exception as e:
        app.logger.exception("Failed to get IP for VM %d", vmid)
        return jsonify({"error": str(e), "status": "error"}), 500


@app.route("/api/mappings")
@admin_required
def api_mappings():
    """Return mappings data for progressive loading."""
    try:
        mapping = get_user_vm_map()
        users = get_pve_users()
        # Skip IPs for mappings - we only need vmid and name
        all_vms = get_all_vms(skip_ips=True)
        
        # Create vmid to name mapping for display
        vm_id_to_name = {v["vmid"]: v.get("name", f"vm-{v['vmid']}") for v in all_vms}
        all_vm_ids = sorted({v["vmid"] for v in all_vms})
        
        return jsonify({
            "mapping": mapping,
            "users": users,
            "all_vm_ids": all_vm_ids,
            "vm_id_to_name": vm_id_to_name,
        })
    except Exception as e:
        app.logger.exception("Failed to get mappings data")
        return jsonify({
            "error": True,
            "message": f"Failed to load mappings: {str(e)}"
        }), 500



@app.route("/api/vm/<int:vmid>/start", methods=["POST"])
@login_required
def api_vm_start(vmid: int):
    user = require_user()
    vm = find_vm_for_user(user, vmid)
    if not vm:
        return jsonify({"ok": False, "error": "VM not found or not allowed"}), 404

    try:
        start_vm(vm)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True})


@app.route("/api/vm/<int:vmid>/stop", methods=["POST"])
@login_required
def api_vm_stop(vmid: int):
    user = require_user()
    vm = find_vm_for_user(user, vmid)
    if not vm:
        return jsonify({"ok": False, "error": "VM not found or not allowed"}), 404

    try:
        shutdown_vm(vm)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# SSH Web Terminal
# ---------------------------------------------------------------------------

@app.route("/ssh/<int:vmid>")
@login_required
def ssh_terminal(vmid: int):
    """Render SSH terminal page for a VM."""
    user = require_user()
    vm = find_vm_for_user(user, vmid)
    
    if not vm:
        abort(404)
    
    ip = request.args.get('ip') or vm.get('ip')
    name = request.args.get('name') or vm.get('name', f'VM {vmid}')
    
    # Verify cached IP is still correct
    if ip:
        verified_ip = verify_vm_ip(vm['node'], vmid, vm['type'], ip)
        if verified_ip and verified_ip != ip:
            ip = verified_ip
            app.logger.info("Updated IP for VM %d: %s", vmid, ip)
    
    if not ip:
        return render_template(
            "error.html",
            error_title="SSH Terminal Not Available",
            error_message="VM does not have an IP address. Please start the VM and wait for network configuration.",
            back_url=url_for("portal")
        ), 503
    
    return render_template(
        "ssh_terminal.html",
        vmid=vmid,
        vm_name=name,
        ip=ip
    )


if WEBSOCKET_AVAILABLE:
    @sock.route("/ws/ssh/<int:vmid>")
    def ssh_websocket(ws, vmid: int):
        """WebSocket endpoint for SSH connection."""
        from ssh_handler import SSHWebSocketHandler
        
        # Get IP and username from query parameters
        ip = request.args.get('ip')
        username = request.args.get('username', 'root')
        
        if not ip:
            ws.send("Error: No IP address provided")
            return
        
        # Create SSH handler with username
        handler = SSHWebSocketHandler(ws, ip, username=username)
        
        # Connect to SSH
        if not handler.connect():
            ws.send("\r\n\x1b[1;31mFailed to establish SSH connection.\x1b[0m\r\n")
            return
        
        # Main message loop
        try:
            while handler.running:
                try:
                    message = ws.receive(timeout=1.0)
                    if message is None:
                        break
                    
                    # Parse message (expecting JSON with type and data)
                    try:
                        msg = json.loads(message)
                        msg_type = msg.get('type')
                        
                        if msg_type == 'input':
                            data = msg.get('data', '')
                            handler.handle_client_input(data)
                        elif msg_type == 'resize':
                            width = msg.get('width', 80)
                            height = msg.get('height', 24)
                            handler.resize_terminal(width, height)
                    except json.JSONDecodeError:
                        # If not JSON, treat as raw input
                        handler.handle_client_input(message)
                        
                except Exception as e:
                    app.logger.debug("WebSocket error: %s", e)
                    break
        finally:
            handler.close()


# ---------------------------------------------------------------------------
# RDP file download
# ---------------------------------------------------------------------------

@app.route("/rdp/<int:vmid>.rdp")
@login_required
def rdp_file(vmid: int):
    user = require_user()
    app.logger.info("rdp_file: user=%s requesting vmid=%s", user, vmid)
    
    vm = find_vm_for_user(user, vmid)
    if not vm:
        app.logger.warning("rdp_file: VM %s not found for user %s", vmid, user)
        abort(404)

    app.logger.info("rdp_file: Found VM: vmid=%s, name=%s, type=%s, category=%s, ip=%s", 
                    vm.get('vmid'), vm.get('name'), vm.get('type'), vm.get('category'), vm.get('ip'))

    if vm.get("category") != "windows":
        app.logger.warning("rdp_file: VM %s is not Windows (category=%s)", vmid, vm.get("category"))
        abort(404)

    # Verify cached IP before generating RDP
    cached_ip = vm.get('ip')
    if cached_ip and cached_ip != "Checking...":
        try:
            verified_ip = verify_vm_ip(vm['node'], vmid, vm.get('type', 'qemu'), cached_ip)
            if verified_ip != cached_ip:
                app.logger.info("rdp_file: IP changed for VM %s from %s to %s", vmid, cached_ip, verified_ip)
                vm['ip'] = verified_ip
        except Exception as e:
            app.logger.warning("rdp_file: IP verification failed for VM %s: %s", vmid, e)

    try:
        content = build_rdp(vm)
        filename = f"{vm.get('name', 'vm')}-{vmid}.rdp"
        
        app.logger.info("rdp_file: generated RDP for VM %s (%s) with IP %s", vmid, vm.get('name'), vm.get('ip'))
        
        # Ensure content is bytes for proper file download
        if isinstance(content, str):
            content = content.encode('utf-8')
        
        resp = Response(content, mimetype='application/x-rdp')
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp
    except ValueError as e:
        # Expected errors like missing IP address
        app.logger.warning("rdp_file: cannot generate RDP for VM %s: %s", vmid, e)
        return render_template(
            "error.html",
            error_title="RDP File Not Available",
            error_message=str(e),
            back_url=url_for("portal")
        ), 503
    except Exception as e:
        app.logger.exception("rdp_file: failed to generate RDP for VM %s: %s", vmid, e)
        return render_template(
            "error.html",
            error_title="RDP Generation Failed",
            error_message=f"Failed to generate RDP file: {str(e)}",
            back_url=url_for("portal")
        ), 500


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Run with threaded mode for concurrent requests
    app.run(host="0.0.0.0", port=8080, threaded=True, debug=False)
