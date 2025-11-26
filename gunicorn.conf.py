# Gunicorn configuration file
import multiprocessing
import os

# Server socket
bind = "0.0.0.0:8080"
backlog = 2048

# Worker processes - use single worker with threads to avoid fork issues entirely
workers = 1
worker_class = "gthread"
threads = multiprocessing.cpu_count() * 4  # Compensate with more threads per worker
worker_connections = 1000
timeout = 30
keepalive = 2

# Logging
accesslog = "-"  # Log to stdout
errorlog = "-"   # Log to stderr
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'

# Process naming
proc_name = "proxmox-lab-gui"

# Server mechanics
daemon = False
pidfile = None
umask = 0
user = None
group = None
tmp_upload_dir = None

# Disable preloading to avoid SSL connection fork issues
preload_app = False

# SSL (uncomment and configure if needed)
# keyfile = "/path/to/key.pem"
# certfile = "/path/to/cert.pem"
