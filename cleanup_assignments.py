#!/usr/bin/env python3
"""Safely delete duplicate or unassigned VM assignments."""

from app import create_app
from app.models import db, VMAssignment, Class

app = create_app()

with app.app_context():
    print("=== CLEANUP OPTIONS ===\n")
    
    # Option 1: Find all unassigned assignments
    print("[1] UNASSIGNED ASSIGNMENTS (can be safely deleted)")
    unassigned = VMAssignment.query.filter_by(assigned_user_id=None).all()
    print(f"Found {len(unassigned)} unassigned assignments:\n")
    
    for assign in unassigned:
        cls_name = Class.query.get(assign.class_id).name if assign.class_id else "DIRECT"
        print(f"  ID: {assign.id}")
        print(f"    VMID: {assign.proxmox_vmid}")
        print(f"    Class: {cls_name} (class_id={assign.class_id})")
        print(f"    Status: {assign.status}")
        print()
    
    print("\n" + "="*60)
    print("[2] DUPLICATE ASSIGNMENTS (same VM + class)")
    print("="*60 + "\n")
    
    # Find duplicates grouped by (vmid, class_id)
    from sqlalchemy import func
    duplicates_query = db.session.query(
        VMAssignment.proxmox_vmid,
        VMAssignment.class_id,
        func.count(VMAssignment.id).label('count')
    ).group_by(
        VMAssignment.proxmox_vmid,
        VMAssignment.class_id
    ).having(func.count(VMAssignment.id) > 1).all()
    
    if not duplicates_query:
        print("No duplicate assignments found.\n")
    else:
        for vmid, class_id, count in duplicates_query:
            assignments = VMAssignment.query.filter_by(
                proxmox_vmid=vmid,
                class_id=class_id
            ).order_by(VMAssignment.id).all()
            
            cls_name = Class.query.get(class_id).name if class_id else "DIRECT"
            print(f"VM {vmid} in {cls_name}: {count} assignments")
            for assign in assignments:
                user_name = assign.assigned_user.username if assign.assigned_user else "UNASSIGNED"
                print(f"  ID: {assign.id} | User: {user_name} | Status: {assign.status}")
            print()
    
    print("\n" + "="*60)
    print("CLEANUP ACTIONS")
    print("="*60 + "\n")
    
    # Prompt user
    action = input("Choose action:\n  [1] Delete ALL unassigned assignments\n  [2] Delete specific assignment ID\n  [3] Cancel\nChoice (1-3): ").strip()
    
    if action == "1":
        count = len(unassigned)
        confirm = input(f"\nDelete {count} unassigned assignments? Type 'YES' to confirm: ").strip()
        if confirm == "YES":
            for assign in unassigned:
                db.session.delete(assign)
            db.session.commit()
            print(f"✓ Deleted {count} unassigned assignments")
        else:
            print("Cancelled")
    
    elif action == "2":
        assign_id = input("Enter assignment ID to delete: ").strip()
        try:
            assign_id = int(assign_id)
            assign = VMAssignment.query.get(assign_id)
            if not assign:
                print("Assignment not found")
            else:
                cls_name = Class.query.get(assign.class_id).name if assign.class_id else "DIRECT"
                user_name = assign.assigned_user.username if assign.assigned_user else "UNASSIGNED"
                print(f"\nWill delete:")
                print(f"  ID: {assign.id}")
                print(f"  VM: {assign.proxmox_vmid}")
                print(f"  Class: {cls_name}")
                print(f"  User: {user_name}")
                
                confirm = input("\nConfirm deletion? Type 'YES': ").strip()
                if confirm == "YES":
                    db.session.delete(assign)
                    db.session.commit()
                    print("✓ Assignment deleted")
                else:
                    print("Cancelled")
        except ValueError:
            print("Invalid ID")
    
    elif action == "3":
        print("Cancelled")
    else:
        print("Invalid choice")
