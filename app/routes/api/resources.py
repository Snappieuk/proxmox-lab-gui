#!/usr/bin/env python3
"""
API endpoints for resource management (URLs/cards accessible to teachers and admins).
"""

import logging
from flask import Blueprint, jsonify, request, session
from app.models import db, Resource, User
from app.utils.decorators import login_required, admin_required
from app.utils.auth_helpers import get_current_user
from app.services.user_manager import is_admin_user
from datetime import datetime

logger = logging.getLogger(__name__)

resources_bp = Blueprint('api_resources', __name__, url_prefix='/api/resources')


def _check_teacher_or_admin(user):
    """Check if user is admin or teacher."""
    if is_admin_user(user):
        return True
    
    local_user = User.query.filter_by(username=user).first()
    if local_user and local_user.role in ['teacher', 'adminer']:
        return True
    
    return False


@resources_bp.route('', methods=['GET'])
@login_required
def list_resources():
    """List all active resources in display order (teachers and admins only)."""
    user = session.get('user')
    
    # Check if user is teacher or admin
    if not _check_teacher_or_admin(user):
        return jsonify({'ok': False, 'error': 'Access denied'}), 403
    
    try:
        resources = Resource.query.filter_by(is_active=True).order_by(Resource.display_order).all()
        return jsonify({
            'ok': True,
            'data': [r.to_dict() for r in resources]
        }), 200
    except Exception as e:
        logger.error(f"Failed to list resources: {e}", exc_info=True)
        return jsonify({'ok': False, 'error': 'Failed to list resources'}), 500


@resources_bp.route('', methods=['POST'])
@admin_required
def create_resource():
    """Create a new resource card (admin only).
    
    Required fields: name, url
    Optional fields: description, icon, color, display_order
    """
    try:
        user = session.get('user')
        
        data = request.get_json() or {}
        
        # Validate required fields
        if not data.get('name'):
            return jsonify({'ok': False, 'error': 'Resource name is required'}), 400
        if not data.get('url'):
            return jsonify({'ok': False, 'error': 'Resource URL is required'}), 400
        
        # Validate URL format (basic check)
        url = data.get('url', '').strip()
        if not (url.startswith('http://') or url.startswith('https://') or url.startswith('/')):
            return jsonify({'ok': False, 'error': 'URL must start with http://, https://, or /'}), 400
        
        # Get current user for created_by
        current_user = User.query.filter_by(username=user).first()
        
        # Create resource
        resource = Resource(
            name=data.get('name', '').strip(),
            description=data.get('description', '').strip() or None,
            url=url,
            icon=data.get('icon', 'ti-external-link').strip(),
            color=data.get('color', 'blue').strip(),
            display_order=int(data.get('display_order', 0)),
            is_active=True,
            created_by=current_user.id if current_user else None
        )
        
        db.session.add(resource)
        db.session.commit()
        
        logger.info(f"Resource created: {resource.id} {resource.name} by {user}")
        
        return jsonify({
            'ok': True,
            'data': resource.to_dict(),
            'message': 'Resource created successfully'
        }), 201
        
    except ValueError as e:
        logger.warning(f"Validation error creating resource: {e}")
        return jsonify({'ok': False, 'error': f'Validation error: {str(e)}'}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to create resource: {e}", exc_info=True)
        return jsonify({'ok': False, 'error': 'Failed to create resource'}), 500


@resources_bp.route('/<int:resource_id>', methods=['GET'])
@login_required
def get_resource(resource_id):
    """Get a specific resource by ID (teachers and admins only)."""
    user = session.get('user')
    
    if not _check_teacher_or_admin(user):
        return jsonify({'ok': False, 'error': 'Access denied'}), 403
    
    try:
        resource = Resource.query.get(resource_id)
        if not resource:
            return jsonify({'ok': False, 'error': 'Resource not found'}), 404
        
        return jsonify({
            'ok': True,
            'data': resource.to_dict()
        }), 200
        
    except Exception as e:
        logger.error(f"Failed to get resource {resource_id}: {e}", exc_info=True)
        return jsonify({'ok': False, 'error': 'Failed to get resource'}), 500


@resources_bp.route('/<int:resource_id>', methods=['PUT'])
@admin_required
def update_resource(resource_id):
    """Update a resource card (admin only).
    
    Updates fields: name, description, url, icon, color, display_order, is_active
    """
    try:
        resource = Resource.query.get(resource_id)
        if not resource:
            return jsonify({'ok': False, 'error': 'Resource not found'}), 404
        
        data = request.get_json() or {}
        
        # Update mutable fields
        if 'name' in data:
            name = data.get('name', '').strip()
            if not name:
                return jsonify({'ok': False, 'error': 'Resource name cannot be empty'}), 400
            resource.name = name
        
        if 'description' in data:
            resource.description = data.get('description', '').strip() or None
        
        if 'url' in data:
            url = data.get('url', '').strip()
            if not url:
                return jsonify({'ok': False, 'error': 'Resource URL cannot be empty'}), 400
            if not (url.startswith('http://') or url.startswith('https://') or url.startswith('/')):
                return jsonify({'ok': False, 'error': 'URL must start with http://, https://, or /'}), 400
            resource.url = url
        
        if 'icon' in data:
            resource.icon = data.get('icon', '').strip()
        
        if 'color' in data:
            resource.color = data.get('color', '').strip()
        
        if 'display_order' in data:
            resource.display_order = int(data.get('display_order', 0))
        
        if 'is_active' in data:
            resource.is_active = bool(data.get('is_active', True))
        
        resource.updated_at = datetime.utcnow()
        db.session.commit()
        
        logger.info(f"Resource updated: {resource.id} {resource.name}")
        
        return jsonify({
            'ok': True,
            'data': resource.to_dict(),
            'message': 'Resource updated successfully'
        }), 200
        
    except ValueError as e:
        logger.warning(f"Validation error updating resource {resource_id}: {e}")
        return jsonify({'ok': False, 'error': f'Validation error: {str(e)}'}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to update resource {resource_id}: {e}", exc_info=True)
        return jsonify({'ok': False, 'error': 'Failed to update resource'}), 500


@resources_bp.route('/<int:resource_id>', methods=['DELETE'])
@admin_required
def delete_resource(resource_id):
    """Delete a resource card (admin only)."""
    try:
        resource = Resource.query.get(resource_id)
        if not resource:
            return jsonify({'ok': False, 'error': 'Resource not found'}), 404
        
        logger.info(f"Resource deleted: {resource.id} {resource.name}")
        
        db.session.delete(resource)
        db.session.commit()
        
        return jsonify({
            'ok': True,
            'message': 'Resource deleted successfully'
        }), 200
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to delete resource {resource_id}: {e}", exc_info=True)
        return jsonify({'ok': False, 'error': 'Failed to delete resource'}), 500


@resources_bp.route('/bulk-reorder', methods=['POST'])
@admin_required
def bulk_reorder_resources():
    """Reorder multiple resources at once.
    
    Expects JSON body with array of {'id': resource_id, 'display_order': order}
    """
    try:
        data = request.get_json() or []
        
        if not isinstance(data, list):
            return jsonify({'ok': False, 'error': 'Request body must be a JSON array'}), 400
        
        for item in data:
            if 'id' not in item or 'display_order' not in item:
                return jsonify({'ok': False, 'error': 'Each item must have "id" and "display_order"'}), 400
            
            resource = Resource.query.get(item.get('id'))
            if not resource:
                return jsonify({'ok': False, 'error': f'Resource {item.get("id")} not found'}), 404
            
            resource.display_order = int(item.get('display_order', 0))
            resource.updated_at = datetime.utcnow()
        
        db.session.commit()
        logger.info(f"Reordered {len(data)} resources")
        
        return jsonify({
            'ok': True,
            'message': f'Successfully reordered {len(data)} resources'
        }), 200
        
    except ValueError as e:
        logger.warning(f"Validation error reordering resources: {e}")
        return jsonify({'ok': False, 'error': f'Validation error: {str(e)}'}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to reorder resources: {e}", exc_info=True)
        return jsonify({'ok': False, 'error': 'Failed to reorder resources'}), 500
