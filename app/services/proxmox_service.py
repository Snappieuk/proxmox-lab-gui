#!/usr/bin/env python3
"""
Proxmox Service - Unified Proxmox API integration.

This module provides comprehensive Proxmox API integration including:
- Thread-safe connection management with multi-cluster support
- VM operations (power control, status, IP discovery)
- User-VM mapping and access control
- VNC/console ticket generation
- ARP-based IP discovery and caching
- Background IP scanning daemon
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
import logging
import os
import re
import ssl
import threading
import time

from proxmoxer import ProxmoxAPI
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import urllib3

from app.config import PROXMOX_CACHE_TTL, DB_IP_CACHE_TTL
from app.services.user_manager import is_admin_user
from app.services.arp_scanner import has_rdp_port_open, invalidate_arp_cache
from app.services.health_service import (
    register_daemon_started,
    update_daemon_sync,
)

# Suppress SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Global HTTP adapter with increased pool size
_http_adapter = HTTPAdapter(
    pool_connections=100,
    pool_maxsize=100,
    max_retries=Retry(total=3, backoff_factor=0.3)
)

# Create unverified SSL context for self-signed certificates
ssl._create_default_https_context = ssl._create_unverified_context

logger = logging.getLogger(__name__)

# ARP scanner availability flag
ARP_SCANNER_AVAILABLE = True  # Module exists and can be imported

# =============================================================================
# CLUSTER MANAGEMENT & CONNECTION POOLING
# =============================================================================

# Thread-safe lock for Proxmox connection
_proxmox_lock = threading.Lock()
_proxmox_connections: Dict[str, Any] = {}


def create_proxmox_connection(cluster: Dict[str, Any], timeout: int = 30) -> ProxmoxAPI:
    """
    Create a Proxmox API connection with standardized settings.
    
    This is the SINGLE centralized function for creating ProxmoxAPI connections.
    All other functions should use this instead of calling ProxmoxAPI() directly.
    
    Args:
        cluster: Cluster configuration dict with keys: host, user, password, verify_ssl, port
        timeout: Request timeout in seconds (default 30)
    
    Returns:
        ProxmoxAPI connection instance with connection pooling enabled
    """
    # Create requests session with connection pooling
    import requests
    session = requests.Session()
    session.mount('http://', _http_adapter)
    session.mount('https://', _http_adapter)
    
    try:
        connection = ProxmoxAPI(
            cluster["host"],
            user=cluster["user"],
            password=cluster["password"],
            verify_ssl=cluster.get("verify_ssl", False),
            port=cluster.get("port", 8006),
            timeout=timeout,
        )
        # Monkey-patch session for connection pooling
        if hasattr(connection, '_backend') and hasattr(connection._backend, 'session'):
            connection._backend.session.mount('http://', _http_adapter)
            connection._backend.session.mount('https://', _http_adapter)
        return connection
    except Exception as e:
        logger.error(f"Failed to create Proxmox connection to {cluster['host']}: {e}")
        # Fallback without session monkey-patching
        return ProxmoxAPI(
            cluster["host"],
            user=cluster["user"],
            password=cluster["password"],
            verify_ssl=cluster.get("verify_ssl", False),
            port=cluster.get("port", 8006),
            timeout=timeout,
        )


def get_clusters_from_db():
    """Load active clusters from database (database-first architecture)."""
    try:
        from app.models import Cluster, db
        from flask import has_app_context
        
        if has_app_context():
            # Use no_autoflush to prevent session issues when called from background threads
            with db.session.no_autoflush:
                clusters = Cluster.query.filter_by(is_active=True).order_by(Cluster.priority.desc(), Cluster.name).all()
                if clusters:
                    return [c.to_dict() for c in clusters]
        else:
            logger.warning("get_clusters_from_db called outside app context")
    except Exception as e:
        logger.error(f"Could not load clusters from database: {e}", exc_info=True)
    
    # No fallback - database is required for database-first architecture
    return []


def get_current_cluster_id() -> str:
    """Get current cluster ID from Flask session or default to first cluster."""
    clusters = get_clusters_from_db()
    try:
        from flask import session
        return session.get("cluster_id", clusters[0]["id"])
    except (RuntimeError, ImportError):
        # No Flask context (e.g., background thread or CLI) - use default
        return clusters[0]["id"]


def get_current_cluster() -> Dict[str, Any]:
    """Get current cluster configuration based on session."""
    clusters = get_clusters_from_db()
    cluster_id = get_current_cluster_id()
    
    for cluster in clusters:
        if cluster["id"] == cluster_id:
            return cluster
    
    # Fallback to first cluster if invalid ID
    return clusters[0]


def get_proxmox_admin_for_cluster(cluster_id: str) -> ProxmoxAPI:
    """Get or create Proxmox connection for a specific cluster.
    
    Unlike get_proxmox_admin(), this doesn't rely on Flask session context.
    Used when fetching VMs from multiple clusters simultaneously.
    
    Args:
        cluster_id: Can be cluster_id string, numeric database ID, or IP address
    """
    global _proxmox_connections
    
    clusters = get_clusters_from_db()
    
    # Fast path: return existing connection if available
    if str(cluster_id) in _proxmox_connections:
        return _proxmox_connections[str(cluster_id)]
    
    # Try to convert to int for database ID lookup
    try:
        numeric_id = int(cluster_id)
        # Load from database by numeric ID
        from app.models import Cluster
        from flask import has_app_context
        if has_app_context():
            db_cluster = Cluster.query.get(numeric_id)
            if db_cluster:
                cluster = db_cluster.to_dict()
                cluster_id = cluster["id"]
            else:
                raise ValueError(f"Unknown cluster database ID: {numeric_id}")
        else:
            raise ValueError(f"Cannot lookup numeric cluster ID {numeric_id} without app context")
    except (ValueError, TypeError):
        # Not a number, find the cluster config by ID or IP
        cluster = next((c for c in clusters if c["id"] == cluster_id), None)
        if not cluster:
            # Try matching by IP address
            cluster = next((c for c in clusters if c["host"] == cluster_id), None)
            if not cluster:
                raise ValueError(f"Unknown cluster_id or cluster_ip: {cluster_id}")
            cluster_id = cluster["id"]

    # Check if connection exists (fast path without lock)
    if cluster_id in _proxmox_connections:
        return _proxmox_connections[cluster_id]
    
    # Create connection OUTSIDE lock (slow network operation)
    new_connection = create_proxmox_connection(cluster)
    
    # Now acquire lock ONLY for dictionary write (fast)
    with _proxmox_lock:
        # Double-check - another thread may have created it while we were connecting
        if cluster_id not in _proxmox_connections:
            _proxmox_connections[cluster_id] = new_connection
            logger.info("Connected to Proxmox cluster '%s' at %s (pool_size=50)", cluster["name"], cluster["host"])

    return _proxmox_connections[cluster_id]


def get_proxmox_admin() -> ProxmoxAPI:
    """Get or create Proxmox connection for current cluster (lazy initialization).
    
    Thread-safe singleton pattern per cluster: uses double-checked locking to ensure
    only one Proxmox connection per cluster is created even in multi-threaded scenarios.
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
            # Create requests session with connection pooling for large deployments
            import requests
            session = requests.Session()
            session.mount('http://', _http_adapter)
            session.mount('https://', _http_adapter)
            
            _proxmox_connections[cluster_id] = create_proxmox_connection(cluster)
            
            logger.info("Connected to Proxmox cluster '%s' at %s (pool_size=100)", cluster["name"], cluster["host"])
    
    return _proxmox_connections[cluster_id]


def switch_cluster(cluster_id: str) -> None:
    """Switch to a different Proxmox cluster.
    
    Invalidates all cached connections and forces reconnection on next use.
    """
    global _proxmox_connections
    
    clusters = get_clusters_from_db()
    
    # Validate cluster ID
    valid = any(c["id"] == cluster_id for c in clusters)
    if not valid:
        raise ValueError(f"Invalid cluster ID: {cluster_id}")
    
    with _proxmox_lock:
        # Clear all connections to force fresh connections
        _proxmox_connections = {}
        logger.info("Cleared all cluster connections for switch to: %s", cluster_id)


def create_proxmox_client() -> ProxmoxAPI:
    """Create a new Proxmox client for thread-local use.
    
    Used by ThreadPoolExecutor workers to avoid sharing the main client
    across threads, since ProxmoxAPI may not be fully thread-safe.
    """
    cluster = get_current_cluster()
    return create_proxmox_connection(cluster)


def probe_proxmox() -> Dict[str, Any]:
    """Return diagnostics information helpful for admin troubleshooting."""
    from app.services.settings_service import get_all_admin_users, get_all_admin_groups
    
    # Get settings from database (aggregated from all clusters)
    admin_users = get_all_admin_users()
    admin_groups = get_all_admin_groups()
    
    info: Dict[str, Any] = {
        "ok": True,
        "nodes": [],
        "nodes_count": 0,
        "resources_count": 0,
        "resources_sample": [],
        "valid_nodes": [],  # No longer used - kept for backward compat
        "admin_users": admin_users,
        "admin_group": admin_groups[0] if admin_groups else None,  # Legacy single group
        "admin_groups": admin_groups,  # New multi-group support
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

# =============================================================================
# VM CACHING & RESOURCE QUERIES
# =============================================================================

# Thread-safe locks for caches
_vm_cache_lock = threading.Lock()

# Per-cluster VM cache - dict mapping cluster_id to cache data
_vm_cache_data: Dict[str, Optional[List[Dict[str, Any]]]] = {}
_vm_cache_ts: Dict[str, float] = {}
_vm_cache_loaded: Dict[str, bool] = {}

# Short-lived cluster resources cache (PROXMOX_CACHE_TTL seconds, default 10)
# Per-cluster cache
_cluster_cache_data: Dict[str, Optional[List[Dict[str, Any]]]] = {}
_cluster_cache_ts: Dict[str, float] = {}
_cluster_cache_lock = threading.Lock()

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
    logger.info("VM cache file disabled - using database (VMInventory) instead")
    _vm_cache_loaded[cache_key] = True
    return

def _save_vm_cache() -> None:
    """Save VM cache to JSON file for all clusters.
    
    Skips writing if the on-disk cache timestamp matches the in-memory timestamp,
    indicating no changes since last save.
    """
    cache_key = "all_clusters"
    if _vm_cache_data.get(cache_key) is None:
        return
    
    # VM cache file persistence disabled (database-first architecture)
    # All VM data now stored in VMInventory table via background_sync
    # This function remains as a no-op for backwards compatibility
    logger.debug("VM cache file writes disabled - using database-first architecture")

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


# DELETED: invalidate_cluster_cache() duplicate - use version from proxmox_service.py


def _get_cached_ip_from_db(cluster_id: str, vmid: int) -> Optional[str]:
    """Get IP from database cache if not expired (1 hour TTL)."""
    from datetime import datetime

    from flask import has_app_context

    from app.models import VMAssignment, VMInventory
    
    if not has_app_context():
        logger.debug("_get_cached_ip_from_db: skipping (no app context)")
        return None
    
    try:
        # Check VMAssignment first (class VMs)
        assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
        if assignment and assignment.cached_ip and assignment.ip_updated_at:
            age = datetime.utcnow() - assignment.ip_updated_at
            if age.total_seconds() < DB_IP_CACHE_TTL:
                return assignment.cached_ip
        
        # Check VMInventory for all VMs (primary source of truth)
        inventory = VMInventory.query.filter_by(cluster_id=cluster_id, vmid=vmid).first()
        if inventory and inventory.ip and inventory.ip not in ('N/A', 'Fetching...', ''):
            return inventory.ip
    except Exception as e:
        logger.debug(f"Failed to get cached IP from DB for VM {vmid}: {e}")
    
    return None


def _get_cached_ips_batch(cluster_id: str, vmids: List[int]) -> Dict[int, str]:
    """Get multiple IPs from database cache in batch.
    
    Returns:
        Dict mapping vmid to cached IP (only includes VMs with valid cached IPs)
    """
    from datetime import datetime, timedelta

    from flask import has_app_context

    from app.models import VMAssignment, VMInventory
    
    if not has_app_context():
        logger.debug("_get_cached_ips_batch: skipping (no app context)")
        return {}
    
    results: Dict[int, str] = {}
    cutoff_seconds = DB_IP_CACHE_TTL
    cutoff = datetime.utcnow() - timedelta(seconds=cutoff_seconds)
    
    try:
        # Get all VMAssignments for these VMs
        assignments = VMAssignment.query.filter(
            VMAssignment.proxmox_vmid.in_(vmids),
            VMAssignment.cached_ip.isnot(None),
            VMAssignment.ip_updated_at >= cutoff
        ).all()
        
        for assignment in assignments:
            results[assignment.proxmox_vmid] = assignment.cached_ip
        
        # Get remaining VMs from VMInventory (primary source of truth)
        remaining_vmids = [v for v in vmids if v not in results]
    
    except Exception as e:
        logger.debug(f"Failed to batch fetch cached IPs from DB: {e}")
    
    # Check VMInventory table for remaining VMs
    try:
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

    from app.models import VMAssignment, VMInventory, db
    
    if not has_app_context():
        logger.debug("_cache_ip_to_db: skipping (no app context)")
        return
    
    if not ip:
        return
    
    try:
        now = datetime.utcnow()
        
        # Try to update VMAssignment first
        assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
        if assignment:
            assignment.cached_ip = ip
            assignment.ip_updated_at = now
            if mac:
                assignment.mac_address = mac
            db.session.commit()
            return
        
        # Otherwise update VMInventory (primary source of truth)
        inventory = VMInventory.query.filter_by(cluster_id=cluster_id, vmid=vmid).first()
        if inventory:
            inventory.ip = ip
            inventory.last_updated = now
            if mac:
                inventory.mac_address = mac
        else:
            # Create new VMInventory entry
            inventory = VMInventory(
                vmid=vmid,
                cluster_id=cluster_id,
                ip=ip,
                mac_address=mac,
                last_updated=now
            )
            db.session.add(inventory)
        
        db.session.commit()
    except Exception as e:
        logger.debug(f"Failed to cache IP to DB for VM {vmid}: {e}")
        db.session.rollback()


def _clear_vm_ip_cache(cluster_id: str, vmid: int) -> None:
    """Clear IP cache for specific VM in database."""
    from flask import has_app_context

    from app.models import VMAssignment, VMInventory, db
    
    if not has_app_context():
        logger.debug("_clear_vm_ip_cache: skipping (no app context)")
        return
    
    try:
        # Clear VMAssignment cache
        assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
        if assignment and assignment.cached_ip:
            assignment.cached_ip = None
            assignment.ip_updated_at = None
            logger.debug("Cleared VMAssignment IP cache for VM %d", vmid)
        
        # Clear VMInventory IP
        inventory = VMInventory.query.filter_by(cluster_id=cluster_id, vmid=vmid).first()
        if inventory:
            inventory.ip = None
            logger.debug("Cleared VMInventory IP for VM %d", vmid)
        
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


def fast_verify_vm_ip(cluster_id: str, node: str, vmid: int, vmtype: str, cached_ip: str, subnets: List[str] = None) -> Optional[str]:
    """
    Fast IP verification using ping + ARP check.
    
    Workflow:
    1. Ping the cached IP
    2. Check if ARP table MAC matches VM's MAC
    3. If match: return cached_ip (verified)
    4. If no match: run full ARP scan and return discovered IP
    
    Args:
        cluster_id: Cluster ID
        node: Proxmox node name
        vmid: VM ID
        vmtype: 'qemu' or 'lxc'
        cached_ip: Cached IP to verify
        subnets: Subnets to scan if verification fails (defaults to ARP_SUBNETS from config)
    
    Returns:
        Verified or newly discovered IP, or cached_ip if nothing else works
    """
    if not cached_ip or cached_ip in ("Checking...", "N/A", "Fetching...", ""):
        # No cached IP to verify - skip to full scan
        return _fallback_arp_scan(cluster_id, node, vmid, vmtype, subnets)
    
    # Get VM's MAC address
    vm_mac = _get_vm_mac(node, vmid, vmtype, cluster_id)
    if not vm_mac:
        logger.debug("fast_verify_vm_ip: VM %s:%d has no MAC, cannot verify", cluster_id, vmid)
        return cached_ip
    
    # Quick verification: ping IP and check ARP table
    if ARP_SCANNER_AVAILABLE:
        from app.services.arp_scanner import verify_single_ip
        try:
            if verify_single_ip(cached_ip, vm_mac, timeout=1):
                logger.debug("fast_verify_vm_ip: VM %s:%d IP %s verified via ARP", cluster_id, vmid, cached_ip)
                return cached_ip
            else:
                logger.info("fast_verify_vm_ip: VM %s:%d cached IP %s failed verification, running full scan", 
                           cluster_id, vmid, cached_ip)
        except Exception as e:
            logger.debug("fast_verify_vm_ip: ARP verification failed for VM %s:%d: %s", cluster_id, vmid, e)
    
    # Verification failed - run full ARP scan
    return _fallback_arp_scan(cluster_id, node, vmid, vmtype, subnets)


def _fallback_arp_scan(cluster_id: str, node: str, vmid: int, vmtype: str, subnets: List[str] = None) -> Optional[str]:
    """
    Fallback: run full ARP scan to discover VM IP.
    
    Args:
        cluster_id: Cluster ID
        node: Proxmox node name
        vmid: VM ID
        vmtype: 'qemu' or 'lxc'
        subnets: Subnets to scan (defaults to ARP_SUBNETS from config)
    
    Returns:
        Discovered IP or None
    """
    if not ARP_SCANNER_AVAILABLE:
        return None
    
    from app.services.arp_scanner import discover_ips_via_arp

    # Get VM MAC
    vm_mac = _get_vm_mac(node, vmid, vmtype, cluster_id)
    if not vm_mac:
        return None
    
    # Use configured subnets or get from cluster settings
    if not subnets:
        from app.services.proxmox_service import get_clusters_from_db
        from app.services.settings_service import get_arp_subnets
        
        # Get cluster config to extract ARP subnets
        clusters = get_clusters_from_db()
        cluster_dict = next((c for c in clusters if c.get("id") == cluster_id), {})
        subnets = get_arp_subnets(cluster_dict)
    
    # Run synchronous ARP scan (background=False for immediate results)
    try:
        vm_mac_map = {vmid: vm_mac}
        discovered = discover_ips_via_arp(vm_mac_map, subnets, background=False)
        new_ip = discovered.get(vmid)
        
        if new_ip:
            logger.info("fast_verify_vm_ip: VM %s:%d discovered new IP via ARP scan: %s", cluster_id, vmid, new_ip)
            _cache_ip_to_db(cluster_id, vmid, new_ip)
            return new_ip
    except Exception as e:
        logger.debug("fast_verify_vm_ip: ARP scan failed for VM %s:%d: %s", cluster_id, vmid, e)
    
    return None


def _save_ip_to_proxmox(node: str, vmid: int, vmtype: str, ip: Optional[str]) -> None:
    """
    Save discovered IP to Proxmox VM/LXC notes field for persistence.
    Stores in format: [CACHED_IP:192.168.1.100]
    This survives cache expiration and doesn't require re-scanning.
    
    WARNING: This is SLOW - requires reading AND writing VM config (2 API calls per VM).
    Only enable via enable_ip_persistence in cluster settings if you really need it.
    """
    # Default to disabled for performance (can be enabled per-cluster in database)
    enable_ip_persistence = False  # TODO: Get from cluster settings when cluster_id available
    if not enable_ip_persistence or not ip:
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
    
    WARNING: Only used if enable_ip_persistence=true (disabled by default for performance).
    """
    # Default to disabled for performance (can be enabled per-cluster in database)
    enable_ip_persistence = False  # TODO: Get from cluster settings when cluster_id available
    if not enable_ip_persistence:
        return None
    
    description = raw.get('description', '')
    if not description:
        return None
    
    match = re.search(r'\[CACHED_IP:([^\]]+)\]', description)
    if match:
        ip = match.group(1).strip()
        logger.debug("Found cached IP %s in VM %d notes", ip, raw.get('vmid'))
        return ip
    return None


def _get_vm_mac(node: str, vmid: int, vmtype: str, cluster_id: str = None) -> Optional[str]:
    """
    Get MAC address for a VM.
    
    This delegates to vm_utils.get_vm_mac_address_api() and normalizes the result
    for ARP scanning compatibility.
    
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
        
        # Use canonical implementation from vm_utils
        from app.services.vm_utils import get_vm_mac_address_api, normalize_mac_address
        
        raw_mac = get_vm_mac_address_api(proxmox, node, vmid, vmtype)
        if not raw_mac:
            return None
        
        # Normalize for ARP scanner (lowercase, no separators)
        if ARP_SCANNER_AVAILABLE:
            return normalize_mac_address(raw_mac)
        
        return None
    
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
    
    - QEMU VMs: Try guest agent network-get-interfaces API (requires running VM + guest agent)
    - LXC containers: Use network interfaces API (works even when stopped)
    Returns None if IP not found (will fall back to ARP scan).
    
    Used by ThreadPoolExecutor workers for parallel IP lookups.
    Creates a short-lived client to avoid thread-safety issues with ProxmoxAPI.
    """
    # Default to enabled (can be disabled per-cluster in database)
    enable_ip_lookup = True  # TODO: Get from cluster settings when cluster_id available
    if not enable_ip_lookup:
        return None
    
    try:
        client = create_proxmox_client()
        
        # QEMU VMs - try guest agent
        if vmtype == "qemu":
            try:
                result = client.nodes(node).qemu(vmid).agent('network-get-interfaces').get()
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
                                    logger.debug(f"QEMU {vmid}: Found IP via guest agent: {found_ip}")
                                    return found_ip
            except Exception as e:
                logger.debug(f"QEMU {vmid}: Guest agent network lookup failed: {e}")
                return None
        
        # LXC containers - use network interfaces API
        elif vmtype == "lxc":
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

    # Try to get MAC address from VM config (for network interface)
    mac_address = None
    try:
        proxmox = get_proxmox_admin()
        if vmtype == "qemu":
            config = proxmox.nodes(node).qemu(vmid).config.get()
            net0 = config.get('net0', '')
            # Parse MAC from net0 config (format: "virtio=XX:XX:XX:XX:XX:XX,bridge=vmbr0")
            if '=' in net0:
                parts = net0.split(',')
                for part in parts:
                    if ':' in part and len(part.replace(':', '')) >= 12:
                        # Found MAC address
                        mac_address = part.split('=')[-1] if '=' in part else part
                        break
        elif vmtype == "lxc":
            config = proxmox.nodes(node).lxc(vmid).config.get()
            net0 = config.get('net0', '')
            # LXC format: "name=eth0,bridge=vmbr0,hwaddr=XX:XX:XX:XX:XX:XX,ip=dhcp,type=veth"
            for part in net0.split(','):
                if part.startswith('hwaddr='):
                    mac_address = part.split('=')[1]
                    break
    except Exception as e:
        logger.debug(f"Could not fetch MAC address for {vmtype} {vmid}: {e}")
    
    return {
        "vmid": vmid,
        "node": node,
        "name": name,
        "status": status,
        "type": vmtype,      # 'qemu' or 'lxc'
        "category": category,
        "ip": ip,
        "mac_address": mac_address,  # Add MAC address
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
    
    Checks VMAssignment.cached_ip for class VMs and VMInventory for all VMs.
    Only uses cached IPs if they're less than 1 hour old.
    
    Args:
        vms: List of VM dictionaries to enrich with cached IPs
    """
    from datetime import datetime, timedelta

    from flask import has_app_context

    from app.models import VMAssignment, VMInventory
    
    if not has_app_context():
        logger.debug("_enrich_from_db_cache: skipping (no app context)")
        return
    
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
                class_vm_cache[assignment.proxmox_vmid] = assignment.cached_ip
            else:
                logger.debug("VM %d: cached IP expired (age=%s)", assignment.proxmox_vmid, age)
                expired += 1
        
        logger.debug("Loaded %d cached IPs from VMAssignment (expired=%d)", 
                    len(class_vm_cache), expired)
    except Exception as e:
        logger.error("Failed to load VMAssignment cache: %s", e)
    
    # Check VMInventory for all VMs (primary source of truth)
    vm_cache = {}
    try:
        inventory_entries = VMInventory.query.filter(
            VMInventory.ip.isnot(None)
        ).all()
        
        for entry in inventory_entries:
            # VMInventory doesn't have separate IP update timestamp, so we use last_updated
            # Note: This is updated on any VM sync, not just IP changes
            if entry.ip and entry.ip not in ('N/A', 'Fetching...', ''):
                vm_cache[entry.vmid] = entry.ip
        
        logger.debug("Loaded %d IPs from VMInventory", len(vm_cache))
    except Exception as e:
        logger.error("Failed to load VMInventory: %s", e)
    
    # Apply cached IPs to VMs
    for vm in vms:
        vmid = vm["vmid"]
        current_ip = vm.get("ip")
        
        # Skip if VM already has an IP from guest agent/interface
        if current_ip and current_ip not in ("N/A", "Fetching...", ""):
            continue
        
        # Check class VM cache first, then VMInventory
        cached_ip = class_vm_cache.get(vmid) or vm_cache.get(vmid)
        
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
    
    First checks database cache (VMAssignment.cached_ip and VMInventory).
    Only runs ARP scan for VMs missing IPs or with expired cache (1hr TTL).
    
    Scans ALL running QEMU VMs and LXC containers without IPs.
    This is the PRIMARY method for QEMU VM IP discovery (no guest agent needed).
    Also used for LXC containers with DHCP that don't have IPs in interface API.
    Validates cached IPs and updates if changed.
    
    Offline VMs can't have network presence, so skipped.
    
    Args:
        vms: List of VM dictionaries to enrich
        force_sync: If True, run scan synchronously (blocks until complete)
                   If False, run scan in background (returns immediately)
    """
    
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
    # Get ARP subnets from first available cluster (background sync uses all clusters)
    from app.services.settings_service import get_arp_subnets
    from app.services.proxmox_service import get_clusters_from_db
    from app.services.arp_scanner import discover_ips_via_arp, has_rdp_port_open
    clusters = get_clusters_from_db()
    subnets = get_arp_subnets(clusters[0]) if clusters else []
    discovered_ips = discover_ips_via_arp(vm_mac_map, subnets=subnets, background=not force_sync)
    
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
    
    # Update RDP availability for ALL VMs with IPs
    # ONLY do this if force_sync=True (synchronous scan that just completed)
    # For background scans, RDP availability will be updated when persisted to database
    if force_sync:
        rdp_updated = 0
        for vm in vms:
            ip = vm.get("ip")
            if ip and ip not in ("N/A", "Fetching...", "") and vm.get("status") == "running":
                if vm.get("category") == "windows":
                    vm["rdp_available"] = True
                    rdp_updated += 1
                else:
                    # For non-Windows VMs, check RDP port cache (now populated by sync scan)
                    has_rdp = has_rdp_port_open(ip)
                    vm["rdp_available"] = has_rdp
                    if has_rdp:
                        rdp_updated += 1
                    logger.debug("VM %d (%s): RDP check for IP %s = %s", vm["vmid"], vm["name"], ip, has_rdp)
        
        if rdp_updated > 0:
            logger.info("Updated RDP availability for %d VMs (synchronous scan)", rdp_updated)
    else:
        logger.info("Skipping immediate RDP check (background scan - will update when persisted)")
    
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
    
    Updates VMAssignment.cached_ip for class VMs and VMInventory for all VMs.
    Only saves IPs that are valid (not N/A, Fetching..., or empty).
    
    Args:
        vms: List of VM dictionaries with discovered IPs
        vm_mac_map: Map of vmid -> MAC address for IP validation
    """
    from datetime import datetime

    from flask import has_app_context

    from app.models import VMAssignment, VMInventory, db
    
    if not has_app_context():
        logger.warning("_save_ips_to_db: skipping (no app context)")
        return
    
    now = datetime.utcnow()
    class_updates = 0
    inventory_updates = 0
    
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
            
            # Update VMInventory for all VMs (primary source of truth)
            cluster_id = vm.get("cluster_id", "default")
            mac = vm_mac_map.get(vmid)
            if not mac:
                # Try to get MAC from VM config
                mac = _get_vm_mac(vm["node"], vmid, vm["type"], cluster_id=cluster_id)
            
            # Find or create VMInventory entry
            inventory = VMInventory.query.filter_by(cluster_id=cluster_id, vmid=vmid).first()
            if inventory:
                # Update existing
                if inventory.ip != ip:
                    inventory.ip = ip
                    inventory.last_updated = now
                    if mac:
                        inventory.mac_address = mac
                    inventory_updates += 1
                    logger.debug("Updated VMInventory %d: ip=%s", vmid, ip)
            else:
                # Create new entry
                inventory = VMInventory(
                    vmid=vmid,
                    cluster_id=cluster_id,
                    ip=ip,
                    mac_address=mac,
                    last_updated=now
                )
                db.session.add(inventory)
                inventory_updates += 1
                logger.debug("Created VMInventory %d: ip=%s", vmid, ip)
        
        # Commit all updates in one transaction
        db.session.commit()
        logger.info("Saved IPs to database: %d class VMs, %d VMInventory updates", class_updates, inventory_updates)
        
    except Exception as e:
        logger.error("Failed to save IPs to database: %s", e)
        db.session.rollback()


# ---------------------------------------------------------------------------
# Background IP Scanner - Continuous IP discovery and database persistence
# ---------------------------------------------------------------------------

_background_scanner_thread = None
_background_scanner_running = False

def start_background_ip_scanner(app=None):
    """Start background thread to continuously scan for IPs and populate database."""
    global _background_scanner_thread, _background_scanner_running
    
    if _background_scanner_thread is not None and _background_scanner_thread.is_alive():
        logger.info("Background IP scanner already running")
        return
    
    _background_scanner_running = True
    
    # Register daemon as started
    register_daemon_started('ip_scanner')
    
    # Pass Flask app to background thread for context
    if app is None:
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
    finally:
        # CRITICAL: Clean up DB session after startup scan
        try:
            from app.models import db
            db.session.remove()
        except Exception as cleanup_err:
            logger.warning(f"Failed to cleanup DB session on startup: {cleanup_err}")
    
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
                    # Report successful scan to health service
                    update_daemon_sync('ip_scanner', full_sync=False, items_processed=len(running_vms))
                else:
                    logger.debug("Background IP scanner: no running VMs found")
                    update_daemon_sync('ip_scanner', full_sync=False, items_processed=0)
            
            # Sleep for 30 seconds before next check (faster updates)
            time.sleep(30)
        except Exception as e:
            logger.exception(f"Background IP scanner error: {e}")
            # Report scan error to health service
            update_daemon_sync('ip_scanner', error=str(e))
            time.sleep(30)  # Sleep 30 seconds on error
        finally:
            # CRITICAL: Clean up DB session after each iteration
            try:
                from app.models import db
                db.session.remove()
            except Exception as cleanup_err:
                logger.warning(f"Failed to cleanup DB session in IP scanner: {cleanup_err}")
    
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
    
    # Get VM cache TTL from settings (use default 300 for multi-cluster queries)
    # Note: For per-cluster queries, use cluster-specific vm_cache_ttl from database
    vm_cache_ttl = 300  # Default 5 minutes for backward compatibility
    cache_valid = cached_vms is not None and (now - cached_ts) < vm_cache_ttl
    
    # Force IP enrichment always now (ignore caller skip_ips request)
    skip_ips = False

    # If cache is valid and not forcing refresh, return cached data (still includes IPs)
    if not force_refresh and cache_valid and cached_vms is not None:
        logger.info("get_all_vms: returning cached data for all clusters (age=%.1fs, ttl=%ds)", 
                   now - cached_ts, vm_cache_ttl)
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
    
    # Get clusters from database
    from app.services.proxmox_service import get_clusters_from_db
    clusters = get_clusters_from_db()
    
    for cluster in clusters:
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
# User ↔ VM mappings - MIGRATED TO DATABASE (VMAssignment table)
# ---------------------------------------------------------------------------
# All VM assignment functions use database-backed system:
# - VMAssignment table in app/models.py
# - API endpoints in app/routes/api/user_vm_assignments.py
#
# Legacy JSON-based _load_mapping() and _save_mapping() functions REMOVED.
# All mappings now use VMAssignment database table.
# ---------------------------------------------------------------------------


def get_user_vm_map() -> Dict[str, List[int]]:
    """
    Get complete user-to-VMID mapping from database.
    
    Returns:
        dict: {username: [vmid1, vmid2, ...]}
    """
    from app.models import User
    
    mapping = {}
    try:
        users = User.query.all()
        for user in users:
            vmids = [a.proxmox_vmid for a in user.vm_assignments if a.proxmox_vmid]
            if vmids:
                mapping[user.username] = sorted(vmids)
    except Exception as e:
        logger.error("Error loading VM mappings from database: %s", e, exc_info=True)
    
    return mapping


def save_user_vm_map(mapping: Dict[str, List[int]]) -> None:
    """
    Save complete user-to-VMID mapping to database.
    
    Args:
        mapping: {username: [vmid1, vmid2, ...]}
        
    Creates/updates VMAssignment records in database.
    """
    from app.models import User, VMAssignment, db
    
    try:
        for username, vmids in mapping.items():
            user = User.query.filter_by(username=username).first()
            if not user:
                logger.warning("User %s not found, skipping VM assignments", username)
                continue
            
            # Get existing assignments for this user
            existing = {a.proxmox_vmid: a for a in user.vm_assignments}
            
            # Add new assignments
            for vmid in vmids:
                if vmid not in existing:
                    assignment = VMAssignment(
                        proxmox_vmid=vmid,
                        assigned_user_id=user.id,
                        status='assigned'
                    )
                    db.session.add(assignment)
            
            # Remove assignments not in new list
            for vmid, assignment in existing.items():
                if vmid not in vmids:
                    db.session.delete(assignment)
        
        db.session.commit()
        logger.info("Updated VM mappings in database")
    except Exception as e:
        logger.error("Error saving VM mappings to database: %s", e, exc_info=True)
        db.session.rollback()


def get_all_vm_ids_and_names() -> List[Dict[str, Any]]:
    """
    Get lightweight VM list (ID, name, cluster, node only - no IP lookups).
    
    Much faster than get_all_vms() because it skips expensive IP address lookups.
    Used by the mappings page where we only need to display VM IDs and names.
    
    Returns: List of {vmid, name, cluster_id, node}
    """
    from app.services.proxmox_service import get_clusters_from_db
    
    results = []
    
    for cluster in get_clusters_from_db():
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
    Set VM mappings for a specific user in database.
    
    Args:
        user: Username (e.g., "student1")
        vmids: List of VM IDs to assign to user
        
    Replaces existing mappings for this user.
    Creates VMAssignment records in database.
    """
    from app.models import User, VMAssignment, db
    
    try:
        user_row = User.query.filter_by(username=user).first()
        if not user_row:
            logger.warning("User %s not found, cannot set VM mappings", user)
            return
        
        # Get existing assignments
        existing = {a.proxmox_vmid: a for a in user_row.vm_assignments}
        
        # Add new assignments
        for vmid in vmids:
            if vmid not in existing:
                assignment = VMAssignment(
                    proxmox_vmid=vmid,
                    assigned_user_id=user_row.id,
                    status='assigned'
                )
                db.session.add(assignment)
        
        # Remove assignments not in new list
        for vmid, assignment in existing.items():
            if vmid not in vmids:
                db.session.delete(assignment)
        
        db.session.commit()
        logger.info("Updated mappings for user %s: %s VMs", user, len(vmids))
    except Exception as e:
        logger.error("Error setting VM mappings for user %s: %s", user, e, exc_info=True)
        db.session.rollback()


def invalidate_mappings_cache() -> None:
    """
    Invalidate the in-memory mappings cache (no-op for database-backed system).
    
    Kept for backward compatibility with code that calls this function.
    Database queries are always fresh, so no cache to invalidate.
    """
    logger.debug("invalidate_mappings_cache called (no-op for database-backed system)")


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Admin group management (DEPRECATED - use user_manager.py)
# ---------------------------------------------------------------------------
# Public API functions moved to app/services/user_manager.py:
# - is_admin_user()
# - get_admin_group_members()
# - add_user_to_admin_group()
# - remove_user_from_admin_group()
# - get_pve_users()
# - create_pve_user()
# - probe_proxmox()
#
# These functions are imported at the top of this file and re-exported
# for backward compatibility with existing code.
#
# ---------------------------------------------------------------------------
# DEPRECATED: Legacy admin group functions removed (moved to user_manager.py)
# All admin group checking now uses settings_service.get_all_admin_groups()
# ---------------------------------------------------------------------------


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
    logger.info("get_vms_for_user: user=%s is_admin=%s all_vms=%d", 
                user, admin, len(vms))
    
    if not admin:
        # Build set of accessible VMIDs from multiple sources
        accessible_vmids = set()
        
        # Source 1: mappings.json (cached in vm.user_mappings)
        mappings_vms = [vm for vm in vms if user in vm.get("user_mappings", [])]
        accessible_vmids.update(vm["vmid"] for vm in mappings_vms)
        logger.debug("get_vms_for_user: user=%s has %d VMs from mappings.json", user, len(mappings_vms))
        
        # Source 2: VMAssignment table (class-based VMs + co-owned class VMs)
        try:
            from flask import has_app_context
            if has_app_context():
                from app.models import User, VMAssignment, Class

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
                    # VMs assigned directly to user
                    assignments = VMAssignment.query.filter_by(assigned_user_id=local_user.id).all()
                    class_vmids = {a.proxmox_vmid for a in assignments}
                    accessible_vmids.update(class_vmids)
                    logger.debug("get_vms_for_user: user=%s has %d VMs from direct assignments", user, len(class_vmids))
                    
                    # VMs from classes they own (where they are the main teacher)
                    owned_classes = Class.query.filter_by(teacher_id=local_user.id).all()
                    for cls in owned_classes:
                        class_vm_assignments = VMAssignment.query.filter_by(class_id=cls.id).all()
                        owned_vmids = {a.proxmox_vmid for a in class_vm_assignments}
                        accessible_vmids.update(owned_vmids)
                        logger.debug("get_vms_for_user: user=%s has %d VMs from owned class '%s'", 
                                   user, len(owned_vmids), cls.name)
                    
                    # VMs from classes they co-own (teacher VMs + all student VMs in those classes)
                    co_owned_classes = local_user.co_owned_classes
                    for cls in co_owned_classes:
                        class_vm_assignments = VMAssignment.query.filter_by(class_id=cls.id).all()
                        co_owned_vmids = {a.proxmox_vmid for a in class_vm_assignments}
                        accessible_vmids.update(co_owned_vmids)
                        logger.debug("get_vms_for_user: user=%s has %d VMs from co-owned class '%s'", 
                                   user, len(co_owned_vmids), cls.name)
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
# Actions: Power control
# ---------------------------------------------------------------------------
# Note: build_rdp() removed - use app.services.rdp_service.build_rdp() instead


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
    
    # Immediately update database status for instant UI response
    try:
        from app.services.inventory_service import update_vm_status_immediate
        update_vm_status_immediate(cluster_id, vmid, 'running')
    except Exception as e:
        logger.debug(f"Failed to update inventory status: {e}")


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
    
    # Immediately update database status for instant UI response
    try:
        from app.services.inventory_service import update_vm_status_immediate
        update_vm_status_immediate(cluster_id, vmid, 'stopped')
    except Exception as e:
        logger.debug(f"Failed to update inventory status: {e}")

# =============================================================================
# VNC / CONSOLE TICKET GENERATION
# =============================================================================

def get_vnc_ticket(node: str, vmid: int, vm_type: str = 'kvm', cluster_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Generate a VNC ticket and port for accessing a VM console via noVNC.
    
    This ticket is required for authenticating WebSocket connections to Proxmox's
    VNC proxy. With generate-password=1, the ticket is valid for 2 hours.
    
    Args:
        node: Proxmox node name where the VM is located (e.g., 'pve1', 'pve2')
        vmid: Virtual machine ID
        vm_type: Type of VM - 'kvm' for QEMU VMs, 'lxc' for LXC containers
        cluster_id: Optional cluster ID. If None, uses current session cluster.
        
    Returns:
        Dictionary containing:
            - ticket: VNC authentication ticket (URL-encoded string)
            - port: VNC WebSocket port number
            - cert: SSL certificate fingerprint (for verification)
            - upid: Unique process ID for this console session
            
    Raises:
        ValueError: If vm_type is invalid
        Exception: If Proxmox API call fails
        
    Example:
        >>> vnc_info = get_vnc_ticket('pve1', 112, 'kvm')
        >>> print(f"Connect to VNC on port {vnc_info['port']}")
        >>> print(f"Use ticket: {vnc_info['ticket'][:20]}...")
        
    Notes:
        - Ticket expires in 2 hours (7200s) with generate-password=1
        - For WebSocket URL format: wss://<host>:8006/api2/json/nodes/<node>/<vm_type>/<vmid>/vncwebsocket?port=<port>&vncticket=<ticket>
        - Must also send PVEAuthCookie in WebSocket request headers
    """
    # Validate VM type
    valid_types = ['kvm', 'qemu', 'lxc']
    if vm_type not in valid_types:
        raise ValueError(f"Invalid vm_type '{vm_type}'. Must be one of: {', '.join(valid_types)}")
    
    # Normalize QEMU type
    if vm_type == 'qemu':
        vm_type = 'kvm'
    
    logger.info(f"Generating VNC ticket for {vm_type.upper()} VM {vmid} on node {node}")
    
    try:
        # Get Proxmox client for specified cluster
        proxmox = get_proxmox_admin_for_cluster(cluster_id) if cluster_id else get_proxmox_admin_for_cluster(None)
        
        # Call Proxmox API to generate VNC ticket
        # Parameters:
        #   - websocket=1: Enable WebSocket mode (required for noVNC)
        #   - generate-password=1: Extend ticket lifetime from 60s to 7200s (2 hours)
        if vm_type == 'kvm':
            ticket_data = proxmox.nodes(node).qemu(vmid).vncproxy.post(websocket=1, **{'generate-password': 1})
        else:  # lxc
            ticket_data = proxmox.nodes(node).lxc(vmid).vncproxy.post(websocket=1, **{'generate-password': 1})
        
        # Extract ticket information
        ticket = ticket_data.get('ticket')
        vnc_port = ticket_data.get('port')
        cert = ticket_data.get('cert', '')
        upid = ticket_data.get('upid', '')
        
        if not ticket or not vnc_port:
            raise Exception("Incomplete VNC ticket data received from Proxmox")
        
        logger.info(f"VNC ticket generated successfully: port={vnc_port}, ticket_length={len(ticket)}")
        
        return {
            'ticket': ticket,
            'port': vnc_port,
            'cert': cert,
            'upid': upid
        }
        
    except Exception as e:
        logger.error(f"Failed to generate VNC ticket for VM {vmid}: {e}", exc_info=True)
        raise Exception(f"VNC ticket generation failed: {str(e)}")


def get_auth_ticket(username: str, password: str, cluster_id: Optional[str] = None) -> Tuple[str, str]:
    """
    Generate Proxmox authentication ticket (PVEAuthCookie) and CSRF token.
    
    This is required in addition to the VNC ticket for WebSocket authentication.
    
    Args:
        username: Proxmox username (e.g., 'root@pam')
        password: Proxmox password
        cluster_id: Optional cluster ID. If None, uses current session cluster.
        
    Returns:
        Tuple of (auth_cookie, csrf_token)
        
    Raises:
        Exception: If authentication fails
        
    Example:
        >>> cookie, csrf = get_auth_ticket('root@pam', 'password')
        >>> # Use cookie in WebSocket connection headers
    """
    try:
        proxmox = get_proxmox_admin_for_cluster(cluster_id) if cluster_id else get_proxmox_admin_for_cluster(None)
        
        auth_result = proxmox.access.ticket.post(username=username, password=password)
        
        pve_auth_cookie = auth_result.get('ticket')
        csrf_token = auth_result.get('CSRFPreventionToken')
        
        if not pve_auth_cookie or not csrf_token:
            raise Exception("Incomplete auth data received from Proxmox")
        
        logger.info(f"Authentication ticket generated for user {username}")
        
        return pve_auth_cookie, csrf_token
        
    except Exception as e:
        logger.error(f"Failed to generate auth ticket: {e}", exc_info=True)
        raise Exception(f"Authentication failed: {str(e)}")


def find_vm_location(vmid: int, cluster_id: Optional[str] = None) -> Optional[Dict[str, str]]:
    """
    Find which node and type (kvm/lxc) a VM is located on.
    
    Args:
        vmid: Virtual machine ID to search for
        cluster_id: Optional cluster ID. If None, searches current session cluster.
        
    Returns:
        Dictionary with 'node', 'type', and 'name' if found, None if not found
        
    Example:
        >>> vm_info = find_vm_location(112)
        >>> if vm_info:
        >>>     print(f"VM 112 is on {vm_info['node']} as {vm_info['type']}")
    """
    try:
        proxmox = get_proxmox_admin_for_cluster(cluster_id) if cluster_id else get_proxmox_admin_for_cluster(None)
        
        # Get list of nodes
        nodes = proxmox.nodes.get()
        
        for node_info in nodes:
            node = node_info['node']
            
            # Check QEMU VMs
            try:
                vms = proxmox.nodes(node).qemu.get()
                for vm in vms:
                    if vm['vmid'] == vmid:
                        return {
                            'node': node,
                            'type': 'kvm',
                            'name': vm.get('name', f'VM-{vmid}')
                        }
            except Exception:
                pass
            
            # Check LXC containers
            try:
                containers = proxmox.nodes(node).lxc.get()
                for vm in containers:
                    if vm['vmid'] == vmid:
                        return {
                            'node': node,
                            'type': 'lxc',
                            'name': vm.get('name', f'CT-{vmid}')
                        }
            except Exception:
                pass
        
        logger.warning(f"VM {vmid} not found on any node")
        return None
        
    except Exception as e:
        logger.error(f"Error finding VM {vmid}: {e}", exc_info=True)
        return None
