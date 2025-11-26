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

# Disable SSL warnings when certificate verification is disabled
if not PVE_VERIFY:
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except ImportError:
        pass  # urllib3 not available, warnings will show but won't crash

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
            for vm in qemu_vms:
                vm['node'] = node
            vms.extend(qemu_vms)
            logger.debug(vms)
        except Exception as e:
            logger.debug("failed to list qemu on %s: %s", node, e)
        
        try:
            lxc_vms = self._admin.nodes(node).lxc.get() or []
            for vm in lxc_vms:
                vm['node'] = node
            vms.extend(lxc_vms)
            logger.debug(lxc_vms)
        except Exception as e:
            logger.debug("failed to list lxc on %s: %s", node, e)
        
        return vms

    def status_qemu(self, node: str, vmid: int):
        return self._admin.nodes(node).qemu(vmid).status.get()


# logger
logger = logging.getLogger(__name__)

# Proxmox connection per-worker (recreated after fork to avoid SSL context issues)
_proxmox_admin = None
_proxmox_admin_wrapper = None
_proxmox_pid = None  # Track which process created the connection
_connection_lock = None  # Thread lock for connection creation

def _get_connection_lock():
    """Get or create a threading lock for connection creation."""
    global _connection_lock
    if _connection_lock is None:
        import threading
        _connection_lock = threading.Lock()
    return _connection_lock

def _create_proxmox_connection():
    """Create a fresh ProxmoxAPI connection with retry on SSL errors."""
    import ssl  # Import here to avoid loading SSL context on module import
    max_retries = 3
    for attempt in range(max_retries):
        try:
            conn = ProxmoxAPI(
                PVE_HOST,
                user=PVE_ADMIN_USER,
                password=PVE_ADMIN_PASS,
                verify_ssl=PVE_VERIFY,
            )
            # Test the connection
            conn.version.get()
            return conn
        except (ssl.SSLError, OSError) as e:
            if attempt < max_retries - 1:
                logger.warning("SSL error creating connection (attempt %d/%d): %s", attempt + 1, max_retries, e)
                time.sleep(0.5)  # Brief delay before retry
            else:
                logger.error("Failed to create Proxmox connection after %d attempts: %s", max_retries, e)
                raise

def get_proxmox_admin(force_reconnect=False):
    """
    Get or create the Proxmox API connection.
    Recreates connection if process has forked (Gunicorn workers) or on SSL errors.
    Thread-safe with locking.
    """
    global _proxmox_admin, _proxmox_pid
    current_pid = os.getpid()
    
    # Recreate connection if we're in a new process (post-fork) or forced
    if force_reconnect or _proxmox_admin is None or _proxmox_pid != current_pid:
        with _get_connection_lock():
            # Double-check after acquiring lock
            if force_reconnect or _proxmox_admin is None or _proxmox_pid != current_pid:
                _proxmox_admin = _create_proxmox_connection()
                _proxmox_pid = current_pid
                logger.info("Connected to Proxmox at %s (PID: %s)", PVE_HOST, current_pid)
    
    if _proxmox_admin is None:
        raise RuntimeError("Failed to establish Proxmox connection")
    return _proxmox_admin

def get_proxmox_admin_wrapper(force_reconnect=False):
    """Get or create the Proxmox admin wrapper (recreated per worker)."""
    global _proxmox_admin_wrapper, _proxmox_pid
    current_pid = os.getpid()
    
    # Recreate wrapper if we're in a new process or forced
    if force_reconnect or _proxmox_admin_wrapper is None or _proxmox_pid != current_pid:
        _proxmox_admin_wrapper = ProxmoxAdminWrapper(get_proxmox_admin(force_reconnect))
    return _proxmox_admin_wrapper

def _api_call_with_retry(func, *args, **kwargs):
    """
    Execute an API call with automatic retry on SSL errors.
    Recreates connection once on SSL/connection errors.
    """
    import ssl  # Import here to avoid loading SSL context on module import
    try:
        return func(*args, **kwargs)
    except (ssl.SSLError, ConnectionError, BrokenPipeError, OSError) as e:
        logger.warning("API call failed with connection error, reconnecting: %s", e)
        # Force reconnection and retry once
        get_proxmox_admin(force_reconnect=True)
        return func(*args, **kwargs)

# ---------------------------------------------------------------------------
# VM cache: single cluster snapshot, reused everywhere
# ---------------------------------------------------------------------------

_vm_cache_data: Optional[List[Dict[str, Any]]] = None
_vm_cache_ts: float = 0.0

# IP address cache: vmid -> (ip, timestamp)
# Separate cache with longer TTL to avoid hammering guest agent
_ip_cache: Dict[int, tuple[Optional[str], float]] = {}
IP_CACHE_TTL = 300  # 5 minutes

def _invalidate_vm_cache() -> None:
    global _vm_cache_data, _vm_cache_ts
    _vm_cache_data = None
    _vm_cache_ts = 0.0

def _get_cached_ip(vmid: int) -> Optional[str]:
    """Get IP from cache if not expired."""
    if vmid in _ip_cache:
        ip, ts = _ip_cache[vmid]
        if time.time() - ts < IP_CACHE_TTL:
            return ip
    return None

def _cache_ip(vmid: int, ip: Optional[str]) -> None:
    """Store IP in cache."""
    _ip_cache[vmid] = (ip, time.time())


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
    IP lookup for both QEMU VMs (via guest agent) and LXC containers (via network interfaces).
    Skipped entirely unless ENABLE_IP_LOOKUP is True.
    """
    if not ENABLE_IP_LOOKUP:
        return None

    # LXC containers - use network interfaces API (much more reliable)
    if vmtype == "lxc":
        try:
            interfaces = _api_call_with_retry(lambda: get_proxmox_admin().nodes(node).lxc(vmid).interfaces.get())
            if interfaces:
                for iface in interfaces:
                    if iface.get("name") in ("eth0", "veth0"):  # Primary interface
                        inet = iface.get("inet")
                        if inet:
                            # inet format is usually "IP/CIDR"
                            ip = inet.split("/")[0] if "/" in inet else inet
                            if ip and not ip.startswith("127."):
                                return ip
                    # Fallback: check inet6 or any interface with an IP
                    inet = iface.get("inet")
                    if inet:
                        ip = inet.split("/")[0] if "/" in inet else inet
                        if ip and not ip.startswith("127."):
                            return ip
        except Exception as e:
            logger.debug("lxc interface call failed for %s/%s: %s", node, vmid, e)
            return None

    # QEMU VMs - use guest agent
    if vmtype == "qemu":
        try:
            data = _api_call_with_retry(lambda: get_proxmox_admin().nodes(node).qemu(vmid).agent.get(
                "network-get-interfaces"
            ))
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
    
    # Check IP cache first to avoid slow guest agent calls
    if not ip:
        ip = _get_cached_ip(vmid)
    
    # Lookup IPs:
    # - LXC: Always try (config available even when stopped)
    # - QEMU: Only when running (guest agent required)
    if not ip:
        if vmtype == "lxc" or status == "running":
            ip = _lookup_vm_ip(node, vmid, vmtype)
            # Cache the result (even if None) to avoid repeated failed lookups
            _cache_ip(vmid, ip)

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
    
    Uses ProxmoxCache for the base VM list, then enriches with IPs.
    """
    global _vm_cache_data, _vm_cache_ts

    now = time.time()
    if _vm_cache_data is not None and (now - _vm_cache_ts) < VM_CACHE_TTL:
        return _vm_cache_data

    # Using the wrapper to keep per-node listing consistent.
    out: List[Dict[str, Any]] = []
    nodes = []
    wrapper = get_proxmox_admin_wrapper()
    if wrapper:
        try:
            nodes = _api_call_with_retry(lambda: wrapper.get_nodes())
        except Exception as e:
            logger.debug("failed to list nodes via wrapper: %s", e)
    else:
        try:
            resources = _api_call_with_retry(lambda: get_proxmox_admin().cluster.resources.get(type="vm")) or []
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
        if wrapper:
            try:
                vmlist = _api_call_with_retry(lambda: wrapper.list_qemu(node))
            except Exception as e:
                logger.debug("failed to list qemu on %s via wrapper: %s", node, e)
                vmlist = []
        else:
            try:
                qemu_vms = _api_call_with_retry(lambda: get_proxmox_admin().nodes(node).qemu.get()) or []
                for vm in qemu_vms:
                    vm['node'] = node
                vmlist = qemu_vms
            except Exception as e:
                logger.debug("failed to list qemu on %s: %s", node, e)
                vmlist = []

        for vm in (vmlist or []):
            out.append(_build_vm_dict(vm))

    # Sort VMs alphabetically by name
    out.sort(key=lambda vm: (vm.get("name") or "").lower())

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
        data = get_proxmox_admin().access.groups(groupid).get()
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
        grp = get_proxmox_admin().access.groups(ADMIN_GROUP).get()
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


def get_vms_for_user(user: str, search: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Non-admin: only VMs listed in mappings.
    Admin: all VMs.
    Optional search parameter filters by VM name (case-insensitive).
    """
    vms = get_all_vms()
    admin = is_admin_user(user)
    logger.debug("get_vms_for_user: user=%s is_admin=%s all_vms=%d", user, admin, len(vms))
    
    if not admin:
        mapping = get_user_vm_map()
        allowed = set(mapping.get(user, []))
        logger.debug("get_vms_for_user: user=%s allowed_vmids=%s", user, allowed)
        if not allowed:
            return []
        vms = [vm for vm in vms if vm["vmid"] in allowed]
    
    # Apply search filter if provided
    if search:
        search_lower = search.lower()
        vms = [
            vm for vm in vms
            if search_lower in (vm.get("name") or "").lower()
            or search_lower in str(vm.get("vmid", ""))
            or search_lower in (vm.get("ip") or "").lower()
        ]
        logger.debug("get_vms_for_user: search=%s filtered_vms=%d", search, len(vms))
    
    # Sort VMs alphabetically by name
    vms.sort(key=lambda vm: (vm.get("name") or "").lower())
    
    logger.debug("get_vms_for_user: user=%s visible_vms=%d", user, len(vms))
    return vms


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
    Raises ValueError if VM is missing required fields or has no IP.
    """
    if not vm:
        raise ValueError("VM dict is None or empty")
    
    vmid = vm.get("vmid")
    if not vmid:
        raise ValueError("VM missing vmid field")
    
    ip = vm.get("ip")
    if not ip or ip == "<ip>":
        raise ValueError(f"VM {vmid} has no IP address - ensure guest agent is installed and VM is running")
    
    name = vm.get("name") or f"vm-{vmid}"
    
    # Minimal valid RDP file format that Windows Remote Desktop will accept
    # Using only essential fields to avoid compatibility issues
    return (
        f"full address:s:{ip}:3389\r\n"
        f"prompt for credentials:i:1\r\n"
        f"administrative session:i:0\r\n"
        f"authentication level:i:2\r\n"
        f"screen mode id:i:2\r\n"
        f"desktopwidth:i:1920\r\n"
        f"desktopheight:i:1080\r\n"
        f"session bpp:i:32\r\n"
        f"compression:i:1\r\n"
        f"keyboardhook:i:2\r\n"
        f"audiocapturemode:i:0\r\n"
        f"videoplaybackmode:i:1\r\n"
        f"connection type:i:7\r\n"
        f"networkautodetect:i:1\r\n"
        f"bandwidthautodetect:i:1\r\n"
        f"displayconnectionbar:i:1\r\n"
        f"enableworkspacereconnect:i:0\r\n"
        f"disable wallpaper:i:0\r\n"
        f"allow font smoothing:i:0\r\n"
        f"allow desktop composition:i:0\r\n"
        f"disable full window drag:i:1\r\n"
        f"disable menu anims:i:1\r\n"
        f"disable themes:i:0\r\n"
        f"disable cursor setting:i:0\r\n"
        f"bitmapcachepersistenable:i:1\r\n"
        f"audiomode:i:0\r\n"
        f"redirectprinters:i:1\r\n"
        f"redirectcomports:i:0\r\n"
        f"redirectsmartcards:i:1\r\n"
        f"redirectclipboard:i:1\r\n"
        f"redirectposdevices:i:0\r\n"
        f"autoreconnection enabled:i:1\r\n"
        f"negotiate security layer:i:1\r\n"
        f"remoteapplicationmode:i:0\r\n"
        f"alternate shell:s:\r\n"
        f"shell working directory:s:\r\n"
        f"gatewayhostname:s:\r\n"
        f"gatewayusagemethod:i:4\r\n"
        f"gatewaycredentialssource:i:4\r\n"
        f"gatewayprofileusagemethod:i:0\r\n"
        f"promptcredentialonce:i:0\r\n"
        f"gatewaybrokeringtype:i:0\r\n"
        f"use redirection server name:i:0\r\n"
        f"rdgiskdcproxy:i:0\r\n"
        f"kdcproxyname:s:\r\n"
    )


def start_vm(vm: Dict[str, Any]) -> None:
    vmid = int(vm["vmid"])
    node = vm["node"]
    vmtype = vm.get("type", "qemu")

    try:
        if vmtype == "lxc":
            get_proxmox_admin().nodes(node).lxc(vmid).status.start.post()
        else:
            get_proxmox_admin().nodes(node).qemu(vmid).status.start.post()
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
            get_proxmox_admin().nodes(node).lxc(vmid).status.shutdown.post()
        else:
            # Graceful shutdown for QEMU; use .stop() if you want hard power-off
            get_proxmox_admin().nodes(node).qemu(vmid).status.shutdown.post()
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
        users = get_proxmox_admin().access.users.get()
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
        nodes = get_proxmox_admin().nodes.get() or []
        info["nodes"] = [n.get("node") for n in nodes if isinstance(n, dict) and "node" in n]
        info["nodes_count"] = len(info["nodes"])
    except Exception as e:
        info["ok"] = False
        info["error"] = f"nodes error: {e}"
        logger.exception("probe_proxmox: nodes error")
        return info

    try:
        resources = get_proxmox_admin().cluster.resources.get(type="vm") or []
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


def create_pve_user(username: str, password: str) -> tuple[bool, Optional[str]]:
    """Create a new user in Proxmox pve realm.
    
    Returns: (success: bool, error_message: Optional[str])
    """
    if not username or not password:
        return False, "Username and password are required"
    
    # Validate username (alphanumeric, underscore, dash)
    if not re.match(r'^[a-zA-Z0-9_-]+$', username):
        return False, "Username can only contain letters, numbers, underscore, and dash"
    
    if len(username) < 3:
        return False, "Username must be at least 3 characters"
    
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    
    try:
        userid = f"{username}@pve"
        get_proxmox_admin().access.users.post(
            userid=userid,
            password=password,
            enable=1,
            comment="Self-registered user"
        )
        logger.info("Created pve user: %s", userid)
        return True, None
    except Exception as e:
        error_msg = str(e)
        if "already exists" in error_msg.lower():
            return False, "Username already exists"
        logger.exception("Failed to create user %s: %s", username, e)
        return False, f"Failed to create user: {error_msg}"
