#!/usr/bin/env python3
"""Fix orphaned VMs by assigning them to the correct class."""

from app import create_app
from app.models import db, VMAssignment, Class

app = create_app()

with app.app_context():
    print("\n=== Available Classes ===\n")
    
    classes = Class.query.all()
    for cls in classes:
        vm_count = VMAssignment.query.filter_by(class_id=cls.id).count()
        print(f"  Class ID {cls.id:3d}  |  Name: {cls.name:30s}  |  VMs in pool: {vm_count}")
    
    if not classes:
        print("  No classes found!")
        exit(1)
    
    print("\n" + "=" * 80)
    print("\nUsage:")
    print("  python3 fix_orphaned_vms.py <class_id> <vmid1,vmid2,vmid3...>")
    print("\nExample:")
    print("  python3 fix_orphaned_vms.py 5 1000,1001,1002")
    print("\nThis will assign VMIDs 1000, 1001, 1002 to Class 5")
    print("=" * 80)
    
    import sys
    if len(sys.argv) < 3:
        print("\nNo arguments provided. Showing classes only.")
        exit(0)
    
    try:
        class_id = int(sys.argv[1])
        vmid_str = sys.argv[2]
        vmids = [int(x.strip()) for x in vmid_str.split(',')]
    except (ValueError, IndexError) as e:
        print(f"\nError parsing arguments: {e}")
        exit(1)
    
    # Verify class exists
    cls = Class.query.get(class_id)
    if not cls:
        print(f"\nError: Class {class_id} not found")
        exit(1)
    
    print(f"\nAssigning {len(vmids)} VMs to Class {class_id} ({cls.name})...")
    
    fixed = 0
    errors = []
    
    for vmid in vmids:
        try:
            # Find existing assignment
            assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
            if not assignment:
                errors.append(f"VMID {vmid}: No assignment found in database")
                continue
            
            # Update class_id
            old_class_id = assignment.class_id
            assignment.class_id = class_id
            db.session.add(assignment)
            
            print(f"  ✓ VMID {vmid}: {old_class_id} → Class {class_id}")
            fixed += 1
        
        except Exception as e:
            errors.append(f"VMID {vmid}: {str(e)}")
    
    db.session.commit()
    
    print(f"\n✓ Fixed {fixed} VMs")
    if errors:
        print(f"\n⚠ Errors ({len(errors)}):")
        for err in errors:
            print(f"  - {err}")
    
    print("\nDone!")

