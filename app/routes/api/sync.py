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

from app.utils.decorators import admin_required, login_required

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
    from sqlalchemy import func

    from app.models import VMInventory, db
    
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


@sync_bp.route('/templates/trigger', methods=['POST'])
@admin_required
def trigger_template_sync():
    """Trigger an immediate template sync (admin only).
    
    Returns:
        JSON with sync results
    """
    from app.services.template_sync import sync_templates_from_proxmox
    
    try:
        stats = sync_templates_from_proxmox(full_sync=True)
        return jsonify({
            'ok': True,
            'stats': stats,
            'message': f"Template sync complete: {stats['templates_found']} found, {stats['templates_added']} added, {stats['templates_updated']} updated"
        })
    except Exception as e:
        logger.exception("Failed to trigger template sync")
        return jsonify({
            'ok': False,
            'error': str(e)
        }), 500


@sync_bp.route('/templates/status', methods=['GET'])
@login_required
def template_sync_status():
    """Get template database statistics.
    
    Returns:
        JSON with template counts by cluster, node, etc.
    """
    from sqlalchemy import func

    from app.models import Template, db
    
    try:
        # Count by cluster
        by_cluster = db.session.query(
            Template.cluster_ip,
            func.count(Template.id).label('count')
        ).group_by(Template.cluster_ip).all()
        
        # Count by node
        by_node = db.session.query(
            Template.node,
            func.count(Template.id).label('count')
        ).group_by(Template.node).all()
        
        # Count class templates
        class_template_count = Template.query.filter_by(is_class_template=True).count()
        
        # Count replicas
        replica_count = Template.query.filter_by(is_replica=True).count()
        
        # Find templates with cached specs
        cached_specs_count = Template.query.filter(Template.specs_cached_at.isnot(None)).count()
        
        return jsonify({
            'ok': True,
            'total_templates': Template.query.count(),
            'by_cluster': {c: count for c, count in by_cluster},
            'by_node': {n: count for n, count in by_node},
            'class_template_count': class_template_count,
            'replica_count': replica_count,
            'cached_specs_count': cached_specs_count,
        })
    except Exception as e:
        logger.exception("Failed to get template sync status")
        return jsonify({
            'ok': False,
            'error': str(e)
        }), 500


@sync_bp.route('/isos/trigger', methods=['POST'])
@admin_required
def trigger_iso_sync():
    """Trigger an immediate ISO sync (admin only).
    
    Returns:
        JSON with sync results
    """
    from app.services.iso_sync import trigger_immediate_iso_sync
    
    try:
        success = trigger_immediate_iso_sync()
        if success:
            return jsonify({
                'ok': True,
                'message': 'ISO sync triggered in background - check logs for progress'
            })
        else:
            return jsonify({
                'ok': False,
                'error': 'Failed to trigger ISO sync'
            }), 500
    except Exception as e:
        logger.exception("Failed to trigger ISO sync")
        return jsonify({
            'ok': False,
            'error': str(e)
        }), 500


@sync_bp.route('/isos/status', methods=['GET'])
@login_required
def iso_sync_status():
    """Get ISO database statistics.
    
    Returns:
        JSON with ISO counts by cluster, node, storage, etc.
    """
    from sqlalchemy import func

    from app.models import ISOImage, db
    
    try:
        # Count by cluster
        by_cluster = db.session.query(
            ISOImage.cluster_id,
            func.count(ISOImage.id).label('count')
        ).group_by(ISOImage.cluster_id).all()
        
        # Count by node
        by_node = db.session.query(
            ISOImage.node,
            func.count(ISOImage.id).label('count')
        ).group_by(ISOImage.node).all()
        
        # Count by storage
        by_storage = db.session.query(
            ISOImage.storage,
            func.count(ISOImage.id).label('count')
        ).group_by(ISOImage.storage).all()
        
        # Calculate total size
        total_size = db.session.query(func.sum(ISOImage.size)).scalar() or 0
        
        return jsonify({
            'ok': True,
            'total_isos': ISOImage.query.count(),
            'by_cluster': {c: count for c, count in by_cluster},
            'by_node': {n: count for n, count in by_node},
            'by_storage': {s: count for s, count in by_storage},
            'total_size_bytes': total_size,
        })
    except Exception as e:
        logger.exception("Failed to get ISO sync status")
        return jsonify({
            'ok': False,
            'error': str(e)
        }), 500
