#!/usr/bin/env python3
"""
Proxmox Service - Core Proxmox API integration.

This module provides thread-safe Proxmox API connection management
with support for multiple clusters.
"""

import logging
import threading
import ssl
from typing import Dict, Any

from proxmoxer import ProxmoxAPI

# Suppress SSL warnings for self-signed certificates
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Create unverified SSL context for self-signed certificates
ssl._create_default_https_context = ssl._create_unverified_context


from app.config import CLUSTERS

logger = logging.getLogger(__name__)

# Thread-safe lock for Proxmox connection
_proxmox_lock = threading.Lock()
_proxmox_connections: Dict[str, Any] = {}


def get_current_cluster_id() -> str:
    """Get current cluster ID from Flask session or default to first cluster."""
    try:
        from flask import session
        return session.get("cluster_id", CLUSTERS[0]["id"])
    except (RuntimeError, ImportError):
        # No Flask context (e.g., background thread or CLI) - use default
        return CLUSTERS[0]["id"]


def get_current_cluster() -> Dict[str, Any]:
    """Get current cluster configuration based on session."""
    cluster_id = get_current_cluster_id()
    
    for cluster in CLUSTERS:
        if cluster["id"] == cluster_id:
            return cluster
    
    # Fallback to first cluster if invalid ID
    return CLUSTERS[0]


def get_proxmox_admin_for_cluster(cluster_id: str) -> ProxmoxAPI:
    """Get or create Proxmox connection for a specific cluster.
    
    Unlike get_proxmox_admin(), this doesn't rely on Flask session context.
    Used when fetching VMs from multiple clusters simultaneously.
    """
    global _proxmox_connections
    
    # Fast path: return existing connection if available
    if cluster_id in _proxmox_connections:
        return _proxmox_connections[cluster_id]

    # Find the cluster config by ID or IP
    cluster = next((c for c in CLUSTERS if c["id"] == cluster_id), None)
    if not cluster:
        # Try matching by IP address
        cluster = next((c for c in CLUSTERS if c["host"] == cluster_id), None)
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
    
    # Validate cluster ID
    valid = any(c["id"] == cluster_id for c in CLUSTERS)
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
    from app.config import VALID_NODES, ADMIN_USERS, ADMIN_GROUP
    
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
