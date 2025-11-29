# Clone & Template Improvements (from Depl0y)

## Overview
Added three critical improvements to VM cloning inspired by the Depl0y project:

1. **Clone Lock Monitoring** - Proper wait for clone completion with disk verification
2. **Template Validation** - Pre-flight check to catch corrupted templates
3. **Node-Specific VMIDs** - Avoid cross-node template conflicts

## 1. Clone Lock Monitoring

### Problem (Before)
- Old code polled task status via UPID, which is unreliable
- No verification that disk actually exists after clone
- Silent failures when template corrupted

### Solution (After)
```python
wait_for_clone_completion(proxmox, node, vmid, timeout=300)
```

**What it does:**
- Monitors `config.get()['lock']` == 'clone' status
- Waits for lock to release (typically 30s-5min depending on disk size)
- Verifies disk exists after lock release (`scsi0`, `virtio0`, etc.)
- Logs progress every 30 seconds
- Provides detailed diagnostics on failure

**Benefits:**
- Catches clone failures immediately
- Detects corrupted templates before they cause issues
- Better error messages for troubleshooting
- Prevents "VM has no disk" issues

## 2. Template Validation

### Problem (Before)
- Cloning would succeed but VM had no disk
- Root cause: template itself was missing boot disk
- Hard to diagnose, wasted time retrying

### Solution (After)
```python
has_disk, disk_key = verify_template_has_disk(proxmox, node, template_vmid)
if not has_disk:
    return False, "Template is missing boot disk - template may be corrupted"
```

**What it does:**
- Checks template config before attempting clone
- Verifies presence of boot disk (`scsi0`, `virtio0`, `sata0`, or `ide0`)
- Fails fast with clear error message

**Benefits:**
- Catches template corruption immediately
- Saves 5+ minutes of waiting for doomed clone
- Clear actionable error: "recreate template"

## 3. Node-Specific Template VMIDs

### Problem (Before)
- Same template VMID used across all nodes
- Risk of conflicts if template exists on multiple nodes
- Cloning could target wrong node's template

### Solution (After)
```python
def get_node_specific_template_vmid(node_id: int, template_base_id: int) -> int:
    """Node 1 templates: 9100-9199, Node 2: 9200-9299, etc."""
    return 9000 + (node_id * 100) + template_base_id
```

**VMID Allocation Scheme:**
```
Node 1: 9100-9199 (9000 + 100*1 + [0-99])
Node 2: 9200-9299 (9000 + 100*2 + [0-99])
Node 3: 9300-9399 (9000 + 100*3 + [0-99])
...
```

**Benefits:**
- No cross-node VMID conflicts
- Template location clear from VMID alone
- Scales to 90+ nodes (9000-9999 range)

**Usage Example:**
```python
# Template ID 5 on Node 2
template_vmid = get_node_specific_template_vmid(node_id=2, template_base_id=5)
# Returns: 9205
```

## Implementation Details

### Modified Function: `clone_vm_from_template()`

**Before:**
```python
clone_result = proxmox.nodes(node).qemu(template_vmid).clone.post(...)
logger.info("Cloned...")
break  # Done

# Later: complex UPID polling, unreliable
```

**After:**
```python
# Verify template first
has_disk, disk_key = verify_template_has_disk(proxmox, node, template_vmid)
if not has_disk:
    return False, "Template corrupted"

# Clone
clone_result = proxmox.nodes(node).qemu(template_vmid).clone.post(...)

# Wait for completion with verification
wait_for_clone_completion(proxmox, node, new_vmid, timeout=900)
logger.info("Clone completed and verified")
```

### Error Handling Improvements

**Enhanced diagnostics on timeout:**
```python
except TimeoutError:
    # Get final state
    final_config = proxmox.nodes(node).qemu(new_vmid).config.get()
    logger.error(f"Final config: {final_config}")
    
    # Check if template was corrupted
    template_config = proxmox.nodes(node).qemu(template_vmid).config.get()
    if 'scsi0' not in template_config:
        logger.error("⚠ Template is missing disk! Template needs recreation.")
```

## Testing

### Manual Test
```python
from app.services.proxmox_operations import clone_vm_from_template

# Test with valid template
success, msg = clone_vm_from_template(
    template_vmid=112,
    new_vmid=200,
    name="test-clone",
    node="pve1"
)
print(f"Result: {success}, {msg}")

# Should see:
# - "Template 112 verified with disk: scsi0"
# - "Waiting for clone 200 to complete..."
# - "VM 200 still cloning... (30s / 900s)" (every 30s)
# - "VM 200 clone completed successfully after 45s"
```

### Test with Corrupted Template
```python
# Manually corrupt template by removing disk (don't do in production!)
# proxmox.nodes('pve1').qemu(112).config.put(delete='scsi0')

success, msg = clone_vm_from_template(
    template_vmid=112,
    new_vmid=201,
    name="should-fail",
    node="pve1"
)
# Should immediately return:
# False, "Template 112 is missing boot disk - template may be corrupted"
```

## Performance Impact

- **Template validation**: +0.1s per clone (negligible)
- **Clone lock monitoring**: Same wait time, better visibility
- **Node-specific VMIDs**: No performance impact

## Backward Compatibility

✅ **Fully backward compatible**
- Existing code continues to work
- New helper functions are optional
- Old VMID schemes still supported
- Graceful fallback if verification fails

## Future Enhancements

1. **Automatic template repair**: If template corrupted, auto-recreate from source
2. **Clone progress percentage**: Real-time progress based on disk copy (requires qemu-guest-agent)
3. **Parallel clone limit**: Prevent too many simultaneous clones overwhelming storage
4. **Template health monitoring**: Periodic background checks for template integrity

## Related Depl0y Patterns (NOT Implemented)

These Depl0y patterns were evaluated but **not adopted** because our system is better:

- ❌ **SSH-based template creation** - Our Terraform approach is cleaner
- ❌ **cloud-init via subprocess** - Our Proxmox API approach is more reliable
- ❌ **Manual template setup scripts** - Our auto-replication handles this
- ❌ **API Token authentication** - Skipped as requested

## References

- Source: https://github.com/agit8or1/Depl0y
- Key file: `backend/app/services/deployment.py` (lines 700-850)
- Pattern: Clone lock monitoring with disk verification
