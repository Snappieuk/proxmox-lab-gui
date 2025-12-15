#!/usr/bin/env python3
"""
Cluster Manager Service - Cluster configuration and cache management.

This module handles cluster configuration persistence and cache invalidation.
"""

import json
import logging
import threading
from typing import Any, Dict, List

from app.config import CLUSTER_CONFIG_FILE, get_clusters_from_db()

logger = logging.getLogger(__name__)

# Cluster cache
_cluster_cache_lock = threading.Lock()
_cluster_cache_data: Dict[str, Any] = {}
_cluster_cache_ts: Dict[str, float] = {}


def get_cluster_config() -> List[Dict[str, Any]]:
    """Get current cluster configuration."""
    return get_clusters_from_db()


def save_cluster_config(clusters: List[Dict[str, Any]]) -> None:
    """Save cluster configuration to JSON file and update runtime config.
    
    Persists changes across restarts by writing to clusters.json.
    """
    import app.config as config

    # Update runtime config
    get_clusters_from_db().clear()
    get_clusters_from_db().extend(clusters)
    config.get_clusters_from_db() = get_clusters_from_db()
    
    # Write to JSON file for persistence
    try:
        with open(CLUSTER_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(clusters, f, indent=2)
        logger.info(f"Saved cluster configuration to {CLUSTER_CONFIG_FILE}")
    except Exception as e:
        logger.error(f"Failed to write cluster config file: {e}")
        raise
    
    # Clear all existing Proxmox connections to force reconnection with new config
    from app.services import proxmox_service
    proxmox_service._proxmox_connections.clear()
    
    # Also invalidate the cluster cache
    invalidate_cluster_cache()
    
    logger.info(f"Updated cluster configuration with {len(clusters)} cluster(s)")


def invalidate_cluster_cache() -> None:
    """Invalidate the short-lived cluster resources cache for current cluster.
    
    Called after VM start/stop operations to ensure fresh data is fetched.
    """
    global _cluster_cache_data, _cluster_cache_ts
    
    from app.services.proxmox_service import get_current_cluster_id
    cluster_id = get_current_cluster_id()
    
    with _cluster_cache_lock:
        _cluster_cache_data[cluster_id] = None
        _cluster_cache_ts[cluster_id] = 0.0
    logger.debug("Invalidated cluster cache for %s", cluster_id)
