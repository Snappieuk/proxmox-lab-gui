#!/usr/bin/env python3
"""
Proxmox Service - Core Proxmox API integration.

This module provides thread-safe Proxmox API connection management
with support for multiple clusters.
"""

import logging
import ssl
import threading
from typing import Any, Dict

# Suppress SSL warnings for self-signed certificates
import urllib3
from proxmoxer import ProxmoxAPI

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Create unverified SSL context for self-signed certificates
ssl._create_default_https_context = ssl._create_unverified_context


logger = logging.getLogger(__name__)

# Thread-safe lock for Proxmox connection
_proxmox_lock = threading.Lock()
_proxmox_connections: Dict[str, Any] = {}


def get_clusters_from_db():
    """Load active clusters from database (database-first architecture)."""
    try:
        from app.models import Cluster
        from flask import has_app_context
        
        if has_app_context():
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
            _proxmox_connections[cluster_id] = ProxmoxAPI(
                cluster["host"],
                user=cluster["user"],
                password=cluster["password"],
                verify_ssl=cluster.get("verify_ssl", False),
            )
            logger.info("Connected to Proxmox cluster '%s' at %s", cluster["name"], cluster["host"])
    
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
    return ProxmoxAPI(
        cluster["host"],
        user=cluster["user"],
        password=cluster["password"],
        verify_ssl=cluster.get("verify_ssl", False),
    )


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
