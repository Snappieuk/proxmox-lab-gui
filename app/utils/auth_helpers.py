#!/usr/bin/env python3
"""
Authentication helper utilities.

Shared authentication functions used across routes.
"""

from typing import Optional

from flask import session

from app.models import User
from app.services.class_service import get_user_by_username


def get_current_user(auto_create_admin: bool = False) -> Optional[User]:
    """
    Get current logged-in user from session.
    
    Args:
        auto_create_admin: If True, auto-create admin account for Proxmox admins
                          with no local account (used in non-API routes)
    
    Returns:
        User object if found, None otherwise
    """
    username = session.get('user')
    if not username:
        return None
    
    # Remove @pve suffix if present for local user lookup
    if '@' in username:
        username_clean = username.split('@')[0]
    else:
        username_clean = username
    
    user = get_user_by_username(username_clean)
    
    # If no local user exists but they're a Proxmox admin, auto-create admin account
    if not user and auto_create_admin:
        from app.services.class_service import create_local_user
        from app.services.user_manager import is_admin_user
        
        if is_admin_user(session.get('user')):
            success, message = create_local_user(username_clean, '', role='adminer')
            if success:
                user = get_user_by_username(username_clean)
    
    return user
