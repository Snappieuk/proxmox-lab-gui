#!/usr/bin/env python3
"""
Clusters API routes blueprint.

JSON API for cluster configuration and switching.
"""

import logging

from flask import Blueprint, jsonify, request, session

from app.config import CLUSTERS
from app.services.proxmox_client import (
    _invalidate_vm_cache,
    get_cluster_config,
    invalidate_cluster_cache,
    save_cluster_config,
    switch_cluster,
)
from app.utils.decorators import admin_required, login_required

logger = logging.getLogger(__name__)

api_clusters_bp = Blueprint('api_clusters', __name__, url_prefix='/api')


@api_clusters_bp.route("/clusters", methods=["GET"])
@login_required
def api_list_clusters():
    """List all configured Proxmox clusters."""
    clusters_list = [
        {
            "id": c["id"],
            "name": c["name"],
            "host": c["host"]
        }
        for c in CLUSTERS
    ]
    return jsonify({"ok": True, "clusters": clusters_list})


@api_clusters_bp.route("/clusters/<cluster_id>/nodes", methods=["GET"])
@login_required
def api_get_cluster_nodes(cluster_id: str):
    """Get list of nodes in a specific cluster."""
    try:
        from app.services.proxmox_service import get_proxmox_admin_for_cluster
        
        # Validate cluster ID
        if cluster_id not in [c["id"] for c in CLUSTERS]:
            return jsonify({"ok": False, "error": "Invalid cluster ID"}), 400
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        nodes = proxmox.nodes.get()
        
        nodes_list = [
            {
                "node": n["node"],
                "status": n.get("status", "unknown"),
                "online": n.get("status") == "online"
            }
            for n in nodes
        ]
        
        return jsonify({"ok": True, "nodes": nodes_list})
        
    except Exception as e:
        logger.exception(f"Failed to get nodes for cluster {cluster_id}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


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


# Constants for resource calculations
BYTES_TO_MB = 1024 * 1024
# CPU overallocation factor: Proxmox best practice allows 3x physical cores for vCPUs
# since VMs rarely use 100% CPU continuously
CPU_OVERALLOCATION_FACTOR = 3


@api_clusters_bp.route("/clusters/<cluster_id>/resources", methods=["GET"])
@login_required
def get_cluster_resources(cluster_id: str):
    """Get resource availability for a specific cluster."""
    from app.services.proxmox_service import get_proxmox_admin_for_cluster
    
    try:
        # Validate cluster ID
        if cluster_id not in [c["id"] for c in CLUSTERS]:
            return jsonify({"ok": False, "error": "Cluster not found"}), 404
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        if not proxmox:
            return jsonify({"ok": False, "error": "Cluster not found"}), 404
        
        # Get all nodes
        nodes = proxmox.nodes.get()
        logger.info(f"Found {len(nodes)} nodes in cluster {cluster_id}")
        
        total_cpu_cores = 0
        total_memory_mb = 0
        failed_nodes = []
        successful_nodes = []
        
        for node_data in nodes:
            node_name = node_data['node']
            node_status = node_data.get('status', 'unknown')
            
            logger.info(f"Checking node {node_name}, status: {node_status}")
            
            # Try to get node info even if status shows offline
            # (status field may be unreliable during connection issues)
            try:
                node_info = proxmox.nodes(node_name).status.get()
                
                # CPU: maxcpu is the number of physical cores
                cpu_cores = node_info.get('maxcpu', 0)
                memory_bytes = node_info.get('maxmem', 0)
                memory_mb = memory_bytes // BYTES_TO_MB
                
                logger.info(f"Node {node_name}: {cpu_cores} cores, {memory_mb} MB RAM")
                
                total_cpu_cores += cpu_cores
                total_memory_mb += memory_mb
                successful_nodes.append(node_name)
                
            except Exception as e:
                logger.warning(f"Failed to get status for node {node_name}: {e}")
                failed_nodes.append(node_name)
                continue
        
        logger.info(f"Cluster {cluster_id} totals: {total_cpu_cores} physical cores, {total_memory_mb} MB RAM from {len(successful_nodes)} nodes")
        
        # Apply overallocation factor for virtual CPUs
        vcpu_available = total_cpu_cores * CPU_OVERALLOCATION_FACTOR
        
        response = {
            "ok": True,
            "cluster_id": cluster_id,
            "resources": {
                "cpu": {
                    "physical_cores": total_cpu_cores,
                    "vcpu_available": vcpu_available,
                    "overallocation_factor": CPU_OVERALLOCATION_FACTOR
                },
                "memory": {
                    "total_mb": total_memory_mb,
                    "available_mb": total_memory_mb
                }
            },
            "nodes_queried": len(successful_nodes),
            "nodes_failed": len(failed_nodes)
        }
        
        # Add warning if some nodes failed
        if failed_nodes:
            response["warning"] = f"Could not query {len(failed_nodes)} node(s): {', '.join(failed_nodes)}"
            logger.warning(f"Cluster {cluster_id} resource query incomplete: {failed_nodes}")
        
        # Warn if no nodes were successfully queried
        if total_cpu_cores == 0 and total_memory_mb == 0:
            response["warning"] = "No nodes available - all nodes failed to respond"
            logger.error(f"Cluster {cluster_id}: All {len(nodes)} nodes failed to provide resource info")
        
        return jsonify(response)
    except Exception as e:
        logger.exception("Failed to get cluster resources")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_clusters_bp.route("/clusters/<cluster_id>/nodes/<node_name>/resources", methods=["GET"])
@login_required
def get_node_resources(cluster_id: str, node_name: str):
    """Get resource availability for a specific node."""
    from app.services.proxmox_service import get_proxmox_admin_for_cluster
    
    try:
        # Validate cluster ID
        if cluster_id not in [c["id"] for c in CLUSTERS]:
            return jsonify({"ok": False, "error": "Cluster not found"}), 404
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        if not proxmox:
            return jsonify({"ok": False, "error": "Cluster not found"}), 404
        
        # Get node status
        try:
            node_info = proxmox.nodes(node_name).status.get()
            
            # CPU: maxcpu is the number of physical cores
            cpu_cores = node_info.get('maxcpu', 0)
            memory_bytes = node_info.get('maxmem', 0)
            memory_mb = memory_bytes // BYTES_TO_MB
            
            logger.info(f"Node {node_name} resources: {cpu_cores} cores, {memory_mb} MB RAM")
            
            # Apply overallocation factor for virtual CPUs
            vcpu_available = cpu_cores * CPU_OVERALLOCATION_FACTOR
            
            response = {
                "ok": True,
                "cluster_id": cluster_id,
                "node_name": node_name,
                "resources": {
                    "cpu": {
                        "physical_cores": cpu_cores,
                        "vcpu_available": vcpu_available,
                        "overallocation_factor": CPU_OVERALLOCATION_FACTOR
                    },
                    "memory": {
                        "total_mb": memory_mb,
                        "available_mb": memory_mb
                    }
                }
            }
            
            return jsonify(response)
            
        except Exception as e:
            logger.error(f"Failed to get status for node {node_name}: {e}")
            return jsonify({"ok": False, "error": f"Failed to query node: {str(e)}"}), 500
            
    except Exception as e:
        logger.exception("Failed to get node resources")
        return jsonify({"ok": False, "error": str(e)}), 500
