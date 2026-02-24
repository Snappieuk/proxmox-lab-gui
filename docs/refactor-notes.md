# Refactor Notes

## Summary

All changes are in `app/services/proxmox_operations.py`.  
Every change is a **pure refactor** — no user-facing or API behaviour was altered.

---

## 1. Cluster-lookup helper: `_get_proxmox_for_cluster()`

**Problem**: 20 functions each repeated the same 6–8 line boilerplate to resolve a
cluster IP to a Proxmox API connection:

```python
cluster_id = None
for cluster in get_clusters_from_db():
    if cluster["host"] == (cluster_ip or CLASS_CLUSTER_IP):
        cluster_id = cluster["id"]
        break
if not cluster_id:
    return False, "Cluster not found"
proxmox = get_proxmox_admin_for_cluster(cluster_id)
```

**Fix**: Extracted `_get_proxmox_for_cluster(cluster_ip=None)` returning
`(proxmox, None)` on success or `(None, error_message)` on failure.
Every affected function now does:

```python
proxmox, err = _get_proxmox_for_cluster(cluster_ip)
if err:
    return False, err   # (callers adapt the return shape as needed)
```

**Functions updated** (20 total):
`replicate_templates_to_all_nodes`, `list_proxmox_templates`,
`clone_vm_from_template`, `start_class_vm`, `stop_class_vm`,
`get_vm_status_from_inventory`, `get_vm_status`, `create_vm_snapshot`,
`revert_vm_to_snapshot`, `delete_vm`, `convert_vm_to_template`,
`save_teacher_template`, `reimage_teacher_vm`, `push_template_to_students`,
`convert_template_to_vm`, `clone_template_for_class`, `create_vm_shells`,
`populate_vm_shell_with_disk`, `remove_vm_disk`, `copy_vm_disk`.

---

## 2. Boot-disk finder: `_find_boot_disk()`

**Problem**: 5 functions each repeated an identical loop to locate the primary disk
in a Proxmox VM config dict:

```python
disk_key = None
disk_config = None
for key in ['scsi0', 'virtio0', 'sata0', 'ide0']:
    if key in config:
        disk_key = key
        disk_config = config[key]
        break
```

**Fix**: Extracted `_find_boot_disk(config)` returning `(disk_key, disk_config)` or
`(None, None)` when no boot disk is found. A module-level constant
`_BOOT_DISK_SLOTS` holds the ordered list of disk slots so the search order is
defined in one place.

**Functions updated**: `verify_template_has_disk`, `move_vm_disk_to_local_storage`,
`populate_vm_shell_with_disk`, `remove_vm_disk`, `copy_vm_disk`.

The linked-clone detection path inside `clone_vm_from_template` was also updated.

---

## 3. VM node resolver: `_get_vm_actual_node()`

**Problem**: `start_class_vm` and `stop_class_vm` each embedded an identical 10-line
block that queries `proxmox.cluster.resources.get(type="vm")` to discover which
node a VM actually lives on (it may have migrated from the `node` hint passed by
the caller).

**Fix**: Extracted `_get_vm_actual_node(proxmox, vmid, fallback_node)` which returns
the resolved node name (always a string, never `None`). Used in the new shared power
helper below, and also used in the updated `get_vm_status`.

---

## 4. Unified VM power action: `_execute_vm_power_action()`

**Problem**: `start_class_vm` and `stop_class_vm` are the clearest example of
**"functions that do the same work but look for a different outcome"**:

| Step | `start_class_vm` | `stop_class_vm` |
|------|-----------------|-----------------|
| Get cluster connection | ✅ identical | ✅ identical |
| Resolve actual node | ✅ identical | ✅ identical |
| Guard: is template? | only start checks | — |
| API call | `.status.start.post()` | `.status.shutdown.post()` |

**Fix**: Both public functions now delegate to
`_execute_vm_power_action(proxmox, vmid, node, action)` where `action` is
`'start'` or `'stop'`. The shared helper performs all common steps; only the
final API call (and the template guard, which only applies to `'start'`) differs
based on `action`.

```python
def start_class_vm(vmid, node, cluster_ip=None):
    proxmox, err = _get_proxmox_for_cluster(cluster_ip)
    if err:
        return False, err
    return _execute_vm_power_action(proxmox, vmid, node, 'start')

def stop_class_vm(vmid, node, cluster_ip=None):
    proxmox, err = _get_proxmox_for_cluster(cluster_ip)
    if err:
        return False, err
    return _execute_vm_power_action(proxmox, vmid, node, 'stop')
```

---

## 5. VMID allocation — use canonical `get_next_available_vmid_api()`

**Problem**: `save_teacher_template`, `clone_template_for_class`, and
`create_vm_shells` each contained an inline VMID allocation loop that was
semantically identical to the existing `get_next_available_vmid_api()` in
`app/services/vm_utils.py`.

**Fix**: All three now call `get_next_available_vmid_api(proxmox, start=N)`.
`create_vm_shells` passes its accumulated `used_vmids` set to the helper so that
each iteration correctly skips VMIDs already reserved in the same batch.

---

## 6. MAC address extraction — use canonical `get_vm_mac_address_api()`

**Problem**: `get_vm_status` and `create_vm_shells` each contained inline MAC
address parsing logic that was functionally equivalent to the existing
`get_vm_mac_address_api()` in `app/services/vm_utils.py`.

**Fix**: Both now call `get_vm_mac_address_api(proxmox, node, vmid)`.

---

## 7. Dead code removal

Line 133 of the original `wait_for_clone_completion()` was an unreachable
`raise TimeoutError(...)` that appeared immediately after another identical
`raise TimeoutError(...)` on the line above it. The duplicate line was removed.

---

## How to extend the generalised helpers

| Helper | Where | When to use |
|--------|-------|-------------|
| `_get_proxmox_for_cluster(cluster_ip)` | `proxmox_operations.py` | Any new function that needs a Proxmox API connection for a given cluster IP |
| `_find_boot_disk(config)` | `proxmox_operations.py` | Any new function that needs to locate the primary disk in a VM config dict |
| `_get_vm_actual_node(proxmox, vmid, fallback)` | `proxmox_operations.py` | Any operation that needs the current node of a VM (which may have migrated) |
| `_execute_vm_power_action(proxmox, vmid, node, action)` | `proxmox_operations.py` | To add new power actions (e.g. `'reboot'`), extend the `if/elif` chain here |
| `get_next_available_vmid_api(proxmox, start, used_vmids)` | `vm_utils.py` | Allocating one or more VMIDs from the Proxmox API |
| `get_vm_mac_address_api(proxmox, node, vmid)` | `vm_utils.py` | Reading the MAC address of a QEMU VM via the Proxmox API |
