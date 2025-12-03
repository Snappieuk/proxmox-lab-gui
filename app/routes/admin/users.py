#!/usr/bin/env python3
"""
Admin Users routes blueprint.

Handles user management for admins.
"""

import logging

from flask import Blueprint, render_template, jsonify, request

from app.utils.decorators import login_required, admin_required


from app.services.user_manager import (
    get_pve_users,
    create_pve_user,
    is_admin_user,
    get_admin_group_members,
    add_user_to_admin_group,
    remove_user_from_admin_group
)
from app.services.class_service import (
    list_all_users,
    create_local_user,
    get_user_by_username,
    update_user_role,
    delete_user
)

logger = logging.getLogger(__name__)

admin_users_bp = Blueprint('admin_users', __name__, url_prefix='/admin/users')


@admin_users_bp.route("")
@admin_required
def users_management():
    """User management view."""
    from app.services.user_manager import require_user
    
    user = require_user()
    
    return render_template("admin/users.html", current_user=user)


@admin_users_bp.route("/api/users")
@admin_required
def api_get_users():
    """Get all users (Proxmox + Local)."""
    try:
        # Get Proxmox users
        proxmox_users = get_pve_users()
        admin_members = get_admin_group_members()
        
        # Get local users
        local_users = list_all_users()
        
        # Combine and enrich
        users_data = []
        
        # Add Proxmox users
        for pve_user in proxmox_users:
            userid = pve_user['userid']
            username = userid.split('@')[0]
            is_admin = is_admin_user(userid)
            
            # Find matching local user
            local_user = next((u for u in local_users if u.username == username), None)
            
            users_data.append({
                'username': username,
                'userid': userid,
                'type': 'proxmox',
                'is_admin': is_admin,
                'local_role': local_user.role if local_user else None,
                'has_local_account': bool(local_user)
            })
        
        # Add local-only users (not in Proxmox)
        proxmox_usernames = {u['userid'].split('@')[0] for u in proxmox_users}
        for local_user in local_users:
            if local_user.username not in proxmox_usernames:
                users_data.append({
                    'username': local_user.username,
                    'userid': None,
                    'type': 'local',
                    'is_admin': False,
                    'local_role': local_user.role,
                    'has_local_account': True
                })
        
        return jsonify({
            'ok': True,
            'users': users_data
        })
        
    except Exception as e:
        logger.exception("Failed to get users")
        return jsonify({'ok': False, 'error': str(e)}), 500


@admin_users_bp.route("/api/users/create", methods=['POST'])
@admin_required
def api_create_user():
    """Create a new Proxmox user."""
    try:
        data = request.get_json()
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        user_type = data.get('type', 'proxmox')
        role = data.get('role', 'user')
        
        if not username or not password:
            return jsonify({'ok': False, 'error': 'Username and password required'}), 400
        
        if user_type == 'proxmox':
            # Create Proxmox user
            success, error = create_pve_user(username, password)
            if not success:
                return jsonify({'ok': False, 'error': error}), 400
            
            # Optionally create local account too
            if data.get('create_local'):
                create_local_user(username, password, role=role)
        else:
            # Create local-only user
            success, error = create_local_user(username, password, role=role)
            if not success:
                return jsonify({'ok': False, 'error': error}), 400
        
        return jsonify({'ok': True, 'message': f'User {username} created successfully'})
        
    except Exception as e:
        logger.exception("Failed to create user")
        return jsonify({'ok': False, 'error': str(e)}), 500


@admin_users_bp.route("/api/users/<username>/role", methods=['POST'])
@admin_required
def api_update_user_role(username):
    """Update local user role."""
    try:
        data = request.get_json()
        new_role = data.get('role')
        
        if new_role not in ('user', 'teacher', 'adminer'):
            return jsonify({'ok': False, 'error': 'Invalid role'}), 400
        
        user = get_user_by_username(username)
        if not user:
            return jsonify({'ok': False, 'error': 'User not found'}), 404
        
        update_user_role(user.id, new_role)
        
        return jsonify({'ok': True, 'message': f'Role updated to {new_role}'})
        
    except Exception as e:
        logger.exception("Failed to update user role")
        return jsonify({'ok': False, 'error': str(e)}), 500


@admin_users_bp.route("/api/users/<username>/admin", methods=['POST'])
@admin_required
def api_toggle_admin(username):
    """Add or remove user from admin group."""
    try:
        data = request.get_json()
        make_admin = data.get('is_admin', False)
        
        userid = f"{username}@pve"
        
        if make_admin:
            success = add_user_to_admin_group(userid)
        else:
            success = remove_user_from_admin_group(userid)
        
        if not success:
            return jsonify({'ok': False, 'error': 'Failed to update admin status'}), 500
        
        action = 'added to' if make_admin else 'removed from'
        return jsonify({'ok': True, 'message': f'User {action} admin group'})
        
    except Exception as e:
        logger.exception("Failed to toggle admin status")
        return jsonify({'ok': False, 'error': str(e)}), 500


@admin_users_bp.route("/api/users/<username>", methods=['DELETE'])
@admin_required
def api_delete_user(username):
    """Delete a local user account."""
    try:
        user = get_user_by_username(username)
        if not user:
            return jsonify({'ok': False, 'error': 'User not found'}), 404
        
        delete_user(user.id)
        
        return jsonify({'ok': True, 'message': f'User {username} deleted'})
        
    except Exception as e:
        logger.exception("Failed to delete user")
        return jsonify({'ok': False, 'error': str(e)}), 500
