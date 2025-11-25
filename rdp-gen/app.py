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

from config import SECRET_KEY
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
)

import re

app = Flask(__name__)
app.secret_key = SECRET_KEY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        user = current_user()
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
        # Already logged in
        return redirect(url_for("index"))

    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        full_user = authenticate_proxmox_user(username, password)
        if not full_user:
            error = "Invalid username or password."
        else:
            session["user"] = full_user
            next_url = request.args.get("next") or url_for("index")
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
@login_required
def index():
    user = current_user()
    vms = get_vms_for_user(user)

    windows_vms = [v for v in vms if v.get("category") == "windows"]
    linux_vms = [v for v in vms if v.get("category") == "linux"]
    other_vms = [v for v in vms if v.get("category") not in ("windows", "linux")]

    return render_template(
        "index.html",
        windows_vms=windows_vms,
        linux_vms=linux_vms,
        other_vms=other_vms,
    )


# ---------------------------------------------------------------------------
# Admin: user ↔ VM mappings
# ---------------------------------------------------------------------------

@app.route("/mappings", methods=["GET", "POST"])
@admin_required
def mappings():
    message = None
    error = None

    if request.method == "POST":
        user = (request.form.get("user") or "").strip()
        vmids_str = (request.form.get("vmids") or "").strip()

        if not user:
            error = "User is required."
        else:
            try:
                if vmids_str:
                    parts = re.split(r"[,\s]+", vmids_str)
                    vmids = [int(p) for p in parts if p]
                else:
                    vmids = []
                set_user_vm_mapping(user, vmids)
                message = "Mapping updated."
            except ValueError:
                error = "VM IDs must be integers."

    mapping = get_user_vm_map()
    users = get_pve_users()
    all_vm_ids = sorted({v["vmid"] for v in get_all_vms()})

    return render_template(
        "mappings.html",
        mapping=mapping,
        users=users,
        all_vm_ids=all_vm_ids,
        message=message,
        error=error,
    )


# ---------------------------------------------------------------------------
# JSON API: VM list + power actions
# ---------------------------------------------------------------------------

@app.route("/api/vms")
@login_required
def api_vms():
    user = current_user()
    vms = get_vms_for_user(user)
    return jsonify(vms)


@app.route("/api/vm/<int:vmid>/start", methods=["POST"])
@login_required
def api_vm_start(vmid: int):
    user = current_user()
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
    user = current_user()
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
    user = current_user()
    vm = find_vm_for_user(user, vmid)
    if not vm:
        abort(404)

    if vm.get("category") != "windows":
        abort(404)

    content = build_rdp(vm)
    filename = f"{vm.get('name', 'vm')}-{vmid}.rdp"

    resp = Response(content)
    resp.headers["Content-Type"] = "application/x-rdp"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
