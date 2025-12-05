#!/usr/bin/env python3
"""
Authentication and authorization decorators.
"""

from functools import wraps

from flask import abort, redirect, request, session, url_for


def login_required(f):
    """Decorator that ensures the user is logged in."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("auth.login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """Decorator that ensures the user is logged in and is an admin."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        from app.services.user_manager import is_admin_user, require_user
        user = require_user()
        if not is_admin_user(user):
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def current_user():
    """Returns the logged-in user ID, e.g. 'student1@pve', or None."""
    return session.get("user")
