# Codebase Cleanup Plan

## Files/Code to DELETE (Completely Unused)

### 1. VMInventory System - REMOVE ENTIRELY
**Why**: Adds complexity, not used by frontend, Terraform workflow doesn't need it
- [ ] Delete `app/services/background_sync.py` 
- [ ] Delete `app/services/inventory_service.py`
- [ ] Delete `migrate_inventory.py`
- [ ] Delete `DATABASE_ARCHITECTURE.md`
- [ ] Remove VMInventory model from `app/models.py`
- [ ] Delete `app/routes/api/sync.py`
- [ ] Remove background_sync startup from `app/__init__.py`
- [ ] Simplify `app/routes/api/vms.py` to only use direct Proxmox API

### 2. Legacy Python Cloning System - REPLACED BY TERRAFORM
**Why**: Terraform handles all VM deployment now
- [ ] Delete `clone_vms_for_class()` function (800+ lines) from `app/services/proxmox_operations.py`
- [ ] Remove `clone_vms_for_class` import from `app/routes/api/classes.py`
- [ ] Delete `app/services/node_selector.py` (Terraform handles node selection)
- [ ] Delete `app/services/clone_progress.py` (or simplify to just progress tracking)

### 3. Legacy rdp-gen References
**Why**: Directory deleted, imports do nothing
- [ ] Remove `import app.utils.paths` from all files (12 locations)
- [ ] Delete `app/utils/paths.py` entirely

### 4. Unused Migration Scripts
**Why**: One-time migrations already run
- [ ] Delete `migrate_templates.py`
- [ ] Delete `migrate_inventory.py`
- [ ] Delete `cleanup_database.py`

### 5. Legacy Mappings System
**Why**: Class-based system replaces this
- [ ] Remove mappings.json logic from `app/services/proxmox_client.py`
- [ ] Delete `app/routes/admin/mappings.py`
- [ ] Delete `app/routes/api/mappings.py`
- [ ] Keep the template replication service (still useful)

### 6. Duplicate/Unused Documentation
- [ ] Delete `TEMPLATE_TRACKING.md` (merged into copilot instructions)
- [ ] Delete `RUN.md` if exists
- [ ] Delete `DATABASE_ARCHITECTURE.md` (VMInventory-specific)

## Files to SIMPLIFY

### 1. `app/routes/api/vms.py` - Remove VMInventory Logic
Keep only:
- Direct Proxmox API calls via `get_vms_for_user()`
- RDP generation
- VM start/stop
- Remove all inventory_mode/background sync logic

### 2. `app/services/proxmox_client.py` - Remove Mappings
Keep only:
- VM listing for authenticated users
- VM operations (start/stop/reboot)
- IP lookup
- Template operations
Remove:
- mappings.json filtering
- Legacy cache logic

### 3. `app/services/proxmox_operations.py` - Keep Only Terraform Support
Keep:
- `replicate_templates_to_all_nodes()` - still useful
- `convert_vm_to_template()` - used by Terraform service
- `create_vm_snapshot()`, `revert_vm_to_snapshot()` - used by students
- `delete_vm()` - cleanup operations
Remove:
- `clone_vms_for_class()` and all its helper functions
- Storage detection logic (Terraform handles this)
- VMID allocation logic (Terraform handles this)

### 4. `app/__init__.py` - Remove VMInventory Startup
Remove:
- background_sync startup
- inventory service initialization

## Estimated Lines Removed
- VMInventory system: ~1000 lines
- Legacy Python cloning: ~800 lines
- Mappings system: ~400 lines
- Migration scripts: ~500 lines
- Unused imports/paths: ~100 lines

**Total: ~2800 lines removed (~30% of codebase)**

## What Stays (Core Functionality)

### Essential Services:
1. **Terraform service** (`app/services/terraform_service.py`) - VM deployment
2. **Class service** (`app/services/class_service.py`) - Database operations
3. **Proxmox service** (`app/services/proxmox_service.py`) - Multi-cluster connections
4. **RDP service** (`app/services/rdp_service.py`) - RDP file generation
5. **SSH service** (`app/services/ssh_service.py`) - WebSocket terminal
6. **ARP scanner** (`app/services/arp_scanner.py`) - IP discovery fallback
7. **User manager** (`app/services/user_manager.py`) - Authentication
8. **Cluster manager** (`app/services/cluster_manager.py`) - Multi-cluster support

### Essential Routes:
1. **Classes** (`app/routes/api/classes.py`) - Class CRUD + VM deployment
2. **Auth** (`app/routes/auth.py`) - Login/logout
3. **VMs** (`app/routes/api/vms.py`) - VM listing/control (SIMPLIFIED)
4. **RDP** (`app/routes/api/rdp.py`) - RDP downloads
5. **SSH** (`app/routes/api/ssh.py`) - Terminal connections
6. **Admin diagnostics** (`app/routes/admin/diagnostics.py`) - System health

### Database Models:
1. **User** - Local accounts
2. **Class** - Lab classes
3. **Template** - Proxmox templates (with replication tracking)
4. **VMAssignment** - Class VM assignments
Remove:
5. ~~VMInventory~~ - DELETE

## Benefits After Cleanup

1. **Simpler architecture**: Direct Proxmox API → Terraform → Database → Frontend
2. **Faster**: No background sync overhead
3. **Easier to debug**: One code path instead of two (direct vs cached)
4. **Less maintenance**: ~2800 fewer lines to maintain
5. **Clearer**: New developers can understand the flow instantly

## Migration Notes

**NO DATA LOSS**: All cleanup is code-only. Database keeps:
- User accounts
- Class definitions
- VM assignments
- Template tracking

**API Compatibility**: Frontend still gets same JSON structure from `/api/vms`
