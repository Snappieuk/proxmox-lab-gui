# Linked Clone Deployment Feature

## Overview

The class creation process now supports two deployment methods:

1. **Config Clone (default)** - Uses config file cloning + QCOW2 overlays
   - Recommended for most use cases
   - Better for template consistency
   - Slower initial deployment but flexible

2. **Linked Clone** - Uses `qm clone` (snapshot-based, NOT full clones)
   - **Much faster** - snapshot-based, shares template disk
   - Dramatically lower disk usage than full clones
   - Automatically load balances VMs across cluster nodes
   - VMs created on same storage as template (supports TRUENAS-NFS, NFS-Datastore, local-lvm, etc.)
   - Good for quick deployments with many VMs

## Key Differences

- **Full Clone** (`--full true`): Full copy of all data, slow, high disk usage
- **Linked Clone** (default `qm clone`): Snapshot-based, shares backing disk, fast, low overhead ✓

## API Usage

### Create Class with Linked Clone Deployment

```bash
POST /api/classes
Content-Type: application/json

{
  "name": "Biology Lab 101",
  "description": "Introductory biology virtual lab",
  "template_id": 104,
  "pool_size": 20,
  "deployment_method": "linked_clone",
  "deployment_node": "pve-node-1"  # Optional: deploy to specific node
}
```

### Create Class with Config Clone Deployment (default)

```bash
POST /api/classes
Content-Type: application/json

{
  "name": "Chemistry Lab 102",
  "description": "Chemistry lab with specialized software",
  "template_id": 105,
  "pool_size": 15,
  "deployment_method": "config_clone"  # This is the default
}
```

## Parameters

- `deployment_method`: `"config_clone"` (default) or `"linked_clone"`
- `deployment_node`: Optional node name for single-node deployment (linked_clone only)

## How Linked Clone Works

1. **Clone Creation**: Uses `qm clone {template_vmid} {new_vmid} --storage {template_storage}` (NO --full flag)
2. **Snapshot-Based**: New VMs reference template disk, only store deltas
3. **Storage Location**: Automatically detects and uses same storage as template (TRUENAS-NFS, NFS-Datastore, local-lvm, etc.)
4. **Naming**: Automatically names VMs as `{class_name}-student-{index}-{vmid}`
5. **Load Balancing**: 
   - Queries current VM count on each cluster node
   - Distributes new VMs across least-loaded nodes
   - If `deployment_node` specified, all VMs go to that node
6. **Database**: Creates VMAssignment records in database after successful clone

## Example Equivalent Shell Command

The equivalent shell command for 20 snapshot-based linked clones would be:

```bash
template_vmid=104
template_storage="TRUENAS-NFS"  # Auto-detected from template
for i in {1..20}; do 
  vmid=$((104 + $i))
  qm clone $template_vmid $vmid --storage $template_storage --name "biology-lab-student-$i-$vmid"
done
```

Note: **NO `--full true` flag** - this creates true linked clones, not full clones!

## Load Balancing Details

The `deploy_linked_clones()` function:
- Queries each node's QEMU and LXC VM count
- Ranks nodes by current load (ascending)
- Distributes VMs round-robin across sorted nodes
- Example: If nodes have [5 VMs, 8 VMs, 3 VMs], template 20 clones go to:
  - Node 3 (3 VMs, least loaded)
  - Node 1 (5 VMs)
  - Node 2 (8 VMs)
  - Back to Node 3, etc.

## Configuration

### SSH Requirements

Linked clone deployment requires SSH access to Proxmox nodes:
- SSH credentials configured in `.env` or database Cluster table
- Uses connection pooling from `ssh_executor.py`
- Automatically tries SSH tunnel if direct command fails

### Cluster Selection

- Uses cluster specified in `"deployment_cluster"` parameter
- Falls back to first active cluster in database
- Each cluster can have independent SSH credentials

## UI Integration

To add a button/option to your class creation form:

```html
<div class="form-group">
  <label>Deployment Method:</label>
  <select name="deployment_method" id="deployment_method">
    <option value="config_clone">Config Clone (Recommended)</option>
    <option value="linked_clone">Linked Clone (Fast)</option>
  </select>
  <small>Config Clone is recommended for most cases.</small>
</div>
```

JavaScript to send the request:

```javascript
const deploymentMethod = document.getElementById('deployment_method').value;

const response = await fetch('/api/classes', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    name: className,
    description: classDescription,
    template_id: templateId,
    pool_size: poolSize,
    deployment_method: deploymentMethod,
    deployment_node: deploymentNode || null
  })
});
```

## Performance Comparison

| Aspect | Config Clone | Linked Clone |
|--------|--------------|--------------|
| Speed | Medium (template export + overlay) | **FAST** (snapshot-based) ✓ |
| Disk Space | Lower (overlays) | **Very Low** (shares backing disk) ✓ |
| Deployment Time (20 VMs) | ~5-10 minutes | **30-60 seconds** ✓ |
| Clone Type | QCOW2 overlays | True snapshot-based linked clones ✓ |
| Load Balancing | Manual | **Automatic** ✓ |
| Storage Support | Any | **Any storage (auto-detected from template)** ✓ |
| Consistency | Guaranteed | Template-dependent |

**Linked Clones are ideal for:**
- Creating large numbers of VMs quickly (20+ students)
- Limited disk space (VMs share template disk)
- Rapid lab deployments
- Any storage backend (TRUENAS-NFS, NFS-Datastore, local SSD, etc.)

## Troubleshooting

### Linked Clone Deployment Fails

1. **SSH Connection Issues**
   - Verify SSH credentials in cluster config
   - Check node is reachable on network
   - Look for "SSH connection failed" in logs

2. **Template Not Found**
   - Confirm template_vmid exists on source node
   - Check template has read permissions
   - Verify node name matches Proxmox hostname

3. **VM Not Created**
   - Check VMID is available (no conflicts with existing VMs)
   - Verify Proxmox storage has sufficient space
   - Look for qm clone errors in syslog

### VMs Not Load-Balanced

- Use `deployment_node` parameter to force single-node deployment
- Check all nodes are online in Proxmox UI
- Verify nodes can be queried via Proxmox API

## Code Reference

### Main Functions

- `deploy_linked_clones()` - Main deployment entry point
- `get_available_vmid()` - Find next available VM ID
- `get_nodes_for_load_balancing()` - Rank nodes by load

### Files

- `app/services/linked_clone_service.py` - Linked clone implementation
- `app/routes/api/class_api.py` - Class creation API endpoint

## Future Enhancements

- [ ] Progress tracking for linked clone deployment
- [ ] Selective node placement rules
- [ ] VM naming scheme customization
- [ ] Pre/post-clone hooks for customization
