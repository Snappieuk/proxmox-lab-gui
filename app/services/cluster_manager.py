#!/usr/bin/env python3
"""
Cluster Manager Service - Cluster configuration and cache management.

This module handles cluster configuration persistence and cache invalidation.
"""

import json
import logging
import threading
from typing import Any, Dict, List

from app.services.proxmox_service import get_clusters_from_db

logger = logging.getLogger(__name__)

# Cluster cache
_cluster_cache_lock = threading.Lock()
_cluster_cache_data: Dict[str, Any] = {}
_cluster_cache_ts: Dict[str, float] = {}


def get_cluster_config() -> List[Dict[str, Any]]:
    """Get current cluster configuration."""
    return get_clusters_from_db()


def save_cluster_config(clusters: List[Dict[str, Any]]) -> None:
    """Save cluster configuration to database.
    
    DEPRECATED: This function is for backward compatibility only.
    Use Cluster model directly for database-first architecture.
    """
    logger.warning("save_cluster_config() is deprecated - use Cluster model directly")
    
    try:
        from app.models import Cluster, db
        
        # Update existing clusters or create new ones
        for cluster_data in clusters:
            cluster_id = cluster_data.get("id")
            if cluster_id:
                cluster = Cluster.query.get(cluster_id)
                if cluster:
                    # Update existing
                    for key, value in cluster_data.items():
                        if hasattr(cluster, key) and key != 'id':
                            setattr(cluster, key, value)
                else:
                    # Create new with specified ID
                    cluster = Cluster(**cluster_data)
                    db.session.add(cluster)
            else:
                # Create new (auto-generate ID)
                cluster = Cluster(**{k: v for k, v in cluster_data.items() if k != 'id'})
                db.session.add(cluster)
        
        db.session.commit()
        logger.info(f"Saved {len(clusters)} clusters to database")
        
        # Clear all existing Proxmox connections to force reconnection with new config
        from app.services import proxmox_service
        proxmox_service._proxmox_connections.clear()
        
        # Also invalidate the cluster cache
        invalidate_cluster_cache()
        
    except Exception as e:
        logger.error(f"Failed to save cluster config: {e}", exc_info=True)
        raise


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
