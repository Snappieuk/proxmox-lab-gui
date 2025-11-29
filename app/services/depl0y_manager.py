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
DEPL0Y_PORT = 3000
DEPL0Y_BACKEND_PORT = 8000


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


def is_depl0y_running() -> bool:
    """Check if Depl0y is already running."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex(('localhost', DEPL0Y_PORT))
            return result == 0
    except Exception:
        return False


def get_depl0y_url() -> str:
    """Get the URL where Depl0y is running."""
    if is_depl0y_running():
        # Try to get the actual hostname
        try:
            hostname = socket.gethostname()
            # Try to get IP address
            import subprocess
            result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                ip = result.stdout.strip().split()[0]
                return f"http://{ip}:{DEPL0Y_PORT}"
        except:
            pass
        return f"http://localhost:{DEPL0Y_PORT}"
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
        
        # Install backend dependencies
        backend_dir = DEPL0Y_DIR / "backend"
        if backend_dir.exists():
            logger.info("Installing Depl0y backend dependencies...")
            subprocess.run(
                ['pip', 'install', '-r', 'requirements.txt'],
                cwd=backend_dir,
                check=True,
                capture_output=True,
                timeout=600
            )
        
        # Install frontend dependencies (if Node.js available)
        frontend_dir = DEPL0Y_DIR / "frontend"
        if frontend_dir.exists():
            try:
                logger.info("Installing Depl0y frontend dependencies...")
                subprocess.run(
                    ['npm', 'install'],
                    cwd=frontend_dir,
                    check=True,
                    capture_output=True,
                    timeout=600
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
        
        backend_dir = DEPL0Y_DIR / "backend"
        if not backend_dir.exists():
            logger.error(f"Depl0y backend directory not found: {backend_dir}")
            return False
        
        logger.info("Starting Depl0y backend...")
        
        # Start backend (uvicorn main:app)
        subprocess.Popen(
            ['uvicorn', 'main:app', '--host', '0.0.0.0', '--port', str(DEPL0Y_BACKEND_PORT)],
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
            subprocess.Popen(
                ['npm', 'run', 'dev', '--', '--port', str(DEPL0Y_PORT), '--host'],
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
        return f"http://localhost:{DEPL0Y_PORT}"  # Return expected URL immediately
    else:
        return _init()
