# Proxmox admin API config
PVE_HOST        = "10.220.15.249"
PVE_ADMIN_USER  = "root@pam"          # admin/service account
PVE_ADMIN_PASS  = "password!"
PVE_VERIFY      = False               # set True if you use valid TLS certs

# Optional: restrict to these nodes, or set to None for all
# Example: VALID_NODES = ["prox1", "prox2", "prox3"]
VALID_NODES = None

# Admin users (Proxmox usernames) that can see all VMs and use admin pages
ADMIN_USERS = [
    "root@pam",
]

# Optional Proxmox group whose members are also treated as admins
ADMIN_GROUP = "adminers"   # set to None to disable group-based admin

# Where per-user VM mappings are stored (JSON file on disk)
MAPPINGS_FILE = "mappings.json"

# Flask secret
SECRET_KEY = "change-me-session-key"

# VM cache TTL (seconds) – avoids hammering Proxmox API
VM_CACHE_TTL = 120

# If True, try to query the guest agent for IP addresses.
# This can be slow on large numbers of VMs.
ENABLE_IP_LOOKUP = False
