# Proxmox admin API config (defaults can be overriden via env vars)
import os

# Multi-cluster configuration
# Each cluster has: id (internal key), name (display), host, user, password
CLUSTERS = [
    {
        "id": "cluster1",
        "name": "Main Cluster (10.220.15.249)",
        "host": os.getenv("PVE_HOST", "10.220.15.249"),
        "user": os.getenv("PVE_ADMIN_USER", "root@pam"),
        "password": os.getenv("PVE_ADMIN_PASS", "password!"),
        "verify_ssl": os.getenv("PVE_VERIFY", "False").lower() in ("true", "1", "yes"),
    },
    {
        "id": "cluster2",
        "name": "Secondary Cluster (10.220.12.6)",
        "host": "10.220.12.6",
        "user": "root@pam",
        "password": "abc123##",
        "verify_ssl": False,
    },
]

# Load cluster config from JSON file if it exists (overrides defaults above)
CLUSTER_CONFIG_FILE = os.getenv("CLUSTER_CONFIG_FILE") or os.path.join(os.path.dirname(__file__), "clusters.json")
if os.path.exists(CLUSTER_CONFIG_FILE):
    try:
        import json
        with open(CLUSTER_CONFIG_FILE, "r", encoding="utf-8") as f:
            CLUSTERS = json.load(f)
    except Exception as e:
        print(f"Warning: Failed to load cluster config from {CLUSTER_CONFIG_FILE}: {e}")
        # Keep defaults if loading fails

# Legacy single-cluster config (deprecated - kept for backwards compatibility)
PVE_HOST        = os.getenv("PVE_HOST", "10.220.15.249")
PVE_ADMIN_USER  = os.getenv("PVE_ADMIN_USER", "root@pam")  # admin/service account
PVE_ADMIN_PASS  = os.getenv("PVE_ADMIN_PASS", "password!")
# PVE_VERIFY: SSL certificate verification - set to False for self-signed certs
_verify = os.getenv("PVE_VERIFY", "False").lower()
PVE_VERIFY = _verify in ("true", "1", "yes")

# Optional: restrict to these nodes, or set to None for all
# Example: VALID_NODES = ["prox1", "prox2", "prox3"]
VALID_NODES = os.getenv("VALID_NODES")
if VALID_NODES:
    VALID_NODES = [n.strip() for n in VALID_NODES.split(",") if n.strip()]
else:
    VALID_NODES = None

# Admin users (Proxmox usernames) that can see all VMs and use admin pages
ADMIN_USERS = os.getenv("ADMIN_USERS")
if ADMIN_USERS:
    ADMIN_USERS = [u.strip() for u in ADMIN_USERS.split(",") if u.strip()]
else:
    ADMIN_USERS = ["root@pam"]

# Optional Proxmox group whose members are also treated as admins
ADMIN_GROUP = os.getenv("ADMIN_GROUP", "adminers")   # set to None to disable group-based admin

# Where per-user VM mappings are stored (JSON file on disk)
MAPPINGS_FILE = os.getenv("MAPPINGS_FILE") or os.path.join(os.path.dirname(__file__), "mappings.json")

# Cluster configuration file (JSON file on disk)
CLUSTER_CONFIG_FILE = os.getenv("CLUSTER_CONFIG_FILE") or os.path.join(os.path.dirname(__file__), "clusters.json")

# IP address cache file (persistent storage)
IP_CACHE_FILE = os.getenv("IP_CACHE_FILE") or os.path.join(os.path.dirname(__file__), "ip_cache.json")

# VM data cache file (persistent storage for VM list across restarts)
VM_CACHE_FILE = os.getenv("VM_CACHE_FILE") or os.path.join(os.path.dirname(__file__), "vm_cache.json")

# Flask secret
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-session-key")

# VM cache TTL (seconds) – avoids hammering Proxmox API
# 5 minutes is good for student labs; start/stop operations invalidate cache automatically
VM_CACHE_TTL = int(os.getenv("VM_CACHE_TTL", "300"))

# Proxmox API cache TTL (seconds) – short-lived cache for cluster-wide queries
# This is used for flask-caching to cache aggregated VM/container lists
PROXMOX_CACHE_TTL = int(os.getenv("PROXMOX_CACHE_TTL", "30"))

# If True, try to query the guest agent for IP addresses.
# This can be slow on large numbers of VMs.
ENABLE_IP_LOOKUP = True

# If True, save discovered IPs to VM description/notes in Proxmox
# WARNING: This is VERY SLOW - adds 2 API calls per VM (read config + write config)
# Disabled by default for better performance - IPs cached in memory instead
ENABLE_IP_PERSISTENCE = os.getenv("ENABLE_IP_PERSISTENCE", "false").lower() in ("true", "1", "yes")

# Database IP cache TTL (seconds). Used for `VMAssignment.cached_ip` and `VMIPCache`.
# Set large value to persist across restarts. Default: 30 days.
DB_IP_CACHE_TTL = int(os.getenv("DB_IP_CACHE_TTL", "2592000"))

# ARP scanner: broadcast addresses to ping for IP discovery
# Scans 10.220.8.0/21 network (10.220.8.0 - 10.220.15.255)
# Set to subnet broadcast addresses on your network
# Example: ["192.168.1.255", "10.0.0.255"]
ARP_SUBNETS = os.getenv("ARP_SUBNETS")
if ARP_SUBNETS:
    ARP_SUBNETS = [s.strip() for s in ARP_SUBNETS.split(",") if s.strip()]
else:
    # Default: Scan both primary and secondary cluster networks
    # 10.220.8.0/21 network (broadcast: 10.220.15.255) - covers 10.220.8.0 to 10.220.15.255
    # This includes both 10.220.15.x (cluster1) and 10.220.12.x (cluster2)
    ARP_SUBNETS = ["10.220.15.255"]

# Depl0y Integration (removed)

# Template replication at startup (disabled by default)
ENABLE_TEMPLATE_REPLICATION = os.getenv("ENABLE_TEMPLATE_REPLICATION", "false").lower() in ("true", "1", "yes")

# QCOW2 Bulk Cloner Configuration
# Base path for QCOW2 template images (NFS shared storage)
QCOW2_TEMPLATE_PATH = os.getenv("QCOW2_TEMPLATE_PATH", "/mnt/pve/TRUENAS-NFS/images")
# Path for VM overlay images (same NFS storage)
QCOW2_IMAGES_PATH = os.getenv("QCOW2_IMAGES_PATH", "/mnt/pve/TRUENAS-NFS/images")
# Concurrency for parallel VM creation
QCOW2_CLONER_CONCURRENCY = int(os.getenv("QCOW2_CLONER_CONCURRENCY", "5"))
# Batch size for starting VMs
QCOW2_CLONER_START_BATCH = int(os.getenv("QCOW2_CLONER_START_BATCH", "5"))
# Default VM memory in MB
QCOW2_CLONER_DEFAULT_MEMORY = int(os.getenv("QCOW2_CLONER_DEFAULT_MEMORY", "4096"))
# Default VM cores
QCOW2_CLONER_DEFAULT_CORES = int(os.getenv("QCOW2_CLONER_DEFAULT_CORES", "2"))
