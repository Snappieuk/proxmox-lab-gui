#!/usr/bin/env python3
"""
Testing and usage examples for the snapshot migration service.

This file demonstrates how to use the snapshot migration functions
directly in Python code (not just via the API).

USAGE:
    python3 test_snapshot_migration.py [class_id]

EXAMPLES:
    # Migrate specific class
    python3 test_snapshot_migration.py 123

    # Show migration status
    python3 -c "
    from app import create_app
    from app.routes.api.snapshots import snapshot_migration_status
    app = create_app()
    with app.test_request_context('/?class_id=123'):
        result = snapshot_migration_status()
    "
"""

import sys
import logging
from app import create_app
from app.services.snapshot_migration import (
    create_baseline_snapshots_for_class,
    create_baseline_snapshots_for_all_linked_clones
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_single_class_migration(class_id: int):
    """Test migrating a single class."""
    print(f"\n{'='*70}")
    print(f"Single Class Migration - Class ID: {class_id}")
    print(f"{'='*70}\n")
    
    app = create_app()
    with app.app_context():
        success, message, stats = create_baseline_snapshots_for_class(class_id)
        
        print(f"Success: {success}")
        print(f"Message: {message}")
        print(f"\nStatistics:")
        print(f"  Created:  {stats.get('created', 0)}")
        print(f"  Failed:   {stats.get('failed', 0)}")
        print(f"  Skipped:  {stats.get('skipped', 0)}")
        
        if success:
            print("\n✅ Migration completed successfully!")
        else:
            print("\n❌ Migration encountered errors")
        
        return success


def test_batch_migration():
    """Test migrating all linked clone classes."""
    print(f"\n{'='*70}")
    print("Batch Migration - All Linked Clone Classes")
    print(f"{'='*70}\n")
    
    app = create_app()
    with app.app_context():
        success, message, stats = create_baseline_snapshots_for_all_linked_clones()
        
        print(f"Success: {success}")
        print(f"Message: {message}")
        print(f"\nStatistics:")
        print(f"  Classes Processed: {stats.get('classes', 0)}")
        print(f"  VMs Created:       {stats.get('vms_created', 0)}")
        print(f"  VMs Failed:        {stats.get('vms_failed', 0)}")
        print(f"  VMs Skipped:       {stats.get('vms_skipped', 0)}")
        
        if success:
            print("\n✅ Batch migration completed successfully!")
        else:
            print("\n❌ Batch migration encountered errors")
        
        return success


def show_usage():
    """Show usage information."""
    print("""
SNAPSHOT MIGRATION TESTING GUIDE
================================

This script demonstrates direct Python usage of the snapshot migration service.

USAGE PATTERNS:

1. Migrate a single class:
   python3 test_snapshot_migration.py 123

2. Migrate all linked clone classes:
   python3 test_snapshot_migration.py --all

3. Check class status:
   python3 test_snapshot_migration.py --status 123

4. Run in Python REPL:
   >>> from app.services.snapshot_migration import create_baseline_snapshots_for_class
   >>> from app import create_app
   >>> app = create_app()
   >>> with app.app_context():
   ...     success, message, stats = create_baseline_snapshots_for_class(123)
   ...     print(stats)

API USAGE (curl):

1. Migrate single class:
   curl -X POST http://localhost:8080/api/admin/snapshots/migrate-class/123

2. Migrate all classes:
   curl -X POST http://localhost:8080/api/admin/snapshots/migrate-all \\
     -H "X-Confirm-Migration: true"

3. Check status:
   curl http://localhost:8080/api/admin/snapshots/status?class_id=123

RESPONSE FORMATS:

Success response:
{
    "ok": true,
    "message": "Snapshot migration complete for MyClass: 5 created, 0 failed, 0 skipped",
    "stats": {
        "created": 5,
        "failed": 0,
        "skipped": 0
    }
}

Error response:
{
    "ok": false,
    "message": "Error details here",
    "stats": {}
}

TROUBLESHOOTING:

Q: Migration is slow
A: That's normal - takes ~10-30 seconds per VM (sequential processing)

Q: Some VMs show "failed"
A: Check Proxmox node SSH connectivity, retry migration

Q: "Snapshot already exists"
A: That VM already has the snapshot, it will be skipped next run

Q: API endpoint returns 404
A: Check the exact URL path and HTTP method

IMPORTANT REMINDERS:

✅ Migration is safe - only adds snapshots
✅ Safe to retry - already-migrated VMs are skipped
✅ Non-destructive - doesn't modify or restart VMs
⚠️  Batch migration requires confirmation header (anti-accident)
⚠️  Admin access required
    """)


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        show_usage()
        return
    
    command = sys.argv[1]
    
    if command == "--help" or command == "-h":
        show_usage()
    
    elif command == "--all":
        print("WARNING: This will migrate ALL linked clone classes!")
        confirm = input("Continue? (yes/no): ").lower()
        if confirm == "yes":
            test_batch_migration()
        else:
            print("Cancelled")
    
    elif command == "--status":
        if len(sys.argv) < 3:
            print("Usage: python3 test_snapshot_migration.py --status <class_id>")
            sys.exit(1)
        
        try:
            class_id = int(sys.argv[2])
            app = create_app()
            with app.app_context():
                from app.models import Class, VMAssignment
                
                class_ = Class.query.get(class_id)
                if not class_:
                    print(f"Class {class_id} not found")
                    return
                
                print(f"\n{'='*70}")
                print(f"Class Status - {class_.name} (ID: {class_id})")
                print(f"{'='*70}\n")
                print(f"Name: {class_.name}")
                print(f"Deployment Method: {getattr(class_, 'deployment_method', 'config_clone')}")
                print(f"Teacher ID: {class_.teacher_id}")
                
                vms = VMAssignment.query.filter_by(class_id=class_id, is_template_vm=False).all()
                print(f"\nVMs in class: {len(vms)}")
                
                for i, vm in enumerate(vms, 1):
                    print(f"\n  {i}. {vm.vm_name} (VMID {vm.proxmox_vmid})")
                    print(f"     Status: {vm.status}")
                    print(f"     User: {vm.assigned_user_id if vm.assigned_user_id else 'Unassigned'}")
                    print(f"     Is Teacher: {vm.is_teacher_vm}")
        
        except (ValueError, IndexError):
            print("Usage: python3 test_snapshot_migration.py --status <class_id>")
            sys.exit(1)
    
    else:
        # Assume it's a class ID
        try:
            class_id = int(command)
            test_single_class_migration(class_id)
        except ValueError:
            print(f"Invalid command: {command}")
            print("Use --help for usage information")
            sys.exit(1)


if __name__ == "__main__":
    main()
