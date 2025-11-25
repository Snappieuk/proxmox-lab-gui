#!/usr/bin/env python3
from flask import (
    Flask, Response, render_template, jsonify,
    request, redirect, url_for, session, abort
)
from config import SECRET_KEY
from auth import login_required, current_user, authenticate_proxmox_user
from proxmox_client import (
    get_all_vms, get_vms_for_user, find_vm_for_user,
    is_admin_user, build_rdp, start_vm, shutdown_vm,
    get_user_vm_map, set_user_vm_mapping, get_pve_users,
)

app = Flask(__name__)
app.secret_key = SECRET_KEY


@app.context_processor
def inject_admin_flag():
    user = session.get("user")
    return {"is_admin": is_admin_user(user) if user else False}


@app.route("/", methods=["GET"])
def root():
    if current_user():
        return redirect(url_for("portal"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        full_user = authenticate_proxmox_user(username, password)
        if full_user:
            session["user"] = full_user
            next_url = request.args.get("next") or url_for("portal")
            return redirect(next_url)
        else:
            error = "Invalid username or password."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/portal")
@login_required
def portal():
    user = current_user()
    vms = get_vms_for_user(user)

    windows_vms = [v for v in vms if v["category"] == "windows"]
    linux_vms   = [v for v in vms if v["category"] == "linux"]
    other_vms   = [v for v in vms if v["category"] == "other"]

    return render_template(
        "index.html",
        windows_vms=windows_vms,
        linux_vms=linux_vms,
        other_vms=other_vms,
        user=user,
    )


@app.route("/admin")
@login_required
def admin_view():
    user = current_user()
    if not is_admin_user(user):
        abort(403)

    vms = get_all_vms()
    windows_vms = [v for v in vms if v["category"] == "windows"]
    linux_vms   = [v for v in vms if v["category"] == "linux"]
    other_vms   = [v for v in vms if v["category"] == "other"]

    return render_template(
        "index.html",
        windows_vms=windows_vms,
        linux_vms=linux_vms,
        other_vms=other_vms,
        user=user,
    )


@app.route("/admin/mappings", methods=["GET", "POST"])
@login_required
def admin_mappings():
    user = current_user()
    if not is_admin_user(user):
        abort(403)

    message = None
    error = None

    if request.method == "POST":
        target_user = request.form.get("user", "").strip()
        vmid_str   = request.form.get("vmids", "").strip()

        if not target_user:
            error = "User is required."
        else:
            try:
                if vmid_str:
                    vmids = [int(v) for v in vmid_str.replace(" ", "").split(",") if v]
                else:
                    vmids = []
                set_user_vm_mapping(target_user, vmids)
                message = f"Updated mapping for {target_user}."
            except ValueError:
                error = "VMIDs must be integers, comma-separated."

    all_vms = get_all_vms()
    vm_ids = [v["vmid"] for v in all_vms]
    users = get_pve_users()
    mapping = get_user_vm_map()

    return render_template(
        "mappings.html",
        user=user,
        users=users,
        mapping=mapping,
        all_vm_ids=vm_ids,
        message=message,
        error=error,
    )


@app.route("/rdp/<int:vmid>.rdp")
@login_required
def rdp(vmid):
    user = current_user()
    vm = find_vm_for_user(user, vmid)
    if not vm:
        return "VMID not found or not allowed", 404
    if vm["category"] != "windows":
        return "RDP only valid for Windows VMs", 400
    if not vm["ip"]:
        return "No IP available", 404

    rdp_text = build_rdp(vm["ip"])
    return Response(
        rdp_text,
        mimetype="application/x-rdp",
        headers={"Content-Disposition": f'attachment; filename=\"vm-{vmid}.rdp\"'}
    )


@app.route("/api/vms")
@login_required
def api_vms():
    user = current_user()
    vms = get_vms_for_user(user)
    return jsonify([
        {
            "vmid": v["vmid"],
            "status": v.get("status", "unknown"),
            "ip": v.get("ip"),
            "category": v.get("category"),
        }
        for v in vms
    ])


@app.route("/api/vm/<int:vmid>/start", methods=["POST"])
@login_required
def api_vm_start(vmid):
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
def api_vm_stop(vmid):
    user = current_user()
    vm = find_vm_for_user(user, vmid)
    if not vm:
        return jsonify({"ok": False, "error": "VM not found or not allowed"}), 404
    try:
        shutdown_vm(vm)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

