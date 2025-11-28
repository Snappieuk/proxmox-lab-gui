#!/usr/bin/env python3
"""
Clusters API routes blueprint.

JSON API for cluster configuration and switching.
"""

import logging

from flask import Blueprint, jsonify, request, session, current_app

from app.utils.decorators import login_required

# Import path setup is no longer needed
import app.utils.paths

from app.config import CLUSTERS
from app.services.proxmox_client import (
    switch_cluster,
    invalidate_cluster_cache,
    _invalidate_vm_cache,
    get_cluster_config,
    save_cluster_config,
)

logger = logging.getLogger(__name__)

api_clusters_bp = Blueprint('api_clusters', __name__, url_prefix='/api')


@api_clusters_bp.route("/switch-cluster", methods=["POST"])
@login_required
def api_switch_cluster():
    """Switch to a different Proxmox cluster."""
    from app.services.user_manager import require_user
    
    user = require_user()
    data = request.get_json()
    cluster_id = data.get("cluster_id")
    
    # Validate cluster ID
    valid_cluster_ids = [c["id"] for c in CLUSTERS]
    if cluster_id not in valid_cluster_ids:
        return jsonify({"ok": False, "error": "Invalid cluster ID"}), 400
    
    # Store in session and mark as modified
    old_cluster = session.get("cluster_id", CLUSTERS[0]["id"])
    session["cluster_id"] = cluster_id
    session.modified = True
    logger.info("User %s switched from cluster %s to %s", user, old_cluster, cluster_id)
    
    # Invalidate all caches to force refresh from new cluster
    switch_cluster(cluster_id)
    invalidate_cluster_cache()
    _invalidate_vm_cache()
    
    logger.info("Cache invalidated, returning success for cluster %s", cluster_id)
    return jsonify({"ok": True, "cluster_id": cluster_id})


@api_clusters_bp.route("/cluster-config", methods=["GET"])
@admin_required
def api_get_cluster_config():
    """Get current cluster configuration."""
    return jsonify({"ok": True, "clusters": get_cluster_config()})


@api_clusters_bp.route("/cluster-config", methods=["POST"])
@admin_required
def api_save_cluster_config():
    """Save updated cluster configuration."""
    data = request.get_json()
    clusters = data.get("clusters", [])
    
    if not clusters:
        return jsonify({"ok": False, "error": "No clusters provided"}), 400
    
    # Validate cluster structure
    for cluster in clusters:
        required = ["id", "name", "host", "user", "password"]
        if not all(k in cluster for k in required):
            return jsonify({"ok": False, "error": f"Cluster missing required fields: {required}"}), 400
    
    try:
        save_cluster_config(clusters)
        return jsonify({"ok": True, "message": "Cluster configuration saved. Refresh the page to see changes."})
    except Exception as e:
        logger.error(f"Failed to save cluster config: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
