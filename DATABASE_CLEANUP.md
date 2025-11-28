# Database Cleanup Guide

## Quick Fix for "Failed to Force Delete Class"

If you're getting errors when trying to delete a class, use the database cleanup utility:

### Option 1: Interactive Mode (Easiest)

```bash
python cleanup_database.py
```

This will show a menu:
1. List all classes
2. List all VM assignments  
3. Force delete class by ID
4. Delete VM assignment by ID
5. Clear IP cache
0. Exit

Choose option **1** to see all classes, then option **3** to delete the problematic class.

### Option 2: Command Line Mode

```bash
# List all classes to find the ID
python cleanup_database.py list-classes

# Delete class by ID (example: ID 5)
python cleanup_database.py delete-class 5
```

## What This Script Does

- ✅ Deletes class from database
- ✅ Deletes VM assignments from database
- ✅ Clears student enrollment records
- ❌ Does NOT delete VMs from Proxmox

**Important**: After using this script, you may need to manually delete the VMs from Proxmox web interface if they still exist.

## Other Useful Commands

### List all VM assignments
```bash
python cleanup_database.py list-vms
```

### Clear IP cache (if you see stale IPs)
```bash
python cleanup_database.py clear-ip-cache
```

## Why Force Delete Might Fail in the UI

Common reasons:
1. VMs were already manually deleted from Proxmox
2. Proxmox cluster is unreachable
3. Permission issues with VM deletion
4. Database foreign key constraints

The cleanup script bypasses Proxmox and directly modifies the database, which is safe for fixing stuck classes.

## Example Workflow

```bash
# 1. See what's in the database
python cleanup_database.py list-classes

Output:
ID    Name                           Teacher              VMs   Students
----------------------------------------------------------------------
1     Test Class                     admin                3     5
2     Production Lab                 teacher1             10    20

# 2. Force delete "Test Class" (ID 1)
python cleanup_database.py delete-class 1

# 3. Verify it's gone
python cleanup_database.py list-classes
```

## Safety

- The script asks for confirmation before deleting anything
- You must type 'yes' to confirm destructive operations
- All operations only affect the database, not Proxmox VMs
- No risk of accidentally deleting VMs from Proxmox
