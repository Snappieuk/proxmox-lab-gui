# Proxmox admin API config (defaults can be overriden via env vars)
import os

PVE_HOST        = os.getenv("PVE_HOST", "10.220.15.249")
PVE_ADMIN_USER  = os.getenv("PVE_ADMIN_USER", "root@pam")  # admin/service account
PVE_ADMIN_PASS  = os.getenv("PVE_ADMIN_PASS", "password!")
PVE_VERIFY      = bool(os.getenv("PVE_VERIFY", "False").lower() in ("true", "1", "yes"))

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

# Flask secret
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-session-key")

# VM cache TTL (seconds) – avoids hammering Proxmox API
VM_CACHE_TTL = int(os.getenv("VM_CACHE_TTL", "120"))

# If True, try to query the guest agent for IP addresses.
# This can be slow on large numbers of VMs.
ENABLE_IP_LOOKUP = True
