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

from config import SECRET_KEY, VM_CACHE_TTL
from auth import login_required, current_user, authenticate_proxmox_user
from proxmox_client import (
    get_all_vms,
    get_vms_for_user,
    find_vm_for_user,
    is_admin_user,
    build_rdp,
    start_vm,
    shutdown_vm,
    get_user_vm_map,
    set_user_vm_mapping,
    get_pve_users,
    proxmox_admin_wrapper,
    probe_proxmox,
    create_pve_user,
)

import re
import logging

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Optional cache (uses wrapper or real client)
from cache import ProxmoxCache
cache = ProxmoxCache(proxmox_admin_wrapper, ttl=VM_CACHE_TTL)


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
    message = None
    error = None
    selected_user = None

    if request.method == "POST":
        # selected_user is the user being edited, not the logged-in user
        selected_user = (request.form.get("user") or "").strip()
        vmids_str = (request.form.get("vmids") or "").strip()

        if not selected_user:
            error = "User is required."
        else:
            try:
                if vmids_str:
                    parts = re.split(r"[,\s]+", vmids_str)
                    vmids = [int(p) for p in parts if p]
                else:
                    vmids = []
                set_user_vm_mapping(selected_user, vmids)
                message = "Mapping updated."
            except ValueError:
                error = "VM IDs must be integers."

    # Progressive loading - return minimal data on GET
    if request.method == "GET":
        return render_template(
            "mappings.html",
            user=user,
            progressive_load=True,
            message=message,
            error=error,
        )
    
    # POST request - load full data for form submission
    mapping = get_user_vm_map()
    users = get_pve_users()
    all_vms = get_all_vms()
    
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
    )


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
    user = require_user()
    try:
        vms = get_vms_for_user(user)
        return jsonify(vms)
    except Exception as e:
        app.logger.exception("Failed to get VMs for user %s", user)
        return jsonify({
            "error": True,
            "message": f"Failed to load VMs: {str(e)}",
            "details": "Check if Proxmox server is reachable and credentials are correct"
        }), 500


@app.route("/api/mappings")
@admin_required
def api_mappings():
    """Return mappings data for progressive loading."""
    try:
        mapping = get_user_vm_map()
        users = get_pve_users()
        all_vms = get_all_vms()
        
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
    app.run(host="0.0.0.0", port=8080)
