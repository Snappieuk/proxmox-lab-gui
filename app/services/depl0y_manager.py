#!/usr/bin/env python3
"""
Depl0y Integration Manager

Automatically installs, runs, and manages Depl0y as a companion service.
"""

import logging
import os
import subprocess
import time
import socket
from pathlib import Path

logger = logging.getLogger(__name__)

# Configuration
DEPL0Y_DIR = Path(__file__).parent.parent.parent / "depl0y"
DEPL0Y_REPO = "https://github.com/agit8or1/Depl0y.git"
# Frontend defaults to 80 for Depl0y; allow override via env
DEPL0Y_PORT = int(os.getenv("DEPL0Y_FRONTEND_PORT", "80"))
DEPL0Y_BACKEND_PORT = int(os.getenv("DEPL0Y_BACKEND_PORT", "8000"))


def find_available_port(start_port: int, max_attempts: int = 10) -> int:
    """Find an available port starting from start_port."""
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('', port))
                return port
            except OSError:
                continue
    return start_port


def is_depl0y_running(host: str = '127.0.0.1') -> bool:
    """Check if Depl0y is already running."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex((host, DEPL0Y_PORT))
            return result == 0
    except Exception:
        return False


def _detect_host_ip() -> str:
    """Detect the server's primary IP address reliably (non-loopback)."""
    # Prefer explicit env override
    host_env = os.getenv('HOST')
    if host_env and host_env not in ('0.0.0.0', '127.0.0.1', 'localhost'):
        return host_env
    try:
        # UDP connect trick to determine outbound interface IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
    except Exception:
        pass
    try:
        # Fallback to hostname resolution
        ip = socket.gethostbyname(socket.gethostname())
        if ip and ip != '127.0.0.1':
            return ip
    except Exception:
        pass
    # Last resort
    return '127.0.0.1'


def get_depl0y_url() -> str:
    """Get the URL where Depl0y is running."""
    host_ip = _detect_host_ip()
    # Check on detected host IP first, then localhost
    if is_depl0y_running(host_ip):
        if DEPL0Y_PORT == 80:
            return f"http://{host_ip}"
        return f"http://{host_ip}:{DEPL0Y_PORT}"
    if is_depl0y_running('127.0.0.1'):
        if DEPL0Y_PORT == 80:
            return f"http://{host_ip}"
        return f"http://{host_ip}:{DEPL0Y_PORT}"
    return None


def install_depl0y() -> bool:
    """Clone and install Depl0y if not present."""
    try:
        if DEPL0Y_DIR.exists():
            logger.info(f"Depl0y directory already exists at {DEPL0Y_DIR}")
            return True
        
        logger.info(f"Cloning Depl0y from {DEPL0Y_REPO}...")
        subprocess.run(
            ['git', 'clone', DEPL0Y_REPO, str(DEPL0Y_DIR)],
            check=True,
            capture_output=True,
            timeout=300
        )
        logger.info("Depl0y cloned successfully")

        # Preferred: use Depl0y's own installer if available
        install_sh = DEPL0Y_DIR / "install.sh"
        if install_sh.exists():
            try:
                logger.info("Running Depl0y's install.sh script...")
                # Provide ports and host via env; script should be idempotent
                env = os.environ.copy()
                env.update({
                    "HOST": "0.0.0.0",
                    "DEPL0Y_FRONTEND_PORT": str(DEPL0Y_PORT),
                    "DEPL0Y_BACKEND_PORT": str(DEPL0Y_BACKEND_PORT),
                })
                subprocess.run(
                    ['bash', str(install_sh)],
                    cwd=DEPL0Y_DIR,
                    env=env,
                    check=True,
                    timeout=1800
                )
                logger.info("Depl0y install.sh finished successfully")
                return True
            except subprocess.CalledProcessError as e:
                logger.warning(f"install.sh failed, falling back to manual install: {e}")

        # Fallback: manual install (backend + frontend deps)
        backend_dir = DEPL0Y_DIR / "backend"
        if backend_dir.exists():
            logger.info("Installing Depl0y backend dependencies (fallback)...")
            subprocess.run(
                ['pip', 'install', '-r', 'requirements.txt'],
                cwd=backend_dir,
                check=True,
                timeout=900
            )

        frontend_dir = DEPL0Y_DIR / "frontend"
        if frontend_dir.exists():
            try:
                logger.info("Installing Depl0y frontend dependencies (fallback)...")
                subprocess.run(
                    ['npm', 'install'],
                    cwd=frontend_dir,
                    check=True,
                    timeout=900
                )
            except (subprocess.CalledProcessError, FileNotFoundError):
                logger.warning("npm not available, skipping frontend build")

        return True
        
    except subprocess.TimeoutExpired:
        logger.error("Depl0y installation timed out")
        return False
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to install Depl0y: {e.stderr.decode() if e.stderr else str(e)}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error installing Depl0y: {e}")
        return False


def start_depl0y_backend() -> bool:
    """Start Depl0y backend service in the background."""
    try:
        if is_depl0y_running():
            logger.info("Depl0y is already running")
            return True

        # If install.sh created a start script or process manager, prefer that
        start_sh = DEPL0Y_DIR / "start.sh"
        if start_sh.exists():
            try:
                logger.info("Starting Depl0y via start.sh...")
                env = os.environ.copy()
                env.update({
                    "HOST": "0.0.0.0",
                    "DEPL0Y_FRONTEND_PORT": str(DEPL0Y_PORT),
                    "DEPL0Y_BACKEND_PORT": str(DEPL0Y_BACKEND_PORT),
                })
                subprocess.Popen(
                    ['bash', str(start_sh)],
                    cwd=DEPL0Y_DIR,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
                time.sleep(4)
            except Exception as e:
                logger.warning(f"start.sh failed, falling back to manual start: {e}")

        backend_dir = DEPL0Y_DIR / "backend"
        if not backend_dir.exists():
            logger.error(f"Depl0y backend directory not found: {backend_dir}")
            return False
        
        logger.info("Starting Depl0y backend...")
        
        # Start backend (uvicorn main:app) with robust fallback
        try:
            subprocess.Popen(
                ['uvicorn', 'main:app', '--host', '0.0.0.0', '--port', str(DEPL0Y_BACKEND_PORT)],
                cwd=backend_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
        except FileNotFoundError:
            import sys
            subprocess.Popen(
                [sys.executable, '-m', 'uvicorn', 'main:app', '--host', '0.0.0.0', '--port', str(DEPL0Y_BACKEND_PORT)],
                cwd=backend_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
        
        # Give it a moment to start
        time.sleep(2)
        
        # Start frontend (if available)
        frontend_dir = DEPL0Y_DIR / "frontend"
        if frontend_dir.exists() and (frontend_dir / "node_modules").exists():
            logger.info("Starting Depl0y frontend...")
            # If port is 80, use preview/build if available; otherwise dev server
            cmd = ['npm', 'run', 'dev', '--', '--port', str(DEPL0Y_PORT), '--host']
            if DEPL0Y_PORT == 80:
                # Prefer preview on 80 to serve built assets
                cmd = ['npm', 'run', 'preview', '--', '--port', str(DEPL0Y_PORT), '--host']
            subprocess.Popen(
                cmd,
                cwd=frontend_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            time.sleep(3)
        
        # Verify it started
        if is_depl0y_running():
            logger.info(f"Depl0y started successfully on port {DEPL0Y_PORT}")
            return True
        else:
            logger.warning("Depl0y process started but not responding on port")
            return False
            
    except Exception as e:
        logger.error(f"Failed to start Depl0y: {e}")
        return False


def initialize_depl0y(background: bool = True) -> str:
    """
    Initialize Depl0y: install if needed, start if not running.
    
    Args:
        background: If True, run in background thread
        
    Returns:
        URL of running Depl0y instance or None
    """
    def _init():
        try:
            # Check if already running
            url = get_depl0y_url()
            if url:
                logger.info(f"Depl0y already running at {url}")
                return url
            
            # Install if needed
            if not DEPL0Y_DIR.exists():
                logger.info("Depl0y not found, installing...")
                if not install_depl0y():
                    logger.error("Failed to install Depl0y")
                    return None
            
            # Start service
            if start_depl0y_backend():
                url = get_depl0y_url()
                logger.info(f"Depl0y initialized at {url}")
                return url
            else:
                logger.error("Failed to start Depl0y")
                return None
                
        except Exception as e:
            logger.exception(f"Error initializing Depl0y: {e}")
            return None
    
    if background:
        import threading
        thread = threading.Thread(target=_init, daemon=True, name="Depl0yInit")
        thread.start()
        # In background mode, avoid returning a localhost placeholder; caller should update later
        return None
    else:
        return _init()
