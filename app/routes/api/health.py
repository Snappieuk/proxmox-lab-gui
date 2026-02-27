#!/usr/bin/env python3
"""Health check API endpoints for monitoring daemon and system health."""

import logging
from flask import Blueprint, jsonify

from app.services.health_service import get_health_status, is_daemon_healthy
from app.utils.decorators import admin_required

logger = logging.getLogger(__name__)

health_bp = Blueprint('health_api', __name__)


@health_bp.route("/api/health/daemons", methods=["GET"])
@admin_required
def api_health_daemons():
    """Get comprehensive health status of all background daemons.
    
    Admin-only endpoint for monitoring system health.
    Returns status of background sync, auto-shutdown, IP scanner, and template sync daemons.
    Also includes resource health (DB connections, VM inventory).
    """
    try:
        status = get_health_status()
        return jsonify({
            "ok": True,
            "health": status,
        }), 200
    except Exception as e:
        logger.exception(f"Failed to get health status: {e}")
        return jsonify({
            "ok": False,
            "error": f"Failed to retrieve health status: {str(e)}",
        }), 500


@health_bp.route("/api/health/daemons/<daemon_name>", methods=["GET"])
@admin_required
def api_health_daemon_specific(daemon_name: str):
    """Get health status of a specific daemon.
    
    Args:
        daemon_name: Name of daemon (background_sync, auto_shutdown, ip_scanner, template_sync)
    
    Returns:
        Daemon-specific health information
    """
    try:
        status = get_health_status()
        daemon_status = status.get("daemons", {}).get(daemon_name)
        
        if daemon_status is None:
            return jsonify({
                "ok": False,
                "error": f"Unknown daemon: {daemon_name}",
            }), 404
        
        is_healthy = is_daemon_healthy(daemon_name)
        
        return jsonify({
            "ok": True,
            "daemon": daemon_name,
            "status": daemon_status,
            "healthy": is_healthy,
        }), 200
    except Exception as e:
        logger.exception(f"Failed to get daemon health: {e}")
        return jsonify({
            "ok": False,
            "error": f"Failed to retrieve daemon health: {str(e)}",
        }), 500


@health_bp.route("/api/health/summary", methods=["GET"])
def api_health_summary():
    """Get quick health summary (public endpoint, no auth required).
    
    Returns minimal health info suitable for monitoring systems (Prometheus, Datadog, etc).
    """
    try:
        status = get_health_status()
        
        # Extract critical info for monitoring
        return jsonify({
            "ok": True,
            "status": status["status"],
            "overall_health": status["overall_health"],
            "timestamp": status["timestamp"],
            "critical_daemons_up": sum(
                1 for d in status.get("daemons", {}).values() 
                if d.get("status") == "running"
            ),
        }), 200
    except Exception as e:
        logger.exception(f"Failed to get health summary: {e}")
        return jsonify({
            "ok": False,
            "error": str(e),
        }), 500
