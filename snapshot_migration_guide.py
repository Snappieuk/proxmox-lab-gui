#!/usr/bin/env python3
"""
Quick reference: Retroactive Snapshot Migration for Linked Clone Classes

This script explains the usage of the snapshot migration API and provides
example curl commands for admins to retroactively add snapshots.

RUN THIS FROM TERMINAL:
    python3 snapshot_migration_guide.py

"""

import sys

def print_section(title):
    """Print a formatted section header."""
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}\n")


def main():
    print_section("SNAPSHOT MIGRATION - QUICK START GUIDE")
    
    print("""
BACKGROUND:
-----------
Linked clone classes now automatically create baseline snapshots on all VMs.
This enables fast (10-30 second) reimaging instead of slow (5-10 minute) delete-recreate.

Existing linked clone classes created BEFORE this feature need retroactive snapshot creation.

KEY QUESTION: Do you need to retroactively add snapshots?
    ✅ YES if you have existing linked clone classes
    ✅ YES if reimage shows "Snapshot not found" errors
    ❌ NO if all your classes are config_clone (the default)
    ❌ NO if all your linked clone classes were just created (already have snapshots)


SNAPSHOT NAMES:
---------------
All baseline snapshots are named: "baseline"

This is consistent and allows reimage to reliably find the snapshot:
    - VM created with qm clone
    - Baseline snapshot created immediately
    - Reimage reverts to "baseline" snapshot


HOW MIGRATION WORKS:
--------------------
The migration service:
    1. Finds all VMs in a class
    2. Checks if "baseline" snapshot exists
    3. Creates snapshot if missing
    4. Skips VMs that already have one
    5. Reports success/failure counts

Migration is NON-DESTRUCTIVE:
    - Doesn't modify or restart VMs
    - Doesn't affect running workloads
    - Can be run multiple times safely
    - Skips VMs that already have snapshots


API ENDPOINTS:
--------------
""")
    
    print_section("OPTION 1: MIGRATE SINGLE CLASS")
    print("""
ENDPOINT:
    POST /api/admin/snapshots/migrate-class/<class_id>

REQUIREMENTS:
    - Admin access
    - Class must be "linked_clone" deployment method

CURL EXAMPLE:
    curl -X POST http://localhost:8080/api/admin/snapshots/migrate-class/123

RESPONSE:
    {
        "ok": true,
        "message": "Snapshot migration complete for My Class: 42 created, 0 failed, 0 skipped",
        "stats": {
            "created": 42,
            "failed": 0,
            "skipped": 0
        }
    }

USE THIS WHEN:
    - You want to migrate a specific class
    - You're testing the migration process
    - You only have a few existing classes
""")
    
    print_section("OPTION 2: MIGRATE ALL CLASSES (BATCH)")
    print("""
ENDPOINT:
    POST /api/admin/snapshots/migrate-all

REQUIREMENTS:
    - Admin access
    - MUST include header: X-Confirm-Migration: true
    - This header prevents accidental batch migrations

CURL EXAMPLE:
    curl -X POST http://localhost:8080/api/admin/snapshots/migrate-all \\
        -H "X-Confirm-Migration: true"

RESPONSE:
    {
        "ok": true,
        "message": "Snapshot migration complete for all linked clone classes:\\n\\n
                    Classes processed: 5\\n
                    Snapshots created: 42\\n
                    Snapshots failed: 0\\n
                    Already had snapshots: 3",
        "stats": {
            "classes": 5,
            "vms_created": 42,
            "vms_failed": 0,
            "vms_skipped": 3
        }
    }

USE THIS WHEN:
    - You want to migrate all existing linked clone classes
    - You've tested single-class migration and it works
    - You have many older classes to update
""")
    
    print_section("OPTION 3: CHECK CLASS STATUS")
    print("""
ENDPOINT:
    GET /api/admin/snapshots/status?class_id=<class_id>

REQUIREMENTS:
    - Admin access

CURL EXAMPLE:
    curl http://localhost:8080/api/admin/snapshots/status?class_id=123

RESPONSE:
    {
        "ok": true,
        "class_name": "My Class",
        "deployment_method": "linked_clone",
        "vm_count": 5,
        "vms": [
            {
                "vmid": 42809,
                "name": "teacher-vm",
                "has_baseline": "unknown",
                "status": "available"
            },
            ...
        ]
    }

USE THIS WHEN:
    - You want to see how many VMs are in a class
    - You're checking the status before migration
    - You're troubleshooting why reimage is failing
""")
    
    print_section("STEP-BY-STEP MIGRATION PROCESS")
    print("""
1. BACKUP (OPTIONAL)
   - Your VMs are NOT modified by snapshot creation
   - Snapshots are safe and non-destructive
   - But always test in dev first!

2. IDENTIFY CLASSES
   - Determine which classes need snapshots
   - Check if deployment_method is "linked_clone"
   - Option: migrate single class first as test

3. RUN MIGRATION
   - For single class: POST /api/admin/snapshots/migrate-class/<id>
   - For all classes: POST /api/admin/snapshots/migrate-all (with confirmation header)

4. VERIFY RESULTS
   - Check response stats (should see some "created" count)
   - GET /api/admin/snapshots/status?class_id=<id> to verify
   - Test reimage functionality

5. TROUBLESHOOT IF NEEDED
   - If errors occurred, check logs for SSH connection issues
   - Retry migration (safe to run multiple times)
   - Check Proxmox node connectivity


EXAMPLE: Complete Workflow
--------------------------

# Step 1: Check a class before migration
curl http://localhost:8080/api/admin/snapshots/status?class_id=5

# Step 2: Migrate that class
curl -X POST http://localhost:8080/api/admin/snapshots/migrate-class/5

# Step 3: Verify successful migration
curl http://localhost:8080/api/admin/snapshots/status?class_id=5

# Step 4: Test reimage on teacher VM
# - Modify teacher VM (install app, change settings, etc)
# - Click "Reimage" in class detail page
# - Should revert to clean baseline state in ~10-30 seconds

# Step 5: After verification, run batch migration for all classes
curl -X POST http://localhost:8080/api/admin/snapshots/migrate-all \\
    -H "X-Confirm-Migration: true"

# Step 6: Monitor logs for any errors
# tail -f log/proxmox-gui.log | grep snapshot
""")
    
    print_section("WHAT IF SOMETHING GOES WRONG?")
    print("""
ISSUE: "Snapshot not found" error when reimaging
FIX:   Run single-class migration or manually create snapshot:
       qm snapshot <vmid> baseline "Baseline snapshot"

ISSUE: Endpoint returns 404
FIX:   Make sure you're using the correct URL:
       - Single: /api/admin/snapshots/migrate-class/123
       - Batch:  /api/admin/snapshots/migrate-all

ISSUE: Endpoint returns 400 on batch migration
FIX:   Add the confirmation header:
       -H "X-Confirm-Migration: true"

ISSUE: Some VMs didn't get snapshots (stats show "failed")
FIX:   Check SSH connectivity to Proxmox nodes
       Check Proxmox node logs for errors
       Retry migration (safe to run multiple times)

ISSUE: Migration is slow
FIX:   That's normal - snapshot creation can take 10-30 seconds per VM
       For 100 VMs, expect 15-50 minutes total
       Each VM is processed sequentially (safe for system load)
""")
    
    print_section("IMPORTANT REMINDERS")
    print("""
✅ SAFE TO RETRY
   - Migration is idempotent (safe to run multiple times)
   - Already-migrated VMs are skipped
   - Partially failed migrations can be retried

✅ NON-DESTRUCTIVE
   - Snapshots don't modify VMs
   - Snapshots don't restart VMs
   - Running workloads are not affected

⚠️  ADMIN ONLY
   - Snapshot migration requires admin access
   - Regular teachers cannot trigger migration

⚠️  BATCH MIGRATION NEEDS CONFIRMATION
   - Must include header: X-Confirm-Migration: true
   - This prevents accidental batch runs

⚠️  TEST FIRST
   - Migrate a single class first
   - Test reimage on that class
   - Then run batch migration if successful
""")
    
    print_section("DEPLOYMENT METHOD COMPARISON")
    print("""
CONFIG_CLONE (Default, requires template):
    ├─ Creation: QCOW2 export + overlay + config clone (slow, ~5-10min)
    ├─ Reimage: Delete + recreate from class base (slow, ~5-10min)
    ├─ Features: Save Template, Push to VMs
    └─ Best for: Templates with max flexibility

LINKED_CLONE (New, qm clone based):
    ├─ Creation: Direct qm clone (fast, ~30-60sec)
    ├─ Reimage: Revert to baseline snapshot (fast, ~10-30sec)
    ├─ Snapshot: Automatic baseline on all VMs
    └─ Best for: Quick deployments, frequent reimaging


WHICH DO I HAVE?
    Check Class.deployment_method field:
    - "config_clone" (default) → no snapshot migration needed
    - "linked_clone" → NEEDS snapshot migration for reimage to work


RETROACTIVE EFFECTS:
    - New linked clones: Automatic snapshots ✅
    - Existing config clones: No changes, reimage still works ✓
    - Existing linked clones: Need migration for reimage to work
""")
    
    print_section("SUMMARY")
    print("""
Snapshot migration adds "baseline" snapshots to existing linked clone VMs,
enabling fast reimaging.

For new classes: Snapshots are automatic ✅
For old classes: Use /api/admin/snapshots/migrate-class/<id>
For all classes: Use /api/admin/snapshots/migrate-all (with confirmation)

Migration is safe, non-destructive, and can be retried.

Test on one class first, then roll out to all classes.
""")
    
    print("")


if __name__ == "__main__":
    main()
