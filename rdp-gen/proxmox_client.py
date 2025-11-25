import json
import logging
import os
import re
import time
from typing import Dict, List, Any, Optional

from proxmoxer import ProxmoxAPI

from config import (
    PVE_HOST,
    PVE_ADMIN_USER,
    PVE_ADMIN_PASS,
    PVE_VERIFY,
    VALID_NODES,
    ADMIN_USERS,
    ADMIN_GROUP,
    MAPPINGS_FILE,
    VM_CACHE_TTL,
    ENABLE_IP_LOOKUP,
)

# ---------------------------------------------------------------------------
# Proxmox connection (admin account)
# ---------------------------------------------------------------------------
class ProxmoxAdminWrapper:
    def __init__(self, admin: ProxmoxAPI):
        self._admin = admin

    def get_nodes(self):
        return self._admin.nodes.get()

    def list_qemu(self, node: str):
        # returns list of QEMU VMs + LXC containers for a node
        # Combine both qemu and lxc to get all VMs/containers
        vms = []
        try:
            qemu_vms = self._admin.nodes(node).qemu.get() or []
            vms.extend(qemu_vms)
            logger.debug(vms)
        except Exception as e:
            logger.debug("failed to list qemu on %s: %s", node, e)
        
        try:
            lxc_vms = self._admin.nodes(node).lxc.get() or []
            vms.extend(lxc_vms)
            logger.debug(lxc_vms)
        except Exception as e:
            logger.debug("failed to list lxc on %s: %s", node, e)
        
        return vms

    def status_qemu(self, node: str, vmid: int):
        return self._admin.nodes(node).qemu(vmid).status.get()


proxmox_admin = ProxmoxAPI(
    PVE_HOST,
    user=PVE_ADMIN_USER,
    password=PVE_ADMIN_PASS,
    verify_ssl=PVE_VERIFY,
)
proxmox_admin_wrapper = ProxmoxAdminWrapper(proxmox_admin)


# proxmox_admin_wrapper is a thin wrapper around `proxmox_admin` used by `ProxmoxCache`.

# logger
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VM cache: single cluster snapshot, reused everywhere
# ---------------------------------------------------------------------------

_vm_cache_data: Optional[List[Dict[str, Any]]] = None
_vm_cache_ts: float = 0.0


def _invalidate_vm_cache() -> None:
    global _vm_cache_data, _vm_cache_ts
    _vm_cache_data = None
    _vm_cache_ts = 0.0


def _guess_category(vm: Dict[str, Any]) -> str:
    """
    Basic OS category guess for UI grouping.

    - All LXC: 'linux'
    - QEMU whose name contains 'win': 'windows'
    - Other QEMU: 'linux'
    - Fallback: 'other'
    """
    vmtype = vm.get("type", "qemu")
    if vmtype == "lxc":
        return "linux"

    name = (vm.get("name") or "").lower()
    if "win" in name or "windows" in name:
        return "windows"

    if vmtype == "qemu":
        return "linux"

    return "other"


def _lookup_vm_ip(node: str, vmid: int, vmtype: str) -> Optional[str]:
    """
    Optional IP lookup via guest agent for QEMU VMs only.
    Skipped entirely unless ENABLE_IP_LOOKUP is True.

    For containers you'd typically use static IP assignment or DNS instead.
    """
    if not ENABLE_IP_LOOKUP:
        return None

    if vmtype != "qemu":
        return None

    try:
        data = proxmox_admin.nodes(node).qemu(vmid).agent.get(
            "network-get-interfaces"
        )
    except Exception as e:
        logger.debug("guest agent call failed for %s/%s: %s", node, vmid, e)
        return None

    if not data:
        return None
    try:
        interfaces = data.get("result", [])
    except AttributeError:
        interfaces = []

    for iface in interfaces:
        addrs = iface.get("ip-addresses", []) or []
        for addr in addrs:
            if addr.get("ip-address-type") == "ipv4":
                ip = addr.get("ip-address")
                # Skip 127.0.0.1
                if ip and not ip.startswith("127."):
                    return ip
    return None


def _build_vm_dict(raw: Dict[str, Any]) -> Dict[str, Any]:
    vmid = int(raw["vmid"])
    node = raw["node"]
    name = raw.get("name", f"vm-{vmid}")
    status = raw.get("status", "unknown")
    vmtype = raw.get("type", "qemu")

    category = _guess_category(raw)
    # Prefer any IP info embedded in the API response (e.g. cloud-init or cache),
    # fallback to guest agent lookup if enabled and running.
    ip = raw.get("ip")
    if not ip and status == "running":
        ip = _lookup_vm_ip(node, vmid, vmtype)

    return {
        "vmid": vmid,
        "node": node,
        "name": name,
        "status": status,
        "type": vmtype,      # 'qemu' or 'lxc'
        "category": category,
        "ip": ip,
    }


def get_all_vms() -> List[Dict[str, Any]]:
    """
    Cached list of all VMs/containers visible to the admin user.

    Each item: { vmid, node, name, status, ip, category, type }
    """
    global _vm_cache_data, _vm_cache_ts

    now = time.time()
    if _vm_cache_data is not None and (now - _vm_cache_ts) < VM_CACHE_TTL:
        return _vm_cache_data

    # Using the wrapper to keep per-node listing consistent.
    out: List[Dict[str, Any]] = []
    nodes = []
    if proxmox_admin_wrapper:
        try:
            nodes = proxmox_admin_wrapper.get_nodes()
            print(nodes)
        except Exception as e:
            logger.debug("failed to list nodes via wrapper: %s", e)
    else:
        try:
            resources = proxmox_admin.cluster.resources.get(type="vm") or []
            # convert to node list in the format expected by wrapper
            nodes = sorted({r["node"] for r in resources})
            nodes = [{"node": n} for n in nodes]
        except Exception as e:
            logger.debug("failed to get cluster resources: %s", e)

    for n in nodes or []:
        node = n["node"]
        if VALID_NODES and node not in VALID_NODES:
            continue
        # list per-node VMs via wrapper if available
        vmlist = []
        if proxmox_admin_wrapper:
            try:
                vmlist = proxmox_admin_wrapper.list_qemu(node)
            except Exception as e:
                logger.debug("failed to list qemu on %s via wrapper: %s", node, e)
                vmlist = []
        else:
            try:
                vmlist = proxmox_admin.nodes(node).qemu.get()
            except Exception as e:
                logger.debug("failed to list qemu on %s: %s", node, e)
                vmlist = []
        

        for vm in (vmlist or []):
            logger.debug(f"Processing VM: {vm}")
            print(f"Processing VM: {vm}")
            try:
                out.append(_build_vm_dict(vm))
                logger.debug(f"Building VM {vm}")
                print(out)
            except Exception:
                continue

    logger.info("get_all_vms: returning %d VM(s)", len(out))

    _vm_cache_data = out
    _vm_cache_ts = now
    return out


# ---------------------------------------------------------------------------
# User ↔ VM mappings (JSON file)
# ---------------------------------------------------------------------------

def _load_mapping() -> Dict[str, List[int]]:
    if not os.path.exists(MAPPINGS_FILE):
        return {}

    try:
        with open(MAPPINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.debug("failed to load mappings file %s: %s", MAPPINGS_FILE, e)
        return {}

    out: Dict[str, List[int]] = {}
    for user, vmids in data.items():
        try:
            out[user] = sorted({int(v) for v in vmids})
        except Exception:
            continue
    return out


def _save_mapping(mapping: Dict[str, List[int]]) -> None:
    tmp = {}
    for user, vmids in mapping.items():
        tmp[user] = sorted({int(v) for v in vmids})
    try:
        with open(MAPPINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(tmp, f, indent=2)
    except Exception as e:
        logger.exception("failed to save mappings file %s: %s", MAPPINGS_FILE, e)


def get_user_vm_map() -> Dict[str, List[int]]:
    """
    Returns { 'user@pve': [vmid, ...], ... }
    """
    return _load_mapping()


def set_user_vm_mapping(user: str, vmids: List[int]) -> None:
    """
    Update mapping for one user. Empty list removes mapping.
    """
    user = (user or "").strip()
    mapping = _load_mapping()
    if not user:
        return

    vmids_clean = sorted({int(v) for v in vmids if v is not None})

    if vmids_clean:
        mapping[user] = vmids_clean
    else:
        mapping.pop(user, None)

    _save_mapping(mapping)


def _user_in_group(user: str, groupid: str) -> bool:
    """
    Handles both possible Proxmox group member formats:
    - members: ["user@pve", "other@pve"]
    - members: [{"userid": "user@pve"}, {"userid": "other@pve"}]
    """
    if not groupid:
        return False

    try:
        data = proxmox_admin.access.groups(groupid).get()
        if not data:
            return False
    except Exception:
        return False

    raw_members = data.get("members", []) or []
    members = []
    for m in raw_members:
        if isinstance(m, str):
            members.append(m)
        elif isinstance(m, dict) and "userid" in m:
            members.append(m["userid"])

    # Accept both realm variants for matching: user (full), or username@pam, username@pve
    variants = {user}
    if "@" in user:
        name, realm = user.split("@", 1)
        for r in ("pam", "pve"):
            variants.add(f"{name}@{r}")
    return any(u in members for u in variants)


def _debug_is_admin_user(user: str) -> bool:
    try:
        grp = proxmox_admin.access.groups(ADMIN_GROUP).get()
    except Exception:
        grp = {}
    raw_members = (grp.get("members", []) or []) if isinstance(grp, dict) else []
    members = []
    for m in raw_members:
        if isinstance(m, str):
            members.append(m)
        elif isinstance(m, dict) and "userid" in m:
            members.append(m["userid"])
    logger.debug("DEBUG: ADMIN_GROUP=%s members=%s, user=%s", ADMIN_GROUP, members, user)
    return user in members


def is_admin_user(user: str) -> bool:
    """
    Admin if:
    - explicitly listed in ADMIN_USERS, OR
    - member of ADMIN_GROUP (if configured).
    """
    if not user:
        return False
    if user in ADMIN_USERS:
        return True
    if ADMIN_GROUP:
        res = _user_in_group(user, ADMIN_GROUP)
        logger.debug("is_admin_user(%s) -> %s (ADMIN_USERS=%s, ADMIN_GROUP=%s)", user, res, ADMIN_USERS, ADMIN_GROUP)
        return res
    return False


def get_vms_for_user(user: str) -> List[Dict[str, Any]]:
    """
    Non-admin: only VMs listed in mappings.
    Admin: all VMs.
    """
    vms = get_all_vms()
    print(vms)
    admin = is_admin_user(user)
    logger.debug("get_vms_for_user: user=%s is_admin=%s all_vms=%d", user, admin, len(vms))
    if admin:
        return vms

    mapping = get_user_vm_map()
    allowed = set(mapping.get(user, []))
    logger.debug("get_vms_for_user: user=%s allowed_vmids=%s", user, allowed)
    if not allowed:
        return []

    result = [vm for vm in vms if vm["vmid"] in allowed]
    logger.debug("get_vms_for_user: user=%s visible_vms=%d", user, len(result))
    return result


def find_vm_for_user(user: str, vmid: int) -> Optional[Dict[str, Any]]:
    """
    Return VM dict if the user is allowed to see/control it.
    """
    try:
        vmid = int(vmid)
    except Exception:
        return None

    for vm in get_vms_for_user(user):
        if vm["vmid"] == vmid:
            return vm
    logger.debug("find_vm_for_user: user=%s vmid=%s -> not found", user, vmid)
    return None


# ---------------------------------------------------------------------------
# Actions: RDP file + power control
# ---------------------------------------------------------------------------

def build_rdp(vm: Dict[str, Any]) -> str:
    """
    Build a minimal .rdp file content for a Windows VM.
    """
    ip = vm.get("ip") or "<ip>"
    name = vm.get("name") or f"vm-{vm.get('vmid')}"
    # Simple RDP profile; Windows will accept this format.
    return (
        f"full address:s:{ip}:3389\r\n"
        f"prompt for credentials:i:1\r\n"
        f"username:s:.\r\n"
        f"administrative session:i:0\r\n"
        f"authentication level:i:2\r\n"
        f"session bpp:i:32\r\n"
        f"screen mode id:i:2\r\n"
        f"desktopwidth:i:1280\r\n"
        f"desktopheight:i:720\r\n"
        f"compression:i:1\r\n"
        f"displayconnectionbar:i:1\r\n"
        f"drivestoredirect:s:*\r\n"
        f"redirectclipboard:i:1\r\n"
        f"autoreconnection enabled:i:1\r\n"
        f"alternate shell:s:\r\n"
        f"shell working directory:s:\r\n"
        f"remoteapplicationmode:i:0\r\n"
        f"alternate full address:s:{ip}\r\n"
        f"pcb:s:{name}\r\n"
    )


def start_vm(vm: Dict[str, Any]) -> None:
    vmid = int(vm["vmid"])
    node = vm["node"]
    vmtype = vm.get("type", "qemu")

    try:
        if vmtype == "lxc":
            proxmox_admin.nodes(node).lxc(vmid).status.start.post()
        else:
            proxmox_admin.nodes(node).qemu(vmid).status.start.post()
    except Exception as e:
        logger.exception("failed to start vm %s/%s: %s", node, vmid, e)

    _invalidate_vm_cache()


def shutdown_vm(vm: Dict[str, Any]) -> None:
    vmid = int(vm["vmid"])
    node = vm["node"]
    vmtype = vm.get("type", "qemu")

    try:
        if vmtype == "lxc":
            # Graceful stop for containers
            proxmox_admin.nodes(node).lxc(vmid).status.shutdown.post()
        else:
            # Graceful shutdown for QEMU; use .stop() if you want hard power-off
            proxmox_admin.nodes(node).qemu(vmid).status.shutdown.post()
    except Exception as e:
        logger.exception("failed to shutdown vm %s/%s: %s", node, vmid, e)

    _invalidate_vm_cache()


# ---------------------------------------------------------------------------
# User listing for mappings UI
# ---------------------------------------------------------------------------

def get_pve_users() -> List[Dict[str, str]]:
    """
    Return list of enabled PVE-realm users: [{ 'userid': 'user@pve' }, ...]
    """
    try:
        users = proxmox_admin.access.users.get()
    except Exception as e:
        logger.debug("failed to list pve users: %s", e)
        return []

    if users is None:
        users = []

    out: List[Dict[str, str]] = []
    for u in users:
        userid = u.get("userid")
        enable = u.get("enable", 1)
        if not userid or "@" not in userid:
            continue
        name, realm = userid.split("@", 1)
        if realm != "pve":
            continue
        if enable == 0:
            continue
        out.append({"userid": userid})

    out.sort(key=lambda x: x["userid"])
    return out


def probe_proxmox() -> Dict[str, Any]:
    """Return diagnostics information helpful for admin troubleshooting.

    Contains node list, resource count, a small sample of resources, and any error
    messages encountered while querying the Proxmox API.
    """
    info: Dict[str, Any] = {
        "ok": True,
        "nodes": [],
        "nodes_count": 0,
        "resources_count": 0,
        "resources_sample": [],
        "valid_nodes": VALID_NODES,
        "admin_users": ADMIN_USERS,
        "admin_group": ADMIN_GROUP,
        "error": None,
    }
    try:
        nodes = proxmox_admin.nodes.get() or []
        info["nodes"] = [n.get("node") for n in nodes if isinstance(n, dict) and "node" in n]
        info["nodes_count"] = len(info["nodes"])
    except Exception as e:
        info["ok"] = False
        info["error"] = f"nodes error: {e}"
        logger.exception("probe_proxmox: nodes error")
        return info

    try:
        resources = proxmox_admin.cluster.resources.get(type="vm") or []
        info["resources_count"] = len(resources)
        info["resources_sample"] = [
            {"vmid": r.get("vmid"), "node": r.get("node"), "name": r.get("name")} for r in resources[:10]
        ]
    except Exception as e:
        info["ok"] = False
        info["error"] = f"resources error: {e}"
        logger.exception("probe_proxmox: resources error")
        return info

    return info
