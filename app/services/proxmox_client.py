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

from app.config import (
    PVE_HOST,
    PVE_ADMIN_USER,
    PVE_ADMIN_PASS,
    PVE_VERIFY,
    VALID_NODES,
    ADMIN_USERS,
    ADMIN_GROUP,
    MAPPINGS_FILE,
    VM_CACHE_FILE,
    VM_CACHE_TTL,
    PROXMOX_CACHE_TTL,
    ENABLE_IP_LOOKUP,
    ENABLE_IP_PERSISTENCE,
    DB_IP_CACHE_TTL,
    ARP_SUBNETS,
    CLUSTERS,
)

# Import ARP scanner for fast IP discovery
try:
    from app.services.arp_scanner import discover_ips_via_arp, normalize_mac, has_rdp_port_open, invalidate_arp_cache
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
# Proxmox connection per cluster - dict mapping cluster_id to ProxmoxAPI instance
_proxmox_connections: Dict[str, Any] = {}

def get_current_cluster_id():
    """Get current cluster ID from Flask session or default to first cluster."""
    try:
        from flask import session
        return session.get("cluster_id", CLUSTERS[0]["id"])
    except (RuntimeError, ImportError):
        # No Flask context (e.g., background thread or CLI) - use default
        return CLUSTERS[0]["id"]


def get_current_cluster():
    """Get current cluster configuration based on session."""
    cluster_id = get_current_cluster_id()
    
    for cluster in CLUSTERS:
        if cluster["id"] == cluster_id:
            return cluster
    
    # Fallback to first cluster if invalid ID somehow
    return CLUSTERS[0]


def switch_cluster(cluster_id: str):
    """Switch to a different Proxmox cluster.
    
    Args:
        cluster_id: ID of the cluster to switch to
    
    Invalidates all cached connections and forces reconnection on next use.
    """
    global _proxmox_connections
    
    # Validate cluster ID
    valid = any(c["id"] == cluster_id for c in CLUSTERS)
    if not valid:
        raise ValueError(f"Invalid cluster ID: {cluster_id}")
    
    with _proxmox_lock:
        # Clear all connections to force fresh connections
        _proxmox_connections = {}
        logger.info("Cleared all cluster connections for switch to: %s", cluster_id)


def get_proxmox_admin_for_cluster(cluster_id: str):
    """Get or create Proxmox connection for a specific cluster.
    
    Unlike get_proxmox_admin(), this doesn't rely on Flask session context.
    Used when fetching VMs from multiple clusters simultaneously.
    """
    global _proxmox_connections
    
    # Fast path: return existing connection if available
    if cluster_id in _proxmox_connections:
        return _proxmox_connections[cluster_id]
    
    # Find the cluster config
    cluster = next((c for c in CLUSTERS if c["id"] == cluster_id), None)
    if not cluster:
        raise ValueError(f"Unknown cluster_id: {cluster_id}")
    
    with _proxmox_lock:
        # Double-check inside lock to handle race conditions
        if cluster_id not in _proxmox_connections:
            _proxmox_connections[cluster_id] = ProxmoxAPI(
                cluster["host"],
                user=cluster["user"],
                password=cluster["password"],
                verify_ssl=cluster.get("verify_ssl", False),
            )
            logger.info("Connected to Proxmox cluster '%s' at %s", cluster["name"], cluster["host"])
    
    return _proxmox_connections[cluster_id]


def get_proxmox_admin():
    """Get or create Proxmox connection for current cluster (lazy initialization).
    
    Thread-safe singleton pattern per cluster: uses double-checked locking to ensure
    only one Proxmox connection per cluster is created even in multi-threaded scenarios.
    Each cluster's ProxmoxAPI client is reused for all subsequent calls to that cluster.
    
    Supports multi-cluster: uses current cluster from Flask session.
    """
    global _proxmox_connections
    
    cluster = get_current_cluster()
    cluster_id = cluster["id"]
    
    # Fast path: return existing connection if available
    if cluster_id in _proxmox_connections:
        return _proxmox_connections[cluster_id]
    
    with _proxmox_lock:
        # Double-check inside lock to handle race conditions
        if cluster_id not in _proxmox_connections:
            _proxmox_connections[cluster_id] = ProxmoxAPI(
                cluster["host"],
                user=cluster["user"],
                password=cluster["password"],
                verify_ssl=cluster.get("verify_ssl", False),
            )
            logger.info("Connected to Proxmox cluster '%s' at %s", cluster["name"], cluster["host"])
    
    return _proxmox_connections[cluster_id]


def get_cluster_config() -> List[Dict[str, Any]]:
    """Get current cluster configuration."""
    return CLUSTERS


def save_cluster_config(clusters: List[Dict[str, Any]]) -> None:
    """Save cluster configuration to JSON file and update runtime config.
    
    Persists changes across restarts by writing to clusters.json.
    """
    global CLUSTERS
    import app.config as config
    from app.config import CLUSTER_CONFIG_FILE
    
    # Update runtime config
    CLUSTERS.clear()
    CLUSTERS.extend(clusters)
    config.CLUSTERS = CLUSTERS
    
    # Write to JSON file for persistence
    try:
        with open(CLUSTER_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(clusters, f, indent=2)
        logger.info(f"Saved cluster configuration to {CLUSTER_CONFIG_FILE}")
    except Exception as e:
        logger.error(f"Failed to write cluster config file: {e}")
        raise
    
    # Clear all existing connections to force reconnection with new config
    global _proxmox_connections
    with _proxmox_lock:
        _proxmox_connections.clear()
    
    logger.info(f"Updated cluster configuration with {len(clusters)} cluster(s)")


def _create_proxmox_client():
    """Create a new Proxmox client for thread-local use.
    
    Used by ThreadPoolExecutor workers to avoid sharing the main client
    across threads, since ProxmoxAPI may not be fully thread-safe.
    """
    cluster = get_current_cluster()
    return ProxmoxAPI(
        cluster["host"],
        user=cluster["user"],
        password=cluster["password"],
        verify_ssl=cluster.get("verify_ssl", False),
    )


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VM cache: single cluster snapshot, reused everywhere
# Thread-safe with locks for concurrent access
# ---------------------------------------------------------------------------

# Thread-safe locks for caches
_vm_cache_lock = threading.Lock()
_mappings_cache_lock = threading.Lock()

# Per-cluster VM cache - dict mapping cluster_id to cache data
_vm_cache_data: Dict[str, Optional[List[Dict[str, Any]]]] = {}
_vm_cache_ts: Dict[str, float] = {}
_vm_cache_loaded: Dict[str, bool] = {}

# Short-lived cluster resources cache (PROXMOX_CACHE_TTL seconds, default 10)
# Per-cluster cache
_cluster_cache_data: Dict[str, Optional[List[Dict[str, Any]]]] = {}
_cluster_cache_ts: Dict[str, float] = {}
_cluster_cache_lock = threading.Lock()

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


def _load_vm_cache() -> None:
    """Load VM cache from JSON file on startup for all clusters."""
    global _vm_cache_data, _vm_cache_ts, _vm_cache_loaded
    cache_key = "all_clusters"
    
    if _vm_cache_loaded.get(cache_key, False):
        return
    
    # DISABLED: JSON cache replaced by database (VMInventory table)
    # Database is loaded on-demand via fetch_vm_inventory() in API routes
    _vm_cache_data[cache_key] = None
    _vm_cache_ts[cache_key] = 0.0
    _vm_cache_loaded[cache_key] = True
    logger.info("VM cache loading from JSON disabled - using database (VMInventory) instead")
    return
    
    # OLD CODE (disabled):
    # if not os.path.exists(VM_CACHE_FILE):
    #     _vm_cache_data[cache_key] = None
    #     _vm_cache_ts[cache_key] = 0.0
    #     _vm_cache_loaded[cache_key] = True
    #     return
    # 
    # try:
    #     with open(VM_CACHE_FILE, "r", encoding="utf-8") as f:
    #         all_data = json.load(f)
    #         cluster_data = all_data.get(cache_key, {})
    #         _vm_cache_data[cache_key] = cluster_data.get("vms", [])
    #         _vm_cache_ts[cache_key] = cluster_data.get("timestamp", 0.0)
    #     cache_count = len(_vm_cache_data.get(cache_key) or [])
    #     logger.info("Loaded VM cache for all clusters with %d VMs from %s", 
    #                cache_count, VM_CACHE_FILE)
    # except Exception as e:
    #     logger.warning("Failed to load VM cache: %s", e)
    #     _vm_cache_data[cache_key] = None
    #     _vm_cache_ts[cache_key] = 0.0
    
    _vm_cache_loaded[cache_key] = True

def _save_vm_cache() -> None:
    """Save VM cache to JSON file for all clusters.
    
    Skips writing if the on-disk cache timestamp matches the in-memory timestamp,
    indicating no changes since last save.
    """
    cache_key = "all_clusters"
    if _vm_cache_data.get(cache_key) is None:
        return
    
    # Load existing data for all clusters
    all_data = {}
    if os.path.exists(VM_CACHE_FILE):
        try:
            with open(VM_CACHE_FILE, "r", encoding="utf-8") as f:
                all_data = json.load(f)
            # Check if on-disk cache is already up-to-date
            disk_cluster_data = all_data.get(cache_key, {})
            disk_ts = disk_cluster_data.get("timestamp", 0.0)
            if disk_ts == _vm_cache_ts.get(cache_key, 0.0):
                logger.debug("Skipping VM cache write: disk timestamp matches in-memory (%.1f)", disk_ts)
                return
        except Exception:
            # If we can't read the file, proceed with write
            pass
    
    try:
        # Update cluster-specific data
        all_data[cache_key] = {
            "vms": _vm_cache_data[cache_key],
            "timestamp": _vm_cache_ts.get(cache_key, time.time())
        }
        with open(VM_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(all_data, f, indent=2)
        cache_count = len(_vm_cache_data.get(cache_key) or [])
        logger.debug("Saved VM cache with %d VMs", cache_count)
    except Exception as e:
        logger.warning("Failed to save VM cache: %s", e)

def _invalidate_vm_cache() -> None:
    """Invalidate VM cache for all clusters."""
    global _vm_cache_data, _vm_cache_ts, _cluster_cache_data, _cluster_cache_ts
    cache_key = "all_clusters"
    with _vm_cache_lock:
        _vm_cache_data[cache_key] = None
        _vm_cache_ts[cache_key] = 0.0
    # Also invalidate cluster-specific caches
    cluster_id = get_current_cluster_id()
    with _cluster_cache_lock:
        _cluster_cache_data[cluster_id] = None
        _cluster_cache_ts[cluster_id] = 0.0
    # Note: Don't reset _vm_cache_loaded - we want to keep trying disk cache
    logger.debug("Invalidated VM cache for all clusters")


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
    cluster_id = get_current_cluster_id()
    
    now = time.time()
    with _cluster_cache_lock:
        cached_data = _cluster_cache_data.get(cluster_id)
        cached_ts = _cluster_cache_ts.get(cluster_id, 0)
        if cached_data is not None and (now - cached_ts) < PROXMOX_CACHE_TTL:
            # Return cached QEMU VMs
            return [r for r in cached_data if r.get("type") == "qemu"]
    
    # Fetch fresh data from Proxmox
    try:
        resources = get_proxmox_admin().cluster.resources.get(type="vm") or []
        with _cluster_cache_lock:
            _cluster_cache_data[cluster_id] = resources
            _cluster_cache_ts[cluster_id] = now
        logger.debug("get_all_qemu_vms: fetched %d resources from cluster %s API", len(resources), cluster_id)
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
    cluster_id = get_current_cluster_id()
    
    now = time.time()
    with _cluster_cache_lock:
        cached_data = _cluster_cache_data.get(cluster_id)
        cached_ts = _cluster_cache_ts.get(cluster_id, 0)
        if cached_data is not None and (now - cached_ts) < PROXMOX_CACHE_TTL:
            # Return cached LXC containers
            return [r for r in cached_data if r.get("type") == "lxc"]
    
    # Fetch fresh data from Proxmox
    try:
        resources = get_proxmox_admin().cluster.resources.get(type="vm") or []
        with _cluster_cache_lock:
            _cluster_cache_data[cluster_id] = resources
            _cluster_cache_ts[cluster_id] = now
        logger.debug("get_all_lxc_containers: fetched %d resources from cluster %s API", len(resources), cluster_id)
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
    cluster_id = get_current_cluster_id()
    
    now = time.time()
    with _cluster_cache_lock:
        cached_data = _cluster_cache_data.get(cluster_id)
        cached_ts = _cluster_cache_ts.get(cluster_id, 0)
        if cached_data is not None and (now - cached_ts) < PROXMOX_CACHE_TTL:
            return list(cached_data)
    
    # Fetch fresh data from Proxmox
    try:
        resources = get_proxmox_admin().cluster.resources.get(type="vm") or []
        with _cluster_cache_lock:
            _cluster_cache_data[cluster_id] = resources
            _cluster_cache_ts[cluster_id] = now
        logger.debug("_get_cluster_resources_cached: fetched %d resources from cluster %s", len(resources), cluster_id)
        return resources
    except Exception as e:
        logger.warning("Failed to fetch cluster resources: %s", e)
        return []


def invalidate_cluster_cache() -> None:
    """Invalidate the short-lived cluster resources cache for current cluster.
    
    Called after VM start/stop operations to ensure fresh data is fetched.
    """
    global _cluster_cache_data, _cluster_cache_ts
    cluster_id = get_current_cluster_id()
    with _cluster_cache_lock:
        _cluster_cache_data[cluster_id] = None
        _cluster_cache_ts[cluster_id] = 0.0
    logger.debug("Invalidated cluster cache for %s", cluster_id)


def _get_cached_ip_from_db(cluster_id: str, vmid: int) -> Optional[str]:
    """Get IP from database cache if not expired (1 hour TTL)."""
    from datetime import datetime, timedelta
    from flask import has_app_context
    from app.models import VMAssignment, VMIPCache
    
    if not has_app_context():
        logger.debug("_get_cached_ip_from_db: skipping (no app context)")
        return None
    
    try:
        # Check VMAssignment first (class VMs)
        assignment = VMAssignment.query.filter_by(vmid=vmid).first()
        if assignment and assignment.cached_ip and assignment.ip_updated_at:
            age = datetime.utcnow() - assignment.ip_updated_at
            if age.total_seconds() < DB_IP_CACHE_TTL:
                return assignment.cached_ip
        
        # Check VMIPCache (legacy VMs)
        cache_entry = VMIPCache.query.filter_by(vmid=vmid, cluster_id=cluster_id).first()
        if cache_entry and cache_entry.cached_ip and cache_entry.ip_updated_at:
            age = datetime.utcnow() - cache_entry.ip_updated_at
            if age.total_seconds() < DB_IP_CACHE_TTL:
                return cache_entry.cached_ip
    except Exception as e:
        logger.debug(f"Failed to get cached IP from DB for VM {vmid}: {e}")
    
    # Final fallback: check VMInventory table for persisted IP
    try:
        from app.models import VMInventory
        inventory = VMInventory.query.filter_by(cluster_id=cluster_id, vmid=vmid).first()
        if inventory and inventory.ip and inventory.ip not in ('N/A', 'Fetching...', ''):
            return inventory.ip
    except Exception:
        pass
    return None


def _get_cached_ips_batch(cluster_id: str, vmids: List[int]) -> Dict[int, str]:
    """Get multiple IPs from database cache in batch.
    
    Returns:
        Dict mapping vmid to cached IP (only includes VMs with valid cached IPs)
    """
    from datetime import datetime, timedelta
    from flask import has_app_context
    from app.models import VMAssignment, VMIPCache
    
    if not has_app_context():
        logger.debug("_get_cached_ips_batch: skipping (no app context)")
        return {}
    
    results: Dict[int, str] = {}
    cutoff_seconds = DB_IP_CACHE_TTL
    cutoff = datetime.utcnow() - timedelta(seconds=cutoff_seconds)
    
    try:
        # Get all VMAssignments for these VMs
        assignments = VMAssignment.query.filter(
            VMAssignment.vmid.in_(vmids),
            VMAssignment.cached_ip.isnot(None),
            VMAssignment.ip_updated_at >= cutoff
        ).all()
        
        for assignment in assignments:
            results[assignment.vmid] = assignment.cached_ip
        
        # Get remaining VMs from VMIPCache
        remaining_vmids = [v for v in vmids if v not in results]
        if remaining_vmids:
            cache_entries = VMIPCache.query.filter(
                VMIPCache.vmid.in_(remaining_vmids),
                VMIPCache.cluster_id == cluster_id,
                VMIPCache.cached_ip.isnot(None),
                VMIPCache.ip_updated_at >= cutoff
            ).all()
            
            for entry in cache_entries:
                results[entry.vmid] = entry.cached_ip
    
    except Exception as e:
        logger.debug(f"Failed to batch fetch cached IPs from DB: {e}")
    
    # Final fallback: check VMInventory table for remaining VMs
    try:
        from app.models import VMInventory
        remaining_vmids = [v for v in vmids if v not in results]
        if remaining_vmids:
            inventory_entries = VMInventory.query.filter(
                VMInventory.cluster_id == cluster_id,
                VMInventory.vmid.in_(remaining_vmids),
                VMInventory.ip.isnot(None)
            ).all()
            for entry in inventory_entries:
                if entry.ip and entry.ip not in ('N/A', 'Fetching...', ''):
                    results[entry.vmid] = entry.ip
    except Exception as e:
        logger.debug(f"Failed to fetch IPs from VMInventory: {e}")
    
    return results


def _cache_ip_to_db(cluster_id: str, vmid: int, ip: Optional[str], mac: Optional[str] = None) -> None:
    """Store IP in database cache."""
    from datetime import datetime
    from flask import has_app_context
    from app.models import db, VMAssignment, VMIPCache
    
    if not has_app_context():
        logger.debug("_cache_ip_to_db: skipping (no app context)")
        return
    
    if not ip:
        return
    
    try:
        now = datetime.utcnow()
        
        # Try to update VMAssignment first
        assignment = VMAssignment.query.filter_by(vmid=vmid).first()
        if assignment:
            assignment.cached_ip = ip
            assignment.ip_updated_at = now
            if mac:
                assignment.mac_address = mac
            db.session.commit()
            return
        
        # Otherwise update/create VMIPCache
        cache_entry = VMIPCache.query.filter_by(vmid=vmid, cluster_id=cluster_id).first()
        if cache_entry:
            cache_entry.cached_ip = ip
            cache_entry.ip_updated_at = now
            if mac:
                cache_entry.mac_address = mac
        else:
            cache_entry = VMIPCache(
                vmid=vmid,
                cluster_id=cluster_id,
                cached_ip=ip,
                mac_address=mac,
                ip_updated_at=now
            )
            db.session.add(cache_entry)
        
        db.session.commit()
    except Exception as e:
        logger.debug(f"Failed to cache IP to DB for VM {vmid}: {e}")
        db.session.rollback()


def _clear_vm_ip_cache(cluster_id: str, vmid: int) -> None:
    """Clear IP cache for specific VM in database."""
    from flask import has_app_context
    from app.models import db, VMAssignment, VMIPCache
    
    if not has_app_context():
        logger.debug("_clear_vm_ip_cache: skipping (no app context)")
        return
    
    try:
        # Clear VMAssignment cache
        assignment = VMAssignment.query.filter_by(vmid=vmid).first()
        if assignment and assignment.cached_ip:
            assignment.cached_ip = None
            assignment.ip_updated_at = None
            logger.debug("Cleared VMAssignment IP cache for VM %d", vmid)
        
        # Clear VMIPCache
        cache_entry = VMIPCache.query.filter_by(vmid=vmid).first()
        if cache_entry:
            db.session.delete(cache_entry)
            logger.debug("Deleted VMIPCache entry for VM %d", vmid)
        
        db.session.commit()
    except Exception as e:
        logger.error("Failed to clear database IP cache for VM %d: %s", vmid, e)
        db.session.rollback()

def verify_vm_ip(cluster_id: str, node: str, vmid: int, vmtype: str, cached_ip: str) -> Optional[str]:
    """Verify cached IP is still correct, return updated IP if different.
    
    This does a quick check without blocking. Returns:
    - cached_ip if still valid
    - new IP if different
    - None if VM is offline or unreachable
    """
    try:
        current_ip = _lookup_vm_ip(node, vmid, vmtype)
        if current_ip and current_ip != cached_ip:
            logger.info("VM %s:%d IP changed: %s -> %s", cluster_id, vmid, cached_ip, current_ip)
            _cache_ip_to_db(cluster_id, vmid, current_ip)
            return current_ip
        return cached_ip
    except Exception as e:
        logger.debug("Failed to verify IP for VM %s:%d: %s", cluster_id, vmid, e)
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


def _get_vm_mac(node: str, vmid: int, vmtype: str, cluster_id: str = None) -> Optional[str]:
    """
    Get MAC address for a VM.
    
    Checks all network interfaces (net0, net1, net2, ...) to find the first valid MAC.
    This handles VMs with multiple NICs or where net0 is not the primary interface.
    
    Args:
        node: Proxmox node name
        vmid: VM ID
        vmtype: VM type ('qemu' or 'lxc')
        cluster_id: Cluster ID to use for connection (defaults to first cluster)
    
    Returns normalized MAC (lowercase, no separators) or None.
    """
    try:
        # Get cluster-specific Proxmox connection
        if cluster_id:
            from app.services.proxmox_service import get_proxmox_admin_for_cluster
            proxmox = get_proxmox_admin_for_cluster(cluster_id)
        else:
            proxmox = get_proxmox_admin()
        
        if vmtype == "lxc":
            # LXC: get config to find MAC
            config = proxmox.nodes(node).lxc(vmid).config.get()
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
            config = proxmox.nodes(node).qemu(vmid).config.get()
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
    Determine OS category based on VM ostype field and name heuristic.
    
    - LXC: always 'linux'
    - QEMU: Check ostype field, fallback to name check for "win"
    - Default QEMU: 'linux'
    
    No extra API calls - uses existing VM data only.
    """
    vmtype = raw.get("type", "qemu")
    if vmtype == "lxc":
        return "linux"
    
    # Check ostype field from VM config
    ostype = (raw.get("ostype") or "").lower()
    name_lower = (raw.get("name") or "").lower()
    
    # Check ostype first, then fallback to name heuristic
    if "win" in ostype or "win" in name_lower:
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
    
    # Get cluster_id from first VM (all VMs in batch should be from same cluster)
    cluster_id = vms[0].get("cluster_id", "cluster1") if vms else "cluster1"
    cached_ips = _get_cached_ips_batch(cluster_id, all_vmids)
    
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
                # Extract cluster_id from the vm dict
                vm = futures[future]
                cluster_id = vm.get("cluster_id", "cluster1")
                _cache_ip_to_db(cluster_id, vmid, ip)
        except Exception as e:
            vm = futures[future]
            logger.debug("Parallel IP lookup failed for VM %s: %s", vm.get("vmid"), e)
    
    logger.info("Parallel IP lookup completed: found %d IPs", len(results))
    return results


def _build_vm_dict(raw: Dict[str, Any], skip_ips: bool = False) -> Dict[str, Any]:
    vmid = int(raw["vmid"])
    node = raw["node"]
    name = raw.get("name", f"vm-{vmid}")
    status = raw.get("status", "unknown")
    vmtype = raw.get("type", "qemu")
    # Preserve cluster identity if provided (added earlier in get_all_vms)
    cluster_id = raw.get("cluster_id") or "unknown_cluster"
    cluster_name = raw.get("cluster_name") or "Unknown Cluster"
    
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
    # 1. Database cache (1-hour TTL) with validation for running VMs
    # 2. LXC: Direct API interface lookup (for static IPs)
    # 3. ARP scanning for QEMU and LXC with DHCP (handled by _enrich_vms_with_arp_ips)
    ip = None
    if not skip_ips:
        # First: Check database cache
        # Note: cluster_id should be in raw dict, fallback to cluster1 for backward compat
        cluster_id = raw.get("cluster_id", "cluster1")
        cached_ip = _get_cached_ip_from_db(cluster_id, vmid)
        if cached_ip:
            # Validate cached IP for running VMs to detect IP changes
            if status == "running" and vmtype == "qemu":
                # Validation happens via ARP scan in background
                ip = cached_ip
            else:
                # For stopped VMs or LXC, trust cache
                ip = cached_ip
        
        # Second: For QEMU, try guest agent network interface API (works when guest agent running)
        if not ip and vmtype == "qemu" and status == "running":
            try:
                proxmox = get_proxmox_admin()
                # Try to get network interfaces from guest agent
                result = proxmox.nodes(node).qemu(vmid).agent('network-get-interfaces').get()
                if result and 'result' in result:
                    interfaces = result['result']
                    # Look for first non-loopback IPv4 address
                    for iface in interfaces:
                        if iface.get('name') in ('lo', 'loopback'):
                            continue
                        ip_addresses = iface.get('ip-addresses', [])
                        for addr in ip_addresses:
                            if addr.get('ip-address-type') == 'ipv4':
                                found_ip = addr.get('ip-address')
                                if found_ip and not found_ip.startswith('127.'):
                                    ip = found_ip
                                    logger.debug(f"QEMU {vmid} ({cluster_id}): Found IP via guest agent: {ip}")
                                    _cache_ip_to_db(cluster_id, vmid, ip)
                                    break
                        if ip:
                            break
            except Exception as e:
                logger.debug(f"QEMU {vmid} ({cluster_id}): Guest agent network lookup failed: {e}")
        
        # Third: For LXC, try direct API lookup (works for static IPs)
        if not ip and vmtype == "lxc":
            # LXC containers: network config API
            ip = _lookup_vm_ip(node, vmid, vmtype)
            if ip:
                logger.debug(f"LXC {vmid} ({cluster_id}): Found IP via API: {ip}")
                _cache_ip_to_db(cluster_id, vmid, ip)
            else:
                logger.debug(f"LXC {vmid} ({cluster_id}): No IP from API, will use ARP scan")
            # If no IP from API and container is running, ARP scan will find it (DHCP case)
        
        # For running VMs/containers without IPs:
        # - Don't set to None (frontend shows "N/A")
        # - ARP scan in _enrich_vms_with_arp_ips() will populate it
        # - Frontend will show "Fetching..." until scan completes
    
    # Check if RDP port is available
    # - All Windows VMs assumed to have RDP (default enabled) when running
    # - Any other VM with port 3389 open (e.g., Linux with xrdp)
    rdp_available = False
    if ip and ip not in ("N/A", "Fetching...", "") and status == "running":
        if category == "windows":
            rdp_available = True  # Windows always has RDP when running
        else:
            # For non-Windows VMs, check if RDP port is actually open
            rdp_available = has_rdp_port_open(ip)  # Check via scan cache

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
        "cluster_id": cluster_id,
        "cluster_name": cluster_name,
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


def _enrich_from_db_cache(vms: List[Dict[str, Any]]) -> None:
    """
    Enrich VMs with cached IP addresses from database (in-place).
    
    Checks VMAssignment.cached_ip for class VMs and VMIPCache for legacy VMs.
    Only uses cached IPs if they're less than 1 hour old.
    
    Args:
        vms: List of VM dictionaries to enrich with cached IPs
    """
    from datetime import datetime, timedelta
    from flask import has_app_context
    from app.models import db, VMAssignment, VMIPCache
    
    if not has_app_context():
        logger.debug("_enrich_from_db_cache: skipping (no app context)")
        return
    
    logger = logging.getLogger(__name__)
    cache_ttl = timedelta(hours=1)
    now = datetime.utcnow()
    hits = 0
    misses = 0
    expired = 0
    
    # Build maps: vmid -> cached_ip
    # Check VMAssignment first (class VMs)
    class_vm_cache = {}
    try:
        assignments = VMAssignment.query.filter(
            VMAssignment.cached_ip.isnot(None),
            VMAssignment.ip_updated_at.isnot(None)
        ).all()
        
        for assignment in assignments:
            age = now - assignment.ip_updated_at
            if age < cache_ttl:
                class_vm_cache[assignment.vmid] = assignment.cached_ip
            else:
                logger.debug("VM %d: cached IP expired (age=%s)", assignment.vmid, age)
                expired += 1
        
        logger.debug("Loaded %d cached IPs from VMAssignment (expired=%d)", 
                    len(class_vm_cache), expired)
    except Exception as e:
        logger.error("Failed to load VMAssignment cache: %s", e)
    
    # Check VMIPCache for legacy/non-class VMs
    legacy_vm_cache = {}
    try:
        cache_entries = VMIPCache.query.filter(
            VMIPCache.cached_ip.isnot(None),
            VMIPCache.ip_updated_at.isnot(None)
        ).all()
        
        for entry in cache_entries:
            age = now - entry.ip_updated_at
            if age < cache_ttl:
                legacy_vm_cache[entry.vmid] = entry.cached_ip
            else:
                logger.debug("VM %d: legacy cached IP expired (age=%s)", entry.vmid, age)
                expired += 1
        
        logger.debug("Loaded %d cached IPs from VMIPCache (expired=%d)", 
                    len(legacy_vm_cache), expired)
    except Exception as e:
        logger.error("Failed to load VMIPCache: %s", e)
    
    # Apply cached IPs to VMs
    for vm in vms:
        vmid = vm["vmid"]
        current_ip = vm.get("ip")
        
        # Skip if VM already has an IP from guest agent/interface
        if current_ip and current_ip not in ("N/A", "Fetching...", ""):
            continue
        
        # Check class VM cache first, then legacy cache
        cached_ip = class_vm_cache.get(vmid) or legacy_vm_cache.get(vmid)
        
        if cached_ip:
            vm["ip"] = cached_ip
            hits += 1
            logger.debug("VM %d (%s): loaded cached IP=%s from database", 
                        vmid, vm.get("name", "unknown"), cached_ip)
        else:
            misses += 1
    
    logger.info("Database IP cache: %d hits, %d misses, %d expired", hits, misses, expired)


def _enrich_vms_with_arp_ips(vms: List[Dict[str, Any]], force_sync: bool = False) -> None:
    """
    Enrich VM list with IPs discovered via ARP scanning (fast, in-place).
    
    First checks database cache (VMAssignment.cached_ip and VMIPCache).
    Only runs ARP scan for VMs missing IPs or with expired cache (1hr TTL).
    
    Scans ALL running QEMU VMs and LXC containers without IPs.
    This is the PRIMARY method for QEMU VM IP discovery (no guest agent needed).
    Also used for LXC containers with DHCP that don't have IPs in interface API.
    Validates cached IPs and updates if changed.
    
    Offline VMs can't have network presence, so skipped.
    
    Args:
        vms: List of VM dictionaries to enrich
        force_sync: If True, run scan synchronously (blocks until complete)
    """
    from datetime import datetime, timedelta
    from app.models import db, VMAssignment, VMIPCache
    
    logger.info("=== ARP SCAN START === Total VMs=%d, ARP_SCANNER_AVAILABLE=%s", 
                len(vms), ARP_SCANNER_AVAILABLE)
    
    if not ARP_SCANNER_AVAILABLE:
        logger.warning("ARP scanner NOT AVAILABLE - checking database cache only")
        # Still check database cache even if ARP scanner unavailable
        _enrich_from_db_cache(vms)
        return
    
    # First, enrich all VMs from database cache
    _enrich_from_db_cache(vms)
    
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
        if vm.get("status") != "running":
            stopped_count += 1
            logger.debug("VM %d (%s) is %s - SKIPPED", vm["vmid"], vm["name"], vm.get("status"))
            continue

        vmtype = vm.get("type")
        cluster_id = vm.get("cluster_id")
        mac = _get_vm_mac(vm["node"], vm["vmid"], vm["type"], cluster_id=cluster_id)
        has_ip = vm.get("ip") and vm.get("ip") not in ("N/A", "Fetching...", "")

        if vmtype == "qemu" or (vmtype == "lxc" and not has_ip):
            if mac:
                composite_key = f"{cluster_id}:{vm['vmid']}"
                vm_mac_map[composite_key] = mac
                if vmtype == "qemu":
                    qemu_count += 1
                    logger.debug("VM %d (%s) QEMU: MAC=%s, current_ip=%s - WILL SCAN", vm["vmid"], vm["name"], mac, vm.get("ip", "N/A"))
                else:
                    lxc_dhcp_count += 1
                    logger.debug("VM %d (%s) LXC DHCP: MAC=%s - WILL SCAN", vm["vmid"], vm["name"], mac)
            else:
                logger.warning("VM %d (%s, %s) is running but has NO MAC found in config", vm["vmid"], vm["name"], vmtype)
        else:
            lxc_with_ip_count += 1
            logger.debug("VM %d (%s) is LXC with IP=%s - SKIP SCAN", vm["vmid"], vm["name"], vm.get("ip"))
    
    # Detect potential VMID collisions across clusters (same vmid present with different MACs)
    vmid_collision_map = {}
    for vm in vms:
        if vm.get("status") == "running" and vm.get("vmid") in vm_mac_map:
            vmid_collision_map.setdefault(vm["vmid"], set()).add(vm.get("cluster_id"))
    collisions = {vmid: clusters for vmid, clusters in vmid_collision_map.items() if len(clusters) > 1}

    if collisions:
        logger.warning("Detected VMID collisions across clusters for ARP scan: %s. IP assignment may be ambiguous; consider unique VMID ranges per cluster.", collisions)

    logger.info("ARP scan candidates: %d QEMU + %d LXC DHCP = %d total (stopped: %d, LXC with IP: %d, collisions: %d)", 
                qemu_count, lxc_dhcp_count, len(vm_mac_map), stopped_count, lxc_with_ip_count, len(collisions))
    
    if not vm_mac_map:
        logger.info("=== ARP SCAN COMPLETE === No running VMs need ARP discovery")
        return
    
    # Run ARP scan (sync or async based on force_sync parameter)
    # force_sync=True blocks until scan completes (used by background sync for database persistence)
    # force_sync=False runs in background thread (used by web requests for fast response)
    discovered_ips = discover_ips_via_arp(vm_mac_map, subnets=ARP_SUBNETS, background=not force_sync)
    
    # Update VMs with discovered IPs (from cache only, background scan will update later)
    # IMPORTANT: Only update VMs that have IPs in discovered_ips
    # Don't touch VMs that already have IPs from database cache
    updated_count = 0
    
    for vm in vms:
        composite_key = f"{vm.get('cluster_id')}:{vm['vmid']}"
        if composite_key in discovered_ips:
            cached_ip = discovered_ips[composite_key]
            if cached_ip and cached_ip not in ("N/A", "Fetching...", ""):
                vm["ip"] = cached_ip
                updated_count += 1
                logger.debug("VM %d (%s): Updated with ARP cached IP=%s", vm["vmid"], vm["name"], cached_ip)
            else:
                logger.debug("VM %d (%s): ARP cache returned invalid IP: %s", vm["vmid"], vm["name"], cached_ip)
        # else leave existing IP
    
    # Update RDP availability for ALL VMs with IPs (after ARP scan completes)
    # This ensures RDP buttons show for VMs that got IPs from database cache too
    rdp_updated = 0
    for vm in vms:
        ip = vm.get("ip")
        if ip and ip not in ("N/A", "Fetching...", "") and vm.get("status") == "running":
            if vm.get("category") == "windows":
                vm["rdp_available"] = True
                rdp_updated += 1
            else:
                # For non-Windows VMs, check RDP port cache
                has_rdp = has_rdp_port_open(ip)
                vm["rdp_available"] = has_rdp
                if has_rdp:
                    rdp_updated += 1
                logger.debug("VM %d (%s): RDP check for IP %s = %s", vm["vmid"], vm["name"], ip, has_rdp)
    
    if rdp_updated > 0:
        logger.info("Updated RDP availability for %d VMs", rdp_updated)
    
    if updated_count == len(vm_mac_map):
        logger.info("=== ARP CACHE HIT === All %d VMs had cached IPs", updated_count)
    elif updated_count > 0:
        logger.info("=== ARP SCAN STARTED (background) === %d/%d VMs from cache, background scan for remaining",
                   updated_count, len(vm_mac_map))
    else:
        logger.info("=== ARP SCAN STARTED (background) === No cached IPs, scanning for %d VMs", len(vm_mac_map))
    
    # Save discovered IPs to database for persistence across restarts
    _save_ips_to_db(vms, vm_mac_map)


def _save_ips_to_db(vms: List[Dict[str, Any]], vm_mac_map: Dict[int, str]) -> None:
    """
    Save discovered IPs to database for persistence across restarts.
    
    Updates VMAssignment.cached_ip for class VMs and VMIPCache for legacy VMs.
    Only saves IPs that are valid (not N/A, Fetching..., or empty).
    
    Args:
        vms: List of VM dictionaries with discovered IPs
        vm_mac_map: Map of vmid -> MAC address for IP validation
    """
    from datetime import datetime
    from flask import has_app_context
    from app.models import db, VMAssignment, VMIPCache
    
    if not has_app_context():
        logger.warning("_save_ips_to_db: skipping (no app context)")
        return
    
    logger = logging.getLogger(__name__)
    now = datetime.utcnow()
    class_updates = 0
    legacy_updates = 0
    
    try:
        # Get all class VMs in one query (use correct column proxmox_vmid)
        class_vmids = {vm["vmid"] for vm in vms if vm.get("ip") and vm["ip"] not in ("N/A", "Fetching...", "")}
        if class_vmids:
            assignments_query = VMAssignment.query.filter(VMAssignment.proxmox_vmid.in_(class_vmids)).all()
            assignments = {a.proxmox_vmid: a for a in assignments_query}
        else:
            assignments = {}
        
        for vm in vms:
            vmid = vm["vmid"]
            ip = vm.get("ip")
            
            # Skip if no valid IP
            if not ip or ip in ("N/A", "Fetching...", ""):
                continue
            
            # Check if this is a class VM
            if vmid in assignments:
                assignment = assignments[vmid]
                # Only update if IP changed or no cached IP
                if assignment.cached_ip != ip:
                    assignment.cached_ip = ip
                    assignment.ip_updated_at = now
                    class_updates += 1
                    logger.debug("Updated VMAssignment %d: cached_ip=%s", vmid, ip)
            else:
                # Legacy VM - use VMIPCache
                mac = vm_mac_map.get(vmid)
                if not mac:
                    # Try to get MAC from VM config
                    cluster_id = vm.get("cluster_id")
                    mac = _get_vm_mac(vm["node"], vmid, vm["type"], cluster_id=cluster_id)
                
                if mac:
                    # Check if entry exists
                    cache_entry = VMIPCache.query.filter_by(vmid=vmid).first()
                    if cache_entry:
                        # Update existing
                        if cache_entry.cached_ip != ip:
                            cache_entry.cached_ip = ip
                            cache_entry.ip_updated_at = now
                            cache_entry.mac_address = mac  # Update MAC if changed
                            legacy_updates += 1
                            logger.debug("Updated VMIPCache %d: cached_ip=%s", vmid, ip)
                    else:
                        # Create new entry
                        cache_entry = VMIPCache(
                            vmid=vmid,
                            mac_address=mac,
                            cached_ip=ip,
                            ip_updated_at=now,
                            cluster_id=vm.get("cluster_id", "default")
                        )
                        db.session.add(cache_entry)
                        legacy_updates += 1
                        logger.debug("Created VMIPCache %d: cached_ip=%s", vmid, ip)
        
        # Commit all updates in one transaction
        db.session.commit()
        logger.info("Saved IPs to database: %d class VMs, %d legacy VMs", class_updates, legacy_updates)
        
    except Exception as e:
        logger.error("Failed to save IPs to database: %s", e)
        db.session.rollback()


# ---------------------------------------------------------------------------
# Background IP Scanner - Continuous IP discovery and database persistence
# ---------------------------------------------------------------------------

_background_scanner_thread = None
_background_scanner_running = False

def start_background_ip_scanner():
    """Start background thread to continuously scan for IPs and populate database."""
    global _background_scanner_thread, _background_scanner_running
    
    if _background_scanner_thread is not None and _background_scanner_thread.is_alive():
        logger.info("Background IP scanner already running")
        return
    
    _background_scanner_running = True
    
    # Pass Flask app to background thread for context
    from flask import current_app
    try:
        app = current_app._get_current_object()
    except RuntimeError:
        logger.warning("Background IP scanner: No Flask app context, scanner disabled")
        return
    
    _background_scanner_thread = threading.Thread(target=_background_ip_scan_loop, args=(app,), daemon=True)
    _background_scanner_thread.start()
    logger.info("Started background IP scanner thread")

def _background_ip_scan_loop(app):
    """Background loop that scans for IPs only when needed (running VMs without IPs)."""
    import time
    logger.info("Background IP scanner: starting...")
    
    # Wait 2 seconds for app to fully start
    time.sleep(2)
    
    # Do an immediate scan on startup to populate database
    try:
        with app.app_context():
            logger.info("Background IP scanner: performing initial scan on startup...")
            vms = get_all_vms(skip_ips=False, force_refresh=True)
            with_ips = sum(1 for vm in vms if vm.get('status') == 'running' and vm.get('ip') and vm['ip'] not in ('N/A', 'Fetching...', ''))
            running_count = sum(1 for vm in vms if vm.get('status') == 'running')
            logger.info(f"Background IP scanner: initial scan complete - {with_ips}/{running_count} running VMs have IPs")
    except Exception as e:
        logger.exception(f"Background IP scanner: initial scan failed: {e}")
    
    while _background_scanner_running:
        try:
            # Use Flask app context for database access
            with app.app_context():
                # Get all VMs with IP check (don't skip IPs anymore - always get fresh data)
                vms = get_all_vms(skip_ips=False, force_refresh=True)
                
                # Count running VMs and those with IPs
                running_vms = [vm for vm in vms if vm.get('status') == 'running']
                with_ips = sum(1 for vm in running_vms if vm.get('ip') and vm['ip'] not in ('N/A', 'Fetching...', ''))
                
                if len(running_vms) > 0:
                    logger.info(f"Background IP scanner: {with_ips}/{len(running_vms)} running VMs have IPs")
                else:
                    logger.debug("Background IP scanner: no running VMs found")
            
            # Sleep for 30 seconds before next check (faster updates)
            time.sleep(30)
        except Exception as e:
            logger.exception(f"Background IP scanner error: {e}")
            time.sleep(30)  # Sleep 30 seconds on error
    
    logger.info("Background IP scanner: stopped")


def get_all_vms(skip_ips: bool = False, force_refresh: bool = False) -> List[Dict[str, Any]]:
    """
    Cached list of ALL VMs/containers from ALL clusters.

    Each item: { vmid, node, name, status, ip, category, type, cluster_id, cluster_name }
    
    Uses /cluster/resources endpoint for fast loading (single API call per cluster).
    Set skip_ips=True to skip ARP scan for fast initial page load.
    Set force_refresh=True to bypass cache and fetch fresh data from Proxmox.
    """
    global _vm_cache_data, _vm_cache_ts
    cache_key = "all_clusters"  # Single cache for all clusters combined

    # Load VM cache from disk on first call
    _load_vm_cache()

    now = time.time()
    # Check if we have valid cached data
    cached_vms = _vm_cache_data.get(cache_key)
    cached_ts = _vm_cache_ts.get(cache_key, 0)
    cache_valid = cached_vms is not None and (now - cached_ts) < VM_CACHE_TTL
    
    # Force IP enrichment always now (ignore caller skip_ips request)
    skip_ips = False

    # If cache is valid and not forcing refresh, return cached data (still includes IPs)
    if not force_refresh and cache_valid and cached_vms is not None:
        logger.info("get_all_vms: returning cached data for all clusters (age=%.1fs)", 
                   now - cached_ts)
        result = []
        for vm in cached_vms:
            result.append({**vm})
        
        return result

    # Build map of previous cached IPs (to preserve while new scan runs)
    prev_ips: Dict[int, str] = {}
    if cached_vms:
        for prev_vm in cached_vms:
            try:
                vmid_prev = int(prev_vm.get("vmid"))
            except Exception:
                continue
            ip_prev = prev_vm.get("ip")
            if ip_prev and ip_prev not in ("N/A", "Fetching...", ""):
                prev_ips[vmid_prev] = ip_prev
    logger.debug("get_all_vms: loaded %d preserved IPs from prior cache", len(prev_ips))

    # Fetch VMs from all clusters
    out: List[Dict[str, Any]] = []
    
    for cluster in CLUSTERS:
        cluster_id = cluster["id"]
        cluster_name = cluster["name"]
        logger.info(f"get_all_vms: Fetching VMs from cluster {cluster_name} ({cluster_id})")
        
        try:
            # Get Proxmox API client for this cluster
            proxmox = get_proxmox_admin_for_cluster(cluster_id)
            resources = proxmox.cluster.resources.get(type="vm") or []
            logger.info("get_all_vms: fetched %d resources from cluster %s", len(resources), cluster_name)
        
            for vm in resources:
                # Skip templates (template=1)
                if vm.get("template") == 1:
                    continue
                
                # Filter by VALID_NODES if configured
                if VALID_NODES and vm.get("node") not in VALID_NODES:
                    continue
                
                # Add cluster info to raw VM data for IP caching
                vm["cluster_id"] = cluster_id
                vm["cluster_name"] = cluster_name
                vm_dict = _build_vm_dict(vm, skip_ips=skip_ips)
                out.append(vm_dict)
        except Exception as e:
            logger.warning(f"Cluster {cluster_name} resources API failed, falling back to per-node queries: {e}")
            # Fallback: per-node queries (slower)
            try:
                proxmox = get_proxmox_admin_for_cluster(cluster_id)
                nodes = proxmox.nodes.get() or []
            except Exception as e2:
                logger.error(f"Failed to list nodes for cluster {cluster_name}: {e2}")
                nodes = []

            for n in nodes:
                node = n["node"]
                if VALID_NODES and node not in VALID_NODES:
                    continue
                
                try:
                    # Get QEMU VMs
                    qemu_vms = proxmox.nodes(node).qemu.get() or []
                    for vm in qemu_vms:
                        # Skip templates
                        if vm.get('template') == 1:
                            continue
                        vm['node'] = node
                        vm['type'] = 'qemu'
                        vm['cluster_id'] = cluster_id
                        vm['cluster_name'] = cluster_name
                        vm_dict = _build_vm_dict(vm, skip_ips=skip_ips)
                        out.append(vm_dict)
                    
                    # Get LXC containers
                    lxc_vms = proxmox.nodes(node).lxc.get() or []
                    for vm in lxc_vms:
                        # Skip templates
                        if vm.get('template') == 1:
                            continue
                        vm['node'] = node
                        vm['type'] = 'lxc'
                        vm['cluster_id'] = cluster_id
                        vm['cluster_name'] = cluster_name
                        vm_dict = _build_vm_dict(vm, skip_ips=skip_ips)
                        out.append(vm_dict)
                except Exception as e:
                    logger.debug(f"Failed to list VMs on {node} in cluster {cluster_name}: {e}")

    # Preserve previous IPs for running VMs if new build produced no IP yet
    preserved = 0
    placeholders_set = 0
    for vm in out:
        vmid = vm.get("vmid")
        status = vm.get("status")
        ip_current = vm.get("ip")
        if status == "running":
            if (not ip_current or ip_current in ("", None, "N/A")) and vmid in prev_ips:
                vm["ip"] = prev_ips[vmid]
                preserved += 1
            elif not ip_current or ip_current in ("", None):
                # Set fetching placeholder instead of wiping to N/A
                vm["ip"] = "Fetching..."
                placeholders_set += 1
        else:
            # Non-running VMs: if no IP, mark N/A
            if not ip_current:
                vm["ip"] = "N/A"

    # Sort VMs alphabetically by name
    out.sort(key=lambda vm: (vm.get("name") or "").lower())

    logger.info(
        "get_all_vms: returning %d VM(s) (preserved_ips=%d, placeholders=%d)",
        len(out), preserved, placeholders_set
    )

    # Always enrich with user mappings (needed for both admin and user views)
    _enrich_with_user_mappings(out)

    # Run ARP-based IP discovery unless skipping IPs
    if not skip_ips and ARP_SCANNER_AVAILABLE:
        logger.info("get_all_vms: calling ARP scan enrichment (force_refresh=%s)", force_refresh)
        # Background sync should always wait for IPs (synchronous scan)
        # Web requests use async scan for fast response (unless small VM set)
        if force_refresh:
            # Background sync - always synchronous to ensure IPs are persisted
            _enrich_vms_with_arp_ips(out, force_sync=True)
        elif len(out) <= 25:
            # Small VM set - run synchronous scan
            _enrich_vms_with_arp_ips(out, force_sync=True)
        else:
            # Large VM set from web request - async scan
            _enrich_vms_with_arp_ips(out, force_sync=False)
    elif skip_ips:
        logger.info("get_all_vms: SKIPPING ARP scan (skip_ips=True)")
    else:
        logger.warning("get_all_vms: SKIPPING ARP scan (scanner not available)")

    # Cache the VM structure for all clusters using consistent key
    _vm_cache_data[cache_key] = out
    _vm_cache_ts[cache_key] = now
    # _save_vm_cache()  # DISABLED: Now using database persistence via persist_vm_inventory()

    # Persist to database inventory (best-effort; avoid hard failure)
    try:
        from flask import has_app_context
        if has_app_context():
            from app.services.inventory_service import persist_vm_inventory
            persist_vm_inventory(out)
        else:
            logger.debug("get_all_vms: skipping inventory persistence (no app context)")
    except Exception as inv_err:
        # Gracefully handle database schema issues (e.g., missing cluster_id column)
        # Run migrate_add_cluster_id.py to fix the schema
        logger.warning(f"get_all_vms: failed to persist VM inventory (run migrate_add_cluster_id.py if you see IntegrityError): {inv_err}")
    
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


def save_user_vm_map(mapping: Dict[str, List[int]]) -> None:
    """
    Save the entire user-VM mapping dictionary.
    
    Thread-safe write-through to disk.
    """
    _save_mapping(mapping)


def get_all_vm_ids_and_names() -> List[Dict[str, Any]]:
    """
    Get lightweight VM list (ID, name, cluster, node only - no IP lookups).
    
    Much faster than get_all_vms() because it skips expensive IP address lookups.
    Used by the mappings page where we only need to display VM IDs and names.
    
    Returns: List of {vmid, name, cluster_id, node}
    """
    results = []
    
    for cluster in CLUSTERS:
        cluster_id = cluster["id"]
        cluster_name = cluster["name"]
        try:
            proxmox = get_proxmox_admin_for_cluster(cluster_id)
            
            # Try cluster resources API first (fastest)
            try:
                resources = proxmox.cluster.resources.get(type="vm") or []
                for vm in resources:
                    # Skip templates
                    if vm.get("template") == 1:
                        continue
                    
                    node = vm.get("node")
                    if VALID_NODES and node not in VALID_NODES:
                        continue
                    
                    results.append({
                        "vmid": int(vm["vmid"]),
                        "name": vm.get("name", f"vm-{vm['vmid']}"),
                        "cluster_id": cluster_id,
                        "cluster_name": cluster_name,
                        "node": node,
                    })
                continue
            except Exception as e:
                logger.warning("Cluster resources API failed for %s, using node queries: %s", cluster_name, e)
            
            # Fallback: per-node queries
            nodes = proxmox.nodes.get() or []
            for node_data in nodes:
                node_name = node_data["node"]
                
                # Skip nodes not in VALID_NODES filter
                if VALID_NODES and node_name not in VALID_NODES:
                    continue
                
                # Get QEMU VMs
                try:
                    qemu_vms = proxmox.nodes(node_name).qemu.get()
                    for vm in qemu_vms:
                        # Skip templates
                        if vm.get("template") == 1:
                            continue
                        results.append({
                            "vmid": int(vm["vmid"]),
                            "name": vm.get("name", f"vm-{vm['vmid']}"),
                            "cluster_id": cluster_id,
                            "cluster_name": cluster_name,
                            "node": node_name,
                        })
                except Exception as e:
                    logger.warning("Failed to fetch QEMU VMs from %s/%s: %s", cluster_id, node_name, e)
                
                # Get LXC containers
                try:
                    lxc_vms = proxmox.nodes(node_name).lxc.get()
                    for vm in lxc_vms:
                        results.append({
                            "vmid": int(vm["vmid"]),
                            "name": vm.get("name", f"ct-{vm['vmid']}"),
                            "cluster_id": cluster_id,
                            "cluster_name": cluster_name,
                            "node": node_name,
                        })
                except Exception as e:
                    logger.warning("Failed to fetch LXC containers from %s/%s: %s", cluster_id, node_name, e)
        
        except Exception as e:
            logger.error("Failed to fetch VMs from cluster %s: %s", cluster_id, e)
    
    # Sort by vmid
    results.sort(key=lambda x: x["vmid"])
    return results


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
        logger.debug("_user_in_group: groupid is None/empty")
        return False

    # Use cached admin group members for the admin group
    if groupid == ADMIN_GROUP:
        members = _get_admin_group_members_cached()
        logger.info("_user_in_group: checking user=%s against admin group '%s', members=%s", user, groupid, members)
    else:
        # For other groups, fetch directly (no caching)
        try:
            data = get_proxmox_admin().access.groups(groupid).get()
            if not data:
                logger.warning("_user_in_group: group '%s' returned no data", groupid)
                return False
        except Exception as e:
            logger.warning("_user_in_group: failed to fetch group '%s': %s", groupid, e)
            return False

        raw_members = data.get("members", []) or []
        members = []
        for m in raw_members:
            if isinstance(m, str):
                members.append(m)
            elif isinstance(m, dict) and "userid" in m:
                members.append(m["userid"])
        logger.debug("_user_in_group: group '%s' has members: %s", groupid, members)

    # Accept both realm variants for matching: user (full), or username@pam, username@pve
    variants = {user}
    if "@" in user:
        name, realm = user.split("@", 1)
        for r in ("pam", "pve"):
            variants.add(f"{name}@{r}")
    else:
        # User without realm - add both common realms
        variants.add(f"{user}@pve")
        variants.add(f"{user}@pam")
    
    logger.debug("_user_in_group: checking variants %s against members %s", variants, members)
    result = any(u in members for u in variants)
    logger.info("_user_in_group: user=%s group=%s result=%s", user, groupid, result)
    return result


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


def get_vm_ip(cluster_id: str, vmid: int, node: str, vmtype: str) -> Optional[str]:
    """
    Get IP address for a specific VM (for lazy loading).
    
    Returns IP from cache if available, otherwise performs API lookup.
    """
    # Check database cache first
    ip = _get_cached_ip_from_db(cluster_id, vmid)
    if ip:
        return ip
    
    # Lookup from API
    ip = _lookup_vm_ip(node, vmid, vmtype)
    if ip:
        _cache_ip_to_db(cluster_id, vmid, ip)
    
    return ip


def get_vms_for_user(user: str, search: Optional[str] = None, skip_ips: bool = False, force_refresh: bool = False) -> List[Dict[str, Any]]:
    """
    Returns VMs for a user. All VMs are fetched from all clusters at once.
    
    Admin: sees all VMs from all clusters (can filter by cluster in UI).
    Non-admin: filtered to VMs accessible via:
      1. mappings.json (legacy manual mappings)
      2. VMAssignment table (class-based assignments)
    
    Optional search parameter filters by VM name (case-insensitive).
    Set skip_ips=True to skip ARP scan for fast initial page load.
    Set force_refresh=True to bypass cache and fetch fresh data.
    """
    # Get all VMs from all clusters
    vms = get_all_vms(skip_ips=skip_ips, force_refresh=force_refresh)
    admin = is_admin_user(user)
    logger.info("get_vms_for_user: user=%s is_admin=%s all_vms=%d ADMIN_USERS=%s ADMIN_GROUP=%s", 
                user, admin, len(vms), ADMIN_USERS, ADMIN_GROUP)
    
    if not admin:
        # Build set of accessible VMIDs from multiple sources
        accessible_vmids = set()
        
        # Source 1: mappings.json (cached in vm.user_mappings)
        mappings_vms = [vm for vm in vms if user in vm.get("user_mappings", [])]
        accessible_vmids.update(vm["vmid"] for vm in mappings_vms)
        logger.debug("get_vms_for_user: user=%s has %d VMs from mappings.json", user, len(mappings_vms))
        
        # Source 2: VMAssignment table (class-based VMs)
        try:
            from flask import has_app_context
            if has_app_context():
                from app.models import VMAssignment, User
                # Normalize username variants (with/without realm)
                user_variants = {user}
                if '@' in user:
                    base = user.split('@', 1)[0]
                    user_variants.add(base)
                    user_variants.add(f"{base}@pve")
                    user_variants.add(f"{base}@pam")
                else:
                    user_variants.add(f"{user}@pve")
                    user_variants.add(f"{user}@pam")
                
                # Find user record
                local_user = User.query.filter(User.username.in_(user_variants)).first()
                if local_user:
                    assignments = VMAssignment.query.filter_by(assigned_user_id=local_user.id).all()
                    class_vmids = {a.proxmox_vmid for a in assignments}
                    accessible_vmids.update(class_vmids)
                    logger.debug("get_vms_for_user: user=%s has %d VMs from class assignments", user, len(class_vmids))
        except Exception as e:
            logger.warning("get_vms_for_user: failed to check VMAssignment for user %s: %s", user, e)
        
        # Filter VMs to accessible set
        vms = [vm for vm in vms if vm["vmid"] in accessible_vmids]
        logger.info("get_vms_for_user: user=%s filtered to %d VMs (mappings.json + class assignments)", user, len(vms))
        
        if not vms:
            logger.info("User %s has no VM access (not in mappings.json or class assignments)", user)
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


def find_vm_for_user(user: str, vmid: int, skip_ip: bool = False) -> Optional[Dict[str, Any]]:
    """
    Return VM dict if the user is allowed to see/control it.
    
    Args:
        user: Username to check access for
        vmid: VM ID to look up
        skip_ip: If True, skip IP lookup for faster response (useful after power actions)
    
    Returns:
        VM dict if found and accessible, None otherwise
    """
    try:
        vmid = int(vmid)
    except Exception:
        return None

    # When skip_ip is True, we only need to check access and get basic status
    # This is much faster for post-power-action status checks
    for vm in get_vms_for_user(user, skip_ips=skip_ip):
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
    cluster_id = vm.get("cluster_id", "cluster1")

    try:
        if vmtype == "lxc":
            get_proxmox_admin().nodes(node).lxc(vmid).status.start.post()
        else:
            get_proxmox_admin().nodes(node).qemu(vmid).status.start.post()
    except Exception as e:
        logger.exception("failed to start vm %s/%s: %s", node, vmid, e)

    _invalidate_vm_cache()
    _clear_vm_ip_cache(cluster_id, vmid)  # Clear IP cache - VM might get new IP on boot
    invalidate_arp_cache()  # Force fresh network scan to discover new IP


def shutdown_vm(vm: Dict[str, Any]) -> None:
    vmid = int(vm["vmid"])
    node = vm["node"]
    vmtype = vm.get("type", "qemu")
    cluster_id = vm.get("cluster_id", "cluster1")

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
    _clear_vm_ip_cache(cluster_id, vmid)  # Clear IP cache - VM no longer has IP when stopped
    invalidate_arp_cache()  # Force fresh network scan to update ARP table


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
