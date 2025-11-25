from functools import wraps

from flask import session, redirect, url_for, request
from proxmoxer import ProxmoxAPI

from config import PVE_HOST, PVE_VERIFY


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def current_user():
    """
    Returns the logged-in user ID, e.g. 'student1@pve', or None.
    """
    return session.get("user")


def authenticate_proxmox_user(username: str, password: str):
    """
    Try to authenticate a PVE realm user: '<username>@pve'.

    Returns the full user id on success (e.g. 'student1@pve'),
    or None on failure.
    """
    username = (username or "").strip()
    if not username or not password:
        return None

    full_user = f"{username}@pve"
    try:
        prox = ProxmoxAPI(
            PVE_HOST,
            user=full_user,
            password=password,
            verify_ssl=PVE_VERIFY,
        )
        # Simple cheap call to verify credentials
        prox.version.get()
        return full_user
    except Exception:
        return None
