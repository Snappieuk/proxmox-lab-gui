# Persistent IP Caching Implementation

## Overview

Implemented database-backed IP caching to eliminate the 10-30 second ARP scan wait on every app restart. IPs are now stored persistently and only refreshed after 1 hour TTL or when VMs change state.

## Changes Made

### 1. Database Schema (`app/models.py`)

**VMAssignment table** - Added columns for class VM IP caching:
- `cached_ip` (VARCHAR(45)) - Stores last known IP address
- `ip_updated_at` (DATETIME) - Timestamp of last IP update (1hr TTL)

**New VMIPCache table** - For legacy/non-class VM IP storage:
- `vmid` (INTEGER, primary key)
- `mac_address` (VARCHAR(17))
- `cached_ip` (VARCHAR(45))
- `ip_updated_at` (DATETIME)
- `cluster_id` (VARCHAR(50))
- Indexes on vmid, mac_address, and cluster_id for fast lookups

### 2. Migration Script (`migrate_database.py`)

Added migrations to update existing databases:
- Adds `cached_ip` and `ip_updated_at` columns to `vm_assignments` table
- Creates new `vm_ip_cache` table with appropriate indexes

**Run migration**: `python migrate_database.py`

### 3. IP Cache Logic (`app/services/proxmox_client.py`)

#### New Functions

**`_enrich_from_db_cache(vms)`** - Loads cached IPs from database
- Queries VMAssignment for class VMs
- Queries VMIPCache for legacy VMs
- Only uses IPs with TTL < 1 hour
- Updates VM dicts with cached IPs in-place
- Logs cache hits/misses/expirations

**`_save_ips_to_db(vms, vm_mac_map)`** - Saves discovered IPs to database
- Updates VMAssignment.cached_ip for class VMs
- Creates/updates VMIPCache entries for legacy VMs
- Sets ip_updated_at timestamp for TTL tracking
- Commits in single transaction for performance

#### Modified Functions

**`_enrich_vms_with_arp_ips(vms, force_sync)`** - Enhanced to use database cache
- First calls `_enrich_from_db_cache()` to load from database
- Only runs ARP scan for VMs without cached IPs
- Calls `_save_ips_to_db()` after discovering new IPs
- Significantly reduces scan time on app restart

**`_clear_vm_ip_cache(cluster_id, vmid)`** - Clears both memory and database
- Clears memory cache (existing behavior)
- Clears VMAssignment.cached_ip if exists
- Deletes VMIPCache entry if exists
- Called automatically on VM start/stop

## Benefits

### Performance Improvements
- **Instant IP display on app restart** - No 10-30 second ARP scan wait
- **IPs persist across restarts** - Database stores last known IPs
- **Reduced network scanning** - Only scans when cache expires (1hr) or VM changes
- **Fast page loads** - Eliminates "Fetching..." delay for known IPs

### How It Works

1. **First Request After Restart**:
   - App queries database for cached IPs (milliseconds)
   - Displays known IPs immediately from database
   - Background ARP scan updates any missing/changed IPs
   - New IPs saved back to database

2. **Subsequent Requests**:
   - Database cache hit if < 1hr old
   - No ARP scan needed
   - IPs displayed instantly

3. **VM State Changes** (start/stop):
   - Database cache automatically invalidated
   - Fresh ARP scan on next request
   - New IP saved to database

4. **Cache Expiration** (after 1 hour):
   - ARP scan runs to verify/update IPs
   - Updated IPs saved to database
   - New 1hr TTL starts

## Testing

### Verify Database Schema
```bash
sqlite3 app/lab_portal.db
.schema vm_assignments
.schema vm_ip_cache
```

Should show `cached_ip` and `ip_updated_at` columns.

### Test IP Persistence
1. Start app, wait for IPs to populate
2. Stop app (Ctrl+C)
3. Restart app
4. **Expected**: IPs appear immediately (< 1 second)
5. Check logs for: "Database IP cache: X hits, Y misses"

### Test Cache Invalidation
1. Start a VM
2. Check logs for: "Cleared VMAssignment IP cache for VM X"
3. Next request should show new IP after ARP scan
4. New IP saved to database

### Monitor Cache Performance
```python
# Check cache hit rates in logs
grep "Database IP cache" app.log
# Example output:
# Database IP cache: 15 hits, 2 misses, 0 expired
```

## Troubleshooting

### IPs not persisting?
- Run migration: `python migrate_database.py`
- Check database schema: `sqlite3 app/lab_portal.db ".schema vm_ip_cache"`
- Verify logs show "Saved IPs to database: X class VMs, Y legacy VMs"

### Stale IPs showing?
- Cache TTL is 1 hour by default
- Start/stop VM to force cache invalidation
- Or wait 1 hour for automatic refresh

### Performance not improving?
- Check logs for "Database IP cache: X hits" (should be > 0)
- Verify database queries complete quickly (< 100ms)
- Ensure indexes exist: `.indices` in sqlite3

## Configuration

No configuration needed - enabled by default. Cache TTL is hardcoded to 1 hour in:
- `_enrich_from_db_cache()`: `cache_ttl = timedelta(hours=1)`

To change TTL, modify this line in `app/services/proxmox_client.py`.

## Code Locations

- Database models: `app/models.py` (lines ~170-200)
- Migration script: `migrate_database.py` (lines ~60-100)
- Cache functions: `app/services/proxmox_client.py` (lines ~1015-1100, ~1240-1330, ~555-585)
