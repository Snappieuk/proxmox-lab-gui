#!/usr/bin/env python3
"""Remove duplicate VM assignments, keeping the one with class_id."""

from app import create_app
from app.models import db, VMAssignment

app = create_app()

with app.app_context():
    print("\n=== Cleaning Up Duplicate Assignments ===\n")
    
    # Find VMIDs with multiple assignments
    from sqlalchemy import func
    duplicates = db.session.query(
        VMAssignment.proxmox_vmid,
        func.count(VMAssignment.id).label('count')
    ).group_by(VMAssignment.proxmox_vmid).having(
        func.count(VMAssignment.id) > 1
    ).all()
    
    if not duplicates:
        print("✓ No duplicates to clean up")
        exit(0)
    
    print(f"Found {len(duplicates)} VMIDs with duplicates\n")
    
    deleted = 0
    
    for vmid, count in duplicates:
        assignments = VMAssignment.query.filter_by(proxmox_vmid=vmid).order_by(VMAssignment.id).all()
        
        # Keep the one with class_id, delete the rest
        keeper = None
        to_delete = []
        
        for asn in assignments:
            if asn.class_id is not None:
                keeper = asn
            else:
                to_delete.append(asn)
        
        # If no assignment has class_id, keep the first one
        if keeper is None:
            keeper = assignments[0]
            to_delete = assignments[1:]
        
        print(f"VMID {vmid}:")
        print(f"  Keeping: ID {keeper.id}, class_id={keeper.class_id}")
        
        for asn in to_delete:
            print(f"  Deleting: ID {asn.id}, class_id={asn.class_id}")
            db.session.delete(asn)
            deleted += 1
    
    db.session.commit()
    
    print(f"\n✓ Deleted {deleted} duplicate records")
    print("\nNow run the recovery tool again with the updated logic")

