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
    return session.get("user")  # e.g. "student1@pve"


def authenticate_proxmox_user(username: str, password: str):
    """
    Only 'pve' realm. Returns 'user@pve' if OK, else None.
    """
    full_user = f"{username}@pve"
    try:
        prox = ProxmoxAPI(
            PVE_HOST,
            user=full_user,
            password=password,
            verify_ssl=PVE_VERIFY,
        )
        prox.version.get()
        return full_user
    except Exception:
        return None

