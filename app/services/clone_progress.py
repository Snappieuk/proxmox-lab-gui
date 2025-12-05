#!/usr/bin/env python3
"""
Progress tracking for VM cloning operations.
Uses in-memory dict to track clone progress for different tasks.
"""

import threading
from datetime import datetime, timedelta
from typing import Any, Dict

# Global progress tracker: task_id -> progress_data
_clone_progress: Dict[str, Dict[str, Any]] = {}
_progress_lock = threading.Lock()

# Auto-cleanup old entries after 1 hour
_CLEANUP_AGE = timedelta(hours=1)


def start_clone_progress(task_id: str, total_count: int) -> None:
    """Initialize progress tracking for a clone task."""
    with _progress_lock:
        _clone_progress[task_id] = {
            "total": total_count,
            "completed": 0,
            "failed": 0,
            "current_vm": None,
            "status": "in_progress",
            "started_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "errors": [],
            "message": None,
            "progress_percent": 0
        }


def update_clone_progress(task_id: str, completed: int = None, failed: int = None, 
                         current_vm: str = None, status: str = None, error: str = None,
                         message: str = None, progress_percent: float = None) -> None:
    """Update progress for a clone task."""
    with _progress_lock:
        if task_id not in _clone_progress:
            return
        
        progress = _clone_progress[task_id]
        
        if completed is not None:
            progress["completed"] = completed
        if failed is not None:
            progress["failed"] = failed
        if current_vm is not None:
            progress["current_vm"] = current_vm
        if status is not None:
            progress["status"] = status
        if error is not None:
            progress["errors"].append(error)
        if message is not None:
            progress["message"] = message
        if progress_percent is not None:
            progress["progress_percent"] = min(100, max(0, progress_percent))
        
        progress["updated_at"] = datetime.utcnow()


def get_clone_progress(task_id: str) -> Dict[str, Any]:
    """Get current progress for a clone task."""
    with _progress_lock:
        return _clone_progress.get(task_id, {
            "status": "not_found",
            "total": 0,
            "completed": 0,
            "failed": 0
        })


def cleanup_old_progress() -> None:
    """Remove progress entries older than 1 hour."""
    with _progress_lock:
        now = datetime.utcnow()
        to_remove = []
        for task_id, data in _clone_progress.items():
            if now - data["updated_at"] > _CLEANUP_AGE:
                to_remove.append(task_id)
        
        for task_id in to_remove:
            del _clone_progress[task_id]
