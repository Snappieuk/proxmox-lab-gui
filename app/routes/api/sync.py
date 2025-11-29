#!/usr/bin/env python3
"""
API endpoints for VM inventory sync management.

Provides endpoints to:
- Check sync status
- Trigger immediate sync
- View sync statistics
"""

import logging
from flask import Blueprint, jsonify

from app.utils.decorators import login_required, admin_required

logger = logging.getLogger(__name__)

sync_bp = Blueprint('sync_api', __name__, url_prefix='/api/sync')


@sync_bp.route('/status', methods=['GET'])
@login_required
def sync_status():
    """Get current sync status and statistics.
    
    Returns:
        JSON with sync stats
    """
    from app.services.background_sync import get_sync_stats
    
    try:
        stats = get_sync_stats()
        return jsonify({
            'ok': True,
            'stats': stats
        })
    except Exception as e:
        logger.exception("Failed to get sync status")
        return jsonify({
            'ok': False,
            'error': str(e)
        }), 500


@sync_bp.route('/trigger', methods=['POST'])
@admin_required
def trigger_sync():
    """Trigger an immediate full sync (admin only).
    
    Returns:
        JSON with success/failure
    """
    from app.services.background_sync import trigger_immediate_sync
    
    try:
        success = trigger_immediate_sync()
        return jsonify({
            'ok': success,
            'message': 'Sync triggered successfully' if success else 'Sync failed'
        })
    except Exception as e:
        logger.exception("Failed to trigger sync")
        return jsonify({
            'ok': False,
            'error': str(e)
        }), 500


@sync_bp.route('/inventory/summary', methods=['GET'])
@login_required
def inventory_summary():
    """Get VM inventory database summary.
    
    Returns:
        JSON with inventory counts by cluster, status, etc.
    """
    from app.models import VMInventory, db
    from sqlalchemy import func
    
    try:
        # Count by cluster
        by_cluster = db.session.query(
            VMInventory.cluster_id,
            func.count(VMInventory.id).label('count')
        ).group_by(VMInventory.cluster_id).all()
        
        # Count by status
        by_status = db.session.query(
            VMInventory.status,
            func.count(VMInventory.id).label('count')
        ).group_by(VMInventory.status).all()
        
        # Count templates
        template_count = VMInventory.query.filter_by(is_template=True).count()
        
        # Count with errors
        error_count = VMInventory.query.filter(VMInventory.sync_error.isnot(None)).count()
        
        return jsonify({
            'ok': True,
            'total_vms': VMInventory.query.count(),
            'by_cluster': {c: count for c, count in by_cluster},
            'by_status': {s: count for s, count in by_status},
            'template_count': template_count,
            'error_count': error_count,
        })
    except Exception as e:
        logger.exception("Failed to get inventory summary")
        return jsonify({
            'ok': False,
            'error': str(e)
        }), 500
