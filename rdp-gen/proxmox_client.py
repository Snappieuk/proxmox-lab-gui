import json
import logging
import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    VM_CACHE_FILE,
    VM_CACHE_TTL,
    PROXMOX_CACHE_TTL,
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
# Proxmox connection (admin account) - thread-safe singleton
# Uses double-checked locking pattern to ensure only one connection is created
# even when multiple threads attempt to access it concurrently.
# ---------------------------------------------------------------------------

# Thread-safe lock for Proxmox connection - prevents race conditions during initialization
_proxmox_lock = threading.Lock()
# Lazy connection - only created when first accessed, reused by all threads
_proxmox_admin = None

def get_proxmox_admin():
    """Get or create Proxmox connection on first use (lazy initialization).
    
    Thread-safe singleton pattern: uses double-checked locking to ensure
    only one Proxmox connection is created even in multi-threaded scenarios.
    The ProxmoxAPI client is reused for all subsequent calls, avoiding
    authentication overhead on each request.
    """
    global _proxmox_admin
    if _proxmox_admin is not None:
        return _proxmox_admin
    
    with _proxmox_lock:
        # Double-check inside lock to handle race conditions
        if _proxmox_admin is None:
            _proxmox_admin = ProxmoxAPI(
                PVE_HOST,
                user=PVE_ADMIN_USER,
                password=PVE_ADMIN_PASS,
                verify_ssl=PVE_VERIFY,
            )
            logger.info("Connected to Proxmox at %s", PVE_HOST)
    return _proxmox_admin


def _create_proxmox_client():
    """Create a new Proxmox client for thread-local use.
    
    Used by ThreadPoolExecutor workers to avoid sharing the main client
    across threads, since ProxmoxAPI may not be fully thread-safe.
    """
    return ProxmoxAPI(
        PVE_HOST,
        user=PVE_ADMIN_USER,
        password=PVE_ADMIN_PASS,
        verify_ssl=PVE_VERIFY,
    )


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VM cache: single cluster snapshot, reused everywhere
# Thread-safe with locks for concurrent access
# ---------------------------------------------------------------------------

# Thread-safe locks for caches
_vm_cache_lock = threading.Lock()
_ip_cache_lock = threading.Lock()
_mappings_cache_lock = threading.Lock()

_vm_cache_data: Optional[List[Dict[str, Any]]] = None
_vm_cache_ts: float = 0.0
_vm_cache_loaded: bool = False

# Short-lived cluster resources cache (PROXMOX_CACHE_TTL seconds, default 10)
# This is for the raw cluster.resources data before processing
_cluster_cache_data: Optional[List[Dict[str, Any]]] = None
_cluster_cache_ts: float = 0.0
_cluster_cache_lock = threading.Lock()

# IP address cache: vmid -> {"ip": "x.x.x.x", "timestamp": unix_time}
# Stored in JSON file for persistence across restarts
_ip_cache: Dict[int, Dict[str, Any]] = {}
_ip_cache_loaded = False
IP_CACHE_TTL = 3600  # 1 hour (much longer since we validate on use)

# In-memory mappings cache (write-through to disk)
_mappings_cache: Optional[Dict[str, List[int]]] = None
_mappings_cache_loaded = False

# Auto-tuned ThreadPoolExecutor for parallel IP lookups
# Uses CPU count - 1 for workers, bounded between 2 and 8
# Note: Flask's threaded mode handles process lifecycle, so cleanup is automatic.
# For explicit shutdown (e.g., in tests), call shutdown_executor().
DEFAULT_IP_LOOKUP_WORKERS = max(2, min(8, (os.cpu_count() or 4) - 1))
_ip_lookup_executor = ThreadPoolExecutor(max_workers=DEFAULT_IP_LOOKUP_WORKERS)

# Admin group membership cache (avoids repeated Proxmox API calls)
# Thread-safe with lock for concurrent access
_admin_group_cache: Optional[List[str]] = None
_admin_group_ts: float = 0.0
_admin_group_lock = threading.Lock()
ADMIN_GROUP_CACHE_TTL = 120  # 2 minutes


def shutdown_executor() -> None:
    """Shutdown the ThreadPoolExecutor gracefully.
    
    Call this on application shutdown to ensure clean resource release.
    In Flask's normal lifecycle, this is typically handled automatically.
    """
    global _ip_lookup_executor
    _ip_lookup_executor.shutdown(wait=True)


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

def _load_vm_cache() -> None:
    """Load VM cache from JSON file on startup."""
    global _vm_cache_data, _vm_cache_ts, _vm_cache_loaded
    if _vm_cache_loaded:
        return
    
    if not os.path.exists(VM_CACHE_FILE):
        _vm_cache_data = None
        _vm_cache_ts = 0.0
        _vm_cache_loaded = True
        return
    
    try:
        with open(VM_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            _vm_cache_data = data.get("vms", [])
            _vm_cache_ts = data.get("timestamp", 0.0)
        logger.info("Loaded VM cache with %d VMs from %s", len(_vm_cache_data) if _vm_cache_data else 0, VM_CACHE_FILE)
    except Exception as e:
        logger.warning("Failed to load VM cache: %s", e)
        _vm_cache_data = None
        _vm_cache_ts = 0.0
    
    _vm_cache_loaded = True

def _save_vm_cache() -> None:
    """Save VM cache to JSON file.
    
    Skips writing if the on-disk cache timestamp matches the in-memory timestamp,
    indicating no changes since last save.
    """
    if _vm_cache_data is None:
        return
    
    # Check if on-disk cache is already up-to-date
    if os.path.exists(VM_CACHE_FILE):
        try:
            with open(VM_CACHE_FILE, "r", encoding="utf-8") as f:
                disk_data = json.load(f)
            disk_ts = disk_data.get("timestamp", 0.0)
            if disk_ts == _vm_cache_ts:
                logger.debug("Skipping VM cache write: disk timestamp matches in-memory (%.1f)", _vm_cache_ts)
                return
        except Exception:
            # If we can't read the file, proceed with write
            pass
    
    try:
        data = {
            "vms": _vm_cache_data,
            "timestamp": _vm_cache_ts
        }
        with open(VM_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.debug("Saved VM cache with %d VMs", len(_vm_cache_data))
    except Exception as e:
        logger.warning("Failed to save VM cache: %s", e)

def _invalidate_vm_cache() -> None:
    global _vm_cache_data, _vm_cache_ts, _cluster_cache_data, _cluster_cache_ts
    with _vm_cache_lock:
        _vm_cache_data = None
        _vm_cache_ts = 0.0
    with _cluster_cache_lock:
        _cluster_cache_data = None
        _cluster_cache_ts = 0.0
    # Note: Don't reset _vm_cache_loaded - we want to keep trying disk cache


# ---------------------------------------------------------------------------
# Cluster-wide resource queries (single API call for all VMs/containers)
# ---------------------------------------------------------------------------

def get_all_qemu_vms() -> List[Dict[str, Any]]:
    """
    Get all QEMU VMs across the cluster using cluster.resources API.
    
    Uses short-lived cache (PROXMOX_CACHE_TTL seconds) to avoid hammering
    the Proxmox API. Thread-safe.
    
    Returns raw VM data from Proxmox API (not processed).
    """
    global _cluster_cache_data, _cluster_cache_ts
    
    now = time.time()
    with _cluster_cache_lock:
        if _cluster_cache_data is not None and (now - _cluster_cache_ts) < PROXMOX_CACHE_TTL:
            # Return cached QEMU VMs
            return [r for r in _cluster_cache_data if r.get("type") == "qemu"]
    
    # Fetch fresh data from Proxmox
    try:
        resources = get_proxmox_admin().cluster.resources.get(type="vm") or []
        with _cluster_cache_lock:
            _cluster_cache_data = resources
            _cluster_cache_ts = now
        logger.debug("get_all_qemu_vms: fetched %d resources from cluster API", len(resources))
        return [r for r in resources if r.get("type") == "qemu"]
    except Exception as e:
        logger.warning("Failed to fetch cluster resources: %s", e)
        return []


def get_all_lxc_containers() -> List[Dict[str, Any]]:
    """
    Get all LXC containers across the cluster using cluster.resources API.
    
    Uses short-lived cache (PROXMOX_CACHE_TTL seconds) to avoid hammering
    the Proxmox API. Thread-safe.
    
    Returns raw container data from Proxmox API (not processed).
    """
    global _cluster_cache_data, _cluster_cache_ts
    
    now = time.time()
    with _cluster_cache_lock:
        if _cluster_cache_data is not None and (now - _cluster_cache_ts) < PROXMOX_CACHE_TTL:
            # Return cached LXC containers
            return [r for r in _cluster_cache_data if r.get("type") == "lxc"]
    
    # Fetch fresh data from Proxmox
    try:
        resources = get_proxmox_admin().cluster.resources.get(type="vm") or []
        with _cluster_cache_lock:
            _cluster_cache_data = resources
            _cluster_cache_ts = now
        logger.debug("get_all_lxc_containers: fetched %d resources from cluster API", len(resources))
        return [r for r in resources if r.get("type") == "lxc"]
    except Exception as e:
        logger.warning("Failed to fetch cluster resources: %s", e)
        return []


def _get_cluster_resources_cached() -> List[Dict[str, Any]]:
    """
    Get all VM/container resources from cluster API with caching.
    
    Uses PROXMOX_CACHE_TTL (default 10 seconds) for short-lived caching.
    Thread-safe.
    """
    global _cluster_cache_data, _cluster_cache_ts
    
    now = time.time()
    with _cluster_cache_lock:
        if _cluster_cache_data is not None and (now - _cluster_cache_ts) < PROXMOX_CACHE_TTL:
            return list(_cluster_cache_data)
    
    # Fetch fresh data from Proxmox
    try:
        resources = get_proxmox_admin().cluster.resources.get(type="vm") or []
        with _cluster_cache_lock:
            _cluster_cache_data = resources
            _cluster_cache_ts = now
        logger.debug("_get_cluster_resources_cached: fetched %d resources", len(resources))
        return resources
    except Exception as e:
        logger.warning("Failed to fetch cluster resources: %s", e)
        return []


def invalidate_cluster_cache() -> None:
    """Invalidate the short-lived cluster resources cache.
    
    Called after VM start/stop operations to ensure fresh data is fetched.
    """
    global _cluster_cache_data, _cluster_cache_ts
    with _cluster_cache_lock:
        _cluster_cache_data = None
        _cluster_cache_ts = 0.0

def _get_cached_ip(vmid: int) -> Optional[str]:
    """Get IP from persistent cache if not expired. Thread-safe."""
    _load_ip_cache()
    with _ip_cache_lock:
        if vmid in _ip_cache:
            entry = _ip_cache[vmid]
            ip = entry.get("ip")
            ts = entry.get("timestamp", 0)
            if time.time() - ts < IP_CACHE_TTL:
                return ip
    return None


def _get_cached_ips_batch(vmids: List[int]) -> Dict[int, str]:
    """Get multiple IPs from cache in a single lock acquisition.
    
    More efficient than calling _get_cached_ip() in a loop for many VMs.
    Thread-safe.
    
    Returns:
        Dict mapping vmid to cached IP (only includes VMs with valid cached IPs)
    """
    _load_ip_cache()
    now = time.time()
    results: Dict[int, str] = {}
    
    with _ip_cache_lock:
        for vmid in vmids:
            if vmid in _ip_cache:
                entry = _ip_cache[vmid]
                ip = entry.get("ip")
                ts = entry.get("timestamp", 0)
                if ip and now - ts < IP_CACHE_TTL:
                    results[vmid] = ip
    
    return results


def _cache_ip(vmid: int, ip: Optional[str]) -> None:
    """Store IP in persistent cache. Thread-safe."""
    _load_ip_cache()
    if ip:
        with _ip_cache_lock:
            _ip_cache[vmid] = {
                "ip": ip,
                "timestamp": time.time()
            }
        _save_ip_cache()


def _clear_vm_ip_cache(vmid: int) -> None:
    """Clear IP cache for specific VM. Thread-safe."""
    _load_ip_cache()
    with _ip_cache_lock:
        if vmid in _ip_cache:
            del _ip_cache[vmid]
            logger.debug("Cleared IP cache for VM %d", vmid)
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
        
        if not config:
            return
        
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
    
    Checks all network interfaces (net0, net1, net2, ...) to find the first valid MAC.
    This handles VMs with multiple NICs or where net0 is not the primary interface.
    
    Returns normalized MAC (lowercase, no separators) or None.
    """
    try:
        if vmtype == "lxc":
            # LXC: get config to find MAC
            config = get_proxmox_admin().nodes(node).lxc(vmid).config.get()
            if not config:
                logger.debug("VM %d: No config returned", vmid)
                return None
            
            # Check net0-net9 for LXC containers
            for i in range(10):
                net_key = f"net{i}"
                net_config = config.get(net_key, "")
                if not net_config:
                    continue
                
                # LXC format: "name=eth0,bridge=vmbr0,hwaddr=XX:XX:XX:XX:XX:XX,ip=dhcp"
                logger.debug("VM %d LXC %s: %s", vmid, net_key, net_config)
                mac_match = re.search(r'hwaddr=([0-9a-fA-F:]+)', net_config)
                if mac_match and ARP_SCANNER_AVAILABLE:
                    mac = normalize_mac(mac_match.group(1))
                    if mac:
                        logger.debug("VM %d: Extracted MAC %s from %s", vmid, mac, net_key)
                        return mac
        
        elif vmtype == "qemu":
            # QEMU: get config to find MAC from any net interface
            config = get_proxmox_admin().nodes(node).qemu(vmid).config.get()
            if not config:
                logger.debug("VM %d: No config returned", vmid)
                return None
            
            # Check net0-net9 for QEMU VMs
            for i in range(10):
                net_key = f"net{i}"
                net_config = config.get(net_key, "")
                if not net_config:
                    continue
                
                # QEMU format: "virtio=XX:XX:XX:XX:XX:XX,bridge=vmbr0"
                # or: "e1000=XX:XX:XX:XX:XX:XX,bridge=vmbr0"
                logger.info("VM %d QEMU %s config: %s", vmid, net_key, net_config)
                # Match MAC address pattern (6 hex pairs separated by colons)
                mac_match = re.search(r'([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})', net_config)
                if mac_match:
                    raw_mac = mac_match.group(0)
                    logger.info("VM %d: Found MAC in regex: %s", vmid, raw_mac)
                    if ARP_SCANNER_AVAILABLE:
                        mac = normalize_mac(raw_mac)
                        if mac:
                            logger.info("VM %d: Normalized MAC %s -> %s from %s", vmid, raw_mac, mac, net_key)
                            return mac
                        else:
                            logger.warning("VM %d: normalize_mac failed for %s", vmid, raw_mac)
                else:
                    logger.warning("VM %d: No MAC regex match in %s: %s", vmid, net_key, net_config)
            
            logger.debug("VM %d: No MAC found in any net interface", vmid)
    
    except Exception as e:
        logger.debug("Failed to get MAC for %s/%s: %s", node, vmid, e)
    
    return None


def _check_rdp_port(ip: str, timeout: float = 0.5) -> bool:
    """
    Check if RDP port (3389) is open on the given IP address.
    
    Args:
        ip: IP address to check
        timeout: Connection timeout in seconds (default: 0.5)
    
    Returns:
        True if port 3389 is open, False otherwise
    """
    if not ip:
        return False
    
    import socket
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, 3389))
        sock.close()
        return result == 0
    except Exception as e:
        logger.debug("RDP port check failed for %s: %s", ip, e)
        return False


def _guess_category(raw: Dict[str, Any]) -> str:
    """
    Determine OS category based on VM ostype field.
    
    - Checks 'ostype' field in VM data (from config)
    - LXC: always 'linux'
    - QEMU with ostype containing 'win': 'windows'
    - Other QEMU: 'linux'
    """
    vmtype = raw.get("type", "qemu")
    if vmtype == "lxc":
        return "linux"
    
    # Check ostype field from VM config
    ostype = (raw.get("ostype") or "").lower()
    if ostype and "win" in ostype:
        return "windows"

    if vmtype == "qemu":
        return "linux"

    return "other"


def _lookup_vm_ip(node: str, vmid: int, vmtype: str) -> Optional[str]:
    """
    IP lookup for both QEMU VMs (via guest agent) and LXC containers (via network interfaces).
    Skipped entirely unless ENABLE_IP_LOOKUP is True.
    
    This is a convenience wrapper that delegates to _lookup_vm_ip_thread_safe
    to ensure one canonical, thread-safe implementation.
    """
    return _lookup_vm_ip_thread_safe(node, vmid, vmtype)


def _lookup_vm_ip_thread_safe(node: str, vmid: int, vmtype: str) -> Optional[str]:
    """
    Thread-safe IP lookup using a new Proxmox client per call.
    
    Handles LXC containers via network interfaces API.
    Returns None if container uses DHCP but no IP assigned yet (needs ARP scan).
    QEMU VMs use ARP scanning instead (no guest agent dependency).
    
    Used by ThreadPoolExecutor workers for parallel IP lookups.
    Creates a short-lived client to avoid thread-safety issues with ProxmoxAPI.
    """
    if not ENABLE_IP_LOOKUP:
        return None
    
    # Only handle LXC containers - QEMU VMs use ARP scanning
    if vmtype != "lxc":
        return None
    
    try:
        client = _create_proxmox_client()
        
        # LXC containers - use network interfaces API
        try:
            interfaces = client.nodes(node).lxc(vmid).interfaces.get()
            if interfaces:
                for iface in interfaces:
                    # Prefer eth0/veth0 interfaces
                    if iface.get("name") in ("eth0", "veth0"):
                        inet = iface.get("inet")
                        if inet:
                            ip = inet.split("/")[0] if "/" in inet else inet
                            if ip and not ip.startswith("127."):
                                return ip
                    # Fall back to any interface with IP
                    inet = iface.get("inet")
                    if inet:
                        ip = inet.split("/")[0] if "/" in inet else inet
                        if ip and not ip.startswith("127."):
                            return ip
                
                # If we get here, container is running but no IP found in interfaces
                # This happens with DHCP when IP not yet assigned - needs ARP scan
                logger.debug("LXC %d has no IP in interfaces (likely DHCP), will use ARP scan", vmid)
                return None
        except Exception as e:
            logger.debug("lxc interface call failed for %s/%s: %s", node, vmid, e)
            return None
    except Exception as e:
        logger.debug("Thread-safe IP lookup failed for %s/%s: %s", node, vmid, e)
    
    return None


def lookup_ips_parallel(vms: List[Dict[str, Any]]) -> Dict[int, str]:
    """
    Parallel IP lookup for multiple VMs using ThreadPoolExecutor.
    
    Uses a limited concurrency pool (max_workers=10) to avoid overwhelming
    the Proxmox API while still providing significant speedup.
    
    Each worker uses a new Proxmox client to ensure thread-safety.
    
    Args:
        vms: List of VM dicts with 'vmid', 'node', 'type' keys
        
    Returns:
        Dict mapping vmid to discovered IP address
    """
    # Get all vmids and batch-check cache (more efficient than per-VM checks)
    all_vmids = [int(vm["vmid"]) for vm in vms if vm.get("vmid") is not None]
    cached_ips = _get_cached_ips_batch(all_vmids)
    
    # Filter VMs that need IP lookup (running QEMU VMs or all LXC containers)
    vms_needing_lookup = []
    for vm in vms:
        vmid = vm.get("vmid")
        if not vmid:
            continue
        # Skip if already has IP from cache (using batch result)
        if vmid in cached_ips:
            continue
        # LXC containers: always try (config readable even when stopped)
        # QEMU VMs: only when running (guest agent required)
        vmtype = vm.get("type", "qemu")
        if vmtype == "lxc" or (vmtype == "qemu" and vm.get("status") == "running"):
            vms_needing_lookup.append(vm)
    
    if not vms_needing_lookup:
        return {}
    
    logger.info("Starting parallel IP lookup for %d VMs", len(vms_needing_lookup))
    results: Dict[int, str] = {}
    
    def lookup_single_vm(vm: Dict[str, Any]) -> tuple:
        """Lookup IP for a single VM, return (vmid, ip)."""
        vmid = vm["vmid"]
        node = vm["node"]
        vmtype = vm.get("type", "qemu")
        ip = _lookup_vm_ip_thread_safe(node, vmid, vmtype)
        return (vmid, ip)
    
    # Submit all lookups to the executor
    futures = {
        _ip_lookup_executor.submit(lookup_single_vm, vm): vm
        for vm in vms_needing_lookup
    }
    
    # Collect results as they complete
    for future in as_completed(futures):
        try:
            vmid, ip = future.result(timeout=30)
            if ip:
                results[vmid] = ip
                _cache_ip(vmid, ip)
        except Exception as e:
            vm = futures[future]
            logger.debug("Parallel IP lookup failed for VM %s: %s", vm.get("vmid"), e)
    
    logger.info("Parallel IP lookup completed: found %d IPs", len(results))
    return results


def _build_vm_dict(raw: Dict[str, Any], skip_ip: bool = False) -> Dict[str, Any]:
    vmid = int(raw["vmid"])
    node = raw["node"]
    name = raw.get("name", f"vm-{vmid}")
    status = raw.get("status", "unknown")
    vmtype = raw.get("type", "qemu")
    
    # Fetch ostype from VM config if not already in raw data
    if "ostype" not in raw and vmtype == "qemu":
        try:
            config = get_proxmox_admin().nodes(node).qemu(vmid).config.get()
            if config:
                raw["ostype"] = config.get("ostype", "")
            else:
                raw["ostype"] = ""
        except Exception as e:
            logger.debug("Could not fetch ostype for VM %s: %s", vmid, e)
            raw["ostype"] = ""

    category = _guess_category(raw)
    
    # IP lookup strategy (prioritized):
    # 1. Memory cache (1-hour TTL) with validation for running VMs
    # 2. LXC: Direct API interface lookup (for static IPs)
    # 3. ARP scanning for QEMU and LXC with DHCP (handled by _enrich_vms_with_arp_ips)
    ip = None
    if not skip_ip:
        # First: Check memory cache
        cached_ip = _get_cached_ip(vmid)
        if cached_ip:
            # Validate cached IP for running VMs to detect IP changes
            if status == "running" and vmtype == "qemu":
                # Validation happens via ARP scan in background
                ip = cached_ip
            else:
                # For stopped VMs or LXC, trust cache
                ip = cached_ip
        
        # Second: For LXC, try direct API lookup (works for static IPs)
        if not ip and vmtype == "lxc":
            # LXC containers: network config API
            ip = _lookup_vm_ip(node, vmid, vmtype)
            if ip:
                _cache_ip(vmid, ip)
            # If no IP from API and container is running, ARP scan will find it (DHCP case)
        
        # For running VMs/containers without IPs:
        # - Don't set to None (frontend shows "N/A")
        # - ARP scan in _enrich_vms_with_arp_ips() will populate it
        # - Frontend will show "Fetching..." until scan completes
    
    # Check if RDP port is available (Windows VMs only, and only if we have an IP)
    rdp_available = False
    if category == "windows" and ip and ip not in ("N/A", "Fetching...", ""):
        rdp_available = _check_rdp_port(ip)

    return {
        "vmid": vmid,
        "node": node,
        "name": name,
        "status": status,
        "type": vmtype,      # 'qemu' or 'lxc'
        "category": category,
        "ip": ip,
        "rdp_available": rdp_available,
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


def _enrich_vms_with_arp_ips(vms: List[Dict[str, Any]], force_sync: bool = False) -> None:
    """
    Enrich VM list with IPs discovered via ARP scanning (fast, in-place).
    
    Scans ALL running QEMU VMs and LXC containers without IPs.
    This is the PRIMARY method for QEMU VM IP discovery (no guest agent needed).
    Also used for LXC containers with DHCP that don't have IPs in interface API.
    Validates cached IPs and updates if changed.
    
    Offline VMs can't have network presence, so skipped.
    
    Args:
        vms: List of VM dictionaries to enrich
        force_sync: If True, run scan synchronously (blocks until complete)
    """
    logger.info("=== ARP SCAN START === Total VMs=%d, ARP_SCANNER_AVAILABLE=%s", 
                len(vms), ARP_SCANNER_AVAILABLE)
    
    if not ARP_SCANNER_AVAILABLE:
        logger.warning("ARP scanner NOT AVAILABLE - skipping ARP discovery")
        return
    
    # Build map of vmid -> MAC for:
    # 1. ALL running QEMU VMs (primary method)
    # 2. Running LXC containers WITHOUT IPs (DHCP case)
    vm_mac_map = {}
    qemu_count = 0
    lxc_dhcp_count = 0
    stopped_count = 0
    lxc_with_ip_count = 0
    
    for vm in vms:
        # Skip if VM is not running (offline VMs won't be in ARP table)
        if vm.get("status") != "running":
            stopped_count += 1
            logger.debug("VM %d (%s) is %s - SKIPPED", 
                        vm["vmid"], vm["name"], vm.get("status"))
            continue
        
        vmtype = vm.get("type")
        has_ip = vm.get("ip") and vm.get("ip") not in ("N/A", "Fetching...", "")
        
        # Include QEMU VMs (always use ARP)
        # Include LXC without IPs (DHCP case)
        if vmtype == "qemu" or (vmtype == "lxc" and not has_ip):
            mac = _get_vm_mac(vm["node"], vm["vmid"], vm["type"])
            if mac:
                vm_mac_map[vm["vmid"]] = mac
                if vmtype == "qemu":
                    qemu_count += 1
                    logger.debug("VM %d (%s) QEMU: MAC=%s, current_ip=%s - WILL SCAN", 
                                vm["vmid"], vm["name"], mac, vm.get("ip", "N/A"))
                else:
                    lxc_dhcp_count += 1
                    logger.debug("VM %d (%s) LXC DHCP: MAC=%s - WILL SCAN", 
                                vm["vmid"], vm["name"], mac)
            else:
                logger.warning("VM %d (%s, %s) is running but has NO MAC found in config", 
                              vm["vmid"], vm["name"], vmtype)
        else:
            lxc_with_ip_count += 1
            logger.debug("VM %d (%s) is LXC with IP=%s - SKIP SCAN", 
                        vm["vmid"], vm["name"], vm.get("ip"))
    
    logger.info("ARP scan candidates: %d QEMU + %d LXC DHCP = %d total (stopped: %d, LXC with IP: %d)", 
                qemu_count, lxc_dhcp_count, len(vm_mac_map), stopped_count, lxc_with_ip_count)
    
    if not vm_mac_map:
        logger.info("=== ARP SCAN COMPLETE === No running VMs need ARP discovery")
        return
    
    # Log the MAC map being sent to scanner
    logger.info("VM MAC map being sent to ARP scanner: %s", 
                {vmid: mac for vmid, mac in list(vm_mac_map.items())[:10]})
    
    logger.info("Starting ARP discovery for %d running VMs (synchronous mode)...", len(vm_mac_map))
    
    # Run ARP scan synchronously for immediate results (fast: ~1-2 seconds)
    # This ensures IPs are available on first page load
    discovered_ips = discover_ips_via_arp(vm_mac_map, subnets=ARP_SUBNETS, background=False)
    
    logger.info("ARP discovery returned %d IPs: %s", len(discovered_ips),
                {vmid: ip for vmid, ip in list(discovered_ips.items())[:10]})  # Show first 10
    
    # Update VMs with discovered IPs and cache them
    updated_count = 0
    changed_count = 0
    
    for vm in vms:
        if vm["vmid"] in discovered_ips:
            new_ip = discovered_ips[vm["vmid"]]
            old_ip = vm.get("ip")
            
            # Update VM and cache
            vm["ip"] = new_ip
            _cache_ip(vm["vmid"], new_ip)
            updated_count += 1
            
            # Check RDP port availability for Windows VMs
            if vm.get("category") == "windows" and new_ip:
                rdp_available = _check_rdp_port(new_ip)
                vm["rdp_available"] = rdp_available
                logger.debug("VM %d (%s): Windows RDP port check = %s", 
                            vm["vmid"], vm["name"], rdp_available)
            
            # Log IP changes for validation tracking
            if old_ip and old_ip != new_ip:
                changed_count += 1
                logger.info("VM %d (%s): IP CHANGED %s -> %s", 
                           vm["vmid"], vm["name"], old_ip, new_ip)
            else:
                logger.info("VM %d (%s): IP FOUND %s", vm["vmid"], vm["name"], new_ip)
    
    logger.info("=== ARP SCAN COMPLETE === Updated %d VMs, %d changed IPs", 
                updated_count, changed_count)



def get_all_vms(skip_ips: bool = False, force_refresh: bool = False) -> List[Dict[str, Any]]:
    """
    Cached list of all VMs/containers visible to the admin user.

    Each item: { vmid, node, name, status, ip, category, type }
    
    Uses /cluster/resources endpoint for fast loading (single API call).
    Set skip_ips=True to skip ARP scan for fast initial page load.
    Set force_refresh=True to bypass cache and fetch fresh data from Proxmox.
    """
    global _vm_cache_data, _vm_cache_ts

    # Load both caches from disk on first call
    _load_vm_cache()
    _load_ip_cache()

    now = time.time()
    # Check if we have valid cached data
    cache_valid = _vm_cache_data is not None and (now - _vm_cache_ts) < VM_CACHE_TTL
    
    # If cache is valid and not forcing refresh, return cached data
    if not force_refresh and cache_valid and _vm_cache_data is not None:
        logger.info("get_all_vms: returning cached data (skip_ips=%s, age=%.1fs)", skip_ips, now - _vm_cache_ts)
        result = []
        for vm in _vm_cache_data:
            result.append({**vm})
        
        # Run ARP scan unless skipping IPs
        if not skip_ips and ARP_SCANNER_AVAILABLE:
            logger.info("get_all_vms: calling ARP scan enrichment on cached data")
            _enrich_vms_with_arp_ips(result)
        elif skip_ips:
            logger.info("get_all_vms: SKIPPING ARP scan (skip_ips=True)")
        
        return result

    # Fast path: use cluster resources API (single call vs N node queries)
    out: List[Dict[str, Any]] = []
    try:
        resources = get_proxmox_admin().cluster.resources.get(type="vm") or []
        logger.info("get_all_vms: fetched %d resources from cluster API", len(resources))
        
        for vm in resources:
            # Skip templates (template=1)
            if vm.get("template") == 1:
                continue
            
            # Filter by VALID_NODES if configured
            if VALID_NODES and vm.get("node") not in VALID_NODES:
                continue
            
            out.append(_build_vm_dict(vm))
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
                    # Skip templates
                    if vm.get('template') == 1:
                        continue
                    vm['node'] = node
                    vm['type'] = 'qemu'
                    out.append(_build_vm_dict(vm))
                
                # Get LXC containers
                lxc_vms = get_proxmox_admin().nodes(node).lxc.get() or []
                for vm in lxc_vms:
                    # Skip templates
                    if vm.get('template') == 1:
                        continue
                    vm['node'] = node
                    vm['type'] = 'lxc'
                    out.append(_build_vm_dict(vm))
            except Exception as e:
                logger.debug("failed to list VMs on %s: %s", node, e)

    # Sort VMs alphabetically by name
    out.sort(key=lambda vm: (vm.get("name") or "").lower())

    logger.info("get_all_vms: returning %d VM(s)", len(out))

    # Always enrich with user mappings (needed for both admin and user views)
    _enrich_with_user_mappings(out)

    # Run ARP-based IP discovery unless skipping IPs
    if not skip_ips and ARP_SCANNER_AVAILABLE:
        logger.info("get_all_vms: calling ARP scan enrichment")
        _enrich_vms_with_arp_ips(out)
    elif skip_ips:
        logger.info("get_all_vms: SKIPPING ARP scan (skip_ips=True)")
    else:
        logger.warning("get_all_vms: SKIPPING ARP scan (scanner not available)")

    # Cache the VM structure
    _vm_cache_data = out
    _vm_cache_ts = now
    _save_vm_cache()  # Persist to disk for fast restart
    
    return out


# ---------------------------------------------------------------------------
# User ↔ VM mappings (JSON file with in-memory cache and write-through)
# ---------------------------------------------------------------------------

def _load_mapping() -> Dict[str, List[int]]:
    """Load mappings from disk, using in-memory cache if available. Thread-safe."""
    global _mappings_cache, _mappings_cache_loaded
    
    # Fast path: return cached data if loaded
    with _mappings_cache_lock:
        if _mappings_cache_loaded and _mappings_cache is not None:
            return dict(_mappings_cache)  # Return a copy
    
    # Load from disk
    if not os.path.exists(MAPPINGS_FILE):
        with _mappings_cache_lock:
            _mappings_cache = {}
            _mappings_cache_loaded = True
        return {}

    try:
        with open(MAPPINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.debug("failed to load mappings file %s: %s", MAPPINGS_FILE, e)
        with _mappings_cache_lock:
            _mappings_cache = {}
            _mappings_cache_loaded = True
        return {}

    out: Dict[str, List[int]] = {}
    for user, vmids in data.items():
        try:
            out[user] = sorted({int(v) for v in vmids})
        except Exception:
            continue
    
    # Cache the loaded data
    with _mappings_cache_lock:
        _mappings_cache = dict(out)
        _mappings_cache_loaded = True
    
    return out


def _save_mapping(mapping: Dict[str, List[int]]) -> None:
    """Save mappings to disk and update in-memory cache. Thread-safe."""
    global _mappings_cache
    
    tmp = {}
    for user, vmids in mapping.items():
        tmp[user] = sorted({int(v) for v in vmids})
    
    # Update in-memory cache first (write-through)
    with _mappings_cache_lock:
        _mappings_cache = dict(tmp)
    
    # Write to disk
    try:
        with open(MAPPINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(tmp, f, indent=2)
    except Exception as e:
        logger.exception("failed to save mappings file %s: %s", MAPPINGS_FILE, e)


def get_user_vm_map() -> Dict[str, List[int]]:
    """
    Returns { 'user@pve': [vmid, ...], ... }
    
    Uses in-memory cache with write-through to disk on updates.
    Thread-safe.
    """
    return _load_mapping()


def set_user_vm_mapping(user: str, vmids: List[int]) -> None:
    """
    Update mapping for one user. Empty list removes mapping.
    
    Uses in-memory cache with write-through to disk.
    Thread-safe.
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


def invalidate_mappings_cache() -> None:
    """Invalidate the in-memory mappings cache.
    
    Call this to force a reload from disk on next access.
    """
    global _mappings_cache, _mappings_cache_loaded
    with _mappings_cache_lock:
        _mappings_cache = None
        _mappings_cache_loaded = False


def _get_admin_group_members_cached() -> List[str]:
    """
    Get admin group members from cache or fetch from Proxmox if cache expired.
    Thread-safe with 120s TTL to avoid repeated Proxmox calls.
    
    Returns a list of member userids (strings).
    """
    global _admin_group_cache, _admin_group_ts
    
    if not ADMIN_GROUP:
        return []
    
    now = time.time()
    
    # Check cache first (thread-safe read)
    with _admin_group_lock:
        if _admin_group_cache is not None and (now - _admin_group_ts) < ADMIN_GROUP_CACHE_TTL:
            logger.debug("Using cached admin group members (age=%.1fs)", now - _admin_group_ts)
            return list(_admin_group_cache)
    
    # Cache miss or expired - fetch from Proxmox
    try:
        grp = get_proxmox_admin().access.groups(ADMIN_GROUP).get()
        raw_members = (grp.get("members", []) or []) if isinstance(grp, dict) else []
        members = []
        for m in raw_members:
            if isinstance(m, str):
                members.append(m)
            elif isinstance(m, dict) and "userid" in m:
                members.append(m["userid"])
        
        # Update cache (thread-safe write)
        with _admin_group_lock:
            _admin_group_cache = members
            _admin_group_ts = now
        logger.debug("Refreshed admin group cache: %d members", len(members))
        return members
        
    except Exception as e:
        logger.warning("Failed to get admin group members: %s", e)
        # On error, return cached value if we have one (even if expired)
        with _admin_group_lock:
            if _admin_group_cache is not None:
                logger.debug("Using stale admin group cache due to error")
                return list(_admin_group_cache)
        return []


def _user_in_group(user: str, groupid: str) -> bool:
    """
    Handles both possible Proxmox group member formats:
    - members: ["user@pve", "other@pve"]
    - members: [{"userid": "user@pve"}, {"userid": "other@pve"}]
    
    Uses cached admin group members if groupid is the admin group.
    """
    if not groupid:
        return False

    # Use cached admin group members for the admin group
    if groupid == ADMIN_GROUP:
        members = _get_admin_group_members_cached()
    else:
        # For other groups, fetch directly (no caching)
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
    """Get list of users in the admin group.
    
    Uses the in-memory cache to avoid repeated Proxmox API calls.
    """
    return _get_admin_group_members_cached()


def _invalidate_admin_group_cache() -> None:
    """Invalidate the admin group cache to force a refresh on next access."""
    global _admin_group_cache, _admin_group_ts
    with _admin_group_lock:
        _admin_group_cache = None
        _admin_group_ts = 0.0
    logger.debug("Invalidated admin group cache")


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
            comment = group_info.get('comment', '') if group_info else ''
        except:
            comment = ''
        
        get_proxmox_admin().access.groups(ADMIN_GROUP).put(
            comment=comment,
            members=",".join(current_members)
        )
        # Invalidate cache after modification
        _invalidate_admin_group_cache()
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
            comment = group_info.get('comment', '') if group_info else ''
        except:
            comment = ''
        
        get_proxmox_admin().access.groups(ADMIN_GROUP).put(
            comment=comment,
            members=",".join(current_members)
        )
        # Invalidate cache after modification
        _invalidate_admin_group_cache()
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
    Set skip_ips=True to skip ARP scan for fast initial page load.
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
    _clear_vm_ip_cache(vmid)  # Clear IP cache - VM might get new IP on boot


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
    _clear_vm_ip_cache(vmid)  # Clear IP cache - VM no longer has IP when stopped


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
