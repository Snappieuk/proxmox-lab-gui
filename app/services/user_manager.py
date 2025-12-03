#!/usr/bin/env python3
"""
User Manager Service - User authentication and authorization.

This module handles user authentication, admin checking, and user management.
"""

import re
import logging
import threading
from typing import Dict, List, Any, Optional

from flask import session, abort


from app.config import ADMIN_USERS, ADMIN_GROUP, CLUSTERS
from proxmoxer import ProxmoxAPI

logger = logging.getLogger(__name__)

# Admin group membership cache
_admin_group_cache: Optional[List[str]] = None
_admin_group_ts: float = 0.0
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
    cluster_id = session.get("cluster_id", CLUSTERS[0]["id"])
    cluster = next((c for c in CLUSTERS if c["id"] == cluster_id), CLUSTERS[0])
    
    # If username already has realm suffix, try as-is
    if '@' in username:
        try:
            prox = ProxmoxAPI(
                cluster["host"],
                user=username,
                password=password,
                verify_ssl=cluster.get("verify_ssl", False),
            )
            prox.version.get()
            logger.debug("Proxmox auth succeeded for %s", username)
            return username
        except Exception as e:
            logger.debug("Proxmox auth failed for %s on cluster %s: %s", username, cluster["name"], e)
            return None
    
    # Try with @pve realm for bare username (backwards compatibility)
    full_user = f"{username}@pve"
    try:
        prox = ProxmoxAPI(
            cluster["host"],
            user=full_user,
            password=password,
            verify_ssl=cluster.get("verify_ssl", False),
        )
        prox.version.get()
        logger.debug("Proxmox auth succeeded for %s", full_user)
        return full_user
    except Exception as e:
        logger.debug("Proxmox auth failed for %s on cluster %s: %s", full_user, cluster["name"], e)
        return None


def _get_admin_group_members_cached() -> List[str]:
    """Get admin group members from cache or fetch from Proxmox if cache expired."""
    global _admin_group_cache, _admin_group_ts
    
    if not ADMIN_GROUP:
        return []
    
    import time
    now = time.time()
    
    # Check cache first (thread-safe read)
    with _admin_group_lock:
        if _admin_group_cache is not None and (now - _admin_group_ts) < ADMIN_GROUP_CACHE_TTL:
            logger.debug("Using cached admin group members (age=%.1fs)", now - _admin_group_ts)
            return list(_admin_group_cache)
    
    # Cache miss or expired - fetch from Proxmox
    try:
        from app.services.proxmox_service import get_proxmox_admin
        grp = get_proxmox_admin().access.groups(ADMIN_GROUP).get()
        raw_members = (grp.get("members", []) or []) if isinstance(grp, dict) else []
        members = []
        for m in raw_members:
            if isinstance(m, str):
                members.append(m)
            elif isinstance(m, dict) and "userid" in m:
                members.append(m["userid"])
        
        # Update cache (thread-safe write)
        with _admin_group_lock:
            _admin_group_cache = members
            _admin_group_ts = now
        logger.debug("Refreshed admin group cache: %d members", len(members))
        return members
        
    except Exception as e:
        logger.warning("Failed to get admin group members: %s", e)
        # On error, return cached value if we have one (even if expired)
        with _admin_group_lock:
            if _admin_group_cache is not None:
                logger.debug("Using stale admin group cache due to error")
                return list(_admin_group_cache)
        return []


def _user_in_group(user: str, groupid: str) -> bool:
    """Check if user is in a Proxmox group."""
    if not groupid:
        return False

    # Use cached admin group members for the admin group
    if groupid == ADMIN_GROUP:
        members = _get_admin_group_members_cached()
    else:
        # For other groups, fetch directly (no caching)
        try:
            from app.services.proxmox_service import get_proxmox_admin
            data = get_proxmox_admin().access.groups(groupid).get()
            if not data:
                return False
        except Exception:
            return False

        raw_members = data.get("members", []) or []
        members = []
        for m in raw_members:
            if isinstance(m, str):
                members.append(m)
            elif isinstance(m, dict) and "userid" in m:
                members.append(m["userid"])

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
    1. User authenticated via Proxmox (any Proxmox user = admin)
    2. Local database user with role='adminer'
    """
    if not user:
        return False
    
    # Check if user has Proxmox realm suffix (authenticated via Proxmox)
    if '@' in user and (user.endswith('@pve') or user.endswith('@pam')):
        logger.debug("is_admin_user(%s) -> True (Proxmox user)", user)
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
    """Get list of users in the admin group."""
    return _get_admin_group_members_cached()


def _invalidate_admin_group_cache() -> None:
    """Invalidate the admin group cache to force a refresh on next access."""
    global _admin_group_cache, _admin_group_ts
    with _admin_group_lock:
        _admin_group_cache = None
        _admin_group_ts = 0.0
    logger.debug("Invalidated admin group cache")


def add_user_to_admin_group(user: str) -> bool:
    """Add a user to the admin group in Proxmox."""
    if not ADMIN_GROUP:
        logger.warning("ADMIN_GROUP not configured, cannot add user")
        return False
    
    try:
        from app.services.proxmox_service import get_proxmox_admin
        
        # Get current group info
        current_members = get_admin_group_members()
        if user in current_members:
            logger.info("User %s already in admin group", user)
            return True
        
        current_members.append(user)
        
        # Get current group to fetch comment
        try:
            group_info = get_proxmox_admin().access.groups(ADMIN_GROUP).get()
            comment = group_info.get('comment', '') if group_info else ''
        except:
            comment = ''
        
        get_proxmox_admin().access.groups(ADMIN_GROUP).put(
            comment=comment,
            members=",".join(current_members)
        )
        _invalidate_admin_group_cache()
        logger.info("Added user %s to admin group %s", user, ADMIN_GROUP)
        return True
    except Exception as e:
        logger.exception("Failed to add user %s to admin group: %s", user, e)
        return False


def remove_user_from_admin_group(user: str) -> bool:
    """Remove a user from the admin group in Proxmox."""
    if not ADMIN_GROUP:
        logger.warning("ADMIN_GROUP not configured, cannot remove user")
        return False
    
    try:
        from app.services.proxmox_service import get_proxmox_admin
        
        current_members = get_admin_group_members()
        if user not in current_members:
            logger.info("User %s not in admin group", user)
            return True
        
        current_members.remove(user)
        
        # Get current group to fetch comment
        try:
            group_info = get_proxmox_admin().access.groups(ADMIN_GROUP).get()
            comment = group_info.get('comment', '') if group_info else ''
        except:
            comment = ''
        
        get_proxmox_admin().access.groups(ADMIN_GROUP).put(
            comment=comment,
            members=",".join(current_members)
        )
        _invalidate_admin_group_cache()
        logger.info("Removed user %s from admin group %s", user, ADMIN_GROUP)
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
    from app.config import VALID_NODES
    
    info: Dict[str, Any] = {
        "ok": True,
        "nodes": [],
        "nodes_count": 0,
        "resources_count": 0,
        "resources_sample": [],
        "valid_nodes": VALID_NODES,
        "admin_users": ADMIN_USERS,
        "admin_group": ADMIN_GROUP,
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

