#!/usr/bin/env python3
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
    vms = get_vms_for_user(user)
 #   print("got here")
#  print(vms)
    app.logger.info("portal: user=%s vms=%d", user, len(vms))

    windows_vms = [v for v in vms if v.get("category") == "windows"]
    linux_vms = [v for v in vms if v.get("category") == "linux"]
    other_vms = [v for v in vms if v.get("category") not in ("windows", "linux")]

    message = None
    if not vms:
        if is_admin_user(user):
            message = "No VMs discovered — check Proxmox connection or cluster resources."
        else:
            message = "No VMs assigned to you — ask an admin to map VMs."
    return render_template(
        "index.html",
        windows_vms=windows_vms,
        linux_vms=linux_vms,
        other_vms=other_vms,
        message=message,
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

    mapping = get_user_vm_map()
    users = get_pve_users()
    all_vm_ids = sorted({v["vmid"] for v in get_all_vms()})

    return render_template(
        "mappings.html",
        user=user,
        mapping=mapping,
        users=users,
        all_vm_ids=all_vm_ids,
        message=message,
        error=error,
        selected_user=selected_user,
    )


@app.route("/admin")
@admin_required
def admin_view():
    user = require_user()
    vms = get_all_vms()
    app.logger.info("admin_view: user=%s vms=%d", user, len(vms))
    windows_vms = [v for v in vms if v.get("category") == "windows"]
    linux_vms = [v for v in vms if v.get("category") == "linux"]
    other_vms = [v for v in vms if v.get("category") not in ("windows", "linux")]
    message = None
    probe = probe_proxmox()
    if not vms:
        message = "No VMs discovered — check Proxmox connection or credentials."
    return render_template(
        "index.html",
        windows_vms=windows_vms,
        linux_vms=linux_vms,
        other_vms=other_vms,
        user=user,
        message=message,
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
    vms = get_vms_for_user(user)
    return jsonify(vms)


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

    if vm.get("category") != "windows":
        app.logger.warning("rdp_file: VM %s is not Windows (category=%s)", vmid, vm.get("category"))
        abort(404)

    try:
        content = build_rdp(vm)
        filename = f"{vm.get('name', 'vm')}-{vmid}.rdp"
        
        app.logger.info("rdp_file: generated RDP for VM %s (%s)", vmid, vm.get('name'))
        
        resp = Response(content)
        resp.headers["Content-Type"] = "application/x-rdp"
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
        abort(500)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="0.0.0.0", port=8080)
