#!/usr/bin/env python3
"""
Template Migration API - Migrate templates between clusters.

Handles copying/moving VM templates across Proxmox clusters.
"""

import logging
from flask import Blueprint, jsonify, request

from app.models import Template, db
from app.services.class_service import get_user_by_username
from app.services.user_manager import require_user, is_admin_user
from app.utils.decorators import login_required

logger = logging.getLogger(__name__)

api_template_migrate_bp = Blueprint('api_template_migrate', __name__, url_prefix='/api/templates')


@api_template_migrate_bp.route("/migrate", methods=["POST"])
@login_required
def migrate_template():
    """Migrate a template from one cluster to another.
    
    Request body:
    {
        "source_template_id": int,
        "destination_cluster_id": str,
        "destination_storage": str,
        "operation": "copy" or "move",
        "new_name": str (optional)
    }
    """
    user = require_user()
    
    # Check if user is teacher or admin
    is_admin = is_admin_user(user)
    username = user.split('@')[0] if '@' in user else user
    local_user = get_user_by_username(username)
    
    if not is_admin and (not local_user or local_user.role not in ['teacher', 'adminer']):
        return jsonify({"ok": False, "error": "Access denied. Only teachers and admins can migrate templates."}), 403
    
    data = request.get_json()
    source_template_id = data.get('source_template_id')
    destination_cluster_id = data.get('destination_cluster_id')
    destination_storage = data.get('destination_storage')
    operation = data.get('operation', 'copy')  # 'copy' or 'move'
    new_name = data.get('new_name')
    
    if not all([source_template_id, destination_cluster_id, destination_storage]):
        return jsonify({"ok": False, "error": "Missing required parameters"}), 400
    
    if operation not in ['copy', 'move']:
        return jsonify({"ok": False, "error": "Operation must be 'copy' or 'move'"}), 400
    
    # Get source template
    source_template = Template.query.get(source_template_id)
    if not source_template:
        return jsonify({"ok": False, "error": "Source template not found"}), 404
    
    logger.info(f"Starting template migration: template_id={source_template_id}, "
                f"operation={operation}, destination={destination_cluster_id}")
    
    try:
        # Start migration in background thread
        import threading
        from app.services.template_migration_service import migrate_template_between_clusters
        
        def migration_task():
            try:
                migrate_template_between_clusters(
                    source_template=source_template,
                    destination_cluster_id=destination_cluster_id,
                    destination_storage=destination_storage,
                    operation=operation,
                    new_name=new_name
                )
            except Exception as e:
                logger.exception(f"Template migration failed: {e}")
        
        thread = threading.Thread(target=migration_task, daemon=True)
        thread.start()
        
        return jsonify({
            "ok": True,
            "message": f"Template migration started. This may take several minutes.",
            "operation": operation
        })
        
    except Exception as e:
        logger.exception(f"Failed to start template migration: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_template_migrate_bp.route("/migration-options", methods=["GET"])
@login_required
def get_migration_options():
    """Get available migration options (clusters and storages).
    
    Query params:
    - source_template_id: int (optional, to show compatible destinations)
    """
    user = require_user()
    
    # Check if user is teacher or admin
    is_admin = is_admin_user(user)
    username = user.split('@')[0] if '@' in user else user
    local_user = get_user_by_username(username)
    
    if not is_admin and (not local_user or local_user.role not in ['teacher', 'adminer']):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    
    from app.config import CLUSTERS
    from app.services.proxmox_service import get_proxmox_admin_for_cluster
    
    # Get list of clusters with their available storages
    clusters_info = []
    
    for cluster in CLUSTERS:
        cluster_id = cluster["id"]
        cluster_name = cluster["name"]
        
        try:
            proxmox = get_proxmox_admin_for_cluster(cluster_id)
            
            # Get storage info
            storages = []
            storage_list = proxmox.storage.get()
            
            for storage in storage_list:
                storage_id = storage.get('storage')
                storage_type = storage.get('type')
                content = storage.get('content', '')
                
                # Only include storages that support VM images
                if 'images' in content:
                    storages.append({
                        "id": storage_id,
                        "type": storage_type,
                        "content": content
                    })
            
            clusters_info.append({
                "id": cluster_id,
                "name": cluster_name,
                "host": cluster["host"],
                "storages": storages
            })
            
        except Exception as e:
            logger.warning(f"Failed to get storage info for cluster {cluster_id}: {e}")
            clusters_info.append({
                "id": cluster_id,
                "name": cluster_name,
                "host": cluster["host"],
                "storages": [],
                "error": str(e)
            })
    
    return jsonify({
        "ok": True,
        "clusters": clusters_info
    })
