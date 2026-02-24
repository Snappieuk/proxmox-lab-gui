#!/usr/bin/env python3
"""Check the current state of a specific VM."""

from app import create_app
from app.models import db, VMAssignment, User

app = create_app()

import sys

if len(sys.argv) < 2:
    print("Usage: python3 check_vm.py <vmid>")
    print("Example: python3 check_vm.py 53518")
    exit(1)

vmid = int(sys.argv[1])

with app.app_context():
    print(f"\n=== Checking VM {vmid} ===\n")
    
    assignments = VMAssignment.query.filter_by(proxmox_vmid=vmid).all()
    
    if not assignments:
        print(f"No assignments found for VMID {vmid}")
        exit(0)
    
    print(f"Found {len(assignments)} assignment record(s):\n")
    
    for i, asn in enumerate(assignments, 1):
        print(f"Record {i}:")
        print(f"  ID: {asn.id}")
        print(f"  VMID: {asn.proxmox_vmid}")
        print(f"  Name: {asn.vm_name}")
        print(f"  class_id: {asn.class_id}")
        print(f"  assigned_user_id: {asn.assigned_user_id}")
        
        if asn.assigned_user_id:
            user = User.query.get(asn.assigned_user_id)
            if user:
                print(f"    → Assigned to: {user.username}")
        else:
            print(f"    → Unassigned (pool VM)")
        
        print(f"  Status: {asn.status}")
        print()

