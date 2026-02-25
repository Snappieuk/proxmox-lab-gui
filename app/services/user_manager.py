#!/usr/bin/env python3
"""
User Manager Service - User authentication and authorization.

This module handles user authentication, admin checking, and user management.
"""

import logging
import re
import threading
from typing import Any, Dict, List, Optional

from flask import abort, session
from proxmoxer import ProxmoxAPI

logger = logging.getLogger(__name__)

# Admin group membership cache (per-group caching)
_admin_group_cache: Dict[str, List[str]] = {}  # groupid -> members
_admin_group_ts: Dict[str, float] = {}  # groupid -> timestamp
_admin_group_lock = threading.Lock()
ADMIN_GROUP_CACHE_TTL = 120  # 2 minutes


def require_user() -> str:
    """Return current user or abort with 401."""
    user = session.get("user")
    if not user:
        abort(401)
    return user


def authenticate_proxmox_user(username: str, password: str) -> Optional[str]:
    """
    Try to authenticate a Proxmox user (admin).
    
    Accepts formats:
    - 'root@pam' - full user@realm format (tries as-is)
    - 'admin@pve' - full user@realm format (tries as-is) 
    - 'admin' - bare username (tries with @pve realm)
    
    Returns the full user id on success (e.g. 'root@pam'),
    or None on failure.
    """
    username = (username or "").strip()
    if not username or not password:
        return None

    # Get cluster config from session or default to first cluster
    from app.services.proxmox_service import get_clusters_from_db
    clusters = get_clusters_from_db()
    if not clusters:
        logger.error("No active clusters configured")
        return None
    
    cluster_id = session.get("cluster_id", clusters[0]["id"])
    cluster = next((c for c in clusters if c["id"] == cluster_id), clusters[0])
    
    # If username already has realm suffix, try as-is
    if '@' in username:
        try:
            from app.services.proxmox_service import create_proxmox_connection
            # Create temporary cluster config with test username
            test_cluster = {**cluster, "user": username, "password": password}
            prox = create_proxmox_connection(test_cluster)
            prox.version.get()
            logger.debug("Proxmox auth succeeded for %s", username)
            return username
        except Exception as e:
            logger.debug("Proxmox auth failed for %s on cluster %s: %s", username, cluster["name"], e)
            return None
    
    # Try with @pve realm for bare username (backwards compatibility)
    full_user = f"{username}@pve"
    try:
        from app.services.proxmox_service import create_proxmox_connection
        # Create temporary cluster config with test username
        test_cluster = {**cluster, "user": full_user, "password": password}
        prox = create_proxmox_connection(test_cluster)
        prox.version.get()
        logger.debug("Proxmox auth succeeded for %s", full_user)
        return full_user
    except Exception as e:
        logger.debug("Proxmox auth failed for %s on cluster %s: %s", full_user, cluster["name"], e)
        return None


def _get_admin_group_members_cached(admin_group: str) -> List[str]:
    """Get admin group members from cache or fetch from Proxmox if cache expired.
    
    Args:
        admin_group: The Proxmox group name to check
        
    Returns:
        List of userids in the group
    """
    global _admin_group_cache, _admin_group_ts
    
    if not admin_group:
        return []
    
    import time
    now = time.time()
    
    # Cache key based on group name
    cache_key = admin_group
    
    # Check cache first (thread-safe read)
    with _admin_group_lock:
        if cache_key in _admin_group_cache and (now - _admin_group_ts.get(cache_key, 0)) < ADMIN_GROUP_CACHE_TTL:
            logger.debug("Using cached admin group members for %s (age=%.1fs)", admin_group, now - _admin_group_ts.get(cache_key, 0))
            return list(_admin_group_cache[cache_key])
    
    # Cache miss or expired - fetch from Proxmox
    try:
        from app.services.proxmox_service import get_proxmox_admin
        grp = get_proxmox_admin().access.groups(admin_group).get()
        raw_members = (grp.get("members", []) or []) if isinstance(grp, dict) else []
        members = []
        for m in raw_members:
            if isinstance(m, str):
                members.append(m)
            elif isinstance(m, dict) and "userid" in m:
                members.append(m["userid"])
        
        # Update cache (thread-safe write)
        with _admin_group_lock:
            _admin_group_cache[cache_key] = members
            _admin_group_ts[cache_key] = now
        logger.debug("Refreshed admin group cache for %s: %d members", admin_group, len(members))
        return members
        
    except Exception as e:
        logger.warning("Failed to get admin group members for %s: %s", admin_group, e)
        # On error, return cached value if we have one (even if expired)
        with _admin_group_lock:
            if cache_key in _admin_group_cache:
                logger.debug("Using stale admin group cache for %s due to error", admin_group)
                return list(_admin_group_cache[cache_key])
        return []


def _user_in_group(user: str, groupid: str) -> bool:
    """Check if user is in a Proxmox group."""
    if not groupid:
        return False

    # Use cached admin group members (function handles all groups now)
    members = _get_admin_group_members_cached(groupid)
    if not members:
        return False

    # Accept both realm variants for matching
    variants = {user}
    if "@" in user:
        name, realm = user.split("@", 1)
        for r in ("pam", "pve"):
            variants.add(f"{name}@{r}")
    return any(u in members for u in variants)


def is_admin_user(user: str) -> bool:
    """Check if user is an admin.
    
    Admin if:
    1. User authenticated via Proxmox AND is in ADMIN_GROUP (if configured)
    2. Local database user with role='adminer'
    
    Note: Unlike the old design, not ALL Proxmox users are admins.
    Only Proxmox users in the ADMIN_GROUP (default: 'adminers') are admins.
    """
    if not user:
        return False
    
    # Check if user has Proxmox realm suffix (authenticated via Proxmox)
    if '@' in user and (user.endswith('@pve') or user.endswith('@pam')):
        # Check if user is in any configured admin group
        from app.services.settings_service import get_all_admin_groups
        admin_groups = get_all_admin_groups()
        
        if admin_groups:
            # Check all configured admin groups
            for admin_group in admin_groups:
                if _user_in_group(user, admin_group):
                    logger.debug("is_admin_user(%s) -> True (Proxmox user in %s group)", user, admin_group)
                    return True
            logger.debug("is_admin_user(%s) -> False (Proxmox user but not in any admin group)", user)
            return False
        else:
            # No admin group configured - treat all Proxmox users as admins (legacy behavior)
            logger.debug("is_admin_user(%s) -> True (Proxmox user, no admin group check)", user)
            return True
    
    # Check if local database user with adminer role
    try:
        from app.services.class_service import get_user_by_username
        username = user.split('@')[0] if '@' in user else user
        local_user = get_user_by_username(username)
        if local_user and local_user.role == 'adminer':
            logger.debug("is_admin_user(%s) -> True (local adminer)", user)
            return True
    except Exception as e:
        logger.debug("is_admin_user(%s) -> False (error checking local user: %s)", user, e)
    
    return False


def get_admin_group_members() -> List[str]:
    """Get list of users in all configured admin groups (aggregated across clusters)."""
    from app.services.settings_service import get_all_admin_groups
    admin_groups = get_all_admin_groups()
    
    if not admin_groups:
        return []
    
    # Aggregate members from all admin groups (deduplicated)
    all_members = set()
    for admin_group in admin_groups:
        members = _get_admin_group_members_cached(admin_group)
        all_members.update(members)
    
    return list(all_members)


def _invalidate_admin_group_cache() -> None:
    """Invalidate the admin group cache to force a refresh on next access."""
    global _admin_group_cache, _admin_group_ts
    with _admin_group_lock:
        _admin_group_cache.clear()
        _admin_group_ts.clear()
    logger.debug("Invalidated admin group cache")


def add_user_to_admin_group(user: str, admin_group: str = None) -> bool:
    """Add a user to the admin group in Proxmox.
    
    Args:
        user: Username to add (with realm suffix, e.g., user@pam)
        admin_group: Proxmox group name (if None, uses first configured admin group)
        
    Returns:
        True if successful, False otherwise
    """
    if not admin_group:
        from app.services.settings_service import get_all_admin_groups
        admin_groups = get_all_admin_groups()
        if not admin_groups:
            logger.warning("No admin groups configured, cannot add user")
            return False
        admin_group = admin_groups[0]  # Use first configured group
    
    try:
        from app.services.proxmox_service import get_proxmox_admin

        # Get current group info
        current_members = get_admin_group_members()
        if user in current_members:
            logger.info("User %s already in admin group %s", user, admin_group)
            return True
        
        current_members.append(user)
        
        # Get current group to fetch comment
        try:
            group_info = get_proxmox_admin().access.groups(admin_group).get()
            comment = group_info.get('comment', '') if group_info else ''
        except Exception:
            comment = ''
        
        get_proxmox_admin().access.groups(admin_group).put(
            comment=comment,
            members=",".join(current_members)
        )
        _invalidate_admin_group_cache()
        logger.info("Added user %s to admin group %s", user, admin_group)
        return True
    except Exception as e:
        logger.exception("Failed to add user %s to admin group: %s", user, e)
        return False


def remove_user_from_admin_group(user: str, admin_group: str = None) -> bool:
    """Remove a user from the admin group in Proxmox.
    
    Args:
        user: Username to remove (with realm suffix, e.g., user@pam)
        admin_group: Proxmox group name (if None, uses first configured admin group)
        
    Returns:
        True if successful, False otherwise
    """
    if not admin_group:
        from app.services.settings_service import get_all_admin_groups
        admin_groups = get_all_admin_groups()
        if not admin_groups:
            logger.warning("No admin groups configured, cannot remove user")
            return False
        admin_group = admin_groups[0]  # Use first configured group
    
    try:
        from app.services.proxmox_service import get_proxmox_admin
        
        current_members = get_admin_group_members()
        if user not in current_members:
            logger.info("User %s not in admin group", user)
            return True
        
        current_members.remove(user)
        
        # Get current group to fetch comment
        try:
            group_info = get_proxmox_admin().access.groups(admin_group).get()
            comment = group_info.get('comment', '') if group_info else ''
        except Exception:
            comment = ''
        
        get_proxmox_admin().access.groups(admin_group).put(
            comment=comment,
            members=",".join(current_members)
        )
        _invalidate_admin_group_cache()
        logger.info("Removed user %s from admin group %s", user, admin_group)
        return True
    except Exception as e:
        logger.exception("Failed to remove user %s from admin group: %s", user, e)
        return False


def get_pve_users() -> List[Dict[str, str]]:
    """Return list of enabled PVE-realm users."""
    try:
        from app.services.proxmox_service import get_proxmox_admin
        users = get_proxmox_admin().access.users.get()
    except Exception as e:
        logger.debug("failed to list pve users: %s", e)
        return []

    if users is None:
        users = []

    out: List[Dict[str, str]] = []
    for u in users:
        userid = u.get("userid")
        enable = u.get("enable", 1)
        if not userid or "@" not in userid:
            continue
        name, realm = userid.split("@", 1)
        if realm != "pve":
            continue
        if enable == 0:
            continue
        out.append({"userid": userid})

    out.sort(key=lambda x: x["userid"])
    return out


def create_pve_user(username: str, password: str) -> tuple:
    """Create a new user in Proxmox pve realm.
    
    Returns: (success: bool, error_message: Optional[str])
    """
    if not username or not password:
        return False, "Username and password are required"
    
    # Validate username (alphanumeric, underscore, dash)
    if not re.match(r'^[a-zA-Z0-9_-]+$', username):
        return False, "Username can only contain letters, numbers, underscore, and dash"
    
    if len(username) < 3:
        return False, "Username must be at least 3 characters"
    
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    
    try:
        from app.services.proxmox_service import get_proxmox_admin
        userid = f"{username}@pve"
        get_proxmox_admin().access.users.post(
            userid=userid,
            password=password,
            enable=1,
            comment="Self-registered user"
        )
        logger.info("Created pve user: %s", userid)
        return True, None
    except Exception as e:
        error_msg = str(e)
        if "already exists" in error_msg.lower():
            return False, "Username already exists"
        logger.exception("Failed to create user %s: %s", username, e)
        return False, f"Failed to create user: {error_msg}"


def probe_proxmox() -> Dict[str, Any]:
    """Return diagnostics information for admin troubleshooting.
    
    Contains node list, resource count, sample resources, and any error messages.
    """
    from app.services.settings_service import get_all_admin_users, get_all_admin_groups
    
    # Get admin settings from all clusters
    admin_users = get_all_admin_users()
    admin_groups = get_all_admin_groups()
    
    info: Dict[str, Any] = {
        "ok": True,
        "nodes": [],
        "nodes_count": 0,
        "resources_count": 0,
        "resources_sample": [],
        "valid_nodes": [],  # VALID_NODES removed - no longer used
        "admin_users": admin_users,
        "admin_groups": admin_groups,  # Changed from singular to plural
        "error": None,
    }
    
    try:
        from app.services.proxmox_service import get_proxmox_admin
        nodes = get_proxmox_admin().nodes.get() or []
        info["nodes"] = [n.get("node") for n in nodes if isinstance(n, dict) and "node" in n]
        info["nodes_count"] = len(info["nodes"])
    except Exception as e:
        info["ok"] = False
        info["error"] = f"nodes error: {e}"
        logger.exception("probe_proxmox: nodes error")
        return info
    
    try:
        from app.services.proxmox_service import get_proxmox_admin
        resources = get_proxmox_admin().cluster.resources.get(type="vm") or []
        info["resources_count"] = len(resources)
        info["resources_sample"] = [
            {"vmid": r.get("vmid"), "node": r.get("node"), "name": r.get("name")}
            for r in resources[:10]
        ]
    except Exception as e:
        info["ok"] = False
        info["error"] = f"resources error: {e}"
        logger.exception("probe_proxmox: resources error")
        return info
    
    return info

