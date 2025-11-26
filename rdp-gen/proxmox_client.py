import json
import logging
import os
import re
import time
from typing import Dict, List, Any, Optional

from proxmoxer import ProxmoxAPI
import urllib3
# Completely disable SSL warnings and verification
urllib3.disable_warnings()

from config import (
    PVE_HOST,
    PVE_ADMIN_USER,
    PVE_ADMIN_PASS,
    PVE_VERIFY,
    VALID_NODES,
    ADMIN_USERS,
    ADMIN_GROUP,
    MAPPINGS_FILE,
    IP_CACHE_FILE,
    VM_CACHE_TTL,
    ENABLE_IP_LOOKUP,
    ENABLE_IP_PERSISTENCE,
    ARP_SUBNETS,
)

# Import ARP scanner for fast IP discovery
try:
    from arp_scanner import discover_ips_via_arp, normalize_mac
    ARP_SCANNER_AVAILABLE = True
except ImportError:
    ARP_SCANNER_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("ARP scanner not available, falling back to guest agent only")

# No additional SSL warning suppression needed - already done above

# ---------------------------------------------------------------------------
# Proxmox connection (admin account)
# ---------------------------------------------------------------------------

# Lazy connection - only created when first accessed
_proxmox_admin = None

def get_proxmox_admin():
    """Get or create Proxmox connection on first use (lazy initialization)."""
    global _proxmox_admin
    if _proxmox_admin is None:
        _proxmox_admin = ProxmoxAPI(
            PVE_HOST,
            user=PVE_ADMIN_USER,
            password=PVE_ADMIN_PASS,
            verify_ssl=PVE_VERIFY,
        )
        logger.info("Connected to Proxmox at %s", PVE_HOST)
    return _proxmox_admin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VM cache: single cluster snapshot, reused everywhere
# ---------------------------------------------------------------------------

_vm_cache_data: Optional[List[Dict[str, Any]]] = None
_vm_cache_ts: float = 0.0

# IP address cache: vmid -> {"ip": "x.x.x.x", "timestamp": unix_time}
# Stored in JSON file for persistence across restarts
_ip_cache: Dict[int, Dict[str, Any]] = {}
_ip_cache_loaded = False
IP_CACHE_TTL = 86400  # 24 hours (much longer since we validate on use)

def _load_ip_cache() -> None:
    """Load IP cache from JSON file."""
    global _ip_cache, _ip_cache_loaded
    if _ip_cache_loaded:
        return
    
    if not os.path.exists(IP_CACHE_FILE):
        _ip_cache = {}
        _ip_cache_loaded = True
        return
    
    try:
        with open(IP_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Convert string keys back to int
            _ip_cache = {int(k): v for k, v in data.items()}
        logger.info("Loaded IP cache with %d entries", len(_ip_cache))
    except Exception as e:
        logger.warning("Failed to load IP cache: %s", e)
        _ip_cache = {}
    
    _ip_cache_loaded = True

def _save_ip_cache() -> None:
    """Save IP cache to JSON file."""
    try:
        # Convert int keys to strings for JSON
        data = {str(k): v for k, v in _ip_cache.items()}
        with open(IP_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.debug("Saved IP cache with %d entries", len(_ip_cache))
    except Exception as e:
        logger.warning("Failed to save IP cache: %s", e)

def _invalidate_vm_cache() -> None:
    global _vm_cache_data, _vm_cache_ts
    _vm_cache_data = None
    _vm_cache_ts = 0.0

def _get_cached_ip(vmid: int) -> Optional[str]:
    """Get IP from persistent cache if not expired."""
    _load_ip_cache()
    if vmid in _ip_cache:
        entry = _ip_cache[vmid]
        ip = entry.get("ip")
        ts = entry.get("timestamp", 0)
        if time.time() - ts < IP_CACHE_TTL:
            return ip
    return None

def _cache_ip(vmid: int, ip: Optional[str]) -> None:
    """Store IP in persistent cache."""
    _load_ip_cache()
    if ip:
        _ip_cache[vmid] = {
            "ip": ip,
            "timestamp": time.time()
        }
        _save_ip_cache()

def verify_vm_ip(node: str, vmid: int, vmtype: str, cached_ip: str) -> Optional[str]:
    """Verify cached IP is still correct, return updated IP if different.
    
    This does a quick check without blocking. Returns:
    - cached_ip if still valid
    - new IP if different
    - None if VM is offline or unreachable
    """
    try:
        current_ip = _lookup_vm_ip(node, vmid, vmtype)
        if current_ip and current_ip != cached_ip:
            logger.info("VM %d IP changed: %s -> %s", vmid, cached_ip, current_ip)
            _cache_ip(vmid, current_ip)
            return current_ip
        return cached_ip
    except Exception as e:
        logger.debug("Failed to verify IP for VM %d: %s", vmid, e)
        return cached_ip  # Return cached IP if verification fails

def _save_ip_to_proxmox(node: str, vmid: int, vmtype: str, ip: Optional[str]) -> None:
    """
    Save discovered IP to Proxmox VM/LXC notes field for persistence.
    Stores in format: [CACHED_IP:192.168.1.100]
    This survives cache expiration and doesn't require re-scanning.
    
    WARNING: This is SLOW - requires reading AND writing VM config (2 API calls per VM).
    Only enable via ENABLE_IP_PERSISTENCE=true if you really need it.
    """
    if not ENABLE_IP_PERSISTENCE or not ip:
        return
    
    try:
        # Get current config
        if vmtype == "lxc":
            config = get_proxmox_admin().nodes(node).lxc(vmid).config.get()
        else:
            config = get_proxmox_admin().nodes(node).qemu(vmid).config.get()
        
        current_notes = config.get('description', '')
        
        # Remove old cached IP tag if present
        import re
        notes = re.sub(r'\[CACHED_IP:[^\]]+\]\s*', '', current_notes)
        
        # Add new IP tag
        new_notes = f"{notes.strip()}\n[CACHED_IP:{ip}]".strip()
        
        # Only update if changed
        if new_notes != current_notes:
            if vmtype == "lxc":
                get_proxmox_admin().nodes(node).lxc(vmid).config.put(description=new_notes)
            else:
                get_proxmox_admin().nodes(node).qemu(vmid).config.put(description=new_notes)
            logger.debug("Saved IP %s to VM %d notes", ip, vmid)
    except Exception as e:
        logger.debug("Failed to save IP to Proxmox notes for VM %d: %s", vmid, e)

def _get_ip_from_proxmox_notes(raw: Dict[str, Any]) -> Optional[str]:
    """
    Extract cached IP from Proxmox VM notes field.
    Looks for pattern: [CACHED_IP:192.168.1.100]
    Returns None if not found or invalid.
    
    WARNING: Only used if ENABLE_IP_PERSISTENCE=true (disabled by default for performance).
    """
    if not ENABLE_IP_PERSISTENCE:
        return None
    
    description = raw.get('description', '')
    if not description:
        return None
    
    import re
    match = re.search(r'\[CACHED_IP:([^\]]+)\]', description)
    if match:
        ip = match.group(1).strip()
        logger.debug("Found cached IP %s in VM %d notes", ip, raw.get('vmid'))
        return ip
    return None


def _get_vm_mac(node: str, vmid: int, vmtype: str) -> Optional[str]:
    """
    Get MAC address for a VM.
    
    Returns normalized MAC (lowercase, no separators) or None.
    """
    try:
        if vmtype == "lxc":
            # LXC: get config to find MAC
            config = get_proxmox_admin().nodes(node).lxc(vmid).config.get()
            if not config:
                logger.debug("VM %d: No config returned", vmid)
                return None
            # LXC net0 format: "name=eth0,bridge=vmbr0,hwaddr=XX:XX:XX:XX:XX:XX,ip=dhcp"
            net0 = config.get("net0", "")
            logger.debug("VM %d LXC net0: %s", vmid, net0)
            mac_match = re.search(r'hwaddr=([0-9a-fA-F:]+)', net0)
            if mac_match and ARP_SCANNER_AVAILABLE:
                mac = normalize_mac(mac_match.group(1))
                logger.debug("VM %d: Extracted MAC %s", vmid, mac)
                return mac
        
        elif vmtype == "qemu":
            # QEMU: get config to find MAC from net0
            config = get_proxmox_admin().nodes(node).qemu(vmid).config.get()
            if not config:
                logger.debug("VM %d: No config returned", vmid)
                return None
            # net0 format: "virtio=XX:XX:XX:XX:XX:XX,bridge=vmbr0"
            # or: "e1000=XX:XX:XX:XX:XX:XX,bridge=vmbr0"
            net0 = config.get("net0", "")
            logger.debug("VM %d QEMU net0: %s", vmid, net0)
            # Match MAC address pattern (6 hex pairs separated by colons)
            mac_match = re.search(r'([0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2})', net0)
            if mac_match and ARP_SCANNER_AVAILABLE:
                mac = normalize_mac(mac_match.group(1))
                logger.debug("VM %d: Extracted MAC %s", vmid, mac)
                return mac
            else:
                logger.debug("VM %d: No MAC found in net0", vmid)
    
    except Exception as e:
        logger.debug("Failed to get MAC for %s/%s: %s", node, vmid, e)
    
    return None


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
            interfaces = get_proxmox_admin().nodes(node).lxc(vmid).interfaces.get()
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
            data = get_proxmox_admin().nodes(node).qemu(vmid).agent.get(
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


def _build_vm_dict(raw: Dict[str, Any], skip_ip: bool = False) -> Dict[str, Any]:
    vmid = int(raw["vmid"])
    node = raw["node"]
    name = raw.get("name", f"vm-{vmid}")
    status = raw.get("status", "unknown")
    vmtype = raw.get("type", "qemu")

    category = _guess_category(raw)
    
    # IP lookup strategy (prioritized):
    # 1. Proxmox notes field (persistent across cache expiration)
    # 2. Memory cache (5-minute TTL)
    # 3. API lookup (slow, only if needed)
    ip = None
    if not skip_ip:
        # First: Check Proxmox notes for persisted IP
        ip = _get_ip_from_proxmox_notes(raw)
        
        # Second: Check memory cache if not in notes
        if not ip:
            ip = _get_cached_ip(vmid)
        
        # Third: Lookup IPs from API only if not found in notes or cache
        # - LXC: Network interfaces (config readable even when stopped, so always try)
        # - QEMU: Guest agent (only available when running)
        if not ip:
            if vmtype == "lxc":
                # LXC containers: config is always readable, cache it
                ip = _lookup_vm_ip(node, vmid, vmtype)
                if ip:
                    _cache_ip(vmid, ip)
            elif vmtype == "qemu" and status == "running":
                # QEMU VMs: only query when running (guest agent required)
                ip = _lookup_vm_ip(node, vmid, vmtype)
                if ip:
                    _cache_ip(vmid, ip)

    return {
        "vmid": vmid,
        "node": node,
        "name": name,
        "status": status,
        "type": vmtype,      # 'qemu' or 'lxc'
        "category": category,
        "ip": ip,
        "user_mappings": [],  # Will be populated by _enrich_with_user_mappings()
    }


def _enrich_with_user_mappings(vms: List[Dict[str, Any]]) -> None:
    """
    Enrich VMs with user mapping information (in-place).
    Adds 'user_mappings' field with list of users who can access this VM.
    This is cached once, so we don't recalculate on every request.
    """
    mapping = get_user_vm_map()
    
    # Invert mapping: vmid -> [users]
    vmid_to_users: Dict[int, List[str]] = {}
    for user, vmids in mapping.items():
        for vmid in vmids:
            if vmid not in vmid_to_users:
                vmid_to_users[vmid] = []
            vmid_to_users[vmid].append(user)
    
    # Add to each VM
    for vm in vms:
        vm["user_mappings"] = vmid_to_users.get(vm["vmid"], [])
    
    logger.debug("Enriched %d VMs with user mappings", len(vms))


def _enrich_vms_with_arp_ips(vms: List[Dict[str, Any]]) -> None:
    """
    Enrich VM list with IPs discovered via ARP scanning (fast, in-place).
    
    This is much faster than querying guest agents individually.
    Runs ARP scan in background thread so VMs load immediately.
    Only scans QEMU VMs that are running - LXC containers get IPs from direct API calls,
    and offline QEMU VMs can't have network presence.
    """
    if not ARP_SCANNER_AVAILABLE:
        logger.info("ARP scanner not available, skipping ARP discovery")
        return
    
    # Build map of vmid -> MAC for RUNNING QEMU VMs that don't have IPs yet
    # Skip LXC containers - they get IPs directly from network config API
    vm_mac_map = {}
    for vm in vms:
        # Skip if already has IP
        if vm.get("ip"):
            continue
        
        # Skip LXC containers (handled by direct API call in _build_vm_dict)
        if vm.get("type") == "lxc":
            logger.debug("VM %d (%s) is LXC, skipping ARP (uses direct API)", 
                        vm["vmid"], vm["name"])
            continue
        
        # Skip if QEMU VM is not running (offline VMs won't be in ARP table)
        if vm.get("status") != "running":
            logger.debug("VM %d (%s) is %s, skipping ARP discovery", 
                        vm["vmid"], vm["name"], vm.get("status"))
            continue
        
        mac = _get_vm_mac(vm["node"], vm["vmid"], vm["type"])
        if mac:
            vm_mac_map[vm["vmid"]] = mac
            logger.debug("VM %d (%s) has MAC: %s", vm["vmid"], vm["name"], mac)
        else:
            logger.debug("VM %d (%s) has no MAC found", vm["vmid"], vm["name"])
    
    if not vm_mac_map:
        logger.info("No running QEMU VMs need ARP discovery")
        return
    
    logger.info("Starting background ARP discovery for %d running QEMU VMs", len(vm_mac_map))
    
    # Discover IPs via ARP in BACKGROUND mode (non-blocking)
    # This returns immediately with cached results and starts scan in background
    discovered_ips = discover_ips_via_arp(vm_mac_map, subnets=ARP_SUBNETS, background=True)
    
    # Update VMs with any cached IPs we already have
    for vm in vms:
        if vm["vmid"] in discovered_ips:
            vm["ip"] = discovered_ips[vm["vmid"]]
            _cache_ip(vm["vmid"], vm["ip"])
    
    logger.info("ARP discovery returned %d cached IPs (scan running in background)", len(discovered_ips))



def get_all_vms(skip_ips: bool = False, force_refresh: bool = False) -> List[Dict[str, Any]]:
    """
    Cached list of all VMs/containers visible to the admin user.

    Each item: { vmid, node, name, status, ip, category, type }
    
    Uses /cluster/resources endpoint for fast loading (single API call).
    Set skip_ips=True to avoid IP lookups for initial fast load.
    Set force_refresh=True to bypass cache and fetch fresh data from Proxmox.
    
    Note: Cache stores VM structure. When skip_ips=True, returns cached VMs without IP lookup.
    """
    global _vm_cache_data, _vm_cache_ts

    now = time.time()
    # Check if we have valid cached data
    cache_valid = _vm_cache_data is not None and (now - _vm_cache_ts) < VM_CACHE_TTL
    
    # If cache is valid and not forcing refresh, return cached data
    # When skip_ips=True, return without re-enriching IPs
    if not force_refresh and cache_valid and _vm_cache_data is not None:
        if skip_ips:
            # Return cached VMs without IP data (clear IPs for fast load)
            return [{**vm, "ip": None} for vm in _vm_cache_data]
        else:
            # Return full cached data
            return list(_vm_cache_data)

    # Fast path: use cluster resources API (single call vs N node queries)
    out: List[Dict[str, Any]] = []
    try:
        resources = get_proxmox_admin().cluster.resources.get(type="vm") or []
        logger.info("get_all_vms: fetched %d resources from cluster API", len(resources))
        
        for vm in resources:
            # Filter by VALID_NODES if configured
            if VALID_NODES and vm.get("node") not in VALID_NODES:
                continue
            
            out.append(_build_vm_dict(vm, skip_ip=skip_ips))
    except Exception as e:
        logger.warning("cluster resources API failed, falling back to per-node queries: %s", e)
        # Fallback: per-node queries (slower)
        try:
            nodes = get_proxmox_admin().nodes.get() or []
        except Exception as e2:
            logger.error("failed to list nodes: %s", e2)
            nodes = []

        for n in nodes:
            node = n["node"]
            if VALID_NODES and node not in VALID_NODES:
                continue
            
            try:
                # Get QEMU VMs
                qemu_vms = get_proxmox_admin().nodes(node).qemu.get() or []
                for vm in qemu_vms:
                    vm['node'] = node
                    vm['type'] = 'qemu'
                    out.append(_build_vm_dict(vm, skip_ip=skip_ips))
                
                # Get LXC containers
                lxc_vms = get_proxmox_admin().nodes(node).lxc.get() or []
                for vm in lxc_vms:
                    vm['node'] = node
                    vm['type'] = 'lxc'
                    out.append(_build_vm_dict(vm, skip_ip=skip_ips))
            except Exception as e:
                logger.debug("failed to list VMs on %s: %s", node, e)

    # Sort VMs alphabetically by name
    out.sort(key=lambda vm: (vm.get("name") or "").lower())

    logger.info("get_all_vms: returning %d VM(s)", len(out))

    # Always enrich with user mappings (needed for both admin and user views)
    _enrich_with_user_mappings(out)

    # Try ARP-based IP discovery for VMs without IPs (fast batch operation)
    # Only run when not skipping IPs
    if not skip_ips and ARP_SCANNER_AVAILABLE:
        _enrich_vms_with_arp_ips(out)

    # Cache the VM structure (always cache, even with skip_ips)
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


def get_admin_group_members() -> List[str]:
    """Get list of users in the admin group."""
    if not ADMIN_GROUP:
        return []
    
    try:
        grp = get_proxmox_admin().access.groups(ADMIN_GROUP).get()
    except Exception as e:
        logger.warning("Failed to get admin group members: %s", e)
        return []
    
    raw_members = (grp.get("members", []) or []) if isinstance(grp, dict) else []
    members = []
    for m in raw_members:
        if isinstance(m, str):
            members.append(m)
        elif isinstance(m, dict) and "userid" in m:
            members.append(m["userid"])
    
    return members


def add_user_to_admin_group(user: str) -> bool:
    """
    Add a user to the admin group in Proxmox.
    Returns True if successful, False otherwise.
    """
    if not ADMIN_GROUP:
        logger.warning("ADMIN_GROUP not configured, cannot add user")
        return False
    
    try:
        # Get current group info
        current_members = get_admin_group_members()
        if user in current_members:
            logger.info("User %s already in admin group", user)
            return True
        
        # Add user to group by updating the group
        # Proxmox API requires comment parameter even if empty
        current_members.append(user)
        
        # Get current group to fetch comment
        try:
            group_info = get_proxmox_admin().access.groups(ADMIN_GROUP).get()
            comment = group_info.get('comment', '')
        except:
            comment = ''
        
        get_proxmox_admin().access.groups(ADMIN_GROUP).put(
            comment=comment,
            members=",".join(current_members)
        )
        logger.info("Added user %s to admin group %s", user, ADMIN_GROUP)
        return True
    except Exception as e:
        logger.exception("Failed to add user %s to admin group: %s", user, e)
        return False


def remove_user_from_admin_group(user: str) -> bool:
    """
    Remove a user from the admin group in Proxmox.
    Returns True if successful, False otherwise.
    """
    if not ADMIN_GROUP:
        logger.warning("ADMIN_GROUP not configured, cannot remove user")
        return False
    
    try:
        current_members = get_admin_group_members()
        if user not in current_members:
            logger.info("User %s not in admin group", user)
            return True
        
        # Remove user from group by updating the group
        current_members.remove(user)
        
        # Get current group to fetch comment
        try:
            group_info = get_proxmox_admin().access.groups(ADMIN_GROUP).get()
            comment = group_info.get('comment', '')
        except:
            comment = ''
        
        get_proxmox_admin().access.groups(ADMIN_GROUP).put(
            comment=comment,
            members=",".join(current_members)
        )
        logger.info("Removed user %s from admin group %s", user, ADMIN_GROUP)
        return True
    except Exception as e:
        logger.exception("Failed to remove user %s from admin group: %s", user, e)
        return False


def get_vm_ip(vmid: int, node: str, vmtype: str) -> Optional[str]:
    """
    Get IP address for a specific VM (for lazy loading).
    
    Returns IP from cache if available, otherwise performs API lookup.
    """
    # Check cache first
    ip = _get_cached_ip(vmid)
    if ip:
        return ip
    
    # Lookup from API
    ip = _lookup_vm_ip(node, vmid, vmtype)
    if ip:
        _cache_ip(vmid, ip)
    
    return ip


def get_vms_for_user(user: str, search: Optional[str] = None, skip_ips: bool = False, force_refresh: bool = False) -> List[Dict[str, Any]]:
    """
    Non-admin: only VMs listed in mappings.
    Admin: all VMs.
    Optional search parameter filters by VM name (case-insensitive).
    Set skip_ips=True to skip IP lookups for fast initial load.
    Set force_refresh=True to bypass cache and fetch fresh data.
    
    NOTE: This function now uses cached user_mappings from get_all_vms(),
    so switching between admin/user views doesn't trigger new Proxmox API calls.
    """
    vms = get_all_vms(skip_ips=skip_ips, force_refresh=force_refresh)
    admin = is_admin_user(user)
    logger.debug("get_vms_for_user: user=%s is_admin=%s all_vms=%d", user, admin, len(vms))
    
    if not admin:
        # Filter using cached user_mappings (no need to call get_user_vm_map again)
        vms = [vm for vm in vms if user in vm.get("user_mappings", [])]
        logger.debug("get_vms_for_user: user=%s filtered to %d VMs using cached mappings", user, len(vms))
        if not vms:
            logger.info("User %s has no VM mappings", user)
            return []
    
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
