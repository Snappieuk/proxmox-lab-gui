#!/usr/bin/env python3
"""Diagnostic script to find VMs that are in a class but have class_id=NULL."""

from app import create_app
from app.models import db, VMAssignment, Class

app = create_app()

with app.app_context():
    print("\n=== Diagnostics: Orphaned VMs ===\n")
    
    # Find all VMAssignments with class_id=NULL
    orphaned = VMAssignment.query.filter_by(class_id=None).all()
    
    print(f"Total VMs with class_id=NULL: {len(orphaned)}")
    print("\nThese are direct assignments (not part of a class pool):")
    print("-" * 80)
    
    for va in orphaned:
        assigned_to = "Unassigned" if va.assigned_user_id is None else f"User ID {va.assigned_user_id}"
        print(f"  VMID {va.proxmox_vmid:6d}  |  Name: {va.vm_name:30s}  |  Status: {va.status:12s}  |  {assigned_to}")
    
    print("\n" + "=" * 80)
    print("\nTo determine if these SHOULD be in a class, look for:")
    print("  1. Naming patterns (e.g., 'class5-base', 'class5-student1')")
    print("  2. VMs deployed before February 2026 (likely legacy)")
    print("  3. VMs in the Proxmox UI that should be part of a class pool")
    print("\n" + "=" * 80)

