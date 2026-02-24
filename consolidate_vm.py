#!/usr/bin/env python3
"""Consolidate duplicate VM assignments."""

from app import create_app
from app.models import db, VMAssignment

app = create_app()

import sys

if len(sys.argv) < 2:
    print("Usage: python3 consolidate_vm.py <vmid> [user_id_to_assign]")
    print("\nExample 1: Just remove duplicates")
    print("  python3 consolidate_vm.py 53518")
    print("\nExample 2: Remove duplicates AND assign to user")
    print("  python3 consolidate_vm.py 53518 16")
    exit(1)

vmid = int(sys.argv[1])
user_id_to_assign = int(sys.argv[2]) if len(sys.argv) > 2 else None

with app.app_context():
    print(f"\n=== Consolidating VM {vmid} ===\n")
    
    assignments = VMAssignment.query.filter_by(proxmox_vmid=vmid).all()
    
    if len(assignments) < 2:
        print(f"Only {len(assignments)} record(s) - no consolidation needed")
        exit(0)
    
    # Find the one with class_id (the "good" record)
    keeper = None
    to_delete = []
    
    for asn in assignments:
        if asn.class_id is not None:
            keeper = asn
        else:
            to_delete.append(asn)
    
    # If no one has class_id, keep the first one
    if keeper is None:
        keeper = assignments[0]
        to_delete = assignments[1:]
    
    if not to_delete:
        print("No orphaned records to delete")
        exit(0)
    
    print(f"Keeping Record ID {keeper.id}:")
    print(f"  class_id: {keeper.class_id}")
    print(f"  assigned_user_id: {keeper.assigned_user_id}")
    print(f"  status: {keeper.status}")
    
    # If user_id provided, assign it
    if user_id_to_assign:
        print(f"\nAssigning to user {user_id_to_assign}...")
        keeper.assigned_user_id = user_id_to_assign
        keeper.status = 'assigned'
    
    print(f"\nDeleting {len(to_delete)} duplicate record(s):")
    for asn in to_delete:
        print(f"  Deleting ID {asn.id} (class_id={asn.class_id}, assigned_user_id={asn.assigned_user_id})")
        db.session.delete(asn)
    
    db.session.add(keeper)
    db.session.commit()
    
    print("\n✓ Done")

