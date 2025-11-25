import time
import re
import os
import json
from proxmoxer import ProxmoxAPI
from config import (
    PVE_HOST, PVE_ADMIN_USER, PVE_ADMIN_PASS, PVE_VERIFY,
    VALID_NODES, ADMIN_USERS, VM_CACHE_TTL, MAPPINGS_FILE,
    ADMIN_GROUP,
)

# Admin Proxmox handle (used for all VM queries/actions)
proxmox_admin = ProxmoxAPI(
    PVE_HOST,
    user=PVE_ADMIN_USER,
    password=PVE_ADMIN_PASS,
    verify_ssl=PVE_VERIFY,
)

# In-process cache for VM list
_vm_cache_data = None
_vm_cache_ts = 0.0


_admin_cache = {}
_admin_cache_ttl = 60  # cache 1 minute
_admin_cache_ts = {}


def classify_ostype(ostype: str) -> str:
    ostype = (ostype or "").lower()
    if ostype.startswith("win"):
        return "windows"
    if ostype.startswith("l"):
        return "linux"
    return "other"


def _get_vm_ip_via_agent(node, vmid):
    res = proxmox_admin.nodes(node).qemu(vmid).agent("network-get-interfaces").get()
    for iface in res.get("result", []):
        for ip in iface.get("ip-addresses", []):
            if ip.get("ip-address-type") == "ipv4":
                addr = ip["ip-address"]
                if not addr.startswith("169.254.") and addr != "127.0.0.1":
                    return addr
    return None


def _get_vm_ip_via_cloudinit(node, vmid):
    cfg = proxmox_admin.nodes(node).qemu(vmid).config.get()
    ipconf = cfg.get("ipconfig0", "")
    m = re.search(r"ip=([^/]+)", ipconf)
    return m.group(1) if m else None


def _get_vm_ip(node, vmid):
    # Guest agent first
    try:
        ip = _get_vm_ip_via_agent(node, vmid)
        if ip:
            return ip
    except Exception:
        pass

    # Cloud-init fallback
    try:
        ip = _get_vm_ip_via_cloudinit(node, vmid)
        if ip:
            return ip
    except Exception:
        pass

    return None


def get_all_vms():
    """
    Cached list of all VMs admin can see.
    Each item: { vmid, node, name, status, ip, category }
    """
    global _vm_cache_data, _vm_cache_ts

    now = time.time()
    if _vm_cache_data is not None and (now - _vm_cache_ts) < VM_CACHE_TTL:
        return _vm_cache_data

    vmlist = []
    vms = proxmox_admin.cluster.resources.get(type="vm")

    for vm in vms:
        vmid = vm["vmid"]
        node = vm["node"]

        if VALID_NODES and node not in VALID_NODES:
            continue

        name = vm.get("name", f"vm-{vmid}")
        status = vm.get("status", "unknown")

        # Skip stale entries / wrong node
        try:
            cfg = proxmox_admin.nodes(node).qemu(vmid).config.get()
        except Exception:
            continue

        ostype = cfg.get("ostype", "")
        category = classify_ostype(ostype)
        ip = _get_vm_ip(node, vmid)

        vmlist.append({
            "vmid": vmid,
            "node": node,
            "name": name,
            "status": status,
            "ip": ip,
            "category": category,
        })

    vmlist.sort(key=lambda x: x["vmid"])
    _vm_cache_data = vmlist
    _vm_cache_ts = now
    return vmlist


def _user_in_group(user: str, group: str) -> bool:
    """
    Handles both possible Proxmox group member formats:
    - members: ["user@pve", "other@pve"]
    - members: [{"userid": "user@pve"}, {"userid": "other@pve"}]
    """
    if not group:
        return False

    try:
        grp = proxmox_admin.access.groups(group).get()
    except Exception:
        return False

    raw_members = grp.get("members", []) or []
    members = []

    for m in raw_members:
        if isinstance(m, str):
            members.append(m)
        elif isinstance(m, dict) and "userid" in m:
            members.append(m["userid"])

    return user in members

def is_admin_user(user: str) -> bool:
    """
    Admin if:
    - explicitly listed in ADMIN_USERS, OR
    - member of ADMIN_GROUP.
    """
    if not user:
        return False
    if user in ADMIN_USERS:
        return True
    return _user_in_group(user, ADMIN_GROUP)


def is_admin_user(user: str) -> bool:
    if not user:
        return False

    now = time.time()

    # return cached value if still valid
    if user in _admin_cache and (now - _admin_cache_ts.get(user, 0)) < _admin_cache_ttl:
        return _admin_cache[user]

    # compute fresh
    is_admin = False
    if user in ADMIN_USERS:
        is_admin = True
    elif ADMIN_GROUP:
        is_admin = _user_in_group(user, ADMIN_GROUP)

    # store in cache
    _admin_cache[user] = is_admin
    _admin_cache_ts[user] = now
    return is_admin


# ---------- JSON mappings ----------

def _load_user_vm_map():
    if not os.path.exists(MAPPINGS_FILE):
        return {}
    try:
        with open(MAPPINGS_FILE, "r") as f:
            data = json.load(f)
        result = {}
        for user, vmids in data.items():
            result[str(user)] = [int(v) for v in vmids]
        return result
    except Exception:
        return {}


def _save_user_vm_map(mapping: dict):
    with open(MAPPINGS_FILE, "w") as f:
        json.dump(mapping, f, indent=2)


def get_user_vm_map():
    """
    Return dict { user -> [vmid, ...] }.
    """
    return _load_user_vm_map()


def set_user_vm_mapping(user: str, vmids):
    """
    Set the allowed VMIDs for a given user (overwrite existing).
    vmids should be list[int].
    """
    mapping = _load_user_vm_map()
    if vmids:
        mapping[user] = sorted(set(int(v) for v in vmids))
    else:
        mapping.pop(user, None)
    _save_user_vm_map(mapping)


# ---------- Per-user VM access ----------

def get_vms_for_user(user: str):
    all_vms = get_all_vms()
    if is_admin_user(user):
        return all_vms

    mapping = _load_user_vm_map()
    allowed = set(mapping.get(user, []))
    if not allowed:
        return []
    return [v for v in all_vms if v["vmid"] in allowed]


def find_vm_for_user(user: str, vmid: int):
    vms = get_vms_for_user(user)
    for v in vms:
        if v["vmid"] == vmid:
            return v
    return None


def build_rdp(ip, port=3389):
    return f"""full address:s:{ip}:{port}
prompt for credentials:i:1
screen mode id:i:2
session bpp:i:32
compression:i:1
redirectclipboard:i:1
drivestoredirect:s:
autoreconnection enabled:i:1
authentication level:i:2
enablecredsspsupport:i:1
"""


def start_vm(vm):
    proxmox_admin.nodes(vm["node"]).qemu(vm["vmid"]).status.start.post()


def shutdown_vm(vm):
    # graceful ACPI shutdown
    proxmox_admin.nodes(vm["node"]).qemu(vm["vmid"]).status.shutdown.post()


# ---------- List Proxmox users (for mappings UI) ----------

def get_pve_users():
    """
    Return list of Proxmox users in 'pve' realm (not disabled).
    Each item: { 'userid': 'name@pve' }
    """
    users = proxmox_admin.access.users.get()
    out = []
    for u in users:
        userid = u.get("userid")
        enable = u.get("enable", 1)
        if not userid:
            continue
        if "@" not in userid:
            continue
        name, realm = userid.split("@", 1)
        if realm != "pve":
            continue
        if enable == 0:
            continue
        out.append({"userid": userid})
    out.sort(key=lambda x: x["userid"])
    return out

