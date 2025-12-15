#!/usr/bin/env python3
"""
DEPRECATED Configuration File

All settings have been moved to the database for web-based management.

Configure via /admin/settings UI:
- Cluster connections (host, user, password)
- Storage paths (QCOW2 paths, default storage)
- Admin access (admin groups, admin users)
- IP discovery (ARP subnets, enable IP lookup)
- Cache settings (VM cache TTL)

The only setting that remains in environment variables is SECRET_KEY.
Set SECRET_KEY in .env file or environment variable.
"""

import os

# ============================================================================
# Flask SECRET_KEY (Development default provided, change in production!)
# ============================================================================

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")

# Warn if using default secret key
if SECRET_KEY == "dev-secret-key-change-in-production":
    import warnings
    warnings.warn(
        "Using default SECRET_KEY! This is INSECURE for production.\n"
        "Set SECRET_KEY in .env file or run: export SECRET_KEY=$(openssl rand -hex 32)",
        RuntimeWarning,
        stacklevel=2
    )

# ============================================================================
# Optional: Global cache TTLs (can be overridden per-cluster in database)
# ============================================================================

# Proxmox API cache TTL (seconds) - short-lived cache for cluster-wide queries
PROXMOX_CACHE_TTL = int(os.getenv("PROXMOX_CACHE_TTL", "30"))

# Database IP cache TTL (seconds) - default 30 days
DB_IP_CACHE_TTL = int(os.getenv("DB_IP_CACHE_TTL", "2592000"))

# ============================================================================
# Migration Guide
# ============================================================================

"""
To migrate from old config.py to database:

1. Run migration script:
   python3 migrate_db.py

2. Login to web UI as admin

3. Go to /admin/settings

4. For each cluster, configure:
   - Storage paths (QCOW2 template path, QCOW2 images path)
   - Admin access (admin group, admin users comma-separated)
   - IP discovery (ARP subnets comma-separated)
   - Cache settings (VM cache TTL in seconds)

5. Remove old .env variables (except SECRET_KEY):
   - Remove PVE_HOST, PVE_ADMIN_USER, PVE_ADMIN_PASS
   - Remove ADMIN_USERS, ADMIN_GROUP
   - Remove ARP_SUBNETS
   - Remove QCOW2_TEMPLATE_PATH, QCOW2_IMAGES_PATH
   - Remove VM_CACHE_TTL, ENABLE_IP_LOOKUP, ENABLE_IP_PERSISTENCE

6. Keep only in .env:
   SECRET_KEY=your-random-secret-key
"""
