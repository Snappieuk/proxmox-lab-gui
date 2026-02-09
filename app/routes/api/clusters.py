#!/usr/bin/env python3
"""
Clusters API routes blueprint.

JSON API for cluster configuration and switching.
"""

import logging
from datetime import datetime

from flask import Blueprint, jsonify, request, session

from app.services.proxmox_service import get_clusters_from_db, switch_cluster
from app.services.proxmox_service import _invalidate_vm_cache
from app.utils.decorators import admin_required, login_required

logger = logging.getLogger(__name__)

api_clusters_bp = Blueprint('api_clusters', __name__, url_prefix='/api')


@api_clusters_bp.route("/clusters", methods=["GET"])
@login_required
def api_list_clusters():
    """List all configured Proxmox clusters."""
    logger.info(f"API /clusters called - get_clusters_from_db() has {len(get_clusters_from_db())} entries")
    clusters_list = [
        {
            "id": c["id"],
            "name": c["name"],
            "host": c["host"]
        }
        for c in get_clusters_from_db()
    ]
    logger.info(f"Returning {len(clusters_list)} clusters: {[c['id'] for c in clusters_list]}")
    return jsonify({"ok": True, "clusters": clusters_list})


@api_clusters_bp.route("/clusters/<cluster_id>/nodes", methods=["GET"])
@login_required
def api_get_cluster_nodes(cluster_id: str):
    """Get list of nodes in a specific cluster."""
    try:
        from app.services.proxmox_service import get_proxmox_admin_for_cluster
        
        # Validate cluster ID
        if cluster_id not in [c["id"] for c in get_clusters_from_db()]:
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
    valid_cluster_ids = [c["id"] for c in get_clusters_from_db()]
    if cluster_id not in valid_cluster_ids:
        return jsonify({"ok": False, "error": "Invalid cluster ID"}), 400
    
    # Store in session and mark as modified
    old_cluster = session.get("cluster_id", get_clusters_from_db()[0]["id"])
    session["cluster_id"] = cluster_id
    session.modified = True
    logger.info("User %s switched from cluster %s to %s", user, old_cluster, cluster_id)
    
    # Invalidate all caches to force refresh from new cluster
    switch_cluster(cluster_id)
    _invalidate_vm_cache()
    
    logger.info("Cache invalidated, returning success for cluster %s", cluster_id)
    return jsonify({"ok": True, "cluster_id": cluster_id})


@api_clusters_bp.route("/cluster-config", methods=["GET"])
@admin_required
def api_get_cluster_config():
    """Get all cluster configurations from database (without passwords)."""
    from app.models import Cluster, db
    
    try:
        clusters = Cluster.query.order_by(Cluster.priority.desc(), Cluster.name).all()
        return jsonify({
            "ok": True, 
            "clusters": [c.to_safe_dict() for c in clusters]
        })
    except Exception as e:
        logger.error(f"Failed to load clusters: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_clusters_bp.route("/cluster-config", methods=["POST"])
@admin_required
def api_create_cluster():
    """Create a new cluster configuration."""
    from app.models import Cluster, db
    
    data = request.get_json()
    
    # Validate required fields
    required = ["cluster_id", "name", "host", "user", "password"]
    if not all(k in data for k in required):
        return jsonify({"ok": False, "error": f"Missing required fields: {required}"}), 400
    
    # Check for duplicate cluster_id
    existing = Cluster.query.filter_by(cluster_id=data["cluster_id"]).first()
    if existing:
        return jsonify({"ok": False, "error": f"Cluster ID '{data['cluster_id']}' already exists"}), 400
    
    try:
        cluster = Cluster(
            cluster_id=data["cluster_id"],
            name=data["name"],
            host=data["host"],
            port=data.get("port", 8006),
            user=data["user"],
            password=data["password"],
            verify_ssl=data.get("verify_ssl", False),
            is_default=data.get("is_default", False),
            is_active=data.get("is_active", True),
            allow_vm_deployment=data.get("allow_vm_deployment", True),
            allow_template_sync=data.get("allow_template_sync", True),
            allow_iso_sync=data.get("allow_iso_sync", True),
            auto_shutdown_enabled=data.get("auto_shutdown_enabled", False),
            priority=data.get("priority", 50),
            default_storage=data.get("default_storage"),
            template_storage=data.get("template_storage"),
            iso_storage=data.get("iso_storage"),
            qcow2_template_path=data.get("qcow2_template_path"),
            qcow2_images_path=data.get("qcow2_images_path"),
            admin_group=data.get("admin_group"),
            admin_users=data.get("admin_users"),
            arp_subnets=data.get("arp_subnets"),
            vm_cache_ttl=data.get("vm_cache_ttl"),
            enable_ip_lookup=data.get("enable_ip_lookup", True),
            enable_ip_persistence=data.get("enable_ip_persistence", False),
            description=data.get("description")
        )
        
        # If this is set as default, unset other defaults
        if cluster.is_default:
            Cluster.query.filter(Cluster.id != cluster.id).update({"is_default": False})
        
        db.session.add(cluster)
        db.session.commit()
        
        # Invalidate VM cache to refresh cluster list
        _invalidate_vm_cache()
        
        return jsonify({
            "ok": True, 
            "message": "Cluster created successfully",
            "cluster": cluster.to_safe_dict()
        })
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to create cluster: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_clusters_bp.route("/cluster-config/<int:cluster_db_id>", methods=["PUT"])
@admin_required
def api_update_cluster(cluster_db_id: int):
    """Update an existing cluster configuration."""
    from app.models import Cluster, db
    
    cluster = Cluster.query.get(cluster_db_id)
    if not cluster:
        return jsonify({"ok": False, "error": "Cluster not found"}), 404
    
    data = request.get_json()
    
    try:
        # Update fields (excluding cluster_id which shouldn't change)
        if "name" in data:
            cluster.name = data["name"]
        if "host" in data:
            cluster.host = data["host"]
        if "port" in data:
            cluster.port = data["port"]
        if "user" in data:
            cluster.user = data["user"]
        if "password" in data and data["password"]:  # Only update if password provided
            cluster.password = data["password"]
        if "verify_ssl" in data:
            cluster.verify_ssl = data["verify_ssl"]
        if "is_active" in data:
            cluster.is_active = data["is_active"]
        if "is_default" in data:
            cluster.is_default = data["is_default"]
            # If setting as default, unset others
            if cluster.is_default:
                Cluster.query.filter(Cluster.id != cluster.id).update({"is_default": False})
        if "allow_vm_deployment" in data:
            cluster.allow_vm_deployment = data["allow_vm_deployment"]
        if "allow_template_sync" in data:
            cluster.allow_template_sync = data["allow_template_sync"]
        if "allow_iso_sync" in data:
            cluster.allow_iso_sync = data["allow_iso_sync"]
        if "auto_shutdown_enabled" in data:
            cluster.auto_shutdown_enabled = data["auto_shutdown_enabled"]
        if "priority" in data:
            cluster.priority = data["priority"]
        if "default_storage" in data:
            cluster.default_storage = data["default_storage"]
        if "template_storage" in data:
            cluster.template_storage = data["template_storage"]
        if "iso_storage" in data:
            cluster.iso_storage = data["iso_storage"]
        if "qcow2_template_path" in data:
            cluster.qcow2_template_path = data["qcow2_template_path"]
        if "qcow2_images_path" in data:
            cluster.qcow2_images_path = data["qcow2_images_path"]
        if "admin_group" in data:
            cluster.admin_group = data["admin_group"]
        if "admin_users" in data:
            cluster.admin_users = data["admin_users"]
        if "arp_subnets" in data:
            cluster.arp_subnets = data["arp_subnets"]
        if "vm_cache_ttl" in data:
            cluster.vm_cache_ttl = data["vm_cache_ttl"]
        if "enable_ip_lookup" in data:
            cluster.enable_ip_lookup = data["enable_ip_lookup"]
        if "enable_ip_persistence" in data:
            cluster.enable_ip_persistence = data["enable_ip_persistence"]
        if "description" in data:
            cluster.description = data["description"]
        
        cluster.updated_at = datetime.utcnow()
        db.session.commit()
        
        # Invalidate VM cache to refresh cluster list
        _invalidate_vm_cache()
        
        return jsonify({
            "ok": True, 
            "message": "Cluster updated successfully",
            "cluster": cluster.to_safe_dict()
        })
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to update cluster: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_clusters_bp.route("/clusters/<int:cluster_db_id>/storages", methods=["GET"])
@admin_required
def api_get_cluster_storages(cluster_db_id: int):
    """Get list of storage pools available on a cluster."""
    try:
        from app.models import Cluster
        from app.services.proxmox_service import get_proxmox_admin_for_cluster
        
        # Get cluster from database
        cluster = Cluster.query.get(cluster_db_id)
        if not cluster:
            return jsonify({"ok": False, "error": "Cluster not found"}), 404
        
        # Get Proxmox connection for this cluster
        proxmox = get_proxmox_admin_for_cluster(cluster.cluster_id)
        
        # Fetch storage list from Proxmox
        storages = proxmox.storage.get()
        
        # Filter to only show storages that support disk images (not backup/iso-only)
        storage_list = []
        for storage in storages:
            content_types = storage.get('content', '').split(',')
            # Include storages that can hold VM images or rootdir (for containers)
            if 'images' in content_types or 'rootdir' in content_types:
                # Check if storage is disabled (0) or enabled (1 or missing = assume enabled)
                is_disabled = storage.get('disable', 0) == 1
                is_enabled = storage.get('enabled', 1) == 1  # Default to enabled if not specified
                
                storage_list.append({
                    'storage': storage['storage'],
                    'type': storage.get('type', 'unknown'),
                    'content': storage.get('content', ''),
                    'active': not is_disabled and is_enabled,
                    'enabled': is_enabled
                })
        
        return jsonify({"ok": True, "storages": storage_list})
        
    except Exception as e:
        logger.exception(f"Failed to get storages for cluster {cluster_db_id}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_clusters_bp.route("/cluster-config/<int:cluster_db_id>", methods=["DELETE"])
@admin_required
def api_delete_cluster(cluster_db_id: int):
    """Delete a cluster configuration."""
    from app.models import Cluster, db
    
    cluster = Cluster.query.get(cluster_db_id)
    if not cluster:
        return jsonify({"ok": False, "error": "Cluster not found"}), 404
    
    # Prevent deleting the last cluster
    total_clusters = Cluster.query.count()
    if total_clusters <= 1:
        return jsonify({"ok": False, "error": "Cannot delete the last cluster"}), 400
    
    try:
        cluster_name = cluster.name
        db.session.delete(cluster)
        db.session.commit()
        
        # Invalidate VM cache to refresh cluster list
        _invalidate_vm_cache()
        
        return jsonify({
            "ok": True, 
            "message": f"Cluster '{cluster_name}' deleted successfully"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to delete cluster: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_clusters_bp.route("/cluster-config/<int:cluster_db_id>/test", methods=["POST"])
@admin_required
def api_test_cluster_connection(cluster_db_id: int):
    """Test connection to a cluster."""
    from app.models import Cluster
    from proxmoxer import ProxmoxAPI
    
    cluster = Cluster.query.get(cluster_db_id)
    if not cluster:
        return jsonify({"ok": False, "error": "Cluster not found"}), 404
    
    try:
        # Attempt to connect and get version
        proxmox = ProxmoxAPI(
            cluster.host,
            user=cluster.user,
            password=cluster.password,
            verify_ssl=cluster.verify_ssl,
            port=cluster.port
        )
        version = proxmox.version.get()
        nodes = proxmox.nodes.get()
        
        return jsonify({
            "ok": True,
            "message": "Connection successful",
            "version": version.get("version"),
            "nodes": len(nodes),
            "node_names": [n["node"] for n in nodes]
        })
        
    except Exception as e:
        logger.error(f"Failed to connect to cluster {cluster.name}: {e}")
        return jsonify({
            "ok": False, 
            "error": f"Connection failed: {str(e)}"
        }), 500


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
    
    logger.info(f"[RESOURCES API] GET /clusters/{cluster_id}/resources called")
    logger.info(f"[RESOURCES API] Available cluster IDs: {[c['id'] for c in get_clusters_from_db()]}")
    
    try:
        # Validate cluster ID
        if cluster_id not in [c["id"] for c in get_clusters_from_db()]:
            logger.error(f"[RESOURCES API] Cluster {cluster_id} not found in get_clusters_from_db()")
            return jsonify({"ok": False, "error": "Cluster not found"}), 404
        
        logger.info(f"[RESOURCES API] Getting Proxmox connection for cluster {cluster_id}")
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        if not proxmox:
            logger.error(f"[RESOURCES API] Failed to get Proxmox connection for cluster {cluster_id}")
            return jsonify({"ok": False, "error": "Cluster not found"}), 404
        
        # Get all nodes
        logger.info(f"[RESOURCES API] Querying nodes for cluster {cluster_id}")
        nodes = proxmox.nodes.get()
        logger.info(f"[RESOURCES API] Found {len(nodes)} nodes in cluster {cluster_id}")
        
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
                # Get full node status (not just the status endpoint)
                node_info = proxmox.nodes(node_name).status.get()
                
                # Log the raw response for debugging
                logger.info(f"[RESOURCES API] Node {node_name} raw API response: {node_info}")
                
                # CPU: maxcpu is physical cores, cpu shows current usage (0.0-1.0 per core)
                max_cpu_cores = float(node_info.get('maxcpu', 0))
                cpu_usage = float(node_info.get('cpu', 0))  # Usage ratio (e.g., 0.5 = 50%)
                
                # Check if we got valid data
                if max_cpu_cores == 0:
                    logger.warning(f"Node {node_name} returned maxcpu={max_cpu_cores}, trying alternative API...")
                    # Try using the node list data instead (it has cpuinfo)
                    max_cpu_cores = float(node_data.get('maxcpu', 0))
                    cpu_usage = float(node_data.get('cpu', 0))
                    max_memory_bytes = int(node_data.get('maxmem', 0))
                    used_memory_bytes = int(node_data.get('mem', 0))
                    logger.info(f"Node {node_name} from nodes list: maxcpu={max_cpu_cores}, cpu={cpu_usage}, maxmem={max_memory_bytes}, mem={used_memory_bytes}")
                else:
                    # Memory: maxmem is total bytes, mem is used bytes
                    max_memory_bytes = int(node_info.get('maxmem', 0))
                    used_memory_bytes = int(node_info.get('mem', 0))
                
                used_cpu_cores = max_cpu_cores * cpu_usage
                available_cpu_cores = max_cpu_cores - used_cpu_cores
                
                available_memory_bytes = max_memory_bytes - used_memory_bytes
                available_memory_mb = available_memory_bytes // BYTES_TO_MB
                
                # Sanity check - if showing 0 available but max > 0, log warning
                if max_cpu_cores > 0 and available_cpu_cores <= 0:
                    logger.warning(f"Node {node_name}: CPU shows 0 available but has {max_cpu_cores} total cores (usage: {cpu_usage:.2%})")
                if max_memory_bytes > 0 and available_memory_mb <= 0:
                    logger.warning(f"Node {node_name}: Memory shows 0 available but has {max_memory_bytes // BYTES_TO_MB} MB total (used: {used_memory_bytes // BYTES_TO_MB} MB)")
                
                logger.info(f"Node {node_name}: {max_cpu_cores} cores ({available_cpu_cores:.1f} available), "
                           f"{max_memory_bytes // BYTES_TO_MB} MB RAM ({available_memory_mb} MB available)")
                
                total_cpu_cores += available_cpu_cores
                total_memory_mb += available_memory_mb
                successful_nodes.append(node_name)
                
            except Exception as e:
                # Use debug level for expected connection issues (offline nodes)
                if 'hostname lookup' in str(e) or 'No route to host' in str(e):
                    logger.debug(f"Node {node_name} unreachable (expected if offline): {e}")
                else:
                    logger.error(f"Failed to get status for node {node_name}: {type(e).__name__}: {e}")
                failed_nodes.append(node_name)
                continue
        
        logger.info(f"Cluster {cluster_id} totals: {total_cpu_cores:.1f} available cores, {total_memory_mb} MB available RAM from {len(successful_nodes)} nodes")
        
        # Apply overallocation factor for virtual CPUs
        # Round down to whole vCPUs
        vcpu_available = int(total_cpu_cores * CPU_OVERALLOCATION_FACTOR)
        
        response = {
            "ok": True,
            "cluster_id": cluster_id,
            "resources": {
                "cpu": {
                    "physical_cores_available": round(total_cpu_cores, 1),
                    "vcpu_available": vcpu_available,
                    "overallocation_factor": CPU_OVERALLOCATION_FACTOR
                },
                "memory": {
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
        if cluster_id not in [c["id"] for c in get_clusters_from_db()]:
            return jsonify({"ok": False, "error": "Cluster not found"}), 404
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        if not proxmox:
            return jsonify({"ok": False, "error": "Cluster not found"}), 404
        
        # Get node status
        try:
            node_info = proxmox.nodes(node_name).status.get()
            
            # Log the raw response for debugging
            logger.info(f"[NODE RESOURCES] Node {node_name} raw API response: {node_info}")
            
            # CPU: maxcpu is physical cores, cpu shows current usage ratio
            max_cpu_cores = float(node_info.get('maxcpu', 0))
            cpu_usage = float(node_info.get('cpu', 0))
            
            # Check if we got valid data
            if max_cpu_cores == 0:
                logger.warning(f"Node {node_name} status API returned maxcpu=0, trying nodes list API...")
                # Try using the nodes list as fallback
                nodes = proxmox.nodes.get()
                node_data = next((n for n in nodes if n['node'] == node_name), None)
                if node_data:
                    max_cpu_cores = float(node_data.get('maxcpu', 0))
                    cpu_usage = float(node_data.get('cpu', 0))
                    max_memory_bytes = int(node_data.get('maxmem', 0))
                    used_memory_bytes = int(node_data.get('mem', 0))
                    logger.info(f"Node {node_name} from nodes list: maxcpu={max_cpu_cores}, cpu={cpu_usage}, maxmem={max_memory_bytes}, mem={used_memory_bytes}")
                else:
                    logger.error(f"Node {node_name} not found in nodes list")
                    return jsonify({"ok": False, "error": f"Node {node_name} not found"}), 404
            else:
                # Memory: maxmem is total bytes, mem is used bytes
                max_memory_bytes = int(node_info.get('maxmem', 0))
                used_memory_bytes = int(node_info.get('mem', 0))
            
            used_cpu_cores = max_cpu_cores * cpu_usage
            available_cpu_cores = max_cpu_cores - used_cpu_cores
            
            available_memory_bytes = max_memory_bytes - used_memory_bytes
            available_memory_mb = available_memory_bytes // BYTES_TO_MB
            
            logger.info(f"Node {node_name} resources: {max_cpu_cores} cores ({available_cpu_cores:.1f} available), "
                       f"{max_memory_bytes // BYTES_TO_MB} MB RAM ({available_memory_mb} MB available)")
            
            # Apply overallocation factor for virtual CPUs
            vcpu_available = int(available_cpu_cores * CPU_OVERALLOCATION_FACTOR)
            
            response = {
                "ok": True,
                "cluster_id": cluster_id,
                "node_name": node_name,
                "resources": {
                    "cpu": {
                        "physical_cores_available": round(available_cpu_cores, 1),
                        "vcpu_available": vcpu_available,
                        "overallocation_factor": CPU_OVERALLOCATION_FACTOR
                    },
                    "memory": {
                        "available_mb": available_memory_mb
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


@api_clusters_bp.route("/cluster-config/<int:cluster_db_id>/storage", methods=["GET"])
@admin_required
def api_get_cluster_storage(cluster_db_id: int):
    """Get available storage pools from a cluster."""
    from app.models import Cluster
    from app.services.proxmox_service import get_proxmox_admin_for_cluster
    
    try:
        cluster = Cluster.query.get(cluster_db_id)
        if not cluster:
            return jsonify({"ok": False, "error": "Cluster not found"}), 404
        
        proxmox = get_proxmox_admin_for_cluster(cluster.cluster_id)
        if not proxmox:
            return jsonify({"ok": False, "error": "Failed to connect to cluster"}), 500
        
        # Get storage from first available node
        nodes = proxmox.nodes.get()
        if not nodes:
            return jsonify({"ok": False, "error": "No nodes found in cluster"}), 500
        
        node_name = nodes[0]['node']
        storage_list = proxmox.nodes(node_name).storage.get()
        
        # Filter for VM-compatible storage (types that support VMs)
        vm_storage = []
        for storage in storage_list:
            content = storage.get('content', '')
            storage_type = storage.get('type', '')
            
            # Include storage that supports images or rootdir
            if 'images' in content or 'rootdir' in content:
                vm_storage.append({
                    'storage': storage['storage'],
                    'type': storage_type,
                    'content': content,
                    'active': storage.get('active', 0) == 1,
                    'enabled': storage.get('enabled', 0) == 1
                })
        
        return jsonify({
            "ok": True,
            "storage": vm_storage
        })
        
    except Exception as e:
        logger.error(f"Failed to get storage for cluster {cluster_db_id}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_clusters_bp.route("/cluster-config/<int:cluster_db_id>/groups", methods=["GET"])
@admin_required
def api_get_cluster_groups(cluster_db_id: int):
    """Get Proxmox groups from a cluster."""
    from app.models import Cluster
    from app.services.proxmox_service import get_proxmox_admin_for_cluster
    
    try:
        cluster = Cluster.query.get(cluster_db_id)
        if not cluster:
            return jsonify({"ok": False, "error": "Cluster not found"}), 404
        
        proxmox = get_proxmox_admin_for_cluster(cluster.cluster_id)
        if not proxmox:
            return jsonify({"ok": False, "error": "Failed to connect to cluster"}), 500
        
        # Get groups from Proxmox
        groups = proxmox.access.groups.get()
        
        group_list = [
            {
                'groupid': g['groupid'],
                'comment': g.get('comment', '')
            }
            for g in groups
        ]
        
        return jsonify({
            "ok": True,
            "groups": group_list
        })
        
    except Exception as e:
        logger.error(f"Failed to get groups for cluster {cluster_db_id}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
