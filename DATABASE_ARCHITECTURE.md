# Database-First Architecture for VM Inventory

This document describes the database-first architecture where all frontend VM information comes from the database instead of direct Proxmox API calls, providing instant load times and reducing API load.

## Architecture Overview

### Before (API-First)
```
Frontend → /api/vms → Proxmox API → Response (slow, 5-30s)
```

### After (Database-First)
```
Frontend → /api/vms → Database → Response (fast, <100ms)
                           ↑
Background Service → Proxmox API (every 30-300s)
```

## Key Components

### 1. VMInventory Database Model (`app/models.py`)

Stores complete VM information:

**Basic Fields:**
- `cluster_id`, `cluster_name` - Which cluster
- `vmid`, `name`, `node` - VM identification
- `status` - running/stopped/unknown
- `ip` - IP address
- `type` - qemu/lxc
- `category` - windows/linux/other

**Hardware Specs:**
- `memory` - RAM in MB
- `cores` - CPU cores
- `disk_size` - Disk size in bytes

**Runtime Metrics:**
- `uptime` - Uptime in seconds
- `cpu_usage` - CPU usage percentage
- `memory_usage` - Memory usage percentage

**Configuration:**
- `is_template` - Whether this is a template
- `tags` - Comma-separated tags

**Connectivity:**
- `rdp_available` - RDP connectivity ready
- `ssh_available` - SSH connectivity ready

**Sync Tracking:**
- `last_updated` - Last full update
- `last_status_check` - Last status verification
- `sync_error` - Last error message if any

### 2. Background Sync Service (`app/services/background_sync.py`)

Continuous daemon thread that keeps the database synchronized:

**Full Sync (every 5 minutes):**
- Fetches ALL VMs from ALL clusters
- Updates complete VM information
- Checks RDP/SSH port availability
- Persists to database

**Quick Sync (every 30 seconds):**
- Only updates running/recently active VMs
- Fast status checks (status, uptime, CPU, memory)
- No IP lookups or port scans
- Minimal API load

**Error Handling:**
- Exponential backoff on failures
- Continues running despite errors
- Logs detailed error information
- Per-VM error tracking

### 3. Updated API Endpoint (`app/routes/api/vms.py`)

**Always serves from database:**
```python
@app.route("/api/vms")
def api_vms():
    # Fetch from database (instant)
    inventory = fetch_vm_inventory(cluster_id, search)
    
    # Filter to user's VMs
    owned_vmids = _resolve_user_owned_vmids(username)
    filtered = [vm for vm in inventory if vm['vmid'] in owned_vmids]
    
    return jsonify({"vms": filtered, "mode": "inventory"})
```

**No more:**
- Direct Proxmox API calls for VM lists
- Slow page loads waiting for API responses
- API timeouts affecting user experience
- Race conditions with concurrent requests

### 4. Instant Database Updates on Actions

When user starts/stops a VM, database is immediately updated:

```python
@app.route("/api/vm/<vmid>/start", methods=["POST"])
def api_vm_start(vmid):
    start_vm(vm)  # Proxmox API call
    _update_vm_status_in_db(vmid, cluster_id, "running")  # Instant DB update
    return {"ok": True}
```

User sees status change immediately without waiting for next sync.

## Benefits

### Performance
- **Frontend loads in <100ms** instead of 5-30 seconds
- **No API timeouts** affecting page loads
- **Concurrent users** don't slow down each other
- **Reduced Proxmox API load** by 95%+

### User Experience
- **Instant page loads** - no waiting for VM list
- **Immediate feedback** on start/stop actions
- **Search works instantly** - client-side filtering
- **Smooth experience** even with 100+ VMs

### Reliability
- **Works during Proxmox maintenance** (shows last known state)
- **No cascading failures** from API timeouts
- **Graceful degradation** - stale data is better than no data
- **Error isolation** - one cluster failure doesn't break others

### Scalability
- **Supports more concurrent users** without API overload
- **Handles larger environments** (1000+ VMs)
- **Predictable performance** regardless of cluster load

## Migration Process

### 1. Run Migration Script

```bash
cd /path/to/proxmox-lab-gui
source venv/bin/activate  # or .\venv\Scripts\activate on Windows
python migrate_inventory.py
```

This will:
- Add new columns to VMInventory table
- Perform initial full synchronization
- Display statistics

### 2. Restart Application

```bash
# If using systemd
sudo systemctl restart proxmox-gui

# If running standalone
./start.sh
```

Background sync service starts automatically.

### 3. Verify Operation

Check sync service health:
```bash
curl http://localhost:8080/api/sync/health
```

View statistics:
```bash
curl http://localhost:8080/api/sync/stats
```

Monitor logs:
```bash
# Systemd
sudo journalctl -u proxmox-gui -f

# Standalone
# Check terminal output for "Background sync" messages
```

## API Endpoints

### VM List (Database-First)
```http
GET /api/vms?search=<query>&cluster=<cluster_id>
```

Returns VM list from database instantly.

**Query params:**
- `search` - Filter by name/IP/VMID
- `cluster` - Filter by cluster
- `force_refresh=true` - Force live Proxmox fetch (bypasses DB)
- `live=true` - Return live data (legacy compatibility)

**Response:**
```json
{
  "vms": [
    {
      "vmid": 100,
      "name": "web-server",
      "status": "running",
      "ip": "10.220.15.50",
      "memory": 4096,
      "cores": 2,
      "uptime": 86400,
      "cpu_usage": 25.5,
      "memory_usage": 60.2,
      "rdp_available": false,
      "ssh_available": true,
      "last_updated": "2024-01-15T10:30:00"
    }
  ],
  "mode": "inventory",
  "stale": false
}
```

### Sync Service Stats
```http
GET /api/sync/stats
```

Returns background sync statistics.

**Response:**
```json
{
  "total_vms": 150,
  "fresh_vms": 148,
  "error_vms": 2,
  "running_vms": 45,
  "last_full_sync": 1705315200,
  "last_quick_sync": 1705315800,
  "consecutive_errors": 0,
  "is_running": true
}
```

### Sync Health Check
```http
GET /api/sync/health
```

Health status of background service.

**Response:**
```json
{
  "healthy": true,
  "issues": [],
  "stats": { ... }
}
```

### Trigger Manual Sync (Admin Only)
```http
POST /api/sync/trigger
```

Forces immediate full sync (admin only).

## Monitoring & Troubleshooting

### Check Sync Service Status

```python
# In Python shell or admin endpoint
from app.services.background_sync import get_sync_stats
stats = get_sync_stats()
print(stats)
```

### View Stale VMs

```python
from app.models import VMInventory
from datetime import datetime, timedelta

stale_cutoff = datetime.utcnow() - timedelta(minutes=10)
stale_vms = VMInventory.query.filter(
    VMInventory.last_updated < stale_cutoff
).all()

for vm in stale_vms:
    print(f"{vm.vmid} {vm.name}: last updated {vm.last_updated}")
```

### Check VMs with Errors

```python
from app.models import VMInventory

error_vms = VMInventory.query.filter(
    VMInventory.sync_error.isnot(None)
).all()

for vm in error_vms:
    print(f"{vm.vmid} {vm.name}: {vm.sync_error}")
```

### Manually Trigger Sync

```bash
curl -X POST http://localhost:8080/api/sync/trigger \
  -H "Cookie: session=<your-session-cookie>"
```

Or use the admin UI (if implemented).

### Adjust Sync Intervals

Edit `app/services/background_sync.py`:

```python
FULL_SYNC_INTERVAL = 300  # 5 minutes - complete inventory
QUICK_SYNC_INTERVAL = 30  # 30 seconds - status updates
```

Restart app after changes.

## Performance Tuning

### For Large Environments (500+ VMs)

**Increase full sync interval:**
```python
FULL_SYNC_INTERVAL = 600  # 10 minutes
```

**Decrease quick sync interval for better responsiveness:**
```python
QUICK_SYNC_INTERVAL = 15  # 15 seconds
```

**Add database indexes** (already done):
```python
# VMInventory model has indexes on:
# - cluster_id, vmid (compound unique)
# - status
# - last_updated
```

### For Small Environments (<50 VMs)

**Decrease full sync interval for fresher data:**
```python
FULL_SYNC_INTERVAL = 120  # 2 minutes
```

**Keep quick sync responsive:**
```python
QUICK_SYNC_INTERVAL = 20  # 20 seconds
```

## Compatibility

### Existing Deployments

The system maintains **backward compatibility**:

- Old `force_refresh=true` parameter still works (bypasses DB)
- Old `live=true` parameter still works (direct API call)
- Fallback to live fetch if database is empty
- Existing mappings.json still respected
- Class-based VM assignments still work

### Gradual Migration

You can **gradually adopt** database-first:

1. Run migration script (adds columns, doesn't break anything)
2. Background service starts syncing (existing flows still work)
3. Frontend automatically uses database when available
4. Remove `force_refresh` calls once you trust the sync
5. Eventually disable live mode entirely

## Security Considerations

### Database Access

- Background service needs app context for database writes
- Uses same SQLAlchemy session management as other services
- Transactions committed per-sync to prevent long locks
- Errors rolled back automatically

### API Permissions

- `/api/vms` requires login (same as before)
- Users only see their own VMs (filtered by ownership)
- Admins see all VMs (same as before)
- `/api/sync/trigger` requires admin role

### Error Handling

- Per-VM errors don't stop batch processing
- Sync continues despite individual cluster failures
- Error details stored in `sync_error` field (truncated to 500 chars)
- Sensitive info not exposed in error messages

## Future Enhancements

Potential additions to consider:

1. **Real-time updates** via WebSocket (notify clients of VM status changes)
2. **Historical data** (track VM metrics over time)
3. **Alerting** (notify admins of sync failures or VM issues)
4. **Caching layer** (Redis for even faster reads)
5. **GraphQL API** (flexible data fetching)
6. **Batch operations** (start/stop multiple VMs efficiently)
7. **Scheduled tasks** (auto-shutdown VMs on schedule)

## Support

For issues or questions:

1. Check logs for "Background sync" messages
2. Run `/api/sync/health` to verify service status
3. Check database for stale/error VMs
4. Manually trigger sync with `/api/sync/trigger`
5. Review this documentation for troubleshooting steps
