#!/usr/bin/env python3
"""
Settings service - Database-first configuration management.

Replaces config.py for all runtime settings. Settings are stored in:
- Cluster table: Per-cluster settings (storage, admin groups, IP discovery, cache TTLs)
- SystemSettings table: Global settings (not yet implemented)

All settings have sensible defaults if not configured.
"""

import logging
import os

logger = logging.getLogger(__name__)

# ============================================================================
# Cluster Settings (from database)
# ============================================================================

def get_cluster_setting(cluster_dict, key, default=None):
    """Get a setting from cluster dictionary with fallback to default.
    
    Args:
        cluster_dict: Cluster.to_dict() output
        key: Setting key (e.g., 'vm_cache_ttl', 'admin_group')
        default: Default value if not set
        
    Returns:
        Setting value or default
    """
    value = cluster_dict.get(key)
    return value if value is not None else default


def get_admin_users(cluster_dict):
    """Get admin users list for a cluster.
    
    Returns list of admin usernames from cluster.admin_users (comma-separated)
    or falls back to ['root@pam'] if not configured.
    """
    admin_users_str = cluster_dict.get('admin_users', '')
    if admin_users_str:
        return [u.strip() for u in admin_users_str.split(',') if u.strip()]
    return ['root@pam']  # Default


def get_admin_group(cluster_dict):
    """Get admin group name for a cluster.
    
    Returns cluster.admin_group or None if not configured.
    """
    return cluster_dict.get('admin_group')


def get_arp_subnets(cluster_dict):
    """Get ARP broadcast subnets for a cluster.
    
    Returns list of broadcast addresses from cluster.arp_subnets (comma-separated)
    or falls back to ['10.220.15.255'] if not configured.
    """
    arp_subnets_str = cluster_dict.get('arp_subnets', '')
    if arp_subnets_str:
        return [s.strip() for s in arp_subnets_str.split(',') if s.strip()]
    return ['10.220.15.255']  # Default for 10.220.8.0/21 network


def get_vm_cache_ttl(cluster_dict):
    """Get VM cache TTL for a cluster.
    
    Returns cluster.vm_cache_ttl or 300 (5 minutes) if not configured.
    """
    ttl = cluster_dict.get('vm_cache_ttl')
    return ttl if ttl is not None else 300


def get_qcow2_paths(cluster_dict):
    """Get QCOW2 storage paths for a cluster.
    
    Returns tuple of (template_path, images_path) or defaults if not configured.
    """
    template_path = cluster_dict.get('qcow2_template_path') or '/mnt/pve/TRUENAS-NFS/images'
    images_path = cluster_dict.get('qcow2_images_path') or '/mnt/pve/TRUENAS-NFS/images'
    return template_path, images_path


def get_ip_lookup_settings(cluster_dict):
    """Get IP lookup settings for a cluster.
    
    Returns tuple of (enable_ip_lookup, enable_ip_persistence).
    """
    enable_lookup = cluster_dict.get('enable_ip_lookup', True)
    enable_persistence = cluster_dict.get('enable_ip_persistence', False)
    return enable_lookup, enable_persistence


# ============================================================================
# Global Settings (fallback to environment variables)
# ============================================================================

def get_secret_key():
    """Get Flask SECRET_KEY from environment or default (INSECURE!)."""
    return os.getenv("SECRET_KEY", "change-me-session-key")


def get_proxmox_cache_ttl():
    """Get Proxmox API cache TTL in seconds."""
    return int(os.getenv("PROXMOX_CACHE_TTL", "30"))


def get_db_ip_cache_ttl():
    """Get database IP cache TTL in seconds (30 days default)."""
    return int(os.getenv("DB_IP_CACHE_TTL", "2592000"))


# ============================================================================
# Helper Functions
# ============================================================================

def get_all_admin_users():
    """Get admin users from all active clusters.
    
    Returns set of all admin usernames across all clusters.
    """
    try:
        from app.models import Cluster
        from flask import current_app
        
        if current_app and Cluster.query:
            clusters = Cluster.query.filter_by(is_active=True).all()
            admin_users = set()
            for cluster in clusters:
                cluster_dict = cluster.to_dict()
                admin_users.update(get_admin_users(cluster_dict))
            return admin_users
    except Exception:  # noqa: E722
        pass
    
    # Fallback
    return {'root@pam'}


def get_all_admin_groups():
    """Get admin groups from all active clusters.
    
    Returns set of all admin group names across all clusters.
    """
    try:
        from app.models import Cluster
        from flask import current_app
        
        if current_app and Cluster.query:
            clusters = Cluster.query.filter_by(is_active=True).all()
            admin_groups = set()
            for cluster in clusters:
                cluster_dict = cluster.to_dict()
                group = get_admin_group(cluster_dict)
                if group:
                    admin_groups.add(group)
            return admin_groups
    except Exception:  # noqa: E722
        pass
    
    # Fallback
    return {'adminers'}
