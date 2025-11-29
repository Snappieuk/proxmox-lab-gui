"""API routes for background sync service management."""

from flask import Blueprint, jsonify
from app.utils.decorators import login_required, admin_required

bp = Blueprint("api_sync", __name__, url_prefix="/api/sync")


@bp.route("/stats", methods=["GET"])
@login_required
def get_stats():
    """Get background sync service statistics."""
    from app.services.background_sync import get_sync_stats
    
    stats = get_sync_stats()
    return jsonify(stats)


@bp.route("/trigger", methods=["POST"])
@admin_required
def trigger_sync():
    """Manually trigger an immediate full sync (admin only)."""
    from app.services.background_sync import trigger_immediate_sync
    
    trigger_immediate_sync()
    return jsonify({"ok": True, "message": "Full sync triggered"})


@bp.route("/health", methods=["GET"])
@login_required
def health_check():
    """Check if background sync service is running properly."""
    from app.services.background_sync import get_sync_stats
    from datetime import datetime
    import time
    
    stats = get_sync_stats()
    
    # Determine health status
    is_healthy = True
    issues = []
    
    if not stats['is_running']:
        is_healthy = False
        issues.append("Sync service is not running")
    
    if stats['consecutive_errors'] > 3:
        is_healthy = False
        issues.append(f"Multiple consecutive errors: {stats['consecutive_errors']}")
    
    # Check if last sync was too long ago
    now = time.time()
    if stats['last_full_sync'] > 0:
        time_since_full_sync = now - stats['last_full_sync']
        if time_since_full_sync > 600:  # 10 minutes
            is_healthy = False
            issues.append(f"Last full sync was {int(time_since_full_sync)}s ago")
    
    if stats['error_vms'] > (stats['total_vms'] * 0.2):  # More than 20% errors
        is_healthy = False
        issues.append(f"{stats['error_vms']} VMs have sync errors")
    
    return jsonify({
        "healthy": is_healthy,
        "issues": issues,
        "stats": stats
    })
