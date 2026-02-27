#!/usr/bin/env python3
"""Health monitoring service for background daemons and system resources.

Tracks daemon status, last execution times, and resource health.
Provides centralized health check endpoint for monitoring systems.
"""

import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Thread-safe health status tracking
_health_lock = threading.RLock()
_daemon_status = {
    'background_sync': {
        'status': 'stopped',
        'last_sync_time': None,
        'last_full_sync_time': None,
        'sync_error': None,
        'error_count': 0,
    },
    'auto_shutdown': {
        'status': 'stopped',
        'last_check_time': None,
        'check_error': None,
        'error_count': 0,
        'vms_checked': 0,
    },
    'ip_scanner': {
        'status': 'stopped',
        'last_scan_time': None,
        'scan_error': None,
        'error_count': 0,
    },
    'template_sync': {
        'status': 'stopped',
        'last_sync_time': None,
        'last_full_sync_time': None,
        'sync_error': None,
        'error_count': 0,
        'templates_synced': 0,
    },
}

# Resource health tracking
_resource_health = {
    'db_connections': {
        'active': 0,
        'max_size': 10,
        'peak': 0,
    },
    'vm_inventory': {
        'vm_count': 0,
        'last_update': None,
        'is_stale': False,
    },
}


def register_daemon_started(daemon_name: str) -> None:
    """Register that a daemon has started."""
    with _health_lock:
        if daemon_name in _daemon_status:
            _daemon_status[daemon_name]['status'] = 'running'
            logger.info(f"Daemon '{daemon_name}' registered as running")


def register_daemon_stopped(daemon_name: str) -> None:
    """Register that a daemon has stopped."""
    with _health_lock:
        if daemon_name in _daemon_status:
            _daemon_status[daemon_name]['status'] = 'stopped'
            logger.warning(f"Daemon '{daemon_name}' stopped")


def update_daemon_sync(daemon_name: str, full_sync: bool = False, 
                       error: Optional[str] = None, items_processed: int = 0) -> None:
    """Update daemon sync status after execution.
    
    Args:
        daemon_name: Name of the daemon (e.g., 'background_sync')
        full_sync: Whether this was a full sync (vs quick sync)
        error: Error message if sync failed (None if successful)
        items_processed: Number of items processed in this sync
    """
    with _health_lock:
        if daemon_name not in _daemon_status:
            return
        
        now = datetime.utcnow()
        status = _daemon_status[daemon_name]
        status['last_sync_time'] = now.isoformat()
        
        if full_sync:
            status['last_full_sync_time'] = now.isoformat()
        
        if error:
            status['sync_error'] = error
            status['error_count'] = status.get('error_count', 0) + 1
            logger.warning(f"Daemon '{daemon_name}' sync failed: {error}")
        else:
            status['sync_error'] = None
            status['error_count'] = max(0, status.get('error_count', 0) - 1)  # Clear on success
        
        if items_processed > 0:
            status['items_processed'] = items_processed


def update_daemon_check(daemon_name: str, error: Optional[str] = None, 
                       items_processed: int = 0) -> None:
    """Update daemon check status (auto-shutdown style).
    
    Args:
        daemon_name: Name of the daemon
        error: Error message if check failed (None if successful)
        items_processed: Number of items checked
    """
    with _health_lock:
        if daemon_name not in _daemon_status:
            return
        
        now = datetime.utcnow()
        status = _daemon_status[daemon_name]
        status['last_check_time'] = now.isoformat()
        
        if error:
            status['check_error'] = error
            status['error_count'] = status.get('error_count', 0) + 1
            logger.warning(f"Daemon '{daemon_name}' check failed: {error}")
        else:
            status['check_error'] = None
            status['error_count'] = max(0, status.get('error_count', 0) - 1)
        
        if items_processed > 0:
            status['vms_checked'] = items_processed


def update_resource_db_pool(active_connections: int, peak_connections: int, 
                            max_size: int = 10) -> None:
    """Update database connection pool health.
    
    Args:
        active_connections: Current active connections
        peak_connections: Peak connections seen
        max_size: Maximum pool size
    """
    with _health_lock:
        _resource_health['db_connections']['active'] = active_connections
        _resource_health['db_connections']['max_size'] = max_size
        _resource_health['db_connections']['peak'] = peak_connections


def update_resource_vm_inventory(vm_count: int, is_stale: bool = False) -> None:
    """Update VM inventory health."""
    with _health_lock:
        _resource_health['vm_inventory']['vm_count'] = vm_count
        _resource_health['vm_inventory']['last_update'] = datetime.utcnow().isoformat()
        _resource_health['vm_inventory']['is_stale'] = is_stale


def get_health_status() -> Dict[str, Any]:
    """Get comprehensive health status for all daemons and resources.
    
    Returns:
        Dictionary with health information suitable for JSON response
    """
    with _health_lock:
        now = datetime.utcnow()
        
        # Calculate daemon ages (time since last action)
        daemons_info = {}
        for daemon_name, status in _daemon_status.items():
            daemon_info = {
                'status': status['status'],
                'error_count': status.get('error_count', 0),
            }
            
            # Calculate age in seconds for last activity
            if status.get('last_sync_time'):
                last_time = datetime.fromisoformat(status['last_sync_time'])
                age = (now - last_time).total_seconds()
                daemon_info['last_sync'] = status['last_sync_time']
                daemon_info['age_seconds'] = int(age)
                daemon_info['is_stale'] = age > 600  # Stale if no sync for 10+ min
            
            if status.get('last_check_time'):
                last_time = datetime.fromisoformat(status['last_check_time'])
                age = (now - last_time).total_seconds()
                daemon_info['last_check'] = status['last_check_time']
                daemon_info['age_seconds'] = int(age)
                daemon_info['is_stale'] = age > 600
            
            if status.get('sync_error'):
                daemon_info['last_error'] = status['sync_error']
            
            if status.get('check_error'):
                daemon_info['last_error'] = status['check_error']
            
            if status.get('last_full_sync_time'):
                daemon_info['last_full_sync'] = status['last_full_sync_time']
            
            if status.get('items_processed'):
                daemon_info['items_processed'] = status['items_processed']
            
            if status.get('vms_checked'):
                daemon_info['vms_checked'] = status['vms_checked']
            
            daemons_info[daemon_name] = daemon_info
        
        # Check if overall health is OK
        critical_daemons = ['background_sync', 'auto_shutdown', 'ip_scanner']
        any_critical_down = any(
            daemons_info.get(d, {}).get('status') == 'stopped' 
            for d in critical_daemons
        )
        
        any_stale = any(
            daemons_info.get(d, {}).get('is_stale', False) 
            for d in critical_daemons
        )
        
        overall_health = 'ok'
        if any_critical_down:
            overall_health = 'degraded'
        if any_stale:
            overall_health = 'degraded'
        
        return {
            'status': 'healthy' if overall_health == 'ok' else 'degraded',
            'overall_health': overall_health,
            'timestamp': now.isoformat(),
            'daemons': daemons_info,
            'resources': {
                'db_pool': {
                    'active': _resource_health['db_connections']['active'],
                    'max_size': _resource_health['db_connections']['max_size'],
                    'peak': _resource_health['db_connections']['peak'],
                    'utilization_percent': int(
                        (_resource_health['db_connections']['active'] / 
                         max(_resource_health['db_connections']['max_size'], 1)) * 100
                    ),
                },
                'vm_inventory': {
                    'vm_count': _resource_health['vm_inventory']['vm_count'],
                    'last_update': _resource_health['vm_inventory']['last_update'],
                    'is_stale': _resource_health['vm_inventory']['is_stale'],
                },
            },
        }


def is_daemon_healthy(daemon_name: str, max_age_seconds: int = 600) -> bool:
    """Check if a specific daemon is healthy (running and recent activity).
    
    Args:
        daemon_name: Name of the daemon to check
        max_age_seconds: Maximum age of last activity (default 10 min)
    
    Returns:
        True if daemon is running and recent, False otherwise
    """
    status = get_health_status()
    daemon_info = status.get('daemons', {}).get(daemon_name, {})
    
    if daemon_info.get('status') != 'running':
        return False
    
    age = daemon_info.get('age_seconds', float('inf'))
    return age <= max_age_seconds


def reset_health_status() -> None:
    """Reset all health status (for testing/restart). Non-production use only."""
    with _health_lock:
        for daemon in _daemon_status:
            _daemon_status[daemon]['status'] = 'stopped'
            _daemon_status[daemon]['error_count'] = 0
        logger.warning("Health status reset (non-production operation)")
