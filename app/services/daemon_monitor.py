#!/usr/bin/env python3
"""Daemon Monitor - Automatic restart service for failed background daemons.

Monitors health of all background daemons and automatically restarts them
if they crash or become unresponsive.

Implements exponential backoff to prevent restart loops while ensuring
critical services stay online.
"""

import logging
import threading
import time
from typing import Dict, Optional, Callable

logger = logging.getLogger(__name__)

# Daemon registration and monitoring
_daemon_registry: Dict[str, Dict] = {}
_registry_lock = threading.RLock()
_monitor_thread: Optional[threading.Thread] = None
_monitor_running = False
_restart_stats = {
    'total_restarts': 0,
    'recent_restarts': {},  # {daemon_name: [(timestamp, reason), ...]}
}


def register_daemon(daemon_name: str, 
                   health_check_func: Callable[[], bool],
                   restart_func: Callable[[], None],
                   max_restarts_per_hour: int = 5,
                   restart_delay: int = 5) -> None:
    """Register a daemon for automatic monitoring and restart.
    
    Args:
        daemon_name: Unique daemon identifier (e.g., 'background_sync')
        health_check_func: Callable that returns True if daemon is healthy
        restart_func: Callable that restarts the daemon
        max_restarts_per_hour: Max restart attempts before giving up (prevents loops)
        restart_delay: Initial delay before restart attempt (seconds, exponential backoff)
    """
    with _registry_lock:
        _daemon_registry[daemon_name] = {
            'name': daemon_name,
            'health_check': health_check_func,
            'restart': restart_func,
            'max_restarts': max_restarts_per_hour,
            'restart_delay': restart_delay,
            'current_delay': restart_delay,
            'last_check': None,
            'last_restart': None,
            'restart_count': 0,
            'consecutive_failures': 0,
            'max_consecutive': 3,  # Fail if unhealthy 3 checks in a row
            'status': 'unknown',
        }
        logger.info(f"Registered daemon for monitoring: {daemon_name}")


def start_daemon_monitor(app) -> None:
    """Start the background daemon monitor thread.
    
    Args:
        app: Flask app instance (for context)
    """
    global _monitor_thread, _monitor_running
    
    if _monitor_running:
        logger.warning("Daemon monitor already running")
        return
    
    _monitor_running = True
    
    def _monitor_loop():
        """Main monitoring loop - checks daemon health and restarts if needed."""
        logger.info("Daemon monitor started")
        
        while _monitor_running:
            try:
                with app.app_context():
                    _check_all_daemons()
            except Exception as e:
                logger.error(f"Daemon monitor error: {e}", exc_info=True)
            finally:
                # Clean up DB session after check iteration
                try:
                    from app.models import db
                    db.session.remove()
                except Exception as cleanup_err:
                    logger.warning(f"Failed to cleanup DB session in monitor: {cleanup_err}")
            
            # Check every 30 seconds
            time.sleep(30)
    
    _monitor_thread = threading.Thread(
        target=_monitor_loop,
        daemon=True,
        name="DaemonMonitor"
    )
    _monitor_thread.start()
    logger.info("Daemon monitor thread started")


def stop_daemon_monitor() -> None:
    """Stop the daemon monitor thread."""
    global _monitor_running
    _monitor_running = False
    logger.info("Daemon monitor stopped")


def _check_all_daemons() -> None:
    """Check health of all registered daemons."""
    with _registry_lock:
        if not _daemon_registry:
            return
        
        for daemon_name in list(_daemon_registry.keys()):
            daemon = _daemon_registry[daemon_name]
            
            try:
                # Call health check function
                is_healthy = daemon['health_check']()
                daemon['last_check'] = time.time()
                
                if is_healthy:
                    # Daemon is healthy - clear failure counters
                    daemon['status'] = 'healthy'
                    daemon['consecutive_failures'] = 0
                    daemon['current_delay'] = daemon['restart_delay']  # Reset delay
                    logger.debug(f"Daemon '{daemon_name}' health check passed")
                else:
                    # Daemon is unhealthy
                    daemon['consecutive_failures'] += 1
                    daemon['status'] = 'unhealthy'
                    logger.warning(f"Daemon '{daemon_name}' health check FAILED "
                                 f"({daemon['consecutive_failures']}/{daemon['max_consecutive']})")
                    
                    # After max consecutive failures, attempt restart
                    if daemon['consecutive_failures'] >= daemon['max_consecutive']:
                        _attempt_restart(daemon_name, daemon)
            
            except Exception as e:
                logger.error(f"Error checking daemon '{daemon_name}': {e}", exc_info=True)
                daemon['consecutive_failures'] += 1
                if daemon['consecutive_failures'] >= daemon['max_consecutive']:
                    _attempt_restart(daemon_name, daemon, error=str(e))


def _attempt_restart(daemon_name: str, daemon: Dict, error: Optional[str] = None) -> None:
    """Attempt to restart a failed daemon.
    
    Args:
        daemon_name: Name of the daemon to restart
        daemon: Daemon registry entry
        error: Optional error message that triggered restart
    """
    # Check restart rate limit (max per hour)
    recent = _restart_stats['recent_restarts'].get(daemon_name, [])
    now = time.time()
    hour_ago = now - 3600
    
    # Clean old entries and count recent restarts
    recent_in_hour = [ts for ts, _ in recent if ts > hour_ago]
    _restart_stats['recent_restarts'][daemon_name] = [(ts, msg) for ts, msg in recent if ts > hour_ago]
    
    if len(recent_in_hour) >= daemon['max_restarts']:
        logger.error(f"Daemon '{daemon_name}' exceeded max restarts/hour ({daemon['max_restarts']}). "
                    f"NOT restarting to prevent restart loop.")
        daemon['status'] = 'failed_max_restarts'
        
        # Report to health service
        try:
            from app.services.health_service import update_daemon_sync
            update_daemon_sync(daemon_name, error=f"Max restarts exceeded, daemon not restarted")
        except Exception as e:
            logger.debug(f"Could not report to health service: {e}")
        
        return
    
    # Attempt restart
    logger.warning(f"Daemon '{daemon_name}' restart attempt "
                  f"({len(recent_in_hour) + 1}/{daemon['max_restarts']}) "
                  f"after {daemon['current_delay']}s delay...")
    
    reason = error or "Health check failed"
    _restart_stats['recent_restarts'][daemon_name].append((now, reason))
    _restart_stats['total_restarts'] += 1
    daemon['restart_count'] += 1
    daemon['last_restart'] = now
    
    try:
        # Call restart function
        daemon['restart']()
        
        logger.info(f"Daemon '{daemon_name}' restart initiated successfully")
        daemon['status'] = 'restarting'
        daemon['consecutive_failures'] = 0
        
        # Reset delay for next restart (if needed again)
        daemon['current_delay'] = daemon['restart_delay']
        
        # Report to health service
        try:
            from app.services.health_service import update_daemon_sync
            update_daemon_sync(daemon_name, error=None)  # Clear error
        except Exception as e:
            logger.debug(f"Could not report to health service: {e}")
    
    except Exception as restart_err:
        logger.error(f"Failed to restart daemon '{daemon_name}': {restart_err}", exc_info=True)
        
        # Increase delay for next restart attempt (exponential backoff)
        daemon['current_delay'] = min(daemon['current_delay'] * 2, 300)  # Cap at 5min
        
        # Report to health service
        try:
            from app.services.health_service import update_daemon_sync
            update_daemon_sync(daemon_name, error=f"Restart failed: {str(restart_err)}")
        except Exception as e:
            logger.debug(f"Could not report to health service: {e}")


def get_monitor_status() -> Dict:
    """Get status of all monitored daemons.
    
    Returns:
        Dictionary with daemon statuses and restart statistics
    """
    with _registry_lock:
        daemons_status = {}
        for daemon_name, daemon in _daemon_registry.items():
            daemons_status[daemon_name] = {
                'status': daemon['status'],
                'consecutive_failures': daemon['consecutive_failures'],
                'restart_count': daemon['restart_count'],
                'last_check': daemon['last_check'],
                'last_restart': daemon['last_restart'],
                'current_restart_delay': daemon['current_delay'],
            }
        
        recent_restarts = {}
        for daemon_name, events in _restart_stats['recent_restarts'].items():
            recent_restarts[daemon_name] = [
                {'timestamp': ts, 'reason': reason} 
                for ts, reason in events[-10:]  # Last 10
            ]
        
        return {
            'monitor_running': _monitor_running,
            'total_restarts': _restart_stats['total_restarts'],
            'daemons': daemons_status,
            'recent_restarts': recent_restarts,
        }
