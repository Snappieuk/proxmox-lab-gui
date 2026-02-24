#!/usr/bin/env python3
"""Check for duplicate VM assignments."""

from app import create_app
from app.models import db, VMAssignment

app = create_app()

with app.app_context():
    print("\n=== Checking for Duplicate Assignments ===\n")
    
    # Group by VMID and count
    from sqlalchemy import func
    duplicates = db.session.query(
        VMAssignment.proxmox_vmid,
        func.count(VMAssignment.id).label('count')
    ).group_by(VMAssignment.proxmox_vmid).having(
        func.count(VMAssignment.id) > 1
    ).all()
    
    if not duplicates:
        print("✓ No duplicate assignments found")
        exit(0)
    
    print(f"Found {len(duplicates)} VMIDs with multiple assignments:\n")
    
    for vmid, count in duplicates:
        print(f"  VMID {vmid}: {count} records")
        assignments = VMAssignment.query.filter_by(proxmox_vmid=vmid).all()
        for asn in assignments:
            assigned_to = f"User {asn.assigned_user_id}" if asn.assigned_user_id else "Unassigned"
            print(f"    - ID {asn.id}: class_id={asn.class_id}, {assigned_to}, status={asn.status}")
        print()

